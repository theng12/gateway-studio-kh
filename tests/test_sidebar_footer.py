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
        self.assertIn("Gateway Studio KH\\n", region)
        # A literal version here would go stale the first release it missed.
        self.assertIsNone(re.search(r'\d+\.\d+\.\d+', region))

    def test_the_pill_can_show_both_lines_within_its_height(self):
        """The measured cause of the invisible version: indents wrapped the name to a third line."""
        region = self.pill_region()
        self.assertNotIn("headIndent", region)
        self.assertNotIn("tailIndent", region)
        self.assertIn("lineBreakMode = .byTruncatingTail", region)
        self.assertIn("heightAnchor.constraint(equalToConstant: 34)", region)
        # 2 x 11 pt system line height (~12.96) + 1 pt line spacing fits in 34.
        self.assertIn("lineSpacing = 1", region)
        self.assertIn("usesSingleLineMode = false", region)

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
