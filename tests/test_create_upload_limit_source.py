#!/usr/bin/env python3
"""The menubar Create page's upload verdict: made with the model's own limit, at the right time.

Two findings of the 2026-10-29e review live here, and both are about *when* a number is read:

* `modelChanged` never recomputed the file verdict, so Generate stayed disabled (or enabled) on
  the limit of whichever model was chosen when the file was picked — a Hub takes 100 MiB, a
  cloud API 25 MiB;
* `readUpload` read `maxUploadBytes` inside its background block, which walks the popup's
  selected row — AppKit state, off the main thread.

**This lane does not run the app** (`menubar/build.sh` is a release step and `test_ui.py` is the
owner's own gate), so the contract is checked against the source: the calls that must exist,
in the functions that must make them, and the absence of the hardcoded ceiling. It is a weaker
check than pressing the button, and it is the strongest one this lane can make — the same shape
`tests/test_gateway_lifecycle.py` uses for the app's other stateful rules.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "menubar" / "CreateSection.swift").read_text(encoding="utf-8")


def function_body(name: str) -> str:
    """One Swift function or computed property, from its signature to its closing brace."""
    match = re.search(rf"(?:func|var) {re.escape(name)}\s*(?:\([^)]*\))?[^{{]*\{{", SOURCE)
    if match is None:
        raise AssertionError(f"{name} not found in CreateSection.swift")
    start = match.end()
    depth = 1
    index = start
    while index < len(SOURCE) and depth:
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
        index += 1
    return SOURCE[start:index]


class TheVerdictFollowsTheModel(unittest.TestCase):
    def test_the_limit_is_read_from_the_published_row(self) -> None:
        body = function_body("maxUploadBytes")
        # A computed property, not a constant: the number belongs to the model.
        self.assertIn("upload_limit_bytes", body)
        self.assertIn("selectedModelRow()", body)

    def test_no_hardcoded_ceiling_is_left(self) -> None:
        self.assertNotIn("25 * 1024 * 1024", SOURCE,
                         "the constant that refused a Hub's own 100 MiB upload")
        self.assertNotIn("Self.maxUploadBytes)", SOURCE)

    def test_a_model_change_re_makes_the_verdict(self) -> None:
        body = function_body("modelChanged")
        self.assertIn("showFile(url)", body,
                      "the chosen file must be judged against the model now selected")

    def test_models_arriving_re_judge_a_restored_draft(self) -> None:
        # A restored draft is judged before any model has loaded; the file it remembers has
        # never been checked against the limit the model now in the popup publishes.
        load = function_body("refreshModels")
        self.assertIn("showFile(url)", load)
        self.assertLess(load.index("self.hasLoadedModels = true"),
                        load.index("showFile(url)"),
                        "the re-judge belongs after the rows are applied")

    def test_generate_judges_again_and_says_this_models_number(self) -> None:
        body = function_body("generate")
        self.assertIn("showFile(url)", body,
                      "a file picked under one model and generated under another")
        self.assertIn("statusLabel.stringValue = problem", body)

    def test_read_upload_captures_the_limit_on_the_main_thread(self) -> None:
        body = function_body("readUpload")
        dispatch = body.index("DispatchQueue.global")
        self.assertLess(body.index("let limit = maxUploadBytes"), dispatch,
                        "AppKit state must be read before the block is dispatched")
        self.assertNotIn("maxUploadBytes", body[dispatch:],
                         "and not again inside the background block")


if __name__ == "__main__":
    unittest.main()
