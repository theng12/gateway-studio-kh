#!/usr/bin/env python3
"""Token usage on every job, whatever the outcome (owner, 2026-11-18: "add the Token usage per
job records so it's accurate and has data").

Live 2026-11-18 on 1301: a successful chat job carried a full `usage` block, while the two
content-filtered 100- and 50-scene packs (`8383f81a`, `f7cce0b0`) carried `max_tokens_asked` and
`finish_reason: content_filter` and **no usage at all** — so the tokens those models burned before
the filter were invisible, and so was every paid retry around them. The rules here:

* usage is published for a failed, cancelled or filtered job too — it burned tokens;
* a figure the provider did not report is `null` and renders as "not reported by the provider",
  never 0;
* a pool walk records one entry per member with that member's own usage or an explicit null;
* a late or adopted answer keeps its usage beside the verdict.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-token-usage-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_ARTIFACTS": str(_TEMP_ROOT / "data" / "artifacts"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_API_KEY": "{{SECRET_h7k2m9pq}}",
        "GATEWAY_FLEET": str(_TEMP_ROOT / "fleet.json"),
        "GATEWAY_VOICES": str(_TEMP_ROOT / "voices.json"),
        "GATEWAY_VOICES_OUT": str(_TEMP_ROOT / "voices.json"),
    }
)

USED = {"input_tokens": 18, "output_tokens": 4}


class TokenUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        from gateway import api

        self.api = api

    def _job(self, *, provider: str = "openai-direct") -> dict:
        row, created = self.api.store.submit(
            modality="chat", model_id="gpt-6-luna", params={"messages": []},
            idempotency_key=f"usage-{self.id()}-{os.urandom(4).hex()}",
            provider_id=provider,
        )
        self.assertTrue(created)
        return row

    def test_a_failed_job_publishes_the_tokens_it_burned(self) -> None:
        job = self._job()
        self.api.store.fail(
            job["id"], code="POLICY_REFUSAL", detail="answer stopped early (content_filter)",
            evidence={"finish_reason": "content_filter", "max_tokens_asked": 76800, "usage": USED},
        )
        body = self.api._job_envelope(self.api.store.get(job["id"]))
        self.assertEqual(body["state"], "failed")
        self.assertEqual(USED, body["usage"])
        self.assertEqual("provider", body["usage_source"])
        self.assertEqual(76800, body["max_tokens_asked"])

    def test_an_unreported_figure_is_null_and_never_zero(self) -> None:
        job = self._job()
        self.api.store.fail(
            job["id"], code="PROVIDER_TIMEOUT", detail="the provider went silent",
            evidence={"finish_reason": None, "usage": None},
        )
        body = self.api._job_envelope(self.api.store.get(job["id"]))
        self.assertIsNone(body["usage"], "unreported is null, not {}")
        self.assertIsNone(body["usage_source"])
        self.assertNotIn("0", json.dumps(body.get("usage")), "no zero may stand in for no data")

    def test_a_cancelled_job_keeps_whatever_it_had_spent(self) -> None:
        job = self._job()
        self.api.store.cancel(job["id"], by="operator", reason="stop", origin="console")
        body = self.api._job_envelope(self.api.store.get(job["id"]))
        self.assertEqual(body["state"], "cancelled")
        self.assertIsNone(body["usage"], "no provider figure exists, so nothing is claimed")

    def test_a_late_answer_keeps_its_usage_beside_the_verdict(self) -> None:
        job = self._job()
        self.api.store.fail(job["id"], code="UNKNOWN_OUTCOME", detail="restarted",
                            recovery_state="unknown_outcome", recovery_detail="restarted")
        # The original worker answers after the hold was expired/closed: the verdict stays, the
        # answer is kept beside it — and so is what it cost.
        self.api.store.expire_hold(job["id"], code="HOLD_EXPIRED", detail="waited past its ceiling")
        self.api.store.succeed(job["id"], result={"payload": {"usage": USED, "text": "late"}})
        row = self.api.store.get(job["id"])
        self.assertEqual("succeeded", row["late_result_state"])
        self.assertEqual("failed", row["state"], "a terminal verdict is not reopened")
        body = self.api._job_envelope(row)
        self.assertEqual(USED, body["usage"], "the late answer's cost rides with it")

    def test_a_pool_walk_publishes_each_members_own_usage(self) -> None:
        # The published walk is what the Jobs page draws, so the normalisation (dict or explicit
        # null) is asserted here; the pool itself records the raw entries (tests/test_pools.py).
        job = self._job(provider="pool")
        self.api.store.fail(
            job["id"], code="POOL_EXHAUSTED", detail="no member answered",
            evidence={
                "usage": USED,
                "pool_attempts": [
                    {"member": "a::x", "outcome": "failed", "code": "POLICY_REFUSAL",
                     "usage": {"input_tokens": 900, "output_tokens": 0}},
                    {"member": "b::x", "outcome": "unavailable"},
                    {"member": "c::x", "outcome": "failed", "code": "PROVIDER_AUTH_FAILED",
                     "usage": None},
                ],
            },
        )
        body = self.api._job_envelope(self.api.store.get(job["id"]))
        walk = body["pool_usage"]
        self.assertEqual(["a::x", "b::x", "c::x"], [entry["member"] for entry in walk])
        self.assertEqual({"input_tokens": 900, "output_tokens": 0}, walk[0]["usage"])
        self.assertIsNone(walk[1]["usage"], "a member that never answered says so")
        self.assertIsNone(walk[2]["usage"])


class PoolRecordsItsMembersUsage(unittest.TestCase):
    def test_a_member_call_records_usage_and_a_refusal_records_none(self) -> None:
        from gateway.adapters.base import BackendError, BackendResult
        from gateway.adapters.pool import PoolBackend
        from gateway.providers.definitions import ProviderDefinition

        shared = Path(tempfile.mkdtemp(prefix="gateway-token-usage-pool-"))
        self.addCleanup(shutil.rmtree, shared, ignore_errors=True)
        from gateway import pools

        path = shared / "pools.json"
        with patch.object(pools, "POOLS_PATH", path):
            pools._cache["key"] = None
            pools.create("story-llm", ["a::x", "c::x"])
            refused = BackendError(
                "a: answer stopped early (content_filter)", code="POLICY_REFUSAL", status=200,
                refusal_scope="response",
            )
            refused.evidence = {"usage": {"input_tokens": 900, "output_tokens": 0}}
            adapters = {
                "a::x": _Adapter(refused),
                "c::x": _Adapter(BackendResult(text="ok", payload={"usage": USED})),
            }

            def resolve(_modality, member):
                if member not in adapters:
                    return None, None
                definition = ProviderDefinition(
                    id=member.split("::")[0], display_name="Fake", category="language",
                    kind="openai_compatible", base_url="http://example.test",
                    modalities=("chat",), discovery="static",
                )
                return definition, {"model_id": member.split("::")[1]}

            backend = PoolBackend()
            backend.attach(resolve, lambda definition: adapters[f"{definition.id}::x"])
            from gateway.adapters.base import BackendRequest

            result = backend.generate(BackendRequest(
                provider_id="pool", model_id="story-llm", modality="chat",
                params={"messages": []}, values={}, base_url="",
            ))
        attempts = result.payload["pool"]["attempts"]
        self.assertEqual(["a::x", "c::x"], [a["member"] for a in attempts])
        self.assertEqual({"input_tokens": 900, "output_tokens": 0}, attempts[0]["usage"])
        self.assertEqual(USED, attempts[1]["usage"])


class _Adapter:
    def __init__(self, outcome):
        self.outcome = outcome

    def generate(self, request):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


if __name__ == "__main__":
    unittest.main()
