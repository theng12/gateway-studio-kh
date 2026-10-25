#!/usr/bin/env python3
"""Every setting the owner re-enters after a move has a control (owner, 2026-10-24).

The queue wait limit and the curated model limits lived only in files; these are the saves behind the
two new controls. The pool size, lanes and providers already had theirs.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway import concurrency, limits  # noqa: E402


class QueueWaitLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gw-q-"))
        self.path = self.tmp / "provider-concurrency.local.json"
        self.path.write_text(json.dumps({"global": 60, "queue": {"max_deferrals": 24, "wait_ceiling_seconds": 10800}}))
        p = patch.object(concurrency, "SETTINGS_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)
        concurrency.reset_for_tests()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def saved(self):
        return json.loads(self.path.read_text())

    def test_setting_it_keeps_the_other_queue_numbers_and_the_pool(self):
        concurrency.save_settings({"queue": {"wait_ceiling_seconds": 21600}})
        self.assertEqual(self.saved()["queue"], {"max_deferrals": 24, "wait_ceiling_seconds": 21600})
        self.assertEqual(self.saved()["global"], 60)
        self.assertEqual(concurrency.queue_wait_budget()["wait_ceiling_seconds"], 21600)

    def test_null_returns_to_the_default_and_a_bad_value_changes_nothing(self):
        concurrency.save_settings({"queue": {"wait_ceiling_seconds": None}})
        self.assertEqual(self.saved()["queue"], {"max_deferrals": 24})
        before = self.path.read_text()
        for bad in (30, 8 * 86400, "6h", True):
            with self.assertRaises(concurrency.SettingsRefused):
                concurrency.save_settings({"queue": {"wait_ceiling_seconds": bad}})
        with self.assertRaises(concurrency.SettingsRefused):
            concurrency.save_settings({"queue": {"max_deferrals": 3}})
        self.assertEqual(self.path.read_text(), before)

    def test_an_absent_queue_key_is_left_alone(self):
        concurrency.save_settings({"global": 50})
        self.assertEqual(self.saved()["queue"]["wait_ceiling_seconds"], 10800)


class CuratedModelLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gw-ml-"))
        self.path = self.tmp / "model-limits.local.json"
        self.path.write_text(json.dumps({"_comment": "mine", "models": {"a::x": {"context_window_tokens": 1000}}}))
        for name, value in (("LIMITS_PATH", self.path), ("LEGACY_LIMITS_PATH", self.tmp / "none.json")):
            p = patch.object(limits, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_add_change_and_remove_keep_the_rest_and_the_comment(self):
        limits.set_entry("b::y", {"context_window_tokens": 200000, "max_output_tokens": 8192, "max_prompt_characters": None})
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["_comment"], "mine")
        self.assertEqual(saved["models"]["b::y"], {"context_window_tokens": 200000, "max_output_tokens": 8192})
        self.assertEqual(saved["models"]["a::x"], {"context_window_tokens": 1000})
        self.assertEqual(limits.entries()["b::y"]["max_output_tokens"], 8192)
        limits.set_entry("a::x", None)
        self.assertNotIn("a::x", json.loads(self.path.read_text())["models"])

    def test_refusals_change_nothing(self):
        before = self.path.read_text()
        for key, fields in (("", {"context_window_tokens": 1}), ("a::x", {"temperature": 1}),
                            ("a::x", {"max_output_tokens": -5}), ("a::x", {"max_output_tokens": 1.5}),
                            ("a::x", {"max_output_tokens": True})):
            with self.assertRaises(ValueError):
                limits.set_entry(key, fields)
        self.assertEqual(self.path.read_text(), before)

    def test_an_unreadable_file_is_refused_not_replaced(self):
        self.path.write_text("{ broken")
        with self.assertRaises(ValueError):
            limits.set_entry("b::y", {"max_output_tokens": 10})
        self.assertEqual(self.path.read_text(), "{ broken")


class ControlsExist(unittest.TestCase):
    def test_the_console_has_both_controls(self):
        page = (ROOT / "gateway" / "admin_ui.html").read_text()
        self.assertIn('id="conc-queue-hours"', page)
        self.assertIn("body.queue = { wait_ceiling_seconds:", page)
        self.assertIn('id="model-limits"', page)
        self.assertIn("'/settings/model-limits'", page)


if __name__ == "__main__":
    unittest.main()
