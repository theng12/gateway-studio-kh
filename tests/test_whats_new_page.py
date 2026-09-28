#!/usr/bin/env python3
"""What's New is a **page above Settings**, and it renders as one.

The owner asked for the Studio Hub shape: the notes are a row in the sidebar directly above
Settings, not a modal opened from a link at the bottom of the sidebar. Studio Hub and GatewayKH are
read side by side, so the same thing belongs in the same place in both.

Two checks, and they are deliberately different in kind:

* **The renderer is executed.** `WhatsNewPage.makePage` is compiled and run offscreen with sample
  markdown, and the resulting view is walked: it must be a scrolling page, carry the title with the
  version beside it, and turn `##`, `-` and `**bold**` into real headings, bullets and traits
  rather than showing the markers. That is the half a source check cannot see.
* **The sidebar wiring is read from the source.** The order lives in a `private static let` inside
  `MainWindow.swift`, which cannot be compiled without every section in the app; asserting on it as
  text is weaker than executing it, and it is the honest option here rather than pretending
  otherwise. The check is specific — What's New immediately above Settings, both under the *App*
  heading, and the section list long enough to reach index 14 — so it fails on a reordering rather
  than on any edit at all.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import AppKit

// The page is built from the real renderer with sample notes covering every block kind it
// supports, then walked. `NSApplication` has to exist before any view is instantiated.
let app = NSApplication.shared

let notes = """
## 0.2.66 — What's New is a page now

The notes used to open in a window of their own. They are a row in the sidebar now, directly
above Settings, which is where Studio Hub keeps them.

- **A bullet with a bold lead.** And the rest of its sentence.
- A second bullet, plain.
"""

let page = WhatsNewPage.makePage(notes: notes, version: "9.9.9", build: "abc1234-202601010000")

func descendants(_ view: NSView) -> [NSView] {
    var out: [NSView] = [view]
    for child in view.subviews { out.append(contentsOf: descendants(child)) }
    return out
}

let all = descendants(page)
let texts = all.compactMap { ($0 as? NSTextField)?.stringValue }
let joined = texts.joined(separator: "\n")

// 1. It is a page that scrolls: a tall notes file must never be able to grow the window.
assert(!all.compactMap { $0 as? NSScrollView }.isEmpty, "the notes page has no scroller")

// 2. The header is Studio Hub's: the title, the version beside it, the build stamp.
assert(texts.contains("What's New"), "the page title is missing: \(texts.prefix(6))")
assert(texts.contains("9.9.9"), "the version is missing from the header")
assert(joined.contains("abc1234-202601010000"), "the build stamp is missing")

// 3. **The markdown is rendered, not printed.** The defect this page was rebuilt for was literal
//    `##` and `**` in the reader's face, so the markers must be gone and their meaning present.
assert(!joined.contains("##"), "a heading marker reached the page: \(joined.prefix(120))")
assert(!joined.contains("**"), "a bold marker reached the page: \(joined.prefix(120))")
assert(joined.contains("What's New is a page now"),
       "the heading text is missing: \(joined.prefix(200))")
assert(joined.contains("A bullet with a bold lead."), "the bullet text is missing")

// The bold lead is a real trait on the heading and the bullet, not decoration on the whole line.
let bolded = all.compactMap { $0 as? NSTextField }.contains { field in
    let value = field.attributedStringValue
    var found = false
    value.enumerateAttribute(.font, in: NSRange(location: 0, length: value.length)) { font, range, _ in
        guard let font = font as? NSFont else { return }
        if NSFontManager.shared.traits(of: font).contains(.boldFontMask),
           value.attributedSubstring(from: range).string.contains("A bullet with a bold lead") {
            found = true
        }
    }
    return found
}
assert(bolded, "the bold lead did not render as bold")

print("PASS: What's New renders as a scrolling page with real headings and bullets")
'''


class TheNotesPageRendersAsAPageTest(unittest.TestCase):
    def test_the_page_scrolls_and_the_markdown_becomes_type(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-whats-new-") as directory:
            source = Path(directory) / "main.swift"
            binary = Path(directory) / "page"
            source.write_text(HARNESS)
            compiled = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit",
                 "menubar/PageKit.swift", "menubar/WhatsNewPage.swift",
                 str(source), "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            checked = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("PASS:", checked.stdout)


class TheSidebarPutsItAboveSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (ROOT / "menubar" / "MainWindow.swift").read_text(encoding="utf-8")

    def test_the_section_is_in_the_vocabulary(self) -> None:
        order = re.search(r"private static let order = \[(.*?)\]", self.source, re.S)
        self.assertIsNotNone(order, "the sidebar's section vocabulary is gone")
        names = re.findall(r'"([^"]+)"', order.group(1))
        self.assertIn("what's new", names)
        self.assertEqual(names.index("what's new"), 14,
                         "the section's index must match the one `layout` and `sections` use")

    def test_it_sits_directly_above_settings_under_the_app_heading(self) -> None:
        # Line-based rather than one regex: the layout carries comments of its own, and a pattern
        # that has to step over them is a pattern that will quietly match the wrong array.
        lines = self.source.splitlines()
        start = next(i for i, line in enumerate(lines)
                     if line.strip().startswith("private static let layout"))
        rows = []
        for line in lines[start + 1:]:
            stripped = line.strip()
            if stripped.startswith("]"):
                break
            if stripped.startswith(".header(") or stripped.startswith(".section("):
                rows.append(stripped.rstrip(","))
        self.assertTrue(rows, "the sidebar layout is gone")
        app = rows.index('.header("App")')
        # The two rows immediately after the App heading, in order.
        self.assertTrue(rows[app + 1].startswith(".section(14)"),
                        f"What's New is not the first row under App: {rows[app + 1]}")
        self.assertTrue(rows[app + 2].startswith(".section(12)"),
                        f"Settings is not directly below What's New: {rows[app + 2]}")

    def test_the_section_instance_is_wired_in(self) -> None:
        self.assertIn("WhatsNewSection.shared,", self.source,
                      "the section is declared but never handed to the window")

    def test_the_duplicate_link_is_gone(self) -> None:
        # The row is visible in the sidebar now, so the bottom-of-sidebar link was the same
        # destination twice in one column. Three doors remain (the row, the View menu, Overview).
        self.assertNotIn("whatsNewButton", self.source)

    def test_every_door_lands_on_the_page(self) -> None:
        window = (ROOT / "menubar" / "WhatsNewWindow.swift").read_text(encoding="utf-8")
        self.assertIn('MainWindow.shared.show(section: "what\'s new")', window,
                      "the app menu and Overview still open a window of their own")


if __name__ == "__main__":
    unittest.main()
