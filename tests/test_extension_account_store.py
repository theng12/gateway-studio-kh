"""Durable extension-account credit ledger: observation, binding, reservation and settlement.

Direct JobStore tests against throwaway SQLite files. No gateway import beyond the store, so this
module never touches the owner's Application Support, API keys or another sandbox's state.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.store import (  # noqa: E402
    HOLD_CLOSED,
    RECOVERY_UNKNOWN,
    ExtensionAccountRefused,
    JobStore,
)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "jobs.sqlite3"
        self.stores: list[JobStore] = []
        self.store = self.open_store()

    def tearDown(self) -> None:
        for store in reversed(self.stores):
            try:
                store.close()
            except Exception:
                pass

    def open_store(self) -> JobStore:
        store = JobStore(self.path)
        self.stores.append(store)
        return store

    def drop_store(self, store: JobStore) -> None:
        store.close()
        self.stores.remove(store)

    def account(self, *, provider: str = "flow", account_id: str = "acct-1",
                observed_at: float = 1000.0, balance: float | None = 10.0,
                balance_observed_at: float | None = None, balance_unit: str = "credits",
                **overrides) -> dict:
        data = {
            "provider": provider,
            "account_id": account_id,
            "source": "flow.nzlxg",
            "observed_at": observed_at,
            "connection": "connected",
            "sign_in": "signed_in",
            "readiness": "ready",
            "activity": "idle",
            "attention": None,
            "paused": False,
            "enabled": True,
            "next_eligible_at": 0.0,
            "plan": "pro",
            "credit_buckets": [{"unit": "credits", "value": 10.0}],
            "reset_at": None,
        }
        if balance is not None:
            data["balance"] = {
                "value": balance,
                "unit": balance_unit,
                "source": "flow.nzlxg",
                "observed_at": (balance_observed_at if balance_observed_at is not None
                                else observed_at),
            }
        data.update(overrides)
        return data

    def observe(self, worker: str = "chrome-a", token: str = "token-1", *,
                now: float | None = None, **account) -> dict:
        observed_at = account.pop("observed_at", None)
        if observed_at is None:
            observed_at = 1000.0 if now is None else now
        if now is None:
            now = observed_at
        return self.store.extension_account_observe(
            worker, token, self.account(observed_at=observed_at, **account), now=now,
        )

    def eligible(self, worker: str = "chrome-a", token: str = "token-1", *,
                 account_id: str = "acct-1", now: float = 1000.0, **account) -> None:
        self.observe(worker=worker, token=token, now=now, account_id=account_id, **account)
        self.store.extension_account_set_enabled("flow", account_id, True)

    def job(self, state: str = "accepted") -> dict:
        job, _ = self.store.submit(modality="image", model_id="model", provider_id="flow",
                                   params={}, idempotency_key=None)
        if state == "running":
            self.assertTrue(self.store.mark_running(job["id"]))
        return job

    def quote(self, *, value: float = 2.0, unit: str = "credits",
              payload_sha256: str = "a" * 64, observed_at: float = 1000.0,
              provider: str | None = "flow", account_id: str | None = "acct-1") -> dict:
        quote = {"value": value, "unit": unit, "source": "flow.nzlxg",
                 "observed_at": observed_at, "payload_sha256": payload_sha256}
        if provider is not None:
            quote["provider"] = provider
        if account_id is not None:
            quote["account_id"] = account_id
        return quote

    def reserve(self, job: dict | None, *, worker: str = "chrome-a", token: str = "token-1",
                worker_job_id: str = "wj-1", job_id: str | None = None,
                payload_sha256: str = "a" * 64, now: float = 1000.0,
                max_age: float = 60.0, value: float = 2.0, quote_value: float | None = None,
                unit: str = "credits", account_id: str = "acct-1",
                quote_observed_at: float | None = None,
                quote: dict | None = None) -> dict:
        if job_id is None:
            job_id = job["id"]
        if quote_value is not None:
            value = quote_value
        quote_row = self.quote(
            value=value, unit=unit, payload_sha256=payload_sha256,
            observed_at=now if quote_observed_at is None else quote_observed_at,
            account_id=account_id,
        )
        if quote:
            quote_row.update(quote)
        return self.store.extension_account_reserve(
            worker, token, job_id=job_id, worker_job_id=worker_job_id,
            payload_sha256=payload_sha256, quote=quote_row, now=now, max_age=max_age,
        )

    def reservation(self, worker: str = "chrome-a", worker_job_id: str = "wj-1") -> dict:
        rows = self.store.extension_account_reservations(worker)
        return next(row for row in rows if row["worker_job_id"] == worker_job_id)

    def code(self, call, *args, **kwargs) -> str:
        with self.assertRaises(ExtensionAccountRefused) as caught:
            call(*args, **kwargs)
        return caught.exception.code


class AccountObservationTests(_Base):
    def test_unknowns_are_null_and_gateway_dispatch_stays_off(self) -> None:
        row = self.observe(balance=None, paused=None, enabled=None, next_eligible_at=None)
        self.assertIsNone(row["balance"])
        self.assertIsNone(row["enabled"])
        self.assertTrue(row["paused"])
        self.assertFalse(row["gateway_enabled"])

        status = self.store.extension_account_status(now=1000.0, max_age=60)
        self.assertEqual(len(status), 1)
        self.assertIsNone(status[0]["balance"])
        self.assertIsNone(status[0]["available"])   # unknown, never 0
        self.assertEqual(status[0]["reserved"], 0.0)
        self.assertEqual(status[0]["bindings"][0]["worker_id"], "chrome-a")
        self.assertEqual(status[0]["workers"], ["chrome-a"])
        self.assertNotIn("token_id", self.store.extension_account_history("flow", "acct-1")[0])

    def test_observation_never_enables_and_explicit_off_survives_reopen(self) -> None:
        self.observe()
        self.assertFalse(self.store.extension_account_status(
            now=1000.0, max_age=60)[0]["gateway_enabled"])
        self.store.extension_account_set_enabled("flow", "acct-1", True)
        row = self.observe(now=1001.0)
        self.assertTrue(row["gateway_enabled"])
        self.store.extension_account_set_enabled("flow", "acct-1", False)

        old = self.store
        self.drop_store(old)
        self.store = self.open_store()
        self.assertFalse(self.store.extension_account_status(
            now=1002.0, max_age=60)[0]["gateway_enabled"])

    def test_set_enabled_refuses_an_unobserved_identity(self) -> None:
        self.assertFalse(self.store.extension_account_set_enabled("flow", "not-observed", True))
        self.assertEqual(self.store.extension_account_status(now=1000.0, max_age=60), [])

        self.observe()
        self.assertTrue(self.store.extension_account_set_enabled("flow", "acct-1", True))
        self.assertTrue(self.store.extension_account_status(
            now=1000.0, max_age=60)[0]["gateway_enabled"])

    def test_stale_and_duplicate_observations_do_not_overwrite_or_freshen(self) -> None:
        self.observe(now=1000.0, balance=10.0)
        stale = self.observe(now=1010.0, observed_at=900.0, balance=5.0,
                             connection="disconnected")
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["observed_at"], 1000.0)
        self.assertEqual(stale["received_at"], 1000.0)
        self.assertEqual(stale["balance"]["value"], 10.0)
        self.assertEqual(stale["connection"], "connected")

        duplicate = self.observe(now=1020.0, observed_at=1000.0, balance=7.0)
        self.assertTrue(duplicate["stale"])
        self.assertEqual(duplicate["balance"]["value"], 10.0)
        history = self.store.extension_account_history("flow", "acct-1")
        self.assertEqual({row["observed_at"] for row in history}, {900.0, 1000.0})

    def test_two_workers_share_one_account_balance_without_double_counting(self) -> None:
        self.observe(worker="chrome-image", token="image-token", now=1000.0, balance=10.0)
        self.observe(worker="chrome-video", token="video-token", now=1000.0, balance=10.0)
        status = self.store.extension_account_status(now=1000.0, max_age=60)
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["balance"]["value"], 10.0)
        self.assertEqual(status[0]["workers"], ["chrome-image", "chrome-video"])
        self.assertEqual(len(self.store.extension_account_history("flow", "acct-1")), 1)

    def test_extra_observed_fields_are_retained_but_never_override_gateway_state(self) -> None:
        row = self.observe(pacing={"mode": "slow"}, control_revision="rev-7",
                           gateway_enabled=True)
        self.assertEqual(row["pacing"], {"mode": "slow"})
        self.assertEqual(row["control_revision"], "rev-7")
        self.assertFalse(row["gateway_enabled"])
        status = self.store.extension_account_status(now=1000.0, max_age=60)
        self.assertEqual(status[0]["control_revision"], "rev-7")
        self.assertFalse(status[0]["gateway_enabled"])

    def test_attention_is_a_strict_boolean_without_coercion(self) -> None:
        row = self.observe(attention=True)
        self.assertIs(row["attention"], True)
        self.assertIs(self.store.extension_account_status(
            now=1000.0, max_age=60)[0]["attention"], True)
        row = self.observe(now=1001.0, attention=False)
        self.assertIs(row["attention"], False)
        self.assertEqual(self.code(self.observe, now=1002.0, attention=1), "ACCOUNT_INVALID")
        self.assertEqual(self.code(self.observe, now=1002.0, attention="true"),
                         "ACCOUNT_INVALID")

    def test_binding_change_refused_while_unsettled_and_allowed_after_known_outcome(self) -> None:
        self.eligible()
        self.reserve(self.job())
        code = self.code(self.observe, now=1001.0, account_id="acct-2")
        self.assertEqual(code, "ACCOUNT_BINDING_HELD")

        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="succeeded", now=1002.0))
        row = self.observe(now=1003.0, account_id="acct-2")
        self.assertEqual(row["account_id"], "acct-2")


class AccountReserveTests(_Base):
    def test_reserve_requires_a_durable_unsettled_job_and_writes_canonical_upstream(self) -> None:
        self.eligible()
        self.assertEqual(self.code(self.reserve, None, job_id="missing"), "JOB_NOT_FOUND")

        cancelled = self.job()
        self.store.cancel(cancelled["id"])
        self.assertEqual(self.code(self.reserve, cancelled, worker_job_id="wj-c"),
                         "JOB_NOT_CLAIMABLE")

        settled = self.job()
        self.store.succeed(settled["id"])
        self.assertEqual(self.code(self.reserve, settled, worker_job_id="wj-s"),
                         "JOB_NOT_CLAIMABLE")

        colon_job = self.job()
        self.assertEqual(self.code(self.reserve, colon_job, worker_job_id="bad:job"),
                         "ACCOUNT_INVALID")

        job = self.job(state="running")
        row = self.reserve(job)
        self.assertEqual(row["state"], "reserved")
        self.assertEqual(row["upstream_job_id"], "ext:chrome-a:wj-1")
        self.assertEqual(self.store.get(job["id"])["upstream_job_id"], "ext:chrome-a:wj-1")
        self.assertNotIn("token_id", row)

        again = self.reserve(job)
        self.assertEqual(again["id"], row["id"])
        self.assertEqual(self.code(self.reserve, job, quote={"value": 3.0}),
                         "RESERVATION_MISMATCH")

    def test_reserve_gates_on_account_eligibility_and_actual_next_eligibility(self) -> None:
        cases = [
            ("ACCOUNT_ELIGIBLE_UNKNOWN", {"next_eligible_at": None}),
            ("ACCOUNT_NOT_ELIGIBLE_YET", {"next_eligible_at": 2000.0}),
            ("ACCOUNT_ACTIVITY", {"activity": "busy"}),
            ("ACCOUNT_PAUSED", {"paused": True}),
            ("ACCOUNT_DISABLED", {"enabled": False}),
            ("ACCOUNT_NOT_CONNECTED", {"connection": "disconnected"}),
            ("ACCOUNT_NOT_SIGNED_IN", {"sign_in": "signed_out"}),
            ("ACCOUNT_NOT_READY", {"readiness": "preparing"}),
        ]
        for index, (code, override) in enumerate(cases):
            worker = f"chrome-gate-{index}"
            account_id = f"acct-gate-{index}"
            job = self.job()
            self.observe(worker=worker, account_id=account_id, now=1000.0, **override)
            self.store.extension_account_set_enabled("flow", account_id, True)
            with self.subTest(code=code):
                self.assertEqual(
                    self.code(self.reserve, job, worker=worker, account_id=account_id,
                              worker_job_id=f"wj-gate-{index}"),
                    code,
                )

        disabled = self.job()
        self.observe(worker="chrome-gateway-off", account_id="acct-gateway-off", now=1000.0)
        self.assertEqual(self.code(self.reserve, disabled, worker="chrome-gateway-off",
                                   account_id="acct-gateway-off",
                                   worker_job_id="wj-gateway-off"),
                         "ACCOUNT_GATEWAY_DISABLED")

        # An explicit "eligible now" (epoch 0) is a real statement and passes the scheduler gate.
        self.eligible(worker="chrome-now", account_id="acct-now", now=1001.0,
                      next_eligible_at=0.0)
        row = self.reserve(self.job(), worker="chrome-now", account_id="acct-now",
                           worker_job_id="wj-now", now=1001.0)
        self.assertEqual(row["state"], "reserved")

    def test_reserve_requires_known_fresh_matching_balance_and_quote(self) -> None:
        cases = [
            ("ACCOUNT_CREDITS", {"balance": 1.0}, {}),
            ("ACCOUNT_BALANCE_STALE", {"balance": 10.0, "balance_observed_at": 900.0}, {}),
            ("ACCOUNT_STALE", {"now": 900.0, "balance": 10.0}, {"now": 1000.0}),
            ("ACCOUNT_BALANCE_UNIT", {"balance": 10.0},
             {"quote": {"unit": "usd", "source": "flow.nzlxg"}}),
            ("QUOTE_STALE", {"balance": 10.0}, {"quote": {"observed_at": 900.0}}),
            ("QUOTE_INVALID", {"balance": 10.0}, {"quote": {"observed_at": 1100.0}}),
            ("QUOTE_MISMATCH", {"balance": 10.0}, {"quote": {"payload_sha256": "b" * 64}}),
            ("QUOTE_MISMATCH", {"balance": 10.0}, {"quote": {"provider": "other"}}),
            ("QUOTE_MISMATCH", {"balance": 10.0}, {"quote": {"account_id": "other"}}),
        ]
        for index, (code, observation, reserve_args) in enumerate(cases):
            worker = f"chrome-bal-{index}"
            account_id = f"acct-balance-{index}"
            observe_now = observation.pop("now", 1000.0)
            self.observe(worker=worker, account_id=account_id, now=observe_now, **observation)
            self.store.extension_account_set_enabled("flow", account_id, True)
            reserve_quote = reserve_args.pop("quote", {})
            reserve_now = reserve_args.pop("now", 1000.0)
            with self.subTest(code=code, index=index):
                self.assertEqual(
                    self.code(self.reserve, self.job(), worker=worker, account_id=account_id,
                              worker_job_id=f"wj-balance-{index}", now=reserve_now,
                              quote=reserve_quote, **reserve_args),
                    code,
                )

    def test_an_unreadable_balance_is_claimable_and_reported_as_null(self) -> None:
        """A signed-in, ready account with no readable balance may claim; null is not zero."""
        self.eligible(balance=None)
        row = self.reserve(self.job(), worker_job_id="wj-null-balance")
        self.assertEqual(row["state"], "reserved")
        self.assertIsNone(row["available_after"])

    def test_a_known_balance_still_refuses_a_quote_it_cannot_cover(self) -> None:
        """The overdraw guard is untouched: a quote above the balance refuses, an exact one passes."""
        self.eligible(balance=10.0)
        self.assertEqual(self.code(self.reserve, self.job(), worker_job_id="wj-over", value=11.0),
                         "ACCOUNT_CREDITS")
        self.assertEqual(self.reserve(self.job(), worker_job_id="wj-exact",
                                      value=10.0)["available_after"], 0.0)

    def test_a_free_job_claims_at_zero_and_a_priced_job_still_refuses(self) -> None:
        """Images are free: a zero quote on a zero balance reserves, and priced work still refuses."""
        self.eligible(balance=0.0)
        free = self.reserve(self.job(), worker_job_id="wj-free", value=0.0)
        self.assertEqual(free["state"], "reserved")
        self.assertEqual(free["quote_value"], 0.0)
        self.assertEqual(free["available_after"], 0.0)
        self.assertEqual(self.code(self.reserve, self.job(), worker_job_id="wj-priced", value=1.0),
                         "ACCOUNT_CREDITS")

    def test_held_reservation_blocks_even_a_fresh_balance_until_a_known_outcome(self) -> None:
        self.eligible()
        self.reserve(self.job())
        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="held", now=1001.0))

        self.observe(now=1002.0, balance=10.0)   # fresh balance cannot prove the held job's cost
        job = self.job()
        self.assertEqual(self.code(self.reserve, job, worker_job_id="wj-2", now=1002.0),
                         "ACCOUNT_HELD")

        # The same account's other worker is blocked too.
        self.observe(worker="chrome-b", token="token-b", now=1002.0, balance=10.0)
        self.store.extension_account_set_enabled("flow", "acct-1", True)
        self.assertEqual(self.code(self.reserve, self.job(), worker="chrome-b",
                                   token="token-b", worker_job_id="wj-b", now=1002.0),
                         "ACCOUNT_HELD")
        status = self.store.extension_account_status(now=1002.0, max_age=60)[0]
        self.assertTrue(status["needs_reconciliation"])
        self.assertEqual(status["reservation_states"]["held"], 1)

        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="succeeded", now=1003.0))
        row = self.reserve(job, worker_job_id="wj-2", now=1003.0)
        self.assertEqual(row["state"], "reserved")

    def test_success_retains_debit_until_a_newer_balance_observation_reconciles(self) -> None:
        self.eligible(balance=5.0)
        self.reserve(self.job(), quote_value=3.0)
        self.assertEqual(self.code(self.reserve, self.job(), quote_value=3.0,
                                   worker_job_id="wj-2"), "ACCOUNT_CREDITS")
        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="succeeded", now=1001.0))
        self.assertEqual(self.code(self.reserve, self.job(), quote_value=3.0,
                                   worker_job_id="wj-3", now=1001.0), "ACCOUNT_CREDITS")

        self.observe(now=1002.0, balance=5.0)
        row = self.reserve(self.job(), quote_value=3.0, worker_job_id="wj-4", now=1002.0)
        self.assertEqual(row["state"], "reserved")

    def test_reserve_refuses_a_unit_change_while_a_live_reservation_remains(self) -> None:
        self.eligible(balance=10.0)
        self.reserve(self.job(), worker_job_id="wj-1")   # 2 credits, still reserved
        self.observe(now=1001.0, balance=10.0, balance_unit="usd")

        self.assertEqual(
            self.code(self.reserve, self.job(), worker_job_id="wj-2", now=1001.0, unit="usd"),
            "ACCOUNT_BALANCE_UNIT",
        )
        status = self.store.extension_account_status(now=1001.0, max_age=60)[0]
        self.assertIsNone(status["available"])   # credits and usd are never summed

    def test_unit_change_with_a_live_debit_refuses_until_a_same_unit_observation_reconciles(self) -> None:
        self.eligible(balance=10.0)
        self.reserve(self.job(), worker_job_id="wj-1")
        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="succeeded", now=1001.0))

        # The new balance is observed at the terminal instant, so the old debit is still retained.
        self.observe(now=1002.0, balance=10.0, balance_unit="usd", balance_observed_at=1001.0)
        self.assertEqual(
            self.code(self.reserve, self.job(), worker_job_id="wj-2", now=1002.0, unit="usd"),
            "ACCOUNT_BALANCE_UNIT",
        )

        # A newer balance in a different unit must not reconcile a credit-unit debit: the units are
        # incomparable, so usd remains unauthorizable.
        self.observe(now=1003.0, balance=10.0, balance_unit="usd", balance_observed_at=1003.0)
        self.assertEqual(
            self.code(self.reserve, self.job(), worker_job_id="wj-3", now=1003.0, unit="usd"),
            "ACCOUNT_BALANCE_UNIT",
        )

        # A real same-unit observation after the terminal instant reconciles the old debit.
        self.observe(now=1004.0, balance=10.0, balance_unit="credits", balance_observed_at=1004.0)
        row = self.reserve(self.job(), worker_job_id="wj-4", now=1004.0, unit="credits")
        self.assertEqual(row["state"], "reserved")

    def test_plan_and_unit_boundaries_survive_observation_and_reserve(self) -> None:
        plan = "p" * 120
        unit = "u" * 64
        observed = self.observe(now=1000.0, plan=plan, balance=10.0, balance_unit=unit)
        self.assertEqual(observed["plan"], plan)
        self.assertEqual(observed["balance"]["unit"], unit)
        self.assertTrue(self.store.extension_account_set_enabled("flow", "acct-1", True))

        row = self.reserve(self.job(), unit=unit)
        self.assertEqual(row["state"], "reserved")
        status = self.store.extension_account_status(now=1000.0, max_age=60)[0]
        self.assertEqual(status["plan"], plan)
        self.assertEqual(status["balance"]["unit"], unit)

    def test_definite_failure_does_not_refund_before_a_newer_balance_observation(self) -> None:
        self.eligible(balance=5.0)
        self.reserve(self.job(), quote_value=3.0)
        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="failed", now=1001.0))
        self.assertEqual(self.code(self.reserve, self.job(), quote_value=3.0,
                                   worker_job_id="wj-2", now=1001.0), "ACCOUNT_CREDITS")

        # A balance observed at the terminal instant is not newer than the outcome.
        self.observe(now=1002.0, balance=5.0, balance_observed_at=1001.0)
        self.assertEqual(self.code(self.reserve, self.job(), quote_value=3.0,
                                   worker_job_id="wj-3", now=1002.0), "ACCOUNT_CREDITS")

        self.observe(now=1003.0, balance=4.0, balance_observed_at=1003.0)
        row = self.reserve(self.job(), quote_value=3.0, worker_job_id="wj-4", now=1003.0)
        self.assertEqual(row["state"], "reserved")


class AccountSettleTests(_Base):
    def test_first_known_outcome_wins_and_a_later_known_outcome_settles_a_hold(self) -> None:
        self.eligible()
        self.reserve(self.job())
        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="held", now=1001.0))
        self.assertEqual(self.reservation()["state"], "held")

        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="succeeded", now=1002.0))
        row = self.reservation()
        self.assertEqual((row["state"], row["outcome"]), ("debited", "succeeded"))

        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="failed", now=1003.0))
        row = self.reservation()
        self.assertEqual((row["state"], row["outcome"]), ("debited", "succeeded"))

        self.assertFalse(self.store.extension_account_settle(
            "chrome-a", "token-1", "missing", outcome="succeeded", now=1004.0))
        self.assertFalse(self.store.extension_account_settle(
            "chrome-b", "token-1", "wj-1", outcome="succeeded", now=1004.0))

    def test_owner_closing_an_unknown_job_hold_does_not_release_ledger_credits(self) -> None:
        self.eligible()
        job = self.job(state="running")
        self.reserve(job)
        self.store.fail(job["id"], code="UNKNOWN_OUTCOME", detail="unknown",
                        recovery_state=RECOVERY_UNKNOWN)
        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="held", now=1001.0))

        closed = self.store.close_recovery_hold(
            job["id"], code=HOLD_CLOSED,
            detail="owner closed the job hold without the attempt's answer",
        )
        self.assertEqual(closed["recovery_state"], None)
        self.assertEqual(closed["error_code"], HOLD_CLOSED)

        # The administrative job close says nothing about the provider charge: the ledger credit
        # stays held, is not refunded, and still blocks another claim.
        self.assertEqual(self.reservation()["state"], "held")
        status = self.store.extension_account_status(now=1001.0, max_age=60)[0]
        self.assertTrue(status["needs_reconciliation"])
        self.assertEqual(status["reserved"], 2.0)
        self.assertEqual(self.code(self.reserve, self.job(), worker_job_id="wj-2",
                                   now=1001.0), "ACCOUNT_HELD")
        self.assertTrue(self.store.extension_account_worker_busy("chrome-a"))

        # Only an actual known outcome releases the held reservation.
        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="succeeded", now=1002.0))
        row = self.reserve(self.job(), worker_job_id="wj-3", now=1002.0)
        self.assertEqual(row["state"], "reserved")

    def test_old_token_cannot_settle_a_new_generation(self) -> None:
        self.eligible(token="token-1")
        self.reserve(self.job(), token="token-1")
        self.observe(now=1001.0, token="token-2", balance=10.0)
        self.reserve(self.job(), token="token-2", worker_job_id="wj-2", now=1001.0)

        self.assertFalse(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-2", outcome="succeeded", now=1002.0))
        self.assertIsNone(self.store.extension_account_reservation(
            "chrome-a", "token-1", "wj-2"))
        self.assertTrue(self.store.extension_account_has_reservation("chrome-a", "wj-2"))
        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-2", "wj-2", outcome="succeeded", now=1002.0))
        self.assertTrue(self.store.extension_account_settle(
            "chrome-a", "token-1", "wj-1", outcome="succeeded", now=1002.0))


class AccountOwnershipTests(_Base):
    def test_ownership_lookup_and_unscoped_existence_are_distinct(self) -> None:
        self.eligible()
        self.reserve(self.job())
        owned = self.store.extension_account_reservation("chrome-a", "token-1", "wj-1")
        self.assertIsNotNone(owned)
        self.assertNotIn("token_id", owned)
        self.assertIsNone(self.store.extension_account_reservation(
            "chrome-a", "wrong-token", "wj-1"))
        self.assertIsNone(self.store.extension_account_reservation(
            "chrome-b", "token-1", "wj-1"))
        self.assertTrue(self.store.extension_account_has_reservation("chrome-a", "wj-1"))
        self.assertFalse(self.store.extension_account_has_reservation("chrome-a", "pre-ledger"))

    def test_ledger_lists_have_no_token_digests_and_no_truncated_search(self) -> None:
        self.eligible()
        for index in range(3):
            self.reserve(self.job(), worker_job_id=f"wj-{index}")
        by_worker = self.store.extension_account_reservations("chrome-a")
        self.assertEqual(len(by_worker), 3)
        self.assertTrue(all("token_id" not in row for row in by_worker))
        by_account = self.store.extension_account_reservations(
            provider="flow", account_id="acct-1", limit=2)
        self.assertEqual(len(by_account), 2)
        self.assertTrue(all("token_id" not in row for row in by_account))

    def test_worker_busy_finds_preledger_jobs_with_exact_worker_prefix(self) -> None:
        for _ in range(120):   # unrelated accepted jobs must not hide the canonical one
            self.job()
        old = self.job(state="running")
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE jobs SET upstream_job_id = ? WHERE id = ?",
                ("ext:chrome-a:wj-old", old["id"]),
            )
            self.store._conn.commit()

        self.assertTrue(self.store.extension_account_worker_busy("chrome-a"))
        self.assertFalse(self.store.extension_account_worker_busy("chrome-ax"))
        self.assertFalse(self.store.extension_account_worker_busy("chrome"))
        self.assertFalse(self.store.extension_account_worker_busy("chrome-a:wj"))

        held = self.job()
        self.store.fail(held["id"], code="UNKNOWN_OUTCOME", detail="unknown",
                        recovery_state=RECOVERY_UNKNOWN)
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE jobs SET upstream_job_id = ? WHERE id = ?",
                ("ext:worker%_:wj-held", held["id"]),
            )
            self.store._conn.commit()
        self.assertTrue(self.store.extension_account_worker_busy("worker%_"))
        self.assertFalse(self.store.extension_account_worker_busy("workerX_"))

        closed = self.job()
        self.store.fail(closed["id"], code="UNKNOWN_OUTCOME", detail="unknown",
                        recovery_state=RECOVERY_UNKNOWN)
        self.store.close_recovery_hold(closed["id"], code=HOLD_CLOSED, detail="closed")
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE jobs SET upstream_job_id = ? WHERE id = ?",
                ("ext:chrome-c:wj-closed", closed["id"]),
            )
            self.store._conn.commit()
        self.assertTrue(self.store.extension_account_worker_busy("chrome-c"))

        colon = self.job(state="running")
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE jobs SET upstream_job_id = ? WHERE id = ?",
                ("ext:a:b:wj-colon", colon["id"]),
            )
            self.store._conn.commit()
        self.assertTrue(self.store.extension_account_worker_busy("a:b"))
        self.assertFalse(self.store.extension_account_worker_busy("a"))  # not worker a:b's job


class AccountRestartTests(_Base):
    def test_secondary_open_does_not_hold_but_a_real_restart_does(self) -> None:
        self.eligible()
        self.reserve(self.job())

        secondary = self.open_store()
        self.assertEqual(secondary.extension_account_reservations("chrome-a")[0]["state"],
                         "reserved")
        self.drop_store(secondary)

        old = self.store
        self.drop_store(old)
        self.store = self.open_store()
        row = self.store.extension_account_reservations("chrome-a")[0]
        self.assertEqual(row["state"], "held")
        self.assertGreater(row["held_at"], 0)
        self.assertTrue(self.store.extension_account_status(
            now=row["held_at"], max_age=60)[0]["needs_reconciliation"])


class AccountRestartReconciliationTests(_Base):
    def _restart(self) -> None:
        old = self.store
        self.drop_store(old)
        self.store = self.open_store()

    def test_restart_reconciles_a_known_success_without_a_settle(self) -> None:
        self.eligible()
        job = self.job(state="running")
        self.reserve(job)
        # The canonical job reaches its durable answer, then the process dies before the ledger
        # settles. The exact `ext:` row is a known outcome, so restart must debit it, not hold it.
        self.store.succeed(job["id"], result={"payload": "fixture"})

        self._restart()

        row = self.reservation()
        self.assertEqual(row["state"], "debited")
        self.assertEqual(row["outcome"], "succeeded")
        self.assertGreater(row["terminal_at"], 0)
        status = self.store.extension_account_status(now=row["terminal_at"], max_age=60)[0]
        self.assertFalse(status["needs_reconciliation"])
        self.assertEqual(status["reserved"], 2.0)

        # The debit stays until a newer observation of the same credit unit.
        later = row["terminal_at"] + 1.0
        self.observe(now=later, balance=10.0, balance_observed_at=later)
        self.assertEqual(self.reservation()["state"], "reconciled")

    def test_restart_reconciles_a_definite_provider_failure(self) -> None:
        self.eligible()
        job = self.job(state="running")
        self.reserve(job)
        self.store.fail(job["id"], code="PROVIDER_FAILED", detail="provider rejected",
                        retryable=False)

        self._restart()

        row = self.reservation()
        self.assertEqual(row["state"], "debited")
        self.assertEqual(row["outcome"], "failed")

    def test_restart_requires_the_reserved_canonical_job_even_with_a_shared_upstream(self) -> None:
        self.eligible()
        other = self.job(state="running")
        self.store.succeed(other["id"], result={"payload": "other job"})
        reserved = self.job(state="running")
        self.reserve(reserved)
        # Upstream IDs are not unique in jobs; another job's success is not this charge's proof.
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE jobs SET upstream_job_id = ? WHERE id = ?",
                (self.store._extension_upstream_id("chrome-a", "wj-1"), other["id"]),
            )

        self._restart()

        self.assertEqual(self.reservation()["state"], "held")
        self.assertIsNone(self.reservation()["outcome"])

    def test_restart_keeps_unknown_closed_cancelled_and_newer_attempts_held(self) -> None:
        self.eligible()
        unknown = self.job(state="running")
        self.reserve(unknown)
        self.store.fail(unknown["id"], code="PROVIDER_FAILED", detail="lost",
                        recovery_state=RECOVERY_UNKNOWN)

        self.eligible(worker="chrome-b", token="token-b")
        closed = self.job(state="running")
        self.reserve(closed, worker="chrome-b", token="token-b", worker_job_id="wj-b")
        self.store.fail(closed["id"], code="PROVIDER_FAILED", detail="lost",
                        recovery_state=RECOVERY_UNKNOWN)
        self.store.close_recovery_hold(closed["id"], code=HOLD_CLOSED,
                                       detail="owner closed the hold")

        self.eligible(worker="chrome-c", token="token-c")
        cancelled = self.job(state="running")
        self.reserve(cancelled, worker="chrome-c", token="token-c", worker_job_id="wj-c")
        self.store.cancel(cancelled["id"])
        self.store.fail(cancelled["id"], code="PROVIDER_FAILED", detail="failed after cancel",
                        retryable=False)

        # The canonical id now names a newer attempt: this reservation's own attempt is unknown.
        self.eligible(worker="chrome-d", token="token-d")
        newer = self.job(state="running")
        self.reserve(newer, worker="chrome-d", token="token-d", worker_job_id="wj-d")
        self.store.succeed(newer["id"], result={"payload": "later"})
        self.store._conn.execute(
            "UPDATE jobs SET upstream_job_id = ? WHERE id = ?",
            ("ext:chrome-d:wj-d-newer", newer["id"]),
        )
        self.store._conn.commit()

        self._restart()

        for worker, worker_job_id in (("chrome-a", "wj-1"), ("chrome-b", "wj-b"),
                                      ("chrome-c", "wj-c"), ("chrome-d", "wj-d")):
            row = self.reservation(worker, worker_job_id)
            self.assertEqual(row["state"], "held", worker)
            self.assertIsNone(row["outcome"], worker)


class AccountConcurrencyTests(_Base):
    def test_two_connections_cannot_over_reserve_one_balance(self) -> None:
        self.eligible(balance=10.0)
        first, second = self.job(), self.job()
        other = self.open_store()
        start = threading.Barrier(2)
        results: list[dict] = []
        errors: list[str] = []

        def reserve(store: JobStore, job: dict, worker_job_id: str) -> None:
            try:
                start.wait(timeout=5)
                results.append(store.extension_account_reserve(
                    "chrome-a", "token-1", job_id=job["id"],
                    worker_job_id=worker_job_id, payload_sha256="a" * 64,
                    quote=self.quote(value=10.0), now=1000.0, max_age=60.0,
                ))
            except ExtensionAccountRefused as exc:
                errors.append(exc.code)

        threads = [
            threading.Thread(target=reserve, args=(self.store, first, "wj-first")),
            threading.Thread(target=reserve, args=(other, second, "wj-second")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 1)
        self.assertEqual(errors, ["ACCOUNT_CREDITS"])
        self.assertEqual(len(self.store.extension_account_reservations("chrome-a")), 1)
        self.assertEqual(self.store.extension_account_status(
            now=1000.0, max_age=60)[0]["reserved"], 10.0)


if __name__ == "__main__":
    unittest.main()
