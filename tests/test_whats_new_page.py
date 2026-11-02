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

import datetime
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
assert(joined.contains("9.9.9"), "the version is missing from the header")
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


class TheNotesSayWhichReleaseTheyCameFromTest(unittest.TestCase):
    """Every release's notes carry its version and date, and the next one cannot forget.

    The owner read the two What's New pages side by side: *"one has versions and dates. the other
    doesn't. the gateway doesn't."* Studio Hub's `release-notes.md` groups each entry under
    `## <version> — <date>`; this file listed 46 features as `##` headings of one size with no
    version and no date anywhere, so a reader could see what the gateway does but not when any of it
    arrived — and a fix he had been waiting for looked exactly like one that had always been there.

    **The first thing the regrouping exposed was a gap, not a formatting problem: `0.2.67`'s four
    user-facing changes had never been written up here at all.** Nothing could notice, because a flat
    list has no place where a missing release is absent from.

    The check is on the **newest** section, deliberately. The rows below it are history written
    before this shape existed and are left alone; what has to hold is the shape from here on, which
    is what he asked for — *"make sure in the future. it's also added."*
    """

    NOTES = ROOT / "CHANGELOG.md"
    VERSION_FILE = ROOT / "VERSION"
    RELEASE = re.compile(r"^## (\d+\.\d+\.\d+) — (\d{4}-\d{2}-\d{2})$")

    def _sections(self) -> list[tuple[str, str]]:
        text = self.NOTES.read_text(encoding="utf-8")
        return [m.groups() for m in
                (self.RELEASE.match(line) for line in text.splitlines()) if m]

    def test_the_newest_section_is_the_release_you_are_building(self) -> None:
        sections = self._sections()
        self.assertTrue(sections, "CHANGELOG.md has no `<version> — <date>` release heading at all")
        newest, _ = sections[0]
        self.assertEqual(
            newest, self.VERSION_FILE.read_text(encoding="utf-8").strip(),
            "the top of CHANGELOG.md is not this release: add a `## <VERSION> — <date>` heading for "
            "the notes you are shipping, so the page says when the change arrived",
        )

    def test_that_section_is_the_first_heading_on_the_page(self) -> None:
        # A release heading that is not first would sit under a feature title, and the newest release
        # would read as part of whatever came before it.
        body = self.NOTES.read_text(encoding="utf-8").splitlines()
        first = next(line for line in body if line.startswith("## "))
        self.assertTrue(self.RELEASE.match(first),
                        f"the first `##` heading is a feature title, not a release: {first!r}")

    def test_no_feature_title_is_a_release_heading(self) -> None:
        # A feature title at `##` renders at a release heading's weight (19 pt against 15 pt for
        # `###`), so a stray one reads as a release that never happened.
        for line in self.NOTES.read_text(encoding="utf-8").splitlines():
            if line.startswith("## ") and not self.RELEASE.match(line):
                self.fail(f"feature title at `##` will render as a release heading: {line!r}")

    def test_the_date_is_a_real_iso_date(self) -> None:
        for version, date in self._sections():
            try:
                datetime.date.fromisoformat(date)
            except ValueError:
                self.fail(f"release {version} carries {date!r}, which is not a YYYY-MM-DD date")

    def test_the_notes_use_only_the_block_syntax_the_renderer_implements(self) -> None:
        """`> ` is not a blockquote here — it is a literal `>` printed at the reader.

        The renderer understands exactly four block forms: `#` headings, `- ` bullets, `---` rules,
        and paragraphs. **Bold, italic and `code` work inline; blockquotes do not.** Measured by
        rendering, not by reading: a leading `> ` keeps its character and bold inside it is
        swallowed, so such a note reaches the page as `> **Each release now carries…` — which is
        what the first draft of the newest entry did here, and what looking at the page caught.

        The lesson is one this repo keeps relearning in another form: **this file is prose for a
        renderer, so prose that has not been rendered is not known to work.** The markers are cheap
        to assert, and an unsupported one is invisible until somebody looks.
        """
        # Markdown block constructs the renderer does NOT implement. Each would be printed at the
        # reader as its own marker rather than as the thing it looks like in a text editor.
        unsupported = (
            (">", "blockquote"),
            ("* ", "bullet written with `*` (the renderer wants `- `)"),
            ("+ ", "bullet written with `+` (the renderer wants `- `)"),
            ("```", "fenced code block"),
            ("|", "table row"),
        )
        text = self.NOTES.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            for marker, name in unsupported:
                if stripped.startswith(marker):
                    self.fail(f"CHANGELOG.md:{number} is a {name} (`{marker}`), which the renderer "
                              f"does not implement: {stripped[:60]!r}")
            # An ordered list: `1. `, `2. ` — only bullets are implemented.
            if re.match(r"^\d+\.\s", stripped):
                self.fail(f"CHANGELOG.md:{number} is an ordered list, which the renderer does not "
                          f"implement: {stripped[:60]!r}")


if __name__ == "__main__":
    unittest.main()
