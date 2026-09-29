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

So only an un-retryable `400` — the upstream saying the *body* is wrong, which
every other member would reject for the same reason — ends the walk. A 401, 402,
403, 404, 429, 5xx, timeout or retryable 400 all advance, because those are the
states a pool is built to walk past.

The pool is driven through its adapter directly here rather than over HTTP: the
rule lives in `PoolBackend.generate`, and a fake member adapter makes each
outcome exact instead of hoping a real provider misbehaves on cue.

The gateway modules are imported only after a temporary runtime environment is
installed, so this file cannot read or write the owner's data, credentials,
pools or providers.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class _FakeAdapter:
    """A member adapter that answers with a canned result or raises on cue."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[str] = []

    def generate(self, request):
        self.calls.append(request.model_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _TempEnv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls._env = patch.dict(
            os.environ,
            {
                "GATEWAY_DATA": str(tmp / "data"),
                "GATEWAY_ARTIFACTS": str(tmp / "data" / "artifacts"),
                "GATEWAY_DB": str(tmp / "data" / "jobs.sqlite3"),
                "GATEWAY_CREDENTIALS": str(tmp / "data" / "credentials.json"),
                "GATEWAY_MODEL_CACHE": str(tmp / "data" / "models_cache.json"),
                "GATEWAY_POOLS": str(tmp / "data" / "pools.json"),
                "GATEWAY_APP_SUPPORT": str(tmp / "app-support"),
                "GATEWAY_ENV_FILE": str(tmp / ".env"),
                "GATEWAY_NO_MIGRATE": "1",
                "STUDIOHUB_READY_FILE": str(tmp / "hub-ready.json"),
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_API_KEY": "",
            },
        )
        cls._env.start()
        sys.path.insert(0, str(ROOT))

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        cls._tmp.cleanup()

    def setUp(self):
        from gateway import pools

        self.pools = pools
        self.path = pools.POOLS_PATH
        self.path.unlink(missing_ok=True)


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

    def test_an_unretryable_400_stops_the_walk(self):
        # The body is wrong. Every remaining member would reject it identically,
        # so walking on only adds latency to a request that cannot succeed.
        #
        # `b::x` is a *working* member on purpose: this asserts it was never
        # called, which is the whole claim. A test where the remaining members
        # were also broken would pass whether or not the walk stopped.
        from gateway.adapters.base import BackendError, BackendResult

        self.pools.create("free", ["a::x", "b::x", "c::x"])
        backend, adapters = self._backend(
            {
                "a::x": BackendError("bad body", code="BAD", status=400, retryable=False),
                "b::x": BackendResult(text="from b"),
                "c::x": BackendResult(text="from c"),
            }
        )
        with self.assertRaises(BackendError) as caught:
            backend.generate(self._request())
        self.assertEqual("BAD", caught.exception.code)
        self.assertEqual([], adapters["b::x"].calls, "a request-shaped 400 must not walk on")
        self.assertEqual([], adapters["c::x"].calls)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
