"""Source contracts for the app-wide appearance setting."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPEARANCE = (ROOT / "menubar" / "Appearance.swift").read_text()
MAIN = (ROOT / "menubar" / "main.swift").read_text()
SETTINGS = (ROOT / "menubar" / "SettingsSection.swift").read_text()


class AppearanceTests(unittest.TestCase):
    def test_modes_have_the_four_raw_values(self):
        self.assertRegex(APPEARANCE, r"case system, light, dark\b")
        self.assertIn('case appDefault = "default"', APPEARANCE)
        self.assertIn("enum AppearanceMode: String, CaseIterable", APPEARANCE)

    def test_appearance_mapping(self):
        for case, value in (("system", "nil"), ("light", "NSAppearance(named: .aqua)"),
                            ("dark", "NSAppearance(named: .darkAqua)"), ("appDefault", "nil")):
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
