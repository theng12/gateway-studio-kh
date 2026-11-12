"""Source contracts for the read-only signed update check."""
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "menubar" / "UpdateCheck.swift").read_text()


class UpdateCheckTests(unittest.TestCase):
    def test_enrollment_supplies_feed_and_key(self):
        self.assertIn('Library/KHUpdater/base/config.json', SOURCE)
        self.assertIn('config.feedURL', SOURCE)
        self.assertIn('config.publicKey', SOURCE)
        self.assertNotRegex(SOURCE, r'https?://')
        self.assertIn('Update Studio KH is not set up on this Mac.', SOURCE)

    def test_verifies_before_decoding_payload(self):
        self.assertIn('Curve25519.Signing.PublicKey(rawRepresentation:', SOURCE)
        self.assertIn('key.isValidSignature(signature, for: payload)', SOURCE)
        self.assertIn("The update feed's signature did not verify, so no update answer is shown.", SOURCE)
        self.assertLess(SOURCE.index('key.isValidSignature'), SOURCE.index('decode(Feed.self, from: payload)'))

    def test_finds_gateway_and_compares_versions(self):
        self.assertIn('kh.gateway.menubar', SOURCE)
        self.assertIn('GatewayController.shared.appVersion', SOURCE)
        self.assertIn('(1...6).contains(parts.count)', SOURCE)
        self.assertIn('(1...9).contains(part.utf8.count)', SOURCE)
        self.assertIn('.available(version: version)', SOURCE)
        self.assertIn('.upToDate(version: version)', SOURCE)

    def test_network_and_opening_contract(self):
        self.assertIn('timeoutInterval: 10', SOURCE)
        self.assertIn('.reloadIgnoringLocalCacheData', SOURCE)
        self.assertIn('DispatchQueue.main.async', SOURCE)
        self.assertIn('com.khupdater.app', SOURCE)
        self.assertIn('NSWorkspace.shared', SOURCE)

    def test_no_installer_or_shell(self):
        for forbidden in ('Process(', 'Process()', '/bin/sh', '/bin/bash', 'downloadTask', 'unzip', 'Archive('):
            self.assertNotIn(forbidden, SOURCE)


if __name__ == '__main__':
    unittest.main()
