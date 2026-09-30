#!/usr/bin/env python3
"""Imported price research (CONTRACT-price-evidence.md): validation is the whole point.

A number is published only with a source URL on the provider's own domain AND a verbatim quote that
contains it; everything else is dropped with a reason. Imports only add `provider_page` prices to
rows the provider itself does not price — they enable, route and change nothing else (the owner's
rule: Anthropic models stay off Story routing).
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-evidence-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(ROOT))
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

from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, catalog, price_evidence as pe  # noqa: E402
from gateway.pricing import row_price  # noqa: E402

MODELS = {
    "openai-direct": {"gpt-image-2.5": "image", "gpt-6-luna": "chat"},
    "anthropic-direct": {"claude-sonnet-5": "chat"},
}
IMG_QUOTE = "Image generation: $0.040 per image at 1024x1024 standard quality"


def price(**kw) -> dict:
    base = {"model_id": "gpt-image-2.5", "modality": "image", "unit": "per_image", "usd": 0.04,
            "variant": None, "source_url": "https://openai.com/api/pricing/", "quote": IMG_QUOTE,
            "page_date": None}
    return {**base, **kw}


def evidence(prices=(), limits=(), provider="openai-direct") -> dict:
    return {"schema": pe.SCHEMA, "researched_at": "2026-09-30T12:00:00Z", "providers": [
        {"provider_id": provider, "status": "found", "notes": "", "prices": list(prices),
         "rate_limits": list(limits)}]}


def models_of(pid: str) -> dict:
    return MODELS.get(pid, {})


def reasons(result: dict, pid: str = "openai-direct") -> list[str]:
    return [d["reason"] for d in result[pid]["dropped"]]


class ValidationTest(unittest.TestCase):
    def check(self, item: dict) -> dict:
        return pe.validate(evidence([item]), models_of)["openai-direct"]

    def test_a_number_with_domain_and_quote_is_accepted(self) -> None:
        got = self.check(price())
        self.assertEqual((len(got["prices"]), got["dropped"]), (1, []))
        self.assertEqual(got["prices"][0]["usd"], 0.04)

    def test_off_domain_urls_are_dropped(self) -> None:
        for url in ("https://evilopenai.com/pricing", "https://openai.com.evil.io/x",
                    "http://openai.com/pricing", "https://user@evil.io/openai.com",
                    "https://pricepertoken.com/openai", "", None):
            got = self.check(price(source_url=url))
            self.assertEqual(got["prices"], [], url)
            self.assertIn("official domain", got["dropped"][0]["reason"], url)
        # subdomains of the provider's own domain are fine
        self.assertEqual(len(self.check(price(source_url="https://platform.openai.com/docs"))["prices"]), 1)

    def test_a_quote_without_the_number_is_dropped(self) -> None:
        for quote in ("Image generation: $0.080 per image", "no digits here at all", "", None,
                      "$0.04"):  # bare number, no words: nothing to check it against
            got = self.check(price(quote=quote))
            self.assertEqual(got["prices"], [], quote)
        self.assertIn("quote does not contain", self.check(price(quote="costs $0.08 per image"))["dropped"][0]["reason"])
        # the number as written: trailing zeros, dollar sign, thousands commas
        self.assertEqual(len(self.check(price(usd=2.0, quote="Input: $2.00 / 1M tokens", unit="per_image"))["prices"]), 1)

    def test_unit_model_modality_and_number_are_checked(self) -> None:
        cases = (price(unit="per_second"), price(unit="input_per_1m_tokens"), price(model_id="not-ours"),
                 price(modality="chat"), price(usd=-1), price(usd=True), price(usd="0.04"), price(usd=None))
        for item in cases:
            self.assertEqual(self.check(item)["prices"], [], item)

    def test_other_units_are_kept_as_evidence_and_never_published(self) -> None:
        got = self.check(price(unit="other", unit_text="2 credits per image", usd=None,
                               quote="Each image costs 2 credits"))
        self.assertEqual((got["prices"], len(got["other"])), ([], 1))
        self.assertIsNone(pe.page_price("openai-direct", "image", "gpt-image-2.5"))

    def test_rate_limits_keep_only_verified_numbers(self) -> None:
        tier = {"tier": "Tier 1", "how_reached": "$5 paid", "scope": "per org", "rpm": 500, "tpm": 30000,
                "concurrency": 8, "source_url": "https://platform.openai.com/docs/guides/rate-limits",
                "quote": "Tier 1: 500 RPM, 30K TPM"}
        got = pe.validate(evidence(limits=[tier]), models_of)["openai-direct"]
        self.assertEqual([(t["rpm"], t["tpm"], "concurrency" in t) for t in got["rate_limits"]],
                         [(500, 30000, False)])
        self.assertEqual(reasons({"openai-direct": got}), ["concurrency: quote does not contain 8"])
        off = pe.validate(evidence(limits=[{**tier, "source_url": "https://blog.example.com/x"}]), models_of)
        self.assertEqual(off["openai-direct"]["rate_limits"], [])

    def test_unknown_provider_and_bad_files_are_refused(self) -> None:
        got = pe.validate(evidence([price()], provider="not-a-provider"), models_of)
        self.assertEqual(got["not-a-provider"]["prices"], [])
        self.assertIn("allow-list", reasons(got, "not-a-provider")[0])
        for bad in ({}, {"schema": "other"}, {"schema": pe.SCHEMA}, [], None,
                    {"schema": pe.SCHEMA, "researched_at": "yesterday", "providers": []}):
            with self.assertRaises(ValueError):
                pe.validate(bad, models_of)


class ImportAndPublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.patch = patch.object(catalog.registry.models, "models_for",
                                  side_effect=lambda pid, **_: [{"model_id": m, "modality": mod}
                                                                for m, mod in models_of(pid).items()])
        self.patch.start()
        path = pe.evidence_path()
        if path.exists():
            path.unlink()

    def tearDown(self) -> None:
        self.patch.stop()
        self.client.close()

    def row(self, provider_id: str, modality: str, model_id: str, raw: dict | None = None) -> dict:
        definition = SimpleNamespace(id=provider_id, kind="openai_compatible", display_name="P",
                                     description="", modalities=(modality,), base_url="https://x")
        built = catalog.Catalog.__new__(catalog.Catalog)
        with patch.object(catalog.registry.models, "cached", return_value={"fetched_at": 1.0}):
            return built._story_row(modality, definition, {"model_id": model_id, "modality": modality,
                                                           "display_name": "M", "raw": raw or {}}, usable=set())

    def test_accepted_prices_publish_as_provider_page_with_url_and_date(self) -> None:
        body = self.client.post("/admin/api/pricing/evidence", json=evidence([
            price(), price(model_id="gpt-6-luna", modality="chat", unit="input_per_1m_tokens", usd=1.5,
                           quote="gpt-6-luna input $1.50 per 1M tokens"),
            price(usd=0.5, quote="not the number")])).json()
        self.assertEqual((body["accepted"], body["dropped"]), (2, 1))
        row = self.row("openai-direct", "image", "gpt-image-2.5")
        self.assertEqual(row["price_source"], "provider_page")
        self.assertEqual(row["price_usd"], {"per_image": 0.04})
        self.assertEqual(row["price_url"], "https://openai.com/api/pricing/")
        self.assertEqual(row["price_as_of"], "2026-09-30T12:00:00+00:00")
        chat = self.row("openai-direct", "chat", "gpt-6-luna")
        self.assertEqual(chat["price_usd"], {"input_per_1m_tokens": 1.5})
        # the zero shape is untouched, and unresearched rows stay unpublished
        self.assertEqual(row["credits_per_image"], 0)
        self.assertEqual(self.row("openai-direct", "chat", "other")["price_source"], "unpublished")
        self.assertEqual(self.client.get("/admin/api/pricing/evidence").json()["accepted"], 2)

    def test_variants_are_never_collapsed_into_one_price(self) -> None:
        hd = price(usd=0.08, variant="hd", quote="Image generation: $0.080 per image at 1024x1024 hd quality")
        self.client.post("/admin/api/pricing/evidence", json=evidence([price(variant="standard"), hd]))
        row = self.row("openai-direct", "image", "gpt-image-2.5")
        self.assertEqual(row["price_source"], "unpublished")
        self.assertIsNone(row["price_usd"])

    def test_a_providers_own_price_outranks_the_page(self) -> None:
        self.client.post("/admin/api/pricing/evidence", json=evidence([price()]))
        raw = {"pricing": {"unit": "image", "unit_price": 0.05, "currency": "USD"}}
        row = row_price("fal", "image", raw, 1.0, "openai-direct", "gpt-image-2.5")
        self.assertEqual((row["price_source"], row["price_usd"]), ("provider", {"per_image": 0.05}))

    def test_reimport_replaces_per_provider_and_bad_files_change_nothing(self) -> None:
        self.client.post("/admin/api/pricing/evidence", json=evidence([price()]))
        self.client.post("/admin/api/pricing/evidence", json=evidence(
            [price(usd=0.06, quote="Image generation: $0.060 per image")], provider="anthropic-direct"))
        self.assertEqual(self.row("openai-direct", "image", "gpt-image-2.5")["price_source"], "provider_page")
        self.client.post("/admin/api/pricing/evidence", json=evidence([]))  # openai re-researched: nothing
        self.assertEqual(self.row("openai-direct", "image", "gpt-image-2.5")["price_source"], "unpublished")
        refused = self.client.post("/admin/api/pricing/evidence", json={"schema": "nope"})
        self.assertEqual(refused.status_code, 422)

    def test_importing_enables_and_routes_nothing(self) -> None:
        """Anthropic models stay off Story routing: evidence for them changes no enablement."""
        inventory = catalog.registry.models
        before = (json.dumps(inventory._data.get("enabled"), sort_keys=True),
                  json.dumps(inventory._data.get("user"), sort_keys=True),
                  sorted(r["id"] for r in catalog.catalog.all_rows()))
        self.client.post("/admin/api/pricing/evidence", json=evidence(
            [price(model_id="claude-sonnet-5", modality="chat", unit="input_per_1m_tokens", usd=3.0,
                   source_url="https://claude.com/pricing", quote="Sonnet input $3 / MTok")],
            provider="anthropic-direct"))
        after = (json.dumps(inventory._data.get("enabled"), sort_keys=True),
                 json.dumps(inventory._data.get("user"), sort_keys=True),
                 sorted(r["id"] for r in catalog.catalog.all_rows()))
        self.assertEqual(before, after)
        self.assertFalse([i for i in after[2] if i.startswith("anthropic-direct")])
        self.assertEqual(self.row("anthropic-direct", "chat", "claude-sonnet-5")["price_source"], "provider_page")


class ProvidersPageTest(unittest.TestCase):
    def test_the_providers_page_has_the_import_button_and_reads_the_route(self) -> None:
        html = (ROOT / "gateway/admin_ui.html").read_text()
        for needle in ("Import price research…", 'id="evidence-file"', "importEvidence", "'/pricing/evidence'"):
            self.assertIn(needle, html)


class TemplateAndCliTest(unittest.TestCase):
    def test_the_template_parses_and_its_empty_values_are_all_dropped(self) -> None:
        doc = json.loads((ROOT / "research/pricing/TEMPLATE.json").read_text())
        self.assertEqual(doc["schema"], pe.SCHEMA)
        doc["researched_at"] = "2026-09-30T12:00:00Z"
        got = pe.validate(doc)["openai-direct"]
        self.assertEqual((got["prices"], got["rate_limits"]), ([], []))
        self.assertEqual(len(got["dropped"]), 2)

    def test_cli_runs_the_same_validation_and_exits_1_on_any_drop(self) -> None:
        good, bad = _TEMP_ROOT / "good.json", _TEMP_ROOT / "bad.json"
        good.write_text(json.dumps(evidence([price()])))
        broken = evidence([price(), price(source_url="https://evil.io/x")])
        bad.write_text(json.dumps(broken))
        run = lambda f: subprocess.run([sys.executable, "-m", "gateway.price_evidence", "check", str(f)],
                                       cwd=ROOT, capture_output=True, text=True,
                                       env={**os.environ, "GATEWAY_DATA": str(_TEMP_ROOT / "cli-data")})
        ok, ko = run(good), run(bad)
        self.assertEqual((ok.returncode, ko.returncode), (0, 1), (ok.stdout, ko.stdout))
        self.assertIn("DROP", ko.stdout)
        self.assertIn("official domain", ko.stdout)
        self.assertEqual(run(_TEMP_ROOT / "missing.json").returncode, 1)
        self.assertFalse((_TEMP_ROOT / "cli-data").exists(), "the check must not touch the data dir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
