#!/usr/bin/env python3
"""Adding a provider from the console, instead of editing a file by hand.

Run with::

    .venv/bin/python tests/test_custom_providers.py

**The file was never the missing part.** `providers.local.json` has been read at import since
custom providers were supported, and the providers page already *reports* an entry it could not
load — but nothing could write one, so adding a provider meant finding a file in Application Support
and getting its schema right by hand. The owner asked for the other half: *"i think we can allow an
add custom option. that would make future providers really easy until we can update native
support."*

Two properties this pins, because they are the ones a form like this gets wrong:

* **A provider that will not load is refused now, with the reason.** The loader skips an entry whose
  kind it cannot run, or whose id collides, and says nothing to whoever typed it — so the provider
  simply never appears. Every one of those refusals is checked here, so the operator hears it while
  they are still looking at the form.
* **A restart is stated, never implied.** Providers are read when the gateway starts. A form that
  appeared to add a working provider and left it invisible until the next restart is a control that
  does nothing.

The gateway modules are imported only after a temporary runtime environment is installed, so this
file cannot read or write the owner's data, credentials or providers file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class CustomProviderTests(unittest.TestCase):
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
        from gateway import admin, config
        cls.admin = admin
        # What the gateway actually reads: frozen at first import, which under `discover` is
        # another test file's temp dir, not this class's GATEWAY_DATA.
        cls.path = Path(config.CUSTOM_PROVIDERS_PATH)

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.path.unlink(missing_ok=True)

    def _entries(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text())["providers"]

    # ── what it writes ────────────────────────────────────────────────
    def test_a_provider_is_written_in_the_shape_the_loader_reads(self):
        answer = self.admin.add_custom_provider({
            "display_name": "My Router", "id": "my-router",
            "base_url": "https://example.test/v1", "kind": "openai_compatible",
        })
        self.assertTrue(answer["restart_required"],
                        "the answer must say a restart is needed, not leave it to be discovered")
        entry = self._entries()[0]
        self.assertEqual("my-router", entry["id"])
        self.assertEqual("My Router", entry["display_name"])
        self.assertEqual("https://example.test/v1", entry["base_url"])
        self.assertEqual("openai_compatible", entry["kind"])
        self.assertEqual(["chat"], entry["modalities"])
        # The loader defaults this to `openai_models`; writing it explicitly means the file says
        # what the gateway will do rather than relying on a default in another file.
        self.assertEqual("openai_models", entry["discovery"])

    def test_the_key_field_gets_a_usable_environment_name_by_default(self):
        self.admin.add_custom_provider({
            "display_name": "My Router", "id": "my-router",
            "base_url": "https://example.test/v1", "kind": "openai_compatible"})
        field = self._entries()[0]["credential_fields"][0]
        self.assertEqual("api_key", field["key"])
        self.assertEqual("CUSTOM_MY_ROUTER_KEY", field["env_var"],
                         "a generated name must be one the credentials store can actually read")

    def test_a_provider_that_needs_no_key_says_so(self):
        # `omni_route`-shaped lanes exist: a local router may need no credential at all. An empty
        # list is how the loader is told, and it is different from a field that is never filled.
        self.admin.add_custom_provider({
            "display_name": "Local", "id": "local-router",
            "base_url": "http://127.0.0.1:9999/v1", "kind": "openai_compatible", "needs_key": False})
        self.assertEqual([], self._entries()[0]["credential_fields"])

    def test_adding_a_second_one_keeps_the_first(self):
        for pid in ("one-router", "two-router"):
            self.admin.add_custom_provider({
                "display_name": pid, "id": pid,
                "base_url": "https://example.test/v1", "kind": "openai_compatible"})
        self.assertEqual(["one-router", "two-router"], [e["id"] for e in self._entries()])

    def test_removing_takes_only_that_one(self):
        for pid in ("one-router", "two-router"):
            self.admin.add_custom_provider({
                "display_name": pid, "id": pid,
                "base_url": "https://example.test/v1", "kind": "openai_compatible"})
        self.admin.remove_custom_provider("one-router")
        self.assertEqual(["two-router"], [e["id"] for e in self._entries()])

    def test_removing_something_absent_says_so(self):
        # The file is the authority, not this process: another editor may have removed it already,
        # and answering "removed" for something that was never there hides that.
        with self.assertRaises(Exception) as caught:
            self.admin.remove_custom_provider("never-existed")
        self.assertIn("not in", str(caught.exception))

    # ── what it refuses, and why the refusal matters ──────────────────
    def test_an_unknown_kind_is_refused_with_the_ones_that_work(self):
        with self.assertRaises(Exception) as caught:
            self.admin.add_custom_provider({
                "display_name": "Bad", "id": "bad-router",
                "base_url": "https://example.test/v1", "kind": "not_a_real_kind"})
        message = str(caught.exception)
        self.assertIn("not_a_real_kind", message)
        self.assertIn("openai_compatible", message,
                      "the refusal must name a kind that would work, not only the one that will not")

    def test_a_built_in_id_is_refused(self):
        # The loader would skip it anyway, silently — which is exactly why it is refused here.
        with self.assertRaises(Exception) as caught:
            self.admin.add_custom_provider({
                "display_name": "Fake", "id": "openrouter-main",
                "base_url": "https://example.test/v1", "kind": "openai_compatible"})
        self.assertIn("built-in", str(caught.exception))

    def test_the_fleet_namespace_is_refused(self):
        with self.assertRaises(Exception) as caught:
            self.admin.add_custom_provider({
                "display_name": "Fake", "id": "studio-hub",
                "base_url": "https://example.test/v1", "kind": "openai_compatible"})
        self.assertIn("reserved", str(caught.exception))

    def test_a_location_prefix_with_a_cloud_kind_is_refused(self):
        # `fleet` decides "is this a location" from the id prefix, so a cloud provider wearing it
        # would be given a site's treatment while having no controller behind it.
        with self.assertRaises(Exception) as caught:
            self.admin.add_custom_provider({
                "display_name": "Fake", "id": "studio-hub-something",
                "base_url": "https://example.test/v1", "kind": "openai_compatible"})
        self.assertIn("studio_hub", str(caught.exception))

    def test_a_base_url_that_is_not_a_url_is_refused(self):
        with self.assertRaises(Exception) as caught:
            self.admin.add_custom_provider({
                "display_name": "Bad", "id": "bad-router",
                "base_url": "example.test/v1", "kind": "openai_compatible"})
        self.assertIn("http", str(caught.exception))

    def test_an_id_that_is_not_an_id_is_refused(self):
        for bad in ("", "UPPER CASE", "a", "has space", "../escape"):
            with self.subTest(provider_id=bad):
                with self.assertRaises(Exception):
                    self.admin.add_custom_provider({
                        "display_name": "Bad", "id": bad,
                        "base_url": "https://example.test/v1", "kind": "openai_compatible"})

    def test_the_same_id_twice_is_refused(self):
        body = {"display_name": "One", "id": "one-router",
                "base_url": "https://example.test/v1", "kind": "openai_compatible"}
        self.admin.add_custom_provider(dict(body))
        with self.assertRaises(Exception) as caught:
            self.admin.add_custom_provider(dict(body))
        self.assertIn("already", str(caught.exception))

    def test_a_refusal_writes_nothing(self):
        with self.assertRaises(Exception):
            self.admin.add_custom_provider({
                "display_name": "Bad", "id": "bad-router",
                "base_url": "not-a-url", "kind": "openai_compatible"})
        self.assertEqual([], self._entries(), "a refused provider was written anyway")

    # ── what the console reads ────────────────────────────────────────
    def test_the_list_reports_what_the_loader_could_not_use(self):
        """An entry that was skipped has no provider to list, so this is the only place it shows."""
        self.path.write_text(json.dumps({"providers": [
            {"id": "good-router", "display_name": "Good", "kind": "openai_compatible",
             "base_url": "https://example.test/v1"},
            {"id": "bad-router", "display_name": "Bad", "kind": "not_a_kind",
             "base_url": "https://example.test/v1"},
        ]}))
        from gateway.providers import definitions
        with patch.object(definitions, "skipped_custom_providers",
                          lambda: ({"id": "bad-router", "reason": "unknown kind 'not_a_kind' — nothing can run it"},)):
            listed = self.admin.list_custom_providers()
        by_id = {row["id"]: row for row in listed["providers"]}
        self.assertIsNone(by_id["good-router"]["skipped_reason"])
        self.assertIn("not_a_kind", by_id["bad-router"]["skipped_reason"],
                      "an entry that will not load must say so on the page that lists it")

    def test_the_kinds_offered_are_the_ones_that_can_actually_run(self):
        from gateway.providers.definitions import IMPLEMENTED_KINDS
        listed = self.admin.list_custom_providers()
        self.assertEqual(sorted(IMPLEMENTED_KINDS), listed["kinds"],
                         "the form must offer the implemented kinds themselves, never a copy")

    def test_a_corrupt_file_reads_as_empty_rather_than_raising(self):
        # The page must load even when the file it manages is unreadable, or the operator cannot
        # use the form to see what is wrong with it.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ this is not json")
        self.assertEqual([], self.admin.list_custom_providers()["providers"])


class TheFormSaysARestartIsNeededTest(unittest.TestCase):
    """The one thing an add-a-provider form must not get wrong."""

    def setUp(self):
        self.page = (ROOT / "gateway" / "admin_ui.html").read_text()

    def test_the_console_says_it_in_words(self):
        self.assertIn("Restart the gateway to use it", self.page,
                      "the form adds a provider that does nothing until a restart, and must say so")

    def test_the_route_reports_it_too(self):
        source = (ROOT / "gateway" / "admin.py").read_text()
        self.assertIn('"restart_required": True', source)

    def test_the_button_and_the_panel_are_on_the_providers_page(self):
        providers = self.page[self.page.index('<section id="view-providers"'):]
        providers = providers[:providers.index('<section id="view-create"')]
        self.assertIn("toggleCustomProviderForm()", providers,
                      "the add-a-provider control is not on the providers page")
        self.assertIn('id="custom-provider-panel"', providers)


if __name__ == "__main__":
    unittest.main()
