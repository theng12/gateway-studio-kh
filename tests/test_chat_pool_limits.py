"""A chat pool publishes only limits known for every currently enabled member."""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._isolation import sandbox_env

_SANDBOX = sandbox_env("gateway-chat-pool-limits-")

from gateway import catalog, fleet, pools
from gateway.providers import registry
from gateway.providers.definitions import KIND_OPENAI_COMPATIBLE

FIELDS = ("context_window_tokens", "max_output_tokens", "max_request_tokens")


def tearDownModule():
    _SANDBOX._finalizer.detach()
    archive = Path(__file__).resolve().parents[1] / "_trash/tests" / f"chat-pool-limits-{uuid.uuid4().hex}"
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(_SANDBOX.name, archive)


class ChatPoolLimitsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(pools, "POOLS_PATH", Path(_SANDBOX.name) / self.id() / "pools.json"))
        pools._cache["key"] = None
        pools.create("writing", ["a::one", "b::two"])
        self.definitions = [SimpleNamespace(id=p, kind=KIND_OPENAI_COMPATIBLE) for p in ("a", "b")]
        self.enabled = {"a", "b"}
        self.limits = {
            "a": dict(zip(FIELDS, (49152, 8192, 23000))),
            "b": dict(zip(FIELDS, (32768, 16384, 16000))),
        }
        self.aliases = {}
        self.enterContext(patch.object(registry, "definitions", side_effect=lambda: [*self.definitions, pools.definition()]))
        self.enterContext(patch.object(registry.credentials, "is_enabled", return_value=True))
        self.enterContext(patch.object(registry.credentials, "is_configured", return_value=True))
        self.enterContext(patch.object(registry.models, "models_for", side_effect=self.models_for))
        self.enterContext(patch.object(registry.models, "enabled_models", side_effect=self.enabled_models))
        self.enterContext(patch.object(registry.models, "cached", return_value={}))
        self.enterContext(patch.object(fleet, "usable_location_ids", return_value=set()))
        self.enterContext(patch.object(fleet, "snapshot", return_value={"locations": []}))
        self.enterContext(patch.object(fleet, "is_paused_provider", return_value=False))
        self.enterContext(patch.object(catalog, "_slots_by_location", return_value={}))
        self.enterContext(patch.object(pools, "image_pools", return_value=[]))
        self.enterContext(patch.object(catalog.Catalog, "_story_row", side_effect=self.story_row))
        self.catalog = catalog.Catalog()
        # Exercise the next-read fingerprint path, without starting a background thread.
        self.catalog._refresher = SimpleNamespace(is_alive=lambda: True)

    def models_for(self, provider, **kwargs):
        if provider == "pool":
            return [{"model_id": "writing", "modality": "chat"}]
        if provider not in self.enabled:
            return []
        return [{"model_id": "one" if provider == "a" else "two", "modality": "chat"}]

    def enabled_models(self, modality):
        if modality != "chat":
            return []
        return [(definition, model) for definition in [*self.definitions, pools.definition()]
                for model in self.models_for(definition.id)]

    def story_row(self, modality, definition, model, **kwargs):
        row = {"id": f"{definition.id}::{model['model_id']}", "legacy_ids": self.aliases.get(definition.id, []),
               "chargeable": False, "locked": False}
        if definition.id == "pool":
            row.update({field: None for field in FIELDS})
            row.update({f"{field}_source": "gateway" for field in FIELDS})
            row.update({f"{field}_source_detail": "old pool placeholder" for field in FIELDS})
        else:
            row.update(self.limits[definition.id])
        return row

    def pool_row(self):
        return next(row for row in self.catalog.rows("chat") if row["id"] == "pool::writing")

    def rebuild(self):
        self.catalog.rebuild(force=True)
        return self.pool_row()

    def test_each_field_takes_its_smallest_enabled_member_value(self):
        row = self.pool_row()
        self.assertEqual([32768, 8192, 16000], [row[field] for field in FIELDS])
        for field in FIELDS:
            self.assertEqual("gateway", row[f"{field}_source"])
            self.assertNotIn(f"{field}_source_detail", row)

    def test_one_unknown_field_does_not_erase_the_other_known_limits(self):
        for field in FIELDS:
            with self.subTest(field=field):
                saved = self.limits["b"].pop(field)
                row = self.rebuild()
                self.assertNotIn(field, row)
                self.assertNotIn(f"{field}_source", row)
                self.assertTrue(all(other in row for other in FIELDS if other != field))
                self.limits["b"][field] = saved

    def test_null_and_invalid_values_are_unknown_not_invented_numbers(self):
        for value in (None, True, "32000", -1, 3.5):
            with self.subTest(value=value):
                self.limits["b"]["context_window_tokens"] = value
                row = self.rebuild()
                self.assertNotIn("context_window_tokens", row)
                self.assertNotIn("context_window_tokens_source", row)

    def test_a_stated_zero_is_preserved(self):
        self.limits["b"]["max_output_tokens"] = 0
        self.assertEqual(0, self.rebuild()["max_output_tokens"])

    def test_disabled_member_stops_constraining_the_next_read(self):
        self.enabled.remove("b")
        self.assertEqual(self.limits["a"], {field: self.pool_row()[field] for field in FIELDS})

    def test_reenabled_unknown_member_removes_the_field_on_the_next_read(self):
        self.enabled.remove("b")
        self.assertEqual(49152, self.pool_row()["context_window_tokens"])
        self.limits["b"].pop("context_window_tokens")
        self.enabled.add("b")
        self.assertNotIn("context_window_tokens", self.pool_row())

    def test_editing_membership_recomputes_without_waiting_for_ttl(self):
        self.assertEqual(32768, self.pool_row()["context_window_tokens"])
        pools.update("writing", members=["a::one"])
        self.assertEqual(49152, self.pool_row()["context_window_tokens"])
        pools.update("writing", members=["b::two"])
        self.assertEqual(32768, self.pool_row()["context_window_tokens"])

    def test_no_enabled_members_publishes_no_limits(self):
        self.enabled.clear()
        row = self.pool_row()
        for field in FIELDS:
            self.assertNotIn(field, row)
            self.assertNotIn(f"{field}_source", row)

    def test_legacy_member_alias_uses_its_enabled_published_row(self):
        self.aliases["a"] = ["old-a::one"]
        pools.update("writing", members=["old-a::one"])
        self.assertEqual(49152, self.pool_row()["context_window_tokens"])

    def test_member_limit_updates_are_recomputed_when_catalogue_rebuilds(self):
        self.limits["b"]["max_request_tokens"] = 12000
        self.assertEqual(12000, self.rebuild()["max_request_tokens"])

    def test_members_own_published_limits_are_not_modified(self):
        rows = {row["id"]: row for row in self.catalog.rows("chat")}
        self.assertEqual(self.limits["a"], {field: rows["a::one"][field] for field in FIELDS})
        self.assertEqual(self.limits["b"], {field: rows["b::two"][field] for field in FIELDS})


if __name__ == "__main__":
    unittest.main()
