#!/usr/bin/env python3
"""Model pools — one name, several models, tried in order.

Run with::

    .venv/bin/python tests/test_pools.py

A pool exists so that "the free lane is spent" costs a retry instead of the job.
The behaviour that matters is therefore **which failures advance the walk and
which stop it**, and that is what most of this file pins. The rule is asymmetric
on purpose:

* advancing when we should have stopped costs a few provider round-trips;
* stopping when we should have advanced costs the job.

Gateway `INVALID_REQUEST` stops because local validation proved the request
wrong. A settled upstream 400 is provider/model-specific and advances, as do
other definite refusals. A paid marked send with an unknown outcome stops to
prevent duplicate paid work; free unknown-send fallback remains allowed.

The pool is driven through its adapter directly here rather than over HTTP: the
rule lives in `PoolBackend.generate`, and a fake member adapter makes each
outcome exact instead of hoping a real provider misbehaves on cue.

The gateway modules are imported only after a temporary runtime environment is
installed, so this file cannot read or write the owner's data, credentials,
pools or providers.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_SUITE_TMP: Path | None = None
_SUITE_ENV = None


def setUpModule():
    """Keep all synthetic state isolated, then preserve it under `_trash/`."""
    global _SUITE_TMP, _SUITE_ENV
    _SUITE_TMP = Path(tempfile.mkdtemp(prefix="gateway-pools-"))
    _SUITE_ENV = patch.dict(
        os.environ,
        {
            "GATEWAY_DATA": str(_SUITE_TMP / "data"),
            "GATEWAY_ARTIFACTS": str(_SUITE_TMP / "data" / "artifacts"),
            "GATEWAY_DB": str(_SUITE_TMP / "data" / "jobs.sqlite3"),
            "GATEWAY_CREDENTIALS": str(_SUITE_TMP / "data" / "credentials.json"),
            "GATEWAY_MODEL_CACHE": str(_SUITE_TMP / "data" / "models_cache.json"),
            "GATEWAY_POOLS": str(_SUITE_TMP / "data" / "pools.json"),
            "GATEWAY_APP_SUPPORT": str(_SUITE_TMP / "app-support"),
            "GATEWAY_ENV_FILE": str(_SUITE_TMP / ".env"),
            "GATEWAY_NO_MIGRATE": "1",
            "STUDIOHUB_READY_FILE": str(_SUITE_TMP / "hub-ready.json"),
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_API_KEY": "",
        },
    )
    _SUITE_ENV.start()
    sys.path.insert(0, str(ROOT))


def tearDownModule():
    if _SUITE_ENV is not None:
        _SUITE_ENV.stop()
    if sys.path and sys.path[0] == str(ROOT):
        sys.path.pop(0)
    if _SUITE_TMP is not None and _SUITE_TMP.exists():
        archive = ROOT / "_trash" / "tests" / f"test-pools-{uuid.uuid4().hex}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(_SUITE_TMP), archive)


class _FakeAdapter:
    """A member adapter that answers with a canned result or raises on cue."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[str] = []
        self.requests = []

    def generate(self, request):
        self.calls.append(request.model_id)
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _TempEnv(unittest.TestCase):
    def setUp(self):
        from gateway import pools

        self.pools = pools
        assert _SUITE_TMP is not None
        case = _SUITE_TMP / "cases" / self.__class__.__name__ / self._testMethodName
        self.path = case / "pools.json"
        path_patch = patch.object(pools, "POOLS_PATH", self.path)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        pools._cache["key"] = None
        from gateway import pool_cooldowns

        self.cooldowns = pool_cooldowns
        pool_cooldowns.reset()


# ── the store ────────────────────────────────────────────────────────────
class PoolStoreTests(_TempEnv):
    def test_a_pool_round_trips(self):
        self.pools.create("free", ["a::one", "b::two"], display_name="Free lanes")
        stored = self.pools.get("free")
        self.assertEqual(["a::one", "b::two"], stored["members"])
        self.assertEqual("Free lanes", stored["display_name"])

    def test_order_is_kept_because_it_is_the_priority(self):
        # The member order *is* the routing policy: first member is tried first.
        # Sorting or de-duplicating in a way that reordered would silently change
        # which model answers.
        self.pools.create("free", ["z::last", "a::first", "m::middle"])
        self.assertEqual(["z::last", "a::first", "m::middle"], self.pools.get("free")["members"])

    def test_a_repeated_member_is_kept_once(self):
        self.pools.create("free", ["a::one", "a::one", "b::two"])
        self.assertEqual(["a::one", "b::two"], self.pools.get("free")["members"])

    def test_a_member_must_be_a_namespaced_gateway_id(self):
        # A pool holds this gateway's own ids. Accepting a bare `gemini-3.5-flash`
        # would store something that can never resolve, and the failure would
        # surface as a pool that mysteriously answers less often than it should.
        with self.assertRaises(ValueError) as caught:
            self.pools.create("free", ["gemini-3.5-flash"])
        self.assertIn("namespaced", str(caught.exception))

    def test_a_pool_cannot_contain_a_pool(self):
        # A pool of pools is a loop, and a one-member pool of itself is an
        # infinite one. Refused when written rather than detected per request.
        with self.assertRaises(ValueError) as caught:
            self.pools.create("outer", ["pool::inner"])
        self.assertIn("cannot contain another pool", str(caught.exception))

    def test_a_pool_needs_a_member(self):
        with self.assertRaises(ValueError):
            self.pools.create("empty", [])

    def test_a_name_cannot_contain_the_namespace_separator(self):
        # Story Studio sends back `pool::<name>`; a name holding `::` would
        # resolve to a different id than the one published.
        with self.assertRaises(ValueError):
            self.pools.create("a::b", ["p::m"])

    def test_an_empty_pool_file_drops_memberless_entries(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"version":1,"pools":[{"name":"hollow","members":[]}]}')
        # Published, it would be a pickable row that resolves to nothing and
        # fails on use -- "a model that is enabled but cannot serve" is the worst
        # state, so it never reaches the catalogue.
        self.assertEqual([], self.pools.names())

    def test_update_replaces_members_and_leaves_the_rest_alone(self):
        self.pools.create("free", ["a::one"], display_name="Keep me")
        self.pools.update("free", members=["b::two"])
        stored = self.pools.get("free")
        self.assertEqual(["b::two"], stored["members"])
        self.assertEqual("Keep me", stored["display_name"])

    def test_remove_reports_whether_it_removed_anything(self):
        self.pools.create("free", ["a::one"])
        self.assertTrue(self.pools.remove("free"))
        self.assertFalse(self.pools.remove("free"))

    def test_the_file_is_written_owner_only(self):
        self.pools.create("free", ["a::one"])
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(0o600, mode)


# ── the provider it publishes ────────────────────────────────────────────
class PoolProviderTests(_TempEnv):
    def test_no_pools_means_no_provider(self):
        # An empty `pool` provider would sit in the console and the picker with
        # nothing in it.
        self.assertIsNone(self.pools.definition())

    def test_pools_become_one_provider_whose_models_are_the_pools(self):
        self.pools.create("free", ["a::one"], display_name="Free lanes")
        definition = self.pools.definition()
        self.assertEqual(self.pools.POOL_PROVIDER_ID, definition.id)
        self.assertEqual(["free"], [m["id"] for m in definition.static_models])
        self.assertEqual("Free lanes", definition.static_models[0]["display_name"])

    def test_the_provider_needs_no_credential_and_is_not_discovered(self):
        from gateway.providers import registry

        self.pools.create("free", ["a::one"])
        definition = registry.definition(self.pools.POOL_PROVIDER_ID)
        self.assertIsNotNone(definition)
        # No credential fields is what makes `is_configured` true, which is what
        # lets the pool reach `enabled_models` through the ordinary path.
        self.assertEqual((), definition.credential_fields)
        self.assertTrue(registry.credentials.is_configured(self.pools.POOL_PROVIDER_ID))

    def test_a_pool_is_published_under_the_pool_namespace(self):
        from gateway.catalog import catalog

        self.pools.create("free", ["a::one"], display_name="Free lanes")
        ids = [row["id"] for row in catalog.rows("chat")]
        self.assertIn("pool::free", ids)

    def test_a_disabled_provider_is_reported_rather_than_hidden(self):
        # The health read exists so a stale member is visible. Resolution is not
        # a promise the member will answer -- that is what the pool is for -- but
        # "this names a model you have since disabled" is knowable here.
        self.pools.create("free", ["nope::missing"])
        described = self.pools.describe("free")
        self.assertEqual(0, described["resolvable_count"])
        self.assertFalse(described["members_detail"][0]["resolves"])


# ── the walk: which failures advance ─────────────────────────────────────
class PoolFailoverTests(_TempEnv):
    def _backend(self, outcomes):
        """A PoolBackend whose members resolve to fake adapters.

        `outcomes` maps a member id to either a BackendResult or an exception.
        """
        from gateway.adapters.base import BackendResult
        from gateway.adapters.pool import PoolBackend
        from gateway.providers.definitions import ProviderDefinition

        adapters = {m: _FakeAdapter(o) for m, o in outcomes.items()}

        def resolve(_modality, member):
            if member not in adapters:
                return None, None
            definition = ProviderDefinition(
                id=member.split("::")[0],
                display_name="Fake",
                category="language",
                kind="openai_compatible",
                base_url="http://example.test",
                modalities=("chat",),
                discovery="static",
            )
            return definition, {"model_id": member.split("::")[1]}

        backend = PoolBackend()
        backend.attach(resolve, lambda definition: adapters[f"{definition.id}::x"])
        return backend, adapters

    def _request(self, name="free"):
        from gateway.adapters.base import BackendRequest

        return BackendRequest(
            provider_id="pool",
            model_id=name,
            modality="chat",
            params={"messages": []},
            values={},
            base_url="",
        )

    def test_it_answers_with_the_first_member_that_works(self):
        from gateway.adapters.base import BackendResult

        self.pools.create("free", ["a::x", "b::x", "c::x"])
        backend, adapters = self._backend(
            {"a::x": BackendResult(text="from a"), "b::x": BackendResult(text="from b")}
        )
        result = backend.generate(self._request())
        self.assertEqual("from a", result.text)
        self.assertEqual(["x"], adapters["a::x"].calls)
        self.assertEqual([], adapters["b::x"].calls, "a working first member must not be retried past")

    def test_a_spent_lane_advances_to_the_next(self):
        # The whole point: 429 is what "the free lane is used up" looks like, and
        # it must never end the request.
        from gateway.adapters.base import BackendError, BackendResult

        self.pools.create("free", ["a::x", "b::x"])
        backend, _ = self._backend(
            {
                "a::x": BackendError("rate limited", code="RATE_LIMITED", status=429, retryable=True),
                "b::x": BackendResult(text="from b"),
            }
        )
        self.assertEqual("from b", backend.generate(self._request()).text)

    def test_every_provider_side_status_advances(self):
        from gateway.adapters.base import BackendError, BackendResult

        for status in (401, 402, 403, 404, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.pools.create("free", ["a::x", "b::x"])
                backend, _ = self._backend(
                    {
                        "a::x": BackendError("nope", code="X", status=status),
                        "b::x": BackendResult(text="from b"),
                    }
                )
                self.assertEqual(
                    "from b",
                    backend.generate(self._request()).text,
                    f"a {status} from one member must not end the pool",
                )

    def test_a_timeout_with_no_status_advances(self):
        from gateway.adapters.base import BackendError, BackendResult

        self.pools.create("free", ["a::x", "b::x"])
        backend, _ = self._backend(
            {
                "a::x": BackendError("timed out", code="TIMEOUT", status=None),
                "b::x": BackendResult(text="from b"),
            }
        )
        self.assertEqual("from b", backend.generate(self._request()).text)

    def test_a_retryable_400_advances(self):
        # A retryable 400 is the provider being briefly unhappy, not the body
        # being wrong -- the adapter is the one that knows which, and it said so.
        from gateway.adapters.base import BackendError, BackendResult

        self.pools.create("free", ["a::x", "b::x"])
        backend, _ = self._backend(
            {
                "a::x": BackendError("busy", code="X", status=400, retryable=True),
                "b::x": BackendResult(text="from b"),
            }
        )
        self.assertEqual("from b", backend.generate(self._request()).text)

    def test_an_explicit_local_invalid_request_stops_the_walk(self):
        # The old test treated every settled 400 as proof that the caller's body
        # was wrong. Providers also use 400 for model-specific refusals, so only
        # the gateway's own validation code can make that claim across members.
        #
        # `b::x` is a *working* member on purpose: this asserts it was never
        # called, which is the whole claim. A test where the remaining members
        # were also broken would pass whether or not the walk stopped.
        from gateway.adapters.base import BackendError, BackendResult

        self.pools.create("free", ["a::x", "b::x", "c::x"])
        backend, adapters = self._backend(
            {
                "a::x": BackendError("bad body", code="INVALID_REQUEST", status=422),
                "b::x": BackendResult(text="from b"),
                "c::x": BackendResult(text="from c"),
            }
        )
        with self.assertRaises(BackendError) as caught:
            backend.generate(self._request())
        self.assertEqual("INVALID_REQUEST", caught.exception.code)
        self.assertEqual([], adapters["b::x"].calls, "gateway validation must not walk on")
        self.assertEqual([], adapters["c::x"].calls)

    def test_provider_400s_exhaust_the_pool_and_preserve_every_attempt(self):
        from gateway.adapters.base import BackendError

        self.pools.create("free", ["a::x", "b::x", "c::x"])
        backend, adapters = self._backend({
            member: BackendError(f"{member} rejected", code="PROVIDER_FAILED", status=400)
            for member in ("a::x", "b::x", "c::x")
        })
        with self.assertRaises(BackendError) as caught:
            backend.generate(self._request())
        self.assertEqual("PROVIDER_FAILED", caught.exception.code)
        self.assertEqual(400, caught.exception.status)
        self.assertEqual([1, 1, 1], [len(adapters[m].calls) for m in ("a::x", "b::x", "c::x")])
        for member in ("a::x", "b::x", "c::x"):
            self.assertIn(member, str(caught.exception))

    def test_a_member_that_no_longer_resolves_is_skipped_not_fatal(self):
        from gateway.adapters.base import BackendResult

        self.pools.create("free", ["gone::x", "b::x"])
        backend, _ = self._backend({"b::x": BackendResult(text="from b")})
        self.assertEqual("from b", backend.generate(self._request()).text)

    def test_an_adapter_bug_on_one_member_does_not_take_the_pool_down(self):
        from gateway.adapters.base import BackendResult

        self.pools.create("free", ["a::x", "b::x"])
        backend, _ = self._backend(
            {"a::x": RuntimeError("adapter bug"), "b::x": BackendResult(text="from b")}
        )
        self.assertEqual("from b", backend.generate(self._request()).text)

    def test_the_attempts_are_published_on_the_result(self):
        # "Which member answered, and what did the others say" is the question a
        # pool makes unavoidable. Without it a pool that works less often than
        # its list suggests is unreadable.
        from gateway.adapters.base import BackendError, BackendResult

        self.pools.create("free", ["a::x", "b::x"])
        backend, _ = self._backend(
            {
                "a::x": BackendError("spent", code="RATE_LIMITED", status=429),
                "b::x": BackendResult(text="from b"),
            }
        )
        trail = backend.generate(self._request()).payload["pool"]
        self.assertEqual("b::x", trail["used"])
        self.assertEqual("free", trail["name"])
        self.assertEqual(
            [("a::x", "failed"), ("b::x", "answered")],
            [(a["member"], a["outcome"]) for a in trail["attempts"]],
        )

    def test_every_member_failing_reports_a_provider_failure_not_a_pool_one(self):
        # The job's code and retryability must still describe a provider -- Story
        # Studio reads them to decide whether to advance its own lineup.
        from gateway.adapters.base import BackendError

        self.pools.create("free", ["a::x", "b::x"])
        backend, _ = self._backend(
            {
                "a::x": BackendError("spent", code="RATE_LIMITED", status=429, retryable=True),
                "b::x": BackendError("spent too", code="RATE_LIMITED", status=429, retryable=True),
            }
        )
        with self.assertRaises(BackendError) as caught:
            backend.generate(self._request())
        error = caught.exception
        self.assertEqual("RATE_LIMITED", error.code)
        self.assertEqual(429, error.status)
        self.assertTrue(error.retryable)
        # Both attempts are named, so the sentence is actionable.
        self.assertIn("a::x", str(error))
        self.assertIn("b::x", str(error))

    def test_a_pool_with_no_members_is_named_rather_than_answered(self):
        from gateway.adapters.base import BackendError
        from gateway.adapters.pool import PoolBackend

        backend = PoolBackend()
        backend.attach(lambda *_: (None, None), lambda *_: None)
        with self.assertRaises(BackendError) as caught:
            backend.generate(self._request("vanished"))
        self.assertEqual("POOL_EMPTY", caught.exception.code)


# ── quota memory ─────────────────────────────────────────────────────────
class PoolQuotaMemoryTests(_TempEnv):
    """A member out of quota is skipped until its reset, across restarts, and never on a hunch."""

    _backend = PoolFailoverTests._backend
    _request = PoolFailoverTests._request

    NOW = 1_800_000_000.0  # a fixed instant; the clock below only moves when a test says so

    def setUp(self):
        super().setUp()
        self.now = self.NOW
        patcher = patch.object(self.cooldowns, "_clock", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _quota(self, **kw):
        from gateway.adapters.base import BackendError

        return BackendError(
            "g: HTTP 429: quota", code="PROVIDER_QUOTA_EXHAUSTED", status=429, retryable=True, **kw
        )

    def _ok(self, text):
        from gateway.adapters.base import BackendResult

        return BackendResult(text=text)

    def test_an_exhausted_member_is_not_asked_again_until_its_reset(self):
        self.pools.create("free", ["a::x", "b::x"])
        backend, adapters = self._backend(
            {"a::x": self._quota(retry_after=300), "b::x": self._ok("from b")}
        )
        self.assertEqual("from b", backend.generate(self._request()).text)
        self.assertEqual(1, len(adapters["a::x"].calls))
        self.now += 120  # still inside the 300 s the provider asked for
        result = backend.generate(self._request())
        self.assertEqual("from b", result.text)
        self.assertEqual(1, len(adapters["a::x"].calls), "a cooling member costs no round-trip")
        outcomes = {a["member"]: a["outcome"] for a in result.payload["pool"]["attempts"]}
        self.assertEqual("cooling-down", outcomes["a::x"])

    def test_the_member_is_tried_again_after_the_reset_and_a_success_clears_it(self):
        from gateway.adapters.base import BackendResult

        self.pools.create("free", ["a::x", "b::x"])
        backend, adapters = self._backend(
            {"a::x": self._quota(retry_after=300), "b::x": self._ok("from b")}
        )
        backend.generate(self._request())
        self.now += 301
        adapters["a::x"].outcome = BackendResult(text="a is back")
        self.assertEqual("a is back", backend.generate(self._request()).text)
        self.assertEqual(2, len(adapters["a::x"].calls))
        self.assertIsNone(self.cooldowns.until("a::x"))
        self.assertEqual({}, self.cooldowns.active())

    def test_the_memory_survives_a_restart(self):
        self.pools.create("free", ["a::x", "b::x"])
        backend, adapters = self._backend(
            {"a::x": self._quota(retry_after=600), "b::x": self._ok("from b")}
        )
        backend.generate(self._request())
        self.assertTrue(self.cooldowns._path().exists())
        self.cooldowns.reset()  # a new process: nothing in memory, only the file
        self.now += 60
        backend.generate(self._request())
        self.assertEqual(1, len(adapters["a::x"].calls))
        self.assertEqual(self.NOW + 600, self.cooldowns.until("a::x"))

    def test_the_file_is_written_atomically_and_owner_only(self):
        self.cooldowns.mark("a::x", self.NOW + 60, kind="minute", reason="r")
        path = self.cooldowns._path()
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual([], [p.name for p in path.parent.glob("*.tmp")])

    def test_when_every_member_is_cooling_the_soonest_to_recover_is_tried(self):
        self.pools.create("free", ["a::x", "b::x", "c::x"])
        backend, adapters = self._backend(
            {"a::x": self._ok("from a"), "b::x": self._ok("from b"), "c::x": self._ok("from c")}
        )
        self.cooldowns.mark("a::x", self.NOW + 900, kind="hint", reason="")
        self.cooldowns.mark("b::x", self.NOW + 100, kind="hint", reason="")
        self.cooldowns.mark("c::x", self.NOW + 500, kind="hint", reason="")
        self.assertEqual("from b", backend.generate(self._request()).text)
        self.assertEqual([], adapters["a::x"].calls + adapters["c::x"].calls)
        self.assertIsNone(self.cooldowns.until("b::x"), "its success clears it")

    def test_an_unresolvable_member_does_not_hide_the_all_cooling_rule(self):
        self.pools.create("free", ["gone::x", "a::x"])
        backend, _adapters = self._backend({"a::x": self._ok("from a")})
        self.cooldowns.mark("a::x", self.NOW + 100, kind="hint", reason="")
        self.assertEqual("from a", backend.generate(self._request()).text)

    def test_non_quota_failures_do_not_start_a_cooldown(self):
        from gateway.adapters.base import BackendError

        self.pools.create("free", ["a::x", "b::x"])
        for failure in (
            BackendError("boom", code="PROVIDER_FAILED", status=500, retryable=True),
            BackendError("timeout", code="PROVIDER_UNAVAILABLE", retryable=True),
            BackendError("denied", code="PROVIDER_AUTH_FAILED", status=401),
            BackendError("bad body", code="PROVIDER_FAILED", status=404),
        ):
            backend, adapters = self._backend({"a::x": failure, "b::x": self._ok("from b")})
            backend.generate(self._request())
            backend.generate(self._request())
            self.assertEqual(2, len(adapters["a::x"].calls), f"{failure} must be retried every job")
            self.assertEqual({}, self.cooldowns.active())

    def test_a_provider_quota_word_counts_even_without_a_429(self):
        from gateway.adapters.base import BackendError

        self.pools.create("free", ["a::x", "b::x"])
        backend, _ = self._backend(
            {
                "a::x": BackendError(
                    "RESOURCE_EXHAUSTED: Quota exceeded", code="PROVIDER_FAILED", status=503
                ),
                "b::x": self._ok("from b"),
            }
        )
        backend.generate(self._request())
        self.assertIsNotNone(self.cooldowns.until("a::x"))

    def test_a_per_minute_limit_without_a_hint_waits_sixty_seconds(self):
        ends, kind = self.cooldowns.cooldown_until(self._quota(), self.NOW)
        self.assertEqual((self.NOW + 60, "minute"), (ends, kind))

    def test_a_daily_limit_waits_for_midnight_pacific_even_if_a_short_delay_is_given(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ends, kind = self.cooldowns.cooldown_until(
            self._quota(retry_after=34, quota_scope="day"), self.NOW
        )
        self.assertEqual("daily", kind)
        local = datetime.fromtimestamp(ends, ZoneInfo("America/Los_Angeles"))
        self.assertEqual((0, 0), (local.hour, local.minute))
        self.assertGreater(ends, self.NOW)
        self.assertLessEqual(ends - self.NOW, 24 * 3600 + 3600)

    def test_the_providers_own_retry_hint_in_the_message_is_used(self):
        from gateway.adapters.base import BackendError

        exc = BackendError("g: HTTP 429: Please retry in 34.5s", status=429)
        ends, kind = self.cooldowns.cooldown_until(exc, self.NOW)
        self.assertEqual((self.NOW + 34.5, "hint"), (ends, kind))

    def test_google_and_retry_after_headers_are_read_into_the_error(self):
        from gateway.adapters.base import quota_hints

        class _Response:
            headers = {"retry-after": "42"}

            def json(self):
                return {
                    "error": {
                        "details": [
                            {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                            {"retryDelay": "34s"},
                        ]
                    }
                }

        self.assertEqual({"retry_after": 42.0, "quota_scope": "day"}, quota_hints(_Response()))

        class _Wrapped(_Response):
            headers = {}

            def json(self):
                return [super().json()]

        self.assertEqual({"retry_after": 34.0, "quota_scope": "day"}, quota_hints(_Wrapped()))

    def test_the_description_reports_who_is_cooling_and_why(self):
        self.pools.create("free", ["a::x"])
        self.cooldowns.mark("a::x", self.NOW + 60, kind="daily", reason="quota")
        row = self.pools.describe("free")["members_detail"][0]
        self.assertEqual(self.NOW + 60, row["cooldown_until"])
        self.assertEqual("daily", row["cooldown_kind"])
        self.assertEqual("missing", row["state"])


# ── the admin surface ────────────────────────────────────────────────────
class PoolAdminTests(_TempEnv):
    def test_the_routes_manage_pools_and_say_no_restart_is_needed(self):
        from gateway import admin

        created = admin.create_pool(
            {"name": "free", "members": ["a::one"], "display_name": "Free lanes"}
        )
        # A pool is read from its file on every catalogue build, so it is
        # pickable immediately. Answering "restart" out of symmetry would be
        # false, and a control that appears to do nothing is the failure this
        # codebase keeps paying for.
        self.assertFalse(created["restart_required"])
        self.assertEqual("free", created["created"])

        listed = admin.list_pools()
        self.assertEqual(["free"], [p["name"] for p in listed["pools"]])
        self.assertEqual("pool::", listed["namespace"])

        updated = admin.update_pool("free", {"members": ["b::two"]})
        self.assertEqual(["b::two"], updated["pool"]["members"])

        self.assertEqual("free", admin.delete_pool("free")["removed"])
        self.assertEqual([], admin.list_pools()["pools"])

    def test_updating_a_pool_that_is_not_there_is_a_404(self):
        from gateway import admin

        with self.assertRaises(admin.AdminError) as caught:
            admin.update_pool("ghost", {"members": ["a::one"]})
        self.assertEqual(404, caught.exception.status)

    def test_a_bad_member_is_refused_with_the_reason(self):
        from gateway import admin

        with self.assertRaises(admin.AdminError) as caught:
            admin.create_pool({"name": "free", "members": ["bare-model-id"]})
        self.assertEqual(422, caught.exception.status)
        self.assertIn("namespaced", str(caught.exception))


class PoolPaidLastResortTests(_TempEnv):
    """Owner decision 2026-10-03: a paid member is used only when every free member is out for
    the DAY (or unavailable); a free member on a per-minute limit is waited for instead.

    Members whose provider id starts with `f` are free (`free_tier`); the rest are paid by the
    catalogue's own rule (no price stated is never read as free).
    """

    NOW = 1_800_000_000.0

    def setUp(self):
        super().setUp()
        from gateway.adapters import pool as pool_adapter

        self.adapter_module = pool_adapter
        self.now = self.NOW
        self.slept = 0.0
        self.on_sleep = None
        for patcher in (
            patch.object(self.cooldowns, "_clock", lambda: self.now),
            patch.object(pool_adapter, "_sleep", self._sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _sleep(self, seconds):
        self.now += seconds
        self.slept += seconds
        if self.on_sleep:
            self.on_sleep()

    def _backend(self, outcomes):
        from gateway.adapters.pool import PoolBackend
        from gateway.providers.definitions import ProviderDefinition

        adapters = {m: _FakeAdapter(o) for m, o in outcomes.items()}

        def resolve(_modality, member):
            if member not in adapters:
                return None, None
            provider = member.split("::")[0]
            return ProviderDefinition(
                id=provider, display_name=provider, category="language", kind="openai_compatible",
                base_url="http://example.test", modalities=("chat",), discovery="static",
                free_tier=provider.startswith("f"),
            ), {"model_id": "x"}

        backend = PoolBackend()
        backend.attach(resolve, lambda definition: adapters[f"{definition.id}::x"])
        return backend, adapters

    def _request(self, **params):
        from gateway.adapters.base import BackendRequest

        return BackendRequest(provider_id="pool", model_id="mix", modality="chat",
                              params={"messages": [], **params}, values={}, base_url="")

    _quota = PoolQuotaMemoryTests._quota
    _ok = PoolQuotaMemoryTests._ok

    def _daily(self, member):
        self.cooldowns.mark(member, self.NOW + 10 * 3600, kind="daily", reason="PerDay")

    def test_the_split_is_the_catalogues_free_paid_rule(self):
        from gateway import pricing
        from gateway.providers.definitions import ProviderDefinition

        def definition(pid, free=False):
            return ProviderDefinition(id=pid, display_name=pid, category="language",
                                      kind="openai_compatible", base_url="", modalities=("chat",),
                                      discovery="static", free_tier=free)

        self.assertFalse(pricing.model_chargeable(definition("google-ai-free", True),
                                                  {"model_id": "gemini-3.8-flash"}))
        luna = {"model_id": "openai/gpt-6-luna",
                "raw": {"pricing": {"prompt": "0.0000001", "completion": "0.0000005"}}}
        self.assertTrue(pricing.model_chargeable(definition("openrouter-main"), luna))

    def test_a_free_member_on_a_minute_limit_is_waited_for_and_paid_is_never_called(self):
        self.pools.create("mix", ["f1::x", "f2::x", "paid::x"])
        backend, adapters = self._backend(
            {"f1::x": self._quota(retry_after=40), "f2::x": self._ok("f2"), "paid::x": self._ok("paid")})
        self._daily("f2::x")

        def back():
            adapters["f1::x"].outcome = self._ok("f1 after the wait")
        self.on_sleep = back
        waits = []
        request = self._request()
        request.on_wait = waits.append

        result = backend.generate(request)
        self.assertEqual("f1 after the wait", result.text)
        self.assertEqual([], adapters["paid::x"].calls, "a per-minute limit never costs money")
        self.assertGreaterEqual(self.slept, 40)
        self.assertFalse(result.payload["pool"]["paid"])
        self.assertIn("waited", [a["outcome"] for a in result.payload["pool"]["attempts"]])
        self.assertIn("per-minute", waits[0]["reason"])
        self.assertIsNone(waits[-1], "the job is told the wait is over")

    def test_when_every_free_member_is_out_for_the_day_the_paid_one_answers(self):
        self.pools.create("mix", ["f1::x", "f2::x", "paid::x"])
        backend, adapters = self._backend(
            {"f1::x": self._ok("f1"), "f2::x": self._quota(quota_scope="day"), "paid::x": self._ok("paid")})
        self._daily("f1::x")
        with self.assertLogs("gateway.adapters.pool", "WARNING") as logs:
            result = backend.generate(self._request())
        self.assertEqual("paid", result.text)
        self.assertEqual([], adapters["f1::x"].calls, "a member out for the day costs no round-trip")
        self.assertEqual(1, len(adapters["f2::x"].calls))
        self.assertEqual(0, self.slept, "nothing worth waiting for")
        pool = result.payload["pool"]
        self.assertTrue(pool["paid"])
        self.assertEqual({"member": "paid::x", "outcome": "answered", "paid": True}, pool["attempts"][-1])
        self.assertIn("free members out for the day → used paid member paid::x", logs.output[0])

    def test_unavailable_or_broken_free_members_fall_through_to_paid(self):
        from gateway.adapters.base import BackendError

        self.pools.create("mix", ["gone::x", "f1::x", "paid::x"])  # `gone` does not resolve
        backend, adapters = self._backend(
            {"f1::x": BackendError("boom", code="PROVIDER_FAILED", status=503), "paid::x": self._ok("paid")})
        result = backend.generate(self._request())
        self.assertEqual("paid", result.text)
        self.assertEqual(1, len(adapters["f1::x"].calls))
        self.assertEqual(0, self.slept)

        self.pools.create("mix", ["gone::x", "paid::x"])  # every free member switched off
        self.assertEqual("paid", backend.generate(self._request()).text)

    def test_a_generic_provider_400_from_free_falls_through_to_paid_unchanged(self):
        from gateway.adapters.base import BackendError

        self.pools.create("mix", ["f1::x", "paid::x"])
        backend, adapters = self._backend({
            "f1::x": BackendError("model refused it", code="PROVIDER_FAILED", status=400),
            "paid::x": self._ok("paid"),
        })
        request = self._request(response_format={"type": "json_schema", "json_schema": {"strict": True}})
        result = backend.generate(request)
        self.assertEqual("paid", result.text)
        self.assertIs(request.params, adapters["paid::x"].requests[0].params)

    def test_a_generic_provider_400_from_one_paid_member_reaches_luna(self):
        from gateway.adapters.base import BackendError

        self.pools.create("mix", ["openrouter-paid::x", "gpt6-luna::x"])
        backend, adapters = self._backend({
            "openrouter-paid::x": BackendError(
                "model refused it", code="PROVIDER_FAILED", status=400
            ),
            "gpt6-luna::x": self._ok("luna"),
        })
        result = backend.generate(self._request())
        self.assertEqual("luna", result.text)
        self.assertEqual(1, len(adapters["openrouter-paid::x"].calls))
        self.assertEqual(1, len(adapters["gpt6-luna::x"].calls))

    def test_a_cancellation_during_the_wait_ends_the_job_without_paying(self):
        from gateway.adapters.base import BackendError

        self.pools.create("mix", ["f1::x", "paid::x"])
        backend, adapters = self._backend({"f1::x": self._ok("f1"), "paid::x": self._ok("paid")})
        self.cooldowns.mark("f1::x", self.NOW + 120, kind="minute", reason="")
        with patch.object(self.adapter_module, "_cancel_requested", lambda _r: self.slept >= 5):
            with self.assertRaises(BackendError) as caught:
                backend.generate(self._request(_job_id="job-1"))
        self.assertEqual("POOL_WAIT_CANCELLED", caught.exception.code)
        self.assertIs(False, caught.exception.upstream_accepted)
        self.assertLess(self.slept, 10, "the wait notices the cancel within a step or two")
        self.assertEqual([], adapters["f1::x"].calls + adapters["paid::x"].calls)

    def test_free_members_on_the_minute_limit_for_the_whole_ceiling_fall_back_to_paid(self):
        self.pools.create("mix", ["f1::x", "paid::x"])
        backend, adapters = self._backend({"f1::x": self._quota(), "paid::x": self._ok("paid")})
        with self.assertLogs("gateway.adapters.pool", "WARNING") as logs:
            result = backend.generate(self._request())
        self.assertEqual("paid", result.text)
        self.assertEqual(1, len(adapters["paid::x"].calls))
        self.assertGreater(self.slept, 0, "it waited for the free member first")
        self.assertLessEqual(self.slept, self.adapter_module.FREE_WAIT_CEILING_S)
        pool = result.payload["pool"]
        self.assertTrue(pool["paid"])
        outcomes = [a["outcome"] for a in pool["attempts"]]
        self.assertEqual(["wait-ceiling", "answered"], outcomes[-2:])
        self.assertEqual({"member": "paid::x", "outcome": "answered", "paid": True}, pool["attempts"][-1])
        self.assertIn("free members rate-limited for 15 min → used paid member paid::x", logs.output[0])

    def test_the_ceiling_fallback_goes_through_the_paid_members_in_order(self):
        from gateway.adapters.base import BackendError

        self.pools.create("mix", ["f1::x", "p1::x", "p2::x"])
        backend, adapters = self._backend({
            "f1::x": self._quota(),
            "p1::x": BackendError("boom", code="PROVIDER_FAILED", status=503),
            "p2::x": self._ok("p2")})
        self.assertEqual("p2", backend.generate(self._request()).text)
        self.assertEqual(1, len(adapters["p1::x"].calls))

        backend, adapters = self._backend({"f1::x": self._quota(), "p1::x": self._quota(),
                                           "p2::x": BackendError("boom", code="PROVIDER_FAILED", status=503)})
        with self.assertRaises(BackendError) as caught:
            backend.generate(self._request())
        self.assertEqual(503, caught.exception.status, "the last real failure, once paid ones fail too")

    def test_a_pool_of_only_paid_members_walks_as_before(self):
        self.pools.create("mix", ["p1::x", "p2::x"])
        backend, adapters = self._backend({"p1::x": self._ok("p1"), "p2::x": self._ok("p2")})
        self.cooldowns.mark("p1::x", self.NOW + 300, kind="hint", reason="")
        self.cooldowns.mark("p2::x", self.NOW + 60, kind="minute", reason="")
        result = backend.generate(self._request())
        self.assertEqual("p2", result.text, "the soonest-recovering one, at once")
        self.assertEqual(0, self.slept)
        self.assertTrue(result.payload["pool"]["paid"])

    def test_a_pool_of_only_free_members_walks_as_before(self):
        self.pools.create("mix", ["f1::x", "f2::x"])
        backend, adapters = self._backend({"f1::x": self._ok("f1"), "f2::x": self._ok("f2")})
        self.cooldowns.mark("f1::x", self.NOW + 60, kind="minute", reason="")
        self.cooldowns.mark("f2::x", self.NOW + 30, kind="minute", reason="")
        result = backend.generate(self._request())
        self.assertEqual("f2", result.text, "the soonest-recovering one, without waiting")
        self.assertEqual(0, self.slept)
        self.assertFalse(result.payload["pool"]["paid"])

    def test_the_description_marks_paid_members_and_the_rule(self):
        from gateway.catalog import catalog

        self.pools.create("mix", ["f1::x", "paid::x"])
        backend, _ = self._backend({"f1::x": self._ok("f1"), "paid::x": self._ok("paid")})
        with patch.object(catalog, "resolve", backend._resolve):
            described = self.pools.describe("mix")
        self.assertEqual([False, True], [r["paid"] for r in described["members_detail"]])
        self.assertTrue(described["paid_last_resort"])


class PoolResolverWiringTests(unittest.TestCase):
    """The resolver the live pool backend is attached to must be the catalogue's own.

    Every other pool test attaches a fake resolver, which is how `from .. import catalog`
    (the module, which has no `resolve`) shipped and failed every real pool job with
    AttributeError in under a second.
    """

    def test_the_attached_resolver_reaches_the_catalogue_instance(self):
        from unittest import mock
        from gateway import adapters
        from gateway.catalog import catalog
        with mock.patch.object(catalog, "resolve", return_value=("d", "m")) as resolve:
            self.assertEqual(adapters._resolve_member("chat", "p::m"), ("d", "m"))
        resolve.assert_called_once_with("chat", "p::m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
