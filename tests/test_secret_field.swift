import AppKit

// The masked-secret control: a hidden secret exists only in the secure field, never in a readable one.
var failures = 0
func check(_ ok: Bool, _ what: String) {
    if !ok { failures += 1; print("FAIL: \(what)") }
}

/// Every plain (readable) text field under `view`; an NSSecureTextField draws dots and is not one.
func readableTexts(_ view: NSView) -> [String] {
    var out: [String] = []
    if let field = view as? NSTextField, !(field is NSSecureTextField) { out.append(field.stringValue) }
    for child in view.subviews { out += readableTexts(child) }
    return out
}

@main
enum SecretFieldCheck {
    static func main() {
        let secret = "gw-test-secret-0123456789abcdef"
        let window = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: 600, height: 80),
                              styleMask: [.titled], backing: .buffered, defer: false)
        let field = SecretField(secret, copy: true, width: 320)
        window.contentView = field

        // Masked by default.
        check(!field.isRevealed, "a secret starts masked")
        check(field.secureField.superview === field, "the secure field is on screen while masked")
        check(field.plainField.superview == nil, "the readable field is not in the view tree while masked")
        check(field.plainField.stringValue.isEmpty, "the readable field holds nothing while masked")
        check(!readableTexts(field).contains { $0.contains(secret) }, "no readable text shows the secret while masked")
        check(field.stringValue == secret, "the value is still there to save or copy")
        check(field.revealButton.toolTip == "Show", "the eye says Show while masked")
        check(field.revealButton.image != nil, "the eye has an icon")

        // Copy works without revealing.
        let board = NSPasteboard(name: NSPasteboard.Name("gateway-secret-field-test-\(getpid())"))
        field.pasteboard = board
        var copied = false
        field.onCopy = { copied = true }
        field.copyValue()
        check(board.string(forType: .string) == secret, "Copy puts the value on the clipboard while masked")
        check(copied, "Copy tells the page")
        check(!field.isRevealed, "Copy does not reveal")

        // Reveal, then hide again.
        field.setRevealed(true)
        check(field.plainField.superview === field && field.plainField.stringValue == secret, "the eye shows the value")
        check(field.secureField.superview == nil && field.secureField.stringValue.isEmpty, "the secure copy is cleared while shown")
        check(field.revealButton.toolTip == "Hide", "the eye says Hide while shown")
        field.plainField.stringValue = secret + "-edited"
        field.setRevealed(false)
        check(field.stringValue == secret + "-edited", "an edit made while shown survives hiding")
        check(field.plainField.stringValue.isEmpty && field.plainField.superview == nil, "hiding empties and detaches the readable field")
        check(!readableTexts(field).contains { $0.contains(secret) }, "no readable text shows the secret after hiding")

        // Setting a value while masked keeps it masked.
        field.stringValue = "another-secret-value-000000"
        check(!readableTexts(field).contains { $0.contains("another-secret") }, "a value set while masked stays masked")

        // A read-only secret is masked too.
        let readOnly = SecretField(secret, editable: false)
        check(!readOnly.secureField.isEditable && !readableTexts(readOnly).contains { $0.contains(secret) },
              "a read-only secret is masked and not editable")

        board.releaseGlobally()
        if failures > 0 { exit(1) }
        print("PASS: secret field")
    }
}
