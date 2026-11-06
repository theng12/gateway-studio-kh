"""Hub-stated TTS limits, pool concurrency, and the gateway voice library's labels.

1. The Hub's per-model `maximum_input_characters` is what the catalogue publishes (smallest
   across locations on a merge) and a long narration is not cut off before the Hub's deadline.
2. A pool runs as many requests at once as its members' providers allow -- each provider's own
   limit still holds where the member is called.
3. A gateway-held voice is the Gateway's, not "Studio Hub (this Mac)" / "Qwen3 TTS".
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-tts-limits-")

from gateway import catalog, concurrency, pools  # noqa: E402
from gateway.adapters.base import BackendRequest, BackendResult  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402


def _definition(provider_id: str, kind: str = "openai_compatible") -> ProviderDefinition:
    return ProviderDefinition(
        id=provider_id, display_name=provider_id, category="language", kind=kind,
        base_url="http://example.test", modalities=("chat", "tts"), discovery="static",
    )


class HubStatedTtsLimitTests(unittest.TestCase):
    def _limit(self, raw: dict) -> tuple[int, str]:
        return catalog._tts_characters("m", {"raw": raw}, _definition("studio-hub-local", KIND_STUDIO_HUB))

    def test_the_hub_stated_value_wins_over_the_local_default(self) -> None:
        self.assertEqual(self._limit({"maximum_input_characters": 5000}), (5000, "provider"))
        self.assertEqual(
            self._limit({"lane_contract": {"maximum_input_characters": 4000}}), (4000, "provider"))

    def test_without_a_stated_value_the_default_stands(self) -> None:
        self.assertEqual(self._limit({}), (catalog._TTS_CHARACTERS_LOCAL, "gateway"))
        self.assertEqual(self._limit({"maximum_input_characters": 0})[1], "gateway")

    def test_a_merged_row_publishes_the_smallest(self) -> None:
        row = {"maximum_input_characters": 15000, "maximum_input_characters_source": "gateway"}
        catalog._merge_capabilities(
            row, {"maximum_input_characters": 5000, "maximum_input_characters_source": "provider"}, "tts")
        self.assertEqual((row["maximum_input_characters"], row["maximum_input_characters_source"]),
                         (5000, "provider"))

    def test_the_uniform_hub_request_size_passes_through_for_every_voice_model(self) -> None:
        # Owner 2026-10-03: Studio Hub publishes 1,500 for every voice model (Breeze's measured
        # slowest-Mac size), not only for Breeze; the gateway's 15,000 is only for a Hub that
        # publishes nothing (an older build).
        for family in ("breeze-tts", "voxcpm-mlx", "fish-audio-mlx", "omnivoice", "qwen3-tts",
                       "chatterbox-mlx", "pocket-tts"):
            raw = {"family": family, "lane_contract": {
                "maximum_input_characters": 1500,
                "maximum_input_characters_source": {"evidence": "owner_uniform"}}}
            self.assertEqual(self._limit(raw), (1500, "provider"), family)
            row = catalog._tts_row("m", _definition("studio-hub-local", KIND_STUDIO_HUB), {"raw": raw})
            self.assertEqual(row["maximum_input_characters"], 1500, family)
        merged = {"maximum_input_characters": catalog._TTS_CHARACTERS_LOCAL,
                  "maximum_input_characters_source": "gateway"}
        catalog._merge_capabilities(
            merged, {"maximum_input_characters": 1500, "maximum_input_characters_source": "provider"},
            "tts")
        self.assertEqual(merged["maximum_input_characters"], 1500)

    def test_the_published_tts_row_carries_it(self) -> None:
        row = catalog._tts_row(
            "m", _definition("studio-hub-local", KIND_STUDIO_HUB), {"raw": {"maximum_input_characters": 5000}})
        self.assertEqual(row["maximum_input_characters"], 5000)


class LongNarrationDeadlineTests(unittest.TestCase):
    def _deadline(self, chars: int, raw: dict) -> float:
        from gateway.adapters.studio_hub import lane_deadline_seconds
        from gateway.providers import registry

        request = BackendRequest(
            provider_id="studio-hub-x", model_id="m", modality="tts",
            params={"text": "a" * chars}, values={}, base_url="http://127.0.0.1:1")
        with patch.object(registry.models, "models_for", return_value=[{"model_id": "m", "raw": raw}]):
            return lane_deadline_seconds(request)

    def test_it_scales_with_input_length_past_the_lane_deadline(self) -> None:
        # Per-Mac rule (2026-10-05): ceil(chars / 8.6) x RTF x 3 + 300, RTF 5.22 before the Hub
        # names the Mac; clamped to [the lane's number, 7,200]. Details: test_breeze_deadline.py.
        raw = {"maximum_input_characters": 5000, "lane_contract": {"deadline_seconds": 2700}}
        self.assertEqual(self._deadline(100, raw), 2700)       # short: the Hub's number
        self.assertEqual(self._deadline(15000, raw), 7200)     # long: the two-hour ceiling
        self.assertAlmostEqual(self._deadline(2000, raw), 233 * 5.22 * 3 + 300)

    def test_an_unstated_chunk_still_scales_but_an_unstated_lane_does_not(self) -> None:
        self.assertEqual(self._deadline(15000, {"lane_contract": {"deadline_seconds": 1800}}), 7200)
        self.assertEqual(self._deadline(15000, {}), 7200)


class PoolConcurrencyTests(unittest.TestCase):
    def test_the_budget_is_the_sum_of_the_members_providers(self) -> None:
        with patch.object(registry.credentials, "is_enabled", return_value=True):
            with patch.object(concurrency, "_settings_providers",
                              return_value={"a": {"limit": 5}, "b": {"limit": 10}}):
                budget = concurrency.pool_budget(["a::x", "a::y", "b::z"])
                self.assertEqual((budget.limit, budget.source), (15, concurrency.SOURCE_CURATED))
            with patch.object(concurrency, "_settings_providers", return_value={"a": {"limit": 5}}):
                self.assertEqual(concurrency.pool_budget(["a::x", "c::y"]).limit, 6)  # c: default 1
            with patch.object(concurrency, "_settings_providers", return_value={"pool": {"limit": 2}}):
                self.assertEqual(concurrency.pool_budget(["a::x"]).limit, 2)  # the owner's own entry wins

    def test_the_worker_asks_the_pool_not_the_default_of_one(self) -> None:
        from gateway.worker import Worker

        with patch.object(registry.credentials, "is_enabled", return_value=True), \
                patch.object(pools, "members", return_value=["a::x", "b::y"]), \
                patch.object(concurrency, "_settings_providers",
                             return_value={"a": {"limit": 3}, "b": {"limit": 4}}):
            budget = Worker(None, interval_s=10.0)._provider_budget("pool", "chat", "free")
        self.assertEqual(budget.limit, 7)

    def _run_pool(self, members: list[str], limits: dict, jobs: int,
                  barrier: threading.Barrier | None = None
                  ) -> tuple[dict[str, int], list[BackendResult]]:
        from concurrent.futures import ThreadPoolExecutor

        from gateway.adapters.pool import PoolBackend

        lock = threading.Lock()
        live: dict[str, int] = {}
        peak: dict[str, int] = {}

        class Adapter:
            def generate(self, request):
                with lock:
                    live[request.provider_id] = live.get(request.provider_id, 0) + 1
                    peak[request.provider_id] = max(peak.get(request.provider_id, 0), live[request.provider_id])
                try:
                    if barrier is not None:
                        # Three calls must be in flight together to trip it; a pool that
                        # serialises times out here instead of passing on a fast Mac.
                        barrier.wait(5)
                    else:
                        time.sleep(0.15)
                finally:
                    with lock:
                        live[request.provider_id] -= 1
                return BackendResult(text="ok")

        def resolve(_modality, member):
            provider, _, model = member.partition("::")
            return _definition(provider), {"model_id": model}

        backend = PoolBackend()
        backend.attach(resolve, lambda _definition: Adapter())
        request = BackendRequest(provider_id="pool", model_id="p", modality="chat",
                                 params={"messages": []}, values={}, base_url="")
        with patch.object(pools, "members", return_value=members), \
                patch.object(concurrency, "_settings_providers", return_value=limits), \
                ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = [executor.submit(backend.generate, request) for _ in range(jobs)]
            results = [f.result(30) for f in futures]  # a worker's exception fails the test
        return peak, results

    def test_concurrent_pool_calls_run_together_up_to_the_providers_limit(self) -> None:
        # A barrier of three, twice, is the proof: no wall clock to be slow on a busy Mac.
        peak, results = self._run_pool(
            ["a::x"], {"a": {"limit": 3}}, jobs=6, barrier=threading.Barrier(3))
        self.assertEqual(peak["a"], 3)
        self.assertEqual([r.text for r in results], ["ok"] * 6)

    def test_a_provider_at_its_default_of_one_still_runs_one_at_a_time(self) -> None:
        self.assertEqual(self._run_pool(["a::x"], {}, jobs=3)[0]["a"], 1)


class GatewayVoiceLabelTests(unittest.TestCase):
    def test_a_gateway_held_voice_is_labelled_honestly_with_its_id_unchanged(self) -> None:
        from gateway.voices import SOURCE_LOCAL, Voice, story_row

        voice = Voice(
            id="studio-hub-local::voice-abc", provider_id="studio-hub-local",
            provider_voice_id="voice-abc", display_name="Chris", source=SOURCE_LOCAL,
            raw={"engine_name": "Qwen3 TTS"})
        row = story_row(voice)
        self.assertEqual(row["id"], "studio-hub-local::voice-abc")
        self.assertEqual(row["provider"], "Gateway voice library")
        self.assertFalse(row["engine_name"])
        self.assertIsNone(row["model_family"])
        self.assertIsNone(row["tts_model_id"])


if __name__ == "__main__":
    unittest.main()
