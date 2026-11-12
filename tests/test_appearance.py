"""Source contracts for the app-wide appearance setting."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPEARANCE = (ROOT / "menubar" / "Appearance.swift").read_text()
MAIN = (ROOT / "menubar" / "main.swift").read_text()
SETTINGS = (ROOT / "menubar" / "SettingsSection.swift").read_text()


class AppearanceTests(unittest.TestCase):
    def test_modes_are_exactly_system_light_and_dark(self):
        """Owner's call: three choices, no fourth "Default" palette."""
        self.assertRegex(APPEARANCE, r"case system, light, dark\b")
        self.assertIn("enum AppearanceMode: String, CaseIterable", APPEARANCE)
        self.assertNotIn("appDefault", APPEARANCE)
        self.assertNotIn('"default"', APPEARANCE)
        # No user-visible "Default" wording survives in the picker or its detail line.
        self.assertNotIn("Default", APPEARANCE)
        self.assertNotIn("Default", SETTINGS[SETTINGS.index('section("Appearance"'):][:400])

    def test_a_stored_default_reads_as_system(self):
        """0.2.245's first cut saved "default"; those installs must land on System, not fall through."""
        current = APPEARANCE[APPEARANCE.index("static var current"):]
        self.assertIn("return .system", current)
        self.assertIn("AppearanceMode(rawValue: value) ?? .system", current)

    def test_appearance_mapping(self):
        for case, value in (("system", "nil"), ("light", "NSAppearance(named: .aqua)"),
                            ("dark", "NSAppearance(named: .darkAqua)")):
            self.assertIn(f"case .{case}: NSApp.appearance = {value}", APPEARANCE)

    def test_config_and_launch(self):
        self.assertRegex(MAIN, r"var appearance: String\?")
        launch = MAIN[MAIN.index("func applicationDidFinishLaunching"):]
        self.assertLess(launch.index("AppearanceMode.current.apply()"), launch.index("MainWindow.shared.show"))
        self.assertIn("ConfigStore.shared.config.appearance", APPEARANCE)
        self.assertIn("?? .system", APPEARANCE)
        self.assertIn("ConfigStore.shared.update", APPEARANCE)

    def test_settings_uses_picker_and_persists_selection(self):
        self.assertIn('section("Appearance"', SETTINGS)
        self.assertIn("NSSegmentedControl", SETTINGS)
        self.assertIn("AppearanceMode.set(mode)", SETTINGS)
        self.assertIn("appearanceDetail.stringValue = mode.detail", SETTINGS)

    def test_no_appearance_user_defaults(self):
        self.assertNotIn("UserDefaults", APPEARANCE)
        for line in (MAIN + SETTINGS).splitlines():
            self.assertFalse("UserDefaults" in line and re.search("appearance", line, re.I))


if __name__ == "__main__":
    unittest.main()
