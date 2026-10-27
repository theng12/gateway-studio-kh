#!/usr/bin/env python3
"""The native Hub release publish sheet: checkboxes, defaults, and force-full help text.

Tests `PublishReleaseSheetForm` in `menubar/FleetSection.swift` headless (no window, no network).
Skipped where there is no `swiftc`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import AppKit

enum TextRole { case body, secondary, caption, rowTitle }
enum DS { enum Space { static let xs: CGFloat = 4; static let s: CGFloat = 8; static let m: CGFloat = 12 } }
enum PageKit {
    static func label(_ text: String, _ role: TextRole = .body, colour: NSColor = .labelColor,
                      wrapping: Bool = false) -> NSTextField {
        wrapping ? NSTextField(wrappingLabelWithString: text) : NSTextField(labelWithString: text)
    }
    static func help(_ text: String) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        field.textColor = .secondaryLabelColor
        return field
    }
    static func formRow(_ label: String, _ control: NSView) -> NSStackView {
        let lbl = NSTextField(labelWithString: label)
        lbl.alignment = .right
        lbl.widthAnchor.constraint(equalToConstant: 70).isActive = true
        let row = NSStackView(views: [lbl, control])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = DS.Space.s
        return row
    }
}

// Extracted / included PublishReleaseSheetForm from FleetSection.swift
'''

HARNESS_MAIN = r'''
@main struct Check {
    static func main() {
        let sheet = FleetSection.PublishReleaseSheetForm(
            version: "0.6.204",
            build: "8c20499",
            filename: "StudioHub-0.6.204-8c20499.dmg"
        )
        assert(sheet.versionField.stringValue == "0.6.204", "versionField pre-filled")
        assert(sheet.buildField.stringValue == "8c20499", "buildField pre-filled")

        // Emergency allAtOnce checkbox
        assert(sheet.allAtOnce.title == "All at once (emergency): every controller now",
               "allAtOnce title: \(sheet.allAtOnce.title)")
        assert(sheet.allAtOnce.state == .off, "allAtOnce off by default")

        // Send whole app checkbox
        assert(sheet.forceFull.title == "Send the whole app (not just the changed files)",
               "forceFull title: \(sheet.forceFull.title)")
        assert(sheet.forceFull.state == .off, "forceFull off by default")

        // Help text
        assert(sheet.forceFullHint.stringValue == "Use when a Mac is too far behind for a files-only update.",
               "forceFullHint wording: \(sheet.forceFullHint.stringValue)")

        // Toggle checkbox
        sheet.forceFull.state = .on
        assert(sheet.forceFull.state == .on, "forceFull can be toggled on")

        print("PASS: native publish sheet form")
    }
}
'''


@unittest.skipUnless(shutil.which("swiftc") and sys.platform == "darwin", "needs swiftc on macOS")
class NativePublishSheetTests(unittest.TestCase):
    def test_publish_sheet_force_full_checkbox_and_wording(self) -> None:
        fleet_section = (ROOT / "menubar/FleetSection.swift").read_text()
        self.assertIn("Send the whole app (not just the changed files)", fleet_section)
        self.assertIn("Use when a Mac is too far behind for a files-only update.", fleet_section)
        self.assertIn('body["force_full"] = true', fleet_section)

        # Extract PublishReleaseSheetForm
        marker = "struct PublishReleaseSheetForm {"
        self.assertIn(marker, fleet_section)
        start = fleet_section.index(marker)
        # Find closing brace of the struct
        depth = 0
        end = start
        for i, char in enumerate(fleet_section[start:]):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = start + i + 1
                    break
        struct_code = fleet_section[start:end]

        with tempfile.TemporaryDirectory(prefix="gateway-publish-sheet-") as directory:
            source = Path(directory) / "check.swift"
            binary = Path(directory) / "check"
            source_content = (
                HARNESS
                + "enum FleetSection {\n"
                + struct_code + "\n"
                + "}\n"
                + HARNESS_MAIN
            )
            source.write_text(source_content)
            built = subprocess.run(
                ["swiftc", "-swift-version", "5", "-parse-as-library", "-framework", "AppKit",
                 str(source), "-o", str(binary)],
                capture_output=True, text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            checked = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("PASS: native publish sheet form", checked.stdout)


if __name__ == "__main__":
    unittest.main()
