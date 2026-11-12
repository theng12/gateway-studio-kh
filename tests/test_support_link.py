"""Source contracts for the allow-listed support link."""
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / 'menubar' / 'SupportLink.swift').read_text()


class SupportLinkTests(unittest.TestCase):
    def test_exact_url_and_allow_list(self):
        self.assertIn('https://nowpayments.io/donation/studiokh', SOURCE)
        self.assertIn('donationURL.scheme == "https"', SOURCE)
        self.assertIn('donationURL.host == "nowpayments.io"', SOURCE)
        self.assertIn('donationURL.path == "/donation/studiokh"', SOURCE)
        self.assertIn('else { return false }', SOURCE)
        self.assertIn('NSWorkspace.shared.open(donationURL)', SOURCE)

    def test_no_installer_or_shell(self):
        for forbidden in ('Process(', 'Process()', '/bin/sh', '/bin/bash', 'downloadTask', 'unzip', 'Archive('):
            self.assertNotIn(forbidden, SOURCE)


if __name__ == '__main__':
    unittest.main()
