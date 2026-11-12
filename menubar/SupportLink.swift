import AppKit
import Foundation

enum SupportLink {
    static let donationURL = URL(string: "https://nowpayments.io/donation/studiokh")!

    static func openDonationPage() -> Bool {
        // The app must not become a launcher for arbitrary web addresses.
        guard donationURL.scheme == "https", donationURL.host == "nowpayments.io",
              donationURL.path == "/donation/studiokh" else { return false }
        return NSWorkspace.shared.open(donationURL)
    }
}
