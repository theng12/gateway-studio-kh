"""Source contracts for the sidebar footer: the name/version pill and the What's New door."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "menubar"
WINDOW = (ROOT / "MainWindow.swift").read_text()


class SidebarFooterTests(unittest.TestCase):
    def pill_region(self):
        start = WINDOW.index("let pill = SidebarPillButton")
        return WINDOW[start:WINDOW.index("for (button, symbol, action, tooltip)", start)]

    def test_the_version_line_comes_from_the_bundle(self):
        region = self.pill_region()
        self.assertIn("GatewayController.shared.appVersion", region)
        # One line (owner standard 2026-11-12): the name and version share the line; the name
        # truncates rather than wrapping to a second row.
        self.assertIn("Gateway Studio KH ", region)
        self.assertNotIn("Gateway Studio KH\\n", region)
        # A literal version here would go stale the first release it missed.
        self.assertIsNone(re.search(r'\d+\.\d+\.\d+', region))

    def test_the_pill_stays_on_one_line_within_its_height(self):
        """A wrapped name used to push the version out of the 34 pt pill; it must never wrap."""
        region = self.pill_region()
        self.assertNotIn("headIndent", region)
        self.assertNotIn("tailIndent", region)
        self.assertIn("lineBreakMode = .byTruncatingTail", region)
        self.assertIn("heightAnchor.constraint(equalToConstant: 34)", region)
        self.assertIn("usesSingleLineMode = true", region)
        self.assertIn("wraps = false", region)

    def test_the_footer_is_one_tidy_column(self):
        """Owner standard: Support row, then pill + update on one centred line, then status."""
        start = WINDOW.index("let statusRow = NSStackView(views:")
        ordered = WINDOW[start:WINDOW.index(")", start)]
        self.assertIn("[support, pillRow, statusLine]", ordered)
        self.assertIn("pillRow.alignment = .centerY", WINDOW)
        self.assertIn("statusRow.spacing = DS.Space.s", WINDOW)
        # Equal left/right insets, pinned to the bottom.
        self.assertIn("statusRow.leadingAnchor.constraint(equalTo: sidebarHost.leadingAnchor, constant: DS.Space.l)", WINDOW)
        self.assertIn("statusRow.trailingAnchor.constraint(equalTo: sidebarHost.trailingAnchor, constant: -DS.Space.l)", WINDOW)
        self.assertIn("statusRow.bottomAnchor.constraint(equalTo: sidebarHost.bottomAnchor, constant: -DS.Space.m)", WINDOW)

    def test_the_pill_opens_whats_new_and_that_section_resolves(self):
        self.assertIn("@objc private func openWhatsNew()", WINDOW)
        self.assertIn('show(section: "what\'s new")', WINDOW)
        order = WINDOW[WINDOW.index("private static let order"):]
        order = order[:order.index("]")]
        self.assertIn('"what\'s new"', order)
        self.assertIn("WhatsNewSection.shared", WINDOW)
        # `sectionIndex(named:)` lowercases the request and matches `order` or a section title.
        self.assertIn("let needle = name.lowercased()", WINDOW)
        self.assertIn("Self.order.firstIndex(of: needle)", WINDOW)


if __name__ == "__main__":
    unittest.main()
