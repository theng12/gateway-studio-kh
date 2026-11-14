#!/usr/bin/env python3
"""The Models page publishes one row per model, with its machines beneath it.

H2 from the audit: `_flat_models()` iterated definitions × locations, so a model
every Mac ran appeared once per Mac with the same name, differing only by a
provider name and with nothing saying it was already offered elsewhere — 108 rows
for 21 models. That is not only a presentation defect: the owner had to tick the
same model on every Mac, one at a time.

These drive the server side of the fix — the merge, the "N of M machines" counts,
the provider filter, the bulk route and the facets — with a fake registry, so
there is no gateway, no Hub and no network. What it holds to:

  * a location's model is **one** row, with `locations` naming each Mac;
  * every field the API already published survives unchanged (this is additive);
  * selecting one Mac still finds the model it shares, and a bulk action reaches
    **every** Mac on the row rather than the one the row happens to name;
  * a cloud vendor keeps its own row: two vendors offering one model id are
    different products, and a vendor is not a machine.

Run with::

    .venv/bin/python tests/test_models_grouping.py
"""

from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-model-search-")

from gateway import admin  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.definitions import (  # noqa: E402
    DISCOVERY_STUDIO_HUB,
    KIND_OPENAI_COMPATIBLE,
    KIND_STUDIO_HUB,
    ProviderDefinition,
)

LOCAL = "studio-hub-local"
MAC_A = "studio-hub-aaa"
MAC_B = "studio-hub-bbb"
CLOUD = "openai-direct"

EXISTING_FIELDS = (
    "provider_id", "provider_name", "provider_enabled", "model_id", "display_name",
    "modality", "source", "enabled", "free_lane", "data_use",
)


def _definition(provider_id: str, name: str, kind: str, modalities: tuple[str, ...]):
    return ProviderDefinition(
        id=provider_id, display_name=name, category="multi", kind=kind, base_url="",
        modalities=modalities, discovery=DISCOVERY_STUDIO_HUB,
    )


def _model(model_id: str, modality: str, enabled: bool) -> dict:
    return {
        "model_id": model_id, "display_name": model_id, "modality": modality,
        "source": "discovered", "enabled": enabled, "raw": {},
    }


class _Inventory:
    """Only the three reads `_flat_models` makes of an inventory."""

    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self.rows = rows
        self.writes: list[tuple[str, list[str], bool]] = []

    def models_for(self, provider_id: str, *, include_disabled: bool = True) -> list[dict]:
        return [dict(row) for row in self.rows.get(provider_id, [])]

    @staticmethod
    def can_publish(_definition, _model) -> bool:
        return True

    @staticmethod
    def cached(_provider_id: str) -> dict:
        return {"fetched_at": 1_790_000_000}

    def set_many(self, provider_id: str, model_ids: list[str], enabled: bool) -> int:
        self.writes.append((provider_id, list(model_ids), enabled))
        return len(model_ids)


class _Credentials:
    def __init__(self, off: tuple[str, ...] = ()) -> None:
        self.off = set(off)

    def is_enabled(self, provider_id: str) -> bool:
        return provider_id not in self.off


class GroupingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definitions = [
            _definition(LOCAL, "Studio Hub (this Mac)", KIND_STUDIO_HUB, ("tts", "chat")),
            _definition(MAC_A, "Studio One", KIND_STUDIO_HUB, ("tts", "chat")),
            _definition(MAC_B, "Studio Two", KIND_STUDIO_HUB, ("tts", "chat")),
            _definition(CLOUD, "OpenAI", KIND_OPENAI_COMPATIBLE, ("chat",)),
        ]
        self.inventory = _Inventory({
            LOCAL: [_model("shared", "tts", True), _model("local-only", "tts", False)],
            MAC_A: [_model("shared", "tts", False)],
            MAC_B: [_model("shared", "tts", True)],
            CLOUD: [_model("shared", "chat", True)],
        })
        self.credentials = _Credentials()
        self.patches = patch.multiple(
            registry,
            definitions=lambda: list(self.definitions),
            definition=lambda provider_id: next(
                (d for d in self.definitions if d.id == provider_id), None
            ),
            models=self.inventory,
            credentials=self.credentials,
        )
        self.patches.start()

    def tearDown(self) -> None:
        self.patches.stop()

    def _rows(self) -> list[dict]:
        return admin._flat_models()

    @staticmethod
    def _find(rows: list[dict], provider_id: str, model_id: str) -> dict:
        return next(
            row for row in rows
            if row["provider_id"] == provider_id and row["model_id"] == model_id
        )

    def test_admin_rows_carry_the_catalogues_price_truth(self) -> None:
        """The Models page shows a Price column: the rows must carry what /v1/models publishes."""
        self.inventory.rows[CLOUD] = [
            {**_model("priced", "chat", True),
             "raw": {"pricing": {"prompt": "0.0000015", "completion": "0.000009"}}},
            _model("silent", "chat", True),
        ]
        rows = self._rows()
        local = self._find(rows, LOCAL, "shared")
        self.assertEqual((local["price_source"], local["price_usd"]), ("local", {"per_1k_chars": 0}))
        priced = self._find(rows, CLOUD, "priced")
        self.assertEqual(priced["price_source"], "provider")
        self.assertEqual(priced["price_usd"]["input_per_1m_tokens"], 1.5)
        self.assertTrue(priced["price_as_of"].startswith("2026-"))
        silent = self._find(rows, CLOUD, "silent")
        self.assertEqual((silent["price_source"], silent["price_usd"]), ("unpublished", None))

    def test_a_location_model_is_one_row_naming_every_machine(self) -> None:
        rows = self._rows()
        merged = self._find(rows, LOCAL, "shared")
        self.assertTrue(merged["is_location"])
        self.assertEqual(merged["locations_total"], 3)
        self.assertEqual(merged["locations_enabled"], 2)
        self.assertTrue(merged["enabled"], "one machine serving it is what the catalogue asks")
        self.assertEqual(
            [machine["provider_name"] for machine in merged["locations"]],
            ["Studio Hub (this Mac)", "Studio One", "Studio Two"],
        )
        self.assertEqual(
            [machine["enabled"] for machine in merged["locations"]], [True, False, True]
        )
        self.assertEqual(merged["provider_ids"], [LOCAL, MAC_A, MAC_B])
        # The row's own identity fields come from this Mac first, and the name is
        # the namespace Story Studio is told.
        self.assertEqual(merged["provider_name"], "Studio Hub")
        self.assertEqual(merged["model_id"], "shared")
        self.assertEqual(merged["modality"], "tts")

        # Exactly three rows: one per (model, modality) on a location, one per
        # vendor row. The three Macs' copies are **not** three rows.
        self.assertEqual(len(rows), 3, [row["model_id"] for row in rows])
        self.assertEqual(
            len([row for row in rows if row["model_id"] == "shared" and row["is_location"]]),
            1,
        )

    def test_every_previously_published_field_is_still_there(self) -> None:
        for row in self._rows():
            for field in EXISTING_FIELDS:
                self.assertIn(field, row, f"{field} disappeared from {row['model_id']}")
            self.assertIsInstance(row["enabled"], bool)
            self.assertIsInstance(row["provider_enabled"], bool)
            self.assertIn("locations", row)
            self.assertIn("locations_enabled", row)
            self.assertIn("locations_total", row)
            self.assertIn("provider_ids", row)

    def test_a_cloud_vendor_keeps_its_own_row_and_no_machines(self) -> None:
        rows = self._rows()
        cloud = self._find(rows, CLOUD, "shared")
        self.assertFalse(cloud["is_location"])
        self.assertIsNone(cloud["locations"], "a vendor is not one machine")
        self.assertIsNone(cloud["locations_enabled"])
        self.assertIsNone(cloud["locations_total"])
        self.assertEqual(cloud["provider_ids"], [CLOUD])
        # It is a separate row from the location's `shared`, keyed by namespace.
        self.assertNotEqual(
            self._find(rows, LOCAL, "shared")["provider_name"], cloud["provider_name"]
        )

    def test_selecting_one_machine_finds_the_model_it_shares(self) -> None:
        rows = self._rows()
        for machine in (MAC_A, MAC_B):
            matched = admin._filter_models(rows, provider_id=machine)
            self.assertEqual(
                [row["model_id"] for row in matched], ["shared"],
                f"{machine} must find the model it shares, not only its own primary row",
            )
        # This Mac is also the primary of its own second row, which a filter on it
        # must keep: the filter selects a *machine*, not the row's first name.
        self.assertEqual(
            [row["model_id"] for row in admin._filter_models(rows, provider_id=LOCAL)],
            ["shared", "local-only"],
        )
        # A machine that does not offer it still matches nothing.
        self.assertEqual(
            admin._filter_models(rows, provider_id=CLOUD, modality="tts"), []
        )

    def test_the_provider_filter_offers_providers_and_the_macs_are_locations(self) -> None:
        rows = self._rows()
        options = admin._provider_options(rows)
        self.assertIn((CLOUD, "OpenAI"), options)
        self.assertIn(
            ("studio-hub", "Hub Studio KH (local)"), options,
            "the fleet is one provider entry, named for the owner",
        )
        self.assertNotIn((MAC_A, "Studio One"), options,
                         "a Mac is a location, not a provider")
        # The Macs are offered by the locations sub-filter instead.
        locations = admin._location_options(rows)
        self.assertIn((MAC_A, "Studio One"), locations)
        self.assertIn((MAC_B, "Studio Two"), locations)
        # And the provider entry selects every fleet row.
        matched = admin._filter_models(rows, provider_id="studio-hub")
        self.assertTrue(matched)
        self.assertTrue(all(row["is_location"] for row in matched))

    def test_a_bulk_action_reaches_every_machine_on_the_row(self) -> None:
        rows = self._rows()
        changed = admin._apply([self._find(rows, LOCAL, "shared")], False)
        self.assertEqual(changed, 3, "one row, three machines, three decisions")
        self.assertEqual(
            sorted(provider_id for provider_id, _ids, _enabled in self.inventory.writes),
            sorted([LOCAL, MAC_A, MAC_B]),
        )
        self.assertTrue(all(enabled is False for _p, _ids, enabled in self.inventory.writes))
        self.assertEqual(
            {ids[0] for _p, ids, _e in self.inventory.writes}, {"shared"},
        )

    def test_the_route_publishes_the_grouped_counts_and_consistent_facets(self) -> None:
        page = admin.search_models(limit=100)
        # Three models, not three plus three Mac copies: the count the header
        # shows is a count of models.
        self.assertEqual(page["listable_total"], 3)
        self.assertEqual(page["enabled_total"], 2)
        self.assertEqual(
            sum(page["facets"]["modality"].values()), page["listable_total"],
            "every chip counts the rows the page would show",
        )
        self.assertEqual(
            page["facets"]["status"]["enabled"] + page["facets"]["status"]["disabled"],
            page["listable_total"],
        )
        providers = {pair[0] for pair in page["providers"]}
        self.assertEqual(providers, {"studio-hub", CLOUD},
                         "providers, never Macs — the Macs are the locations list")
        machines = {pair[0] for pair in page["locations"]}
        self.assertTrue({LOCAL, MAC_A, MAC_B} <= machines, page["locations"])
        # The dashboard's own count is over the whole catalogue, per operation.
        self.assertEqual(sum(item["total"] for item in page["summary"].values()),
                         page["listable_total"])
        self.assertEqual(
            sum(item["enabled"] for item in page["summary"].values()),
            page["enabled_total"],
        )
        self.assertTrue(page["summary"]["tts"]["total"] >= 1)

    def test_one_model_id_in_two_modalities_stays_two_rows(self) -> None:
        # Pathological but possible: a group whose rows disagreed about the
        # operation could not be filtered or faceted honestly, and the operation
        # is what the operator shops by.
        self.inventory.rows[MAC_A] = [
            _model("shared", "tts", False), _model("shared", "chat", True),
        ]
        rows = self._rows()
        self.assertEqual(
            sorted(row["modality"] for row in rows if row["model_id"] == "shared"
                   and row["is_location"]),
            ["chat", "tts"],
        )

    def test_a_provider_switched_off_still_shows_its_machines(self) -> None:
        self.credentials.off = {MAC_B}
        merged = self._find(self._rows(), LOCAL, "shared")
        self.assertTrue(merged["provider_enabled"], "one machine still serving it is enough")
        self.assertEqual(
            {machine["provider_name"]: machine["provider_enabled"]
             for machine in merged["locations"]},
            {"Studio Hub (this Mac)": True, "Studio One": True, "Studio Two": False},
            "the switched-off Mac is still named, so the switch can be found",
        )

    def test_model_search_treats_common_id_separators_as_spaces(self) -> None:
        model_id = "mlx-community/gemma-4-26B-A4B-it-qat-4bit"
        self.inventory.rows[LOCAL].append(_model(model_id, "chat", True))

        for query in (
            "gemma 4",
            "GEMMA_4",
            "gemma-4",
            "mlx_community/gemma 4-26b-a4b-it-qat-4bit",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    [row["model_id"] for row in admin._filter_models(self._rows(), q=query)],
                    [model_id],
                )
        self.assertEqual(admin._filter_models(self._rows(), q="gemini 4"), [])

    def test_search_and_bulk_share_the_exact_safe_match(self) -> None:
        model_id = "mlx-community/gemma-4-26B-A4B-it-qat-4bit"
        self.inventory.rows[LOCAL].append(_model(model_id, "chat", False))
        self.credentials.off = {LOCAL}
        before_rows = deepcopy(self.inventory.rows)
        before_off = self.credentials.off.copy()

        page = admin.search_models(q="gemma 4", provider_id=LOCAL, limit=100)
        self.assertEqual(page["matched"], 1)
        self.assertEqual(page["facets"]["status"], {"enabled": 0, "disabled": 1})
        self.assertEqual(page["facets"]["modality"]["chat"], 1)
        self.assertFalse(page["models"][0]["enabled"])
        self.assertFalse(page["models"][0]["provider_enabled"])
        self.assertEqual(self.inventory.rows, before_rows, "search must not change model choices")
        self.assertEqual(self.credentials.off, before_off, "search must not switch on a provider")
        self.assertEqual(self.inventory.writes, [])

        result = admin.bulk_models(
            {"q": "gemma 4", "provider_id": LOCAL, "enabled": True}
        )
        self.assertEqual(result["matched"], page["matched"])
        self.assertEqual(
            self.inventory.writes,
            [(LOCAL, [model_id], True)],
            "the disabled provider's model remains manageable from the matching row",
        )

    def test_a_separator_only_search_matches_nothing_in_browse_facets_or_bulk(self) -> None:
        rows = self._rows()
        self.assertEqual(admin._filter_models(rows, q="   "), rows)
        page = admin.search_models(q="-", limit=100)
        self.assertEqual(page["matched"], 0)
        self.assertEqual(page["facets"]["status"], {"enabled": 0, "disabled": 0})
        result = admin.bulk_models({"q": "-", "enabled": False})
        self.assertEqual(result["matched"], 0)
        self.assertEqual(self.inventory.writes, [])

    def test_meaningful_punctuation_is_preserved_across_browse_facets_and_bulk(self) -> None:
        target = "vendor/c++-instruct"
        unrelated = "vendor/c-model"
        self.inventory.rows[CLOUD] = [
            _model(target, "chat", True),
            _model(unrelated, "chat", True),
        ]

        page = admin.search_models(q="c++", provider_id=CLOUD, limit=100)
        self.assertEqual([row["model_id"] for row in page["models"]], [target])
        self.assertEqual(page["matched"], 1)
        self.assertEqual(page["facets"]["status"], {"enabled": 1, "disabled": 0})
        self.assertEqual(page["facets"]["modality"]["chat"], 1)

        result = admin.bulk_models(
            {"q": "c++", "provider_id": CLOUD, "enabled": False}
        )
        self.assertEqual(result["matched"], page["matched"])
        self.assertEqual(self.inventory.writes, [(CLOUD, [target], False)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
