import AppKit

/// The "Browser password" card on the Settings page: the password a phone browser types at
/// `/admin` over Tailscale. The gateway keeps only a salted hash and accepts a change only
/// from this Mac with the gateway key (`/admin/api/settings/browser-password`), so a
/// signed-in browser can never change it. Setting or removing it signs every browser out.
final class BrowserPasswordSettings: NSObject {
    private let passwordField = SecretField(width: 420)
    private let statusLabel = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    private let setButton = PageKit.primaryButton("Set Password", regular: true)
    private let removeButton = PageKit.button("Remove Password", regular: true)

    lazy var rows: [NSView] = build()

    private func build() -> [NSView] {
        passwordField.placeholderString = "At least 8 characters"
        setButton.target = self
        setButton.action = #selector(setPassword)
        removeButton.target = self
        removeButton.action = #selector(removePassword)
        return [
            PageKit.help(
                "Open Gateway's admin pages from a phone or another Mac's browser at "
                + "http://<this Mac's Tailscale address>:8787/admin and sign in with this password. "
                + "The gateway API key also works there. Only a scrambled hash is kept on this Mac."
            ),
            PageKit.formRow("Password", [passwordField] as [NSView]),
            PageKit.row([setButton, removeButton]),
            statusLabel,
        ]
    }

    func load() {
        GatewayAPI.get("/admin/api/settings/browser-password") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.setStatus("Could not read the browser password setting — \(error.localizedDescription)", .bad)
            case .success(let payload):
                self.apply(payload)
            }
        }
    }

    private func apply(_ payload: [String: Any]) {
        let isSet = payload["set"] as? Bool ?? false
        removeButton.isEnabled = isSet
        setButton.title = isSet ? "Change Password" : "Set Password"
        setStatus(isSet ? "A browser password is set." : "No browser password. Browsers sign in with the gateway API key.", .off)
    }

    private func setStatus(_ text: String, _ health: Health) {
        statusLabel.stringValue = text
        statusLabel.textColor = health == .off ? .secondaryLabelColor : health.text
    }

    @objc private func setPassword() {
        let password = passwordField.stringValue
        guard password.count >= 8 else {
            setStatus("Use at least 8 characters.", .bad)
            return
        }
        setButton.isEnabled = false
        GatewayAPI.put("/admin/api/settings/browser-password", body: ["password": password]) { [weak self] result in
            guard let self else { return }
            self.setButton.isEnabled = true
            switch result {
            case .failure(let error):
                self.setStatus("Not saved — \(error.localizedDescription)", .bad)
            case .success(let payload):
                self.passwordField.stringValue = ""
                self.apply(payload)
                self.setStatus("Saved. Every browser signed in before has been signed out.", .ok)
            }
        }
    }

    @objc private func removePassword() {
        let alert = NSAlert()
        alert.messageText = "Remove the browser password?"
        alert.informativeText = "Browsers are signed out and can sign in again only with the gateway API key."
        alert.addButton(withTitle: "Remove Password")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        GatewayAPI.delete("/admin/api/settings/browser-password") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.setStatus("Not removed — \(error.localizedDescription)", .bad)
            case .success(let payload):
                self.apply(payload)
                self.setStatus("Removed. Browsers were signed out.", .ok)
            }
        }
    }
}
