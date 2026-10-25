"""The masked-secret control (menubar/SecretField.swift) and where the app uses it."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MENUBAR = ROOT / "menubar"


class SecretFieldNative(unittest.TestCase):
    def test_masked_by_default_and_never_readable_while_masked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-secret-field-") as temp:
            binary = Path(temp) / "secret-field-check"
            build = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit",
                 "menubar/PageKit.swift", "menubar/SecretField.swift", "tests/test_secret_field.swift",
                 "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr or build.stdout)
            run = subprocess.run([str(binary)], cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("PASS:", run.stdout)


class SecretsUseTheControl(unittest.TestCase):
    """Every secret the native app shows or takes goes through SecretField."""

    def test_no_bare_secure_field_or_plain_key_field(self) -> None:
        for path in MENUBAR.glob("*.swift"):
            if path.name == "SecretField.swift":
                continue
            source = path.read_text()
            self.assertNotIn("NSSecureTextField(", source,
                             f"{path.name}: use SecretField, which adds the eye, not a bare secure field")
        # One component: the Telegram token field and the rest share SecretField.swift, not a second class.
        self.assertNotIn("class SecretField", (MENUBAR / "PageKit.swift").read_text())
        self.assertIn("SecretField(", (MENUBAR / "TelegramSettings.swift").read_text())
        settings = (MENUBAR / "SettingsSection.swift").read_text()
        self.assertIn("keyField = SecretField(EnvFile.apiKey()", settings,
                      "the gateway API key is shown masked")
        self.assertNotIn("NSTextField(string: EnvFile.apiKey())", settings)

    def test_the_console_secret_inputs_have_an_eye(self) -> None:
        html = (ROOT / "gateway" / "admin_ui.html").read_text()
        inputs = re.findall(r"<input[^>]*type=\"(?:password|\$\{f\.secret \? 'password')[^>]*>", html)
        self.assertGreaterEqual(len(inputs), 2)
        # **One eye per secret input, not a fixed number.** The count was pinned at 2 and the SFX
        # tab's token field made it 3 — a number that has to be edited whenever a secret is added
        # reports the addition, not a missing eye. What the rule actually says is that no password
        # input is left without its control: a mask you cannot look through is a field you cannot
        # check before saving.
        self.assertEqual(html.count('onclick="toggleSecret(this)"'), len(inputs),
                         "every secret input needs its eye")
        self.assertIn("function toggleSecret(button)", html)


if __name__ == "__main__":
    unittest.main()
