#!/usr/bin/env python3
"""Price truth beside the price shape (owner decision 2026-09-30, CONTRACT-pricing.md).

Every catalogue row keeps `constants.zero_pricing`'s zero-valued credit fields exactly (Story's
parser drops a row without them) and now also says `price_source`: `provider` (the provider's own
response carried the number), `local` (the owner's Macs: free) or `unpublished` (nobody told us).
A number is never published without a provider source, and a job envelope carries `cost_usd`
only when the provider's response states what it charged.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-pricing-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
    "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
    "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
    "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    "GATEWAY_API_KEY": "",
})

from gateway import api, catalog  # noqa: E402
from gateway.constants import zero_pricing  # noqa: E402
from gateway.pricing import job_cost, row_price  # noqa: E402
from gateway.providers import discovery  # noqa: E402
from gateway.providers.definitions import KIND_FAL, ProviderDefinition  # noqa: E402

# Shapes below are the providers' real `/models` rows as cached on the owner's Mac (2026-09-30).
OPENROUTER = {"pricing": {"prompt": "0.000002", "completion": "0.00001",
                          "input_cache_read": "0.0000002", "web_search": "0.01"}}
GROQ = {"pricing": {"prompt": "0.000000075", "completion": "0.0000003", "image": "0",
                    "request": "0", "input_cache_read": "0.0000000375"}}
TOKENHARBOR = {"pricing": {"input_usd_per_1m": 3, "output_usd_per_1m": 15}}
XAI = {"prompt_text_token_price": 12500, "completion_text_token_price": 25000,
       "cached_prompt_text_token_price": 2000}
OMNIROUTE = {"pricing": {"input": 2, "output": 8, "cached": 1}}
NOW = 1790000000.0


class RowPriceTest(unittest.TestCase):
    def test_openrouter_shape_is_per_token_usd_converted_to_per_million(self) -> None:
        row = row_price("openai_compatible", "chat", OPENROUTER, NOW)
        self.assertEqual(row["price_source"], "provider")
        self.assertEqual(row["price_usd"], {"input_per_1m_tokens": 2.0, "output_per_1m_tokens": 10.0,
                                            "cached_input_per_1m_tokens": 0.2})
        self.assertTrue(row["price_as_of"].startswith("2026-09-2"))

    def test_groq_tokenharbor_and_xai_read_their_own_stated_units(self) -> None:
        self.assertEqual(row_price("openai_compatible", "chat", GROQ, NOW)["price_usd"]["input_per_1m_tokens"], 0.075)
        self.assertEqual(row_price("openai_compatible", "chat", TOKENHARBOR, NOW)["price_usd"],
                         {"input_per_1m_tokens": 3.0, "output_per_1m_tokens": 15.0})
        # xAI: USD cents per 100M tokens -> 12500 / 10000 = $1.25 per 1M.
        self.assertEqual(row_price("xai", "chat", XAI, NOW)["price_usd"],
                         {"input_per_1m_tokens": 1.25, "output_per_1m_tokens": 2.5,
                          "cached_input_per_1m_tokens": 0.2})

    def test_a_stated_zero_is_a_provider_price_and_a_sentinel_is_not(self) -> None:
        free = row_price("openai_compatible", "chat", {"pricing": {"prompt": "0", "completion": "0"}}, NOW)
        self.assertEqual((free["price_source"], free["price_usd"]["input_per_1m_tokens"]), ("provider", 0.0))
        dynamic = row_price("openai_compatible", "chat", {"pricing": {"prompt": "-1", "completion": "-1"}}, NOW)
        self.assertEqual((dynamic["price_source"], dynamic["price_usd"]), ("unpublished", None))

    def test_no_statement_means_unpublished_never_zero(self) -> None:
        for kind, modality, raw in (
            ("openai_compatible", "chat", {"id": "gpt-x"}),           # OpenAI /models: no price
            ("openai_compatible", "chat", OMNIROUTE),                 # unit undocumented: skipped
            ("openai_compatible", "image", OPENROUTER),               # token price is not an image price
            ("kie", "image", {}), ("bfl", "image", None), ("pool", "chat", {}),
            ("fal", "image", {"pricing": {"unit_price": 0.03, "unit": "megapixel", "currency": "USD"}}),
            ("fal", "image", {"pricing": {"unit_price": 0.03, "unit": "image", "currency": "EUR"}}),
        ):
            row = row_price(kind, modality, raw, NOW)
            self.assertEqual((row["price_source"], row["price_usd"], row["price_as_of"]),
                             ("unpublished", None, None), (kind, modality, raw))

    def test_fal_stated_units(self) -> None:
        for unit, key in (("image", "per_image"), ("video", "per_video"), ("second", "per_second")):
            row = row_price("fal", "video", {"pricing": {"unit_price": 0.4, "unit": unit, "currency": "USD"}}, NOW)
            self.assertEqual((row["price_source"], row["price_usd"]), ("provider", {key: 0.4}))

    def test_the_local_fleet_is_free_and_says_so(self) -> None:
        for modality, key in (("chat", "input_per_1m_tokens"), ("image", "per_image"),
                              ("tts", "per_1k_chars"), ("transcription", "per_second"),
                              ("video", "per_video"), ("music", "per_request")):
            row = row_price("studio_hub", modality, {"pricing": {"prompt": "9"}}, NOW)
            self.assertEqual(row["price_source"], "local")
            self.assertEqual(row["price_usd"][key], 0)
            self.assertIsNone(row["price_as_of"])


class CatalogRowTest(unittest.TestCase):
    def build(self, kind: str, modality: str, raw: dict, provider_id: str = "p-main") -> dict:
        definition = SimpleNamespace(id=provider_id, kind=kind, display_name="P", description="",
                                     modalities=(modality,), base_url="https://x")
        built = catalog.Catalog.__new__(catalog.Catalog)
        with patch.object(catalog.registry.models, "cached", return_value={"fetched_at": NOW}):
            return built._story_row(modality, definition,
                                    {"model_id": "m", "modality": modality, "display_name": "M", "raw": raw},
                                    usable=set())

    def test_rows_publish_the_source_and_keep_the_zero_shape_untouched(self) -> None:
        cases = (("openai_compatible", "chat", OPENROUTER, "provider"),
                 ("openai_compatible", "chat", {}, "unpublished"),
                 ("kie", "image", {}, "unpublished"),
                 ("elevenlabs", "tts", {}, "unpublished"),
                 ("studio_hub", "image", {}, "local"))
        for kind, modality, raw, source in cases:
            row = self.build(kind, modality, raw)
            self.assertEqual(row["price_source"], source, (kind, modality))
            for key, value in zero_pricing(modality).items():
                self.assertEqual(row[key], value, f"{key} on {kind}/{modality} must stay the zero shape")

    def test_no_number_is_published_without_a_provider_source(self) -> None:
        for kind, modality, raw in (("openai_compatible", "chat", OMNIROUTE), ("google", "chat", {}),
                                    ("anthropic", "chat", {"id": "c"}), ("fish", "tts", {}),
                                    ("pool", "chat", {})):
            row = self.build(kind, modality, raw)
            self.assertEqual((row["price_source"], row["price_usd"]), ("unpublished", None))
        local = self.build("studio_hub", "chat", {})
        self.assertTrue(all(v == 0 for v in local["price_usd"].values()))
        priced = self.build("openai_compatible", "chat", OPENROUTER)
        self.assertEqual(priced["price_source"], "provider")


class JobCostTest(unittest.TestCase):
    OPENROUTER_USAGE = {"prompt_tokens": 643, "cost": 0.0027754454, "is_byok": False}

    def test_a_stated_cost_is_published_with_its_source(self) -> None:
        self.assertEqual(job_cost({"usage": self.OPENROUTER_USAGE}),
                         {"cost_usd": 0.0027754454, "cost_source": "provider"})
        self.assertEqual(job_cost({"usage": {"cost": 0.0, "is_byok": False}})["cost_usd"], 0.0)

    def test_nothing_stated_means_nothing_published(self) -> None:
        for payload in (None, {}, {"usage": {"prompt_tokens": 5}},
                        {"usage": {"cost": 1.5}},                       # bare cost: unit not assumed
                        {"usage": {"cost": "n/a", "is_byok": False}},
                        {"usage": {"cost": -1, "is_byok": False}}):
            self.assertEqual(job_cost(payload), {}, payload)

    def test_the_envelope_carries_it_only_when_stated(self) -> None:
        def envelope(usage: dict) -> dict:
            job = {"id": "j1", "modality": "chat", "state": "succeeded", "provider_id": "p",
                   "model_id": "m", "result_json": '{"text": "hi", "payload": {"usage": %s}}'
                   % __import__("json").dumps(usage)}
            with patch.object(api, "_result_of", return_value={"text": "hi", "payload": {"usage": usage}}), \
                    patch.object(api, "catalog_encode", return_value="p::m"), \
                    patch.object(api, "_lifecycle_fields", return_value={}), \
                    patch.object(api, "_contract_fields", return_value={}), \
                    patch.object(api, "_publish_attempt_evidence"), \
                    patch.object(api, "_publish_queue_evidence"):
                return api._job_envelope(job)
        stated = envelope(self.OPENROUTER_USAGE)
        self.assertEqual((stated["cost_usd"], stated["cost_source"]), (0.0027754454, "provider"))
        silent = envelope({"prompt_tokens": 5})
        self.assertNotIn("cost_usd", silent)
        self.assertNotIn("cost_source", silent)


class FalDiscoveryPricesTest(unittest.TestCase):
    def test_discovery_stores_fal_stated_prices_and_survives_a_pricing_failure(self) -> None:
        definition = ProviderDefinition(
            id="fal-ai", display_name="fal", category="multi", kind=KIND_FAL,
            base_url="https://queue.fal.run", catalog_base_url="https://api.fal.ai/v1",
            modalities=("image", "video"), discovery="fal_catalog")
        listing = {"models": [
            {"endpoint_id": "fal-ai/flux/dev", "metadata": {"category": "text-to-image"}},
            {"endpoint_id": "fal-ai/veo3", "metadata": {"category": "text-to-video"}},
        ]}
        pricing = {"prices": [{"endpoint_id": "fal-ai/flux/dev", "unit_price": 0.025,
                               "unit": "image", "currency": "USD"}]}

        class Reply:
            def __init__(self, body, status=200):
                self._body, self.status_code, self.text = body, status, ""
            def json(self):
                return self._body

        def get(url, **_):
            return Reply(pricing if url.endswith("/models/pricing") else listing)

        with patch.object(discovery.httpx, "get", get):
            result = discovery._fal_catalog(definition, {"api_key": "k"})
        by_id = {m.model_id: m for m in result.models}
        self.assertEqual(by_id["fal-ai/flux/dev"].raw["pricing"]["unit_price"], 0.025)
        self.assertNotIn("pricing", by_id["fal-ai/veo3"].raw, "an unstated price stays absent")
        row = row_price("fal", "image", by_id["fal-ai/flux/dev"].raw, NOW)
        self.assertEqual((row["price_source"], row["price_usd"]), ("provider", {"per_image": 0.025}))

        def flaky(url, **_):
            return Reply({}, 500) if url.endswith("/models/pricing") else Reply(listing)

        with patch.object(discovery.httpx, "get", flaky):
            result = discovery._fal_catalog(definition, {"api_key": "k"})
        self.assertEqual(len(result.models), 2)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
