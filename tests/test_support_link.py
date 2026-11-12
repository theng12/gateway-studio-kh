"""Source contracts for the in-app support page and the link behind it."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'menubar'
SOURCE = (ROOT / 'SupportLink.swift').read_text()
SECTION = (ROOT / 'SupportSection.swift').read_text()
WINDOW = (ROOT / 'MainWindow.swift').read_text()


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

    def test_the_footer_opens_an_in_app_page_not_the_browser(self):
        """Support this app shows a page first; the browser is opened only from that page's button."""
        self.assertIn('@objc private func openSupport() { show(section: "support") }', WINDOW)
        self.assertNotIn('@objc private func openSupport() { _ = SupportLink.openDonationPage() }', WINDOW)
        self.assertIn('"support",', WINDOW)

    def test_the_page_explains_what_who_and_how_and_links_out(self):
        for heading in ('What your support pays for', 'Who receives it', 'How it works'):
            self.assertIn(heading, SECTION)
        self.assertIn('PageKit.button("Open the donation page"', SECTION)
        self.assertIn('SupportLink.openDonationPage()', SECTION)

    def test_the_page_is_registered_and_reachable_by_name(self):
        """`order` and `sections` are index-aligned; the support page must be in both."""
        order = WINDOW[WINDOW.index('private static let order'):]
        order = order[:order.index(']')]
        self.assertEqual(order.count('"support"'), 1)
        self.assertIn('SupportSection.shared', WINDOW)
        self.assertIn('show(section: "support")', WINDOW)


if __name__ == '__main__':
    unittest.main()
