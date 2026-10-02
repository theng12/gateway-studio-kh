import AppKit

/// **A secret on screen: masked until asked, with an eye to show or hide it and an optional Copy.**
///
/// Every API key and token the app shows or takes goes through this one control, so they all behave the same
/// way: dots by default, the eye (`eye` / `eye.slash`, "Show" / "Hide" in the tooltip and to VoiceOver) to check
/// the value, and Copy that works while it is still masked, so handing a key on never needs it on screen.
///
/// While masked the value lives **only** in an `NSSecureTextField`, which never gives its text to accessibility or
/// the screen, and which here draws a fixed-length mask (`MaskedSecureTextField`) so the dots do not reveal how
/// long the secret is. The plain field is out of the view tree and empty; revealing moves the value
/// across and hiding moves it back, so there is never a second, readable copy of a hidden secret.
/// `tests/test_secret_field.swift` pins that.
///
/// Reuse it for any new secret (Telegram bot token, backup passphrase, ...):
///
///     let token = SecretField(placeholder: "Bot token", width: 320)            // editable, masked
///     let key = SecretField(EnvFile.apiKey(), editable: false, copy: true)    // read-only, masked, Copy
///     token.stringValue                                                        // the value, either state
final class SecretField: NSStackView, NSTextFieldDelegate {
    /// The field that holds the value while it is hidden.
    let secureField = MaskedSecureTextField(string: "")
    /// The field that holds the value while it is shown. Empty and detached while hidden.
    let plainField = NSTextField(string: "")
    let revealButton = NSButton()
    private(set) var copyButton: NSButton?
    private(set) var isRevealed = false
    /// Called after Copy put the value on the clipboard, so a page can say so in its status line.
    var onCopy: (() -> Void)?
    /// The clipboard Copy writes to; a test gives it a private one.
    var pasteboard: NSPasteboard = .general
    /// Called whenever the text is edited (typed or pasted), so a card can tell it has unsaved changes.
    var onChange: (() -> Void)?

    init(_ value: String = "", placeholder: String? = nil, editable: Bool = true,
         copy: Bool = false, width: CGFloat? = nil) {
        super.init(frame: .zero)
        orientation = .horizontal
        alignment = .centerY
        spacing = DS.Space.xs
        translatesAutoresizingMaskIntoConstraints = false
        for field in [secureField, plainField] as [NSTextField] {
            field.font = TextRole.mono.font
            field.placeholderString = placeholder
            field.delegate = self
            field.isEditable = editable
            field.isSelectable = editable
            field.lineBreakMode = .byTruncatingMiddle
            field.translatesAutoresizingMaskIntoConstraints = false
            if let width { field.widthAnchor.constraint(equalToConstant: width).isActive = true }
            if !editable {
                field.isBordered = false
                field.drawsBackground = false
            }
        }
        // A read-only revealed value can still be selected and copied by hand.
        plainField.isSelectable = true
        secureField.stringValue = value

        revealButton.bezelStyle = .rounded
        revealButton.isBordered = false
        revealButton.imagePosition = .imageOnly
        revealButton.target = self
        revealButton.action = #selector(toggle)
        revealButton.setContentHuggingPriority(.required, for: .horizontal)

        addArrangedSubview(secureField)
        addArrangedSubview(revealButton)
        if copy {
            let button = PageKit.button("Copy", target: self, action: #selector(copyValue))
            addArrangedSubview(button)
            copyButton = button
        }
        updateEye()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// The secret, whichever field holds it.
    var stringValue: String {
        get { activeField.stringValue }
        set { activeField.stringValue = newValue }
    }

    var placeholderString: String? {
        get { secureField.placeholderString }
        set { secureField.placeholderString = newValue; plainField.placeholderString = newValue }
    }

    /// The field on screen, for first-responder and key-view wiring.
    var activeField: NSTextField { isRevealed ? plainField : secureField }

    func setRevealed(_ revealed: Bool) {
        guard revealed != isRevealed else { return }
        let value = stringValue
        let (from, to): (NSTextField, NSTextField) = revealed ? (secureField, plainField) : (plainField, secureField)
        to.stringValue = value
        from.stringValue = ""
        insertArrangedSubview(to, at: 0)
        removeArrangedSubview(from)
        from.removeFromSuperview()
        isRevealed = revealed
        updateEye()
    }

    func controlTextDidChange(_ notification: Notification) { onChange?() }

    @objc private func toggle() { setRevealed(!isRevealed) }

    @objc func copyValue() {
        let value = stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        pasteboard.clearContents()
        pasteboard.setString(value, forType: .string)
        onCopy?()
    }

    private func updateEye() {
        let word = isRevealed ? "Hide" : "Show"
        revealButton.image = NSImage(systemSymbolName: isRevealed ? "eye.slash" : "eye",
                                     accessibilityDescription: word)
        revealButton.contentTintColor = .secondaryLabelColor
        revealButton.toolTip = word
        revealButton.setAccessibilityLabel(word)
    }
}

/// An `NSSecureTextField` that, when not being edited, draws the same fixed run of dots whatever the length of
/// the secret (like the Hub's SecretMask), so a hidden key does not give away how long it is. While it is being
/// typed into, the system field editor draws its usual one dot per character.
final class MaskedSecureTextField: NSSecureTextField {
    override class var cellClass: AnyClass? {
        get { MaskedSecureTextFieldCell.self }
        set {}
    }
}

final class MaskedSecureTextFieldCell: NSSecureTextFieldCell {
    static let mask = String(repeating: "\u{2022}", count: 12)

    override func drawInterior(withFrame cellFrame: NSRect, in controlView: NSView) {
        guard !stringValue.isEmpty, (controlView as? NSTextField)?.currentEditor() == nil else {
            return super.drawInterior(withFrame: cellFrame, in: controlView)
        }
        let text = NSAttributedString(string: Self.mask, attributes: [
            .font: font ?? TextRole.mono.font, .foregroundColor: NSColor.textColor])
        var area = drawingRect(forBounds: cellFrame)
        let height = text.size().height
        area.origin.y += (area.height - height) / 2
        area.size.height = height
        text.draw(in: area)
    }
}
