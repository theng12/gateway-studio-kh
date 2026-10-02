import AppKit

/// The Telegram card on the Settings page: where messages go, which events send one, quiet hours, and the
/// daily summary time. The gateway owns the settings (`/admin/api/settings/telegram`) and the bot token is a
/// secret it never sends back, so the token field only ever holds what is typed here.
///
/// The event list comes from the gateway (title and one-line description per type), so a new event type
/// there appears here without a change to this file.
final class TelegramSettings: NSObject, NSTextFieldDelegate {
    private static let fieldWidth: CGFloat = 420

    private let enabledCheck = NSButton(checkboxWithTitle: "Send Telegram notifications", target: nil, action: nil)
    private let tokenField = SecretField(width: TelegramSettings.fieldWidth)
    private let chatField = NSTextField(string: "")
    private let quietCheck = NSButton(checkboxWithTitle: "Hold messages from", target: nil, action: nil)
    private let quietFrom = TelegramSettings.timePicker()
    private let quietTo = TelegramSettings.timePicker()
    private let summaryTime = TelegramSettings.timePicker()
    private let eventsStack = NSStackView()
    private let statusLabel = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    private let saveButton = PageKit.primaryButton("Save Telegram Settings", regular: true)
    private let testButton = PageKit.button("Send Test Message", regular: true)

    private var eventChecks: [(key: String, button: NSButton)] = []
    /// A change the owner has made and not saved: a re-read on activation must not put the old values back.
    private var dirty = false

    /// The rows of the Settings card.
    lazy var rows: [NSView] = build()

    // MARK: Layout

    private static func timePicker() -> NSDatePicker {
        let picker = NSDatePicker()
        picker.datePickerStyle = .textFieldAndStepper
        picker.datePickerElements = .hourMinute
        picker.controlSize = .regular
        return picker
    }

    private func separator() -> NSBox {
        let box = NSBox()
        box.boxType = .separator
        return box
    }

    private func build() -> [NSView] {
        chatField.font = TextRole.mono.font
        chatField.placeholderString = "-1001234567890"
        chatField.delegate = self
        chatField.widthAnchor.constraint(equalToConstant: 220).isActive = true
        tokenField.placeholderString = "123456789:AA…"
        tokenField.onChange = { [weak self] in self?.dirty = true }

        for check in [enabledCheck, quietCheck] {
            check.target = self
            check.action = #selector(changed)
        }
        for picker in [quietFrom, quietTo, summaryTime] {
            picker.target = self
            picker.action = #selector(changed)
        }
        saveButton.target = self
        saveButton.action = #selector(save)
        testButton.target = self
        testButton.action = #selector(sendTest)

        eventsStack.orientation = .vertical
        eventsStack.alignment = .leading
        eventsStack.spacing = DS.Space.m
        eventsStack.addArrangedSubview(PageKit.loading("Reading the settings…"))

        let buttons = PageKit.row([saveButton, testButton])
        let quietRow = PageKit.row([
            quietCheck, quietFrom,
            PageKit.label("to", .secondary, colour: .secondaryLabelColor), quietTo,
        ])
        return [
            PageKit.help(
                "Get a message on your phone when a Mac or group goes offline, and a summary of "
                + "yesterday every morning. Create a bot with @BotFather, send it a message, then paste "
                + "its token and your chat ID here. Nothing is sent until both are saved."
            ),
            enabledCheck,
            PageKit.formRow("Bot token", [
                tokenField,
                PageKit.help("Kept on this Mac and never shown again once saved. The eye shows what you typed."),
            ] as [NSView]),
            PageKit.formRow("Chat ID", [
                chatField,
                PageKit.help("Your own chat, or a group (its ID starts with a minus sign)."),
            ] as [NSView]),
            buttons,
            statusLabel,
            separator(),
            PageKit.label("Tell me when", .rowTitle),
            eventsStack,
            separator(),
            PageKit.formRow("Quiet hours", [
                quietRow,
                PageKit.help("Messages that arrive in this window are held and sent together when it ends."),
            ] as [NSView]),
            PageKit.formRow("Daily summary at", [
                summaryTime,
                PageKit.help("Yesterday's jobs, audio and images, the top failures, and which Macs are offline."),
            ] as [NSView]),
        ]
    }

    /// One switch: its title, and the one-line description under it, indented to the title.
    private func eventRow(_ title: String, _ detail: String, key: String) -> NSView {
        let check = NSButton(checkboxWithTitle: title, target: self, action: #selector(changed))
        eventChecks.append((key, check))
        let help = PageKit.help(detail)
        help.preferredMaxLayoutWidth = PageKit.textWidth - 20
        let indented = NSStackView(views: [NSView(), help])
        indented.orientation = .horizontal
        indented.spacing = 0
        indented.views.first?.widthAnchor.constraint(equalToConstant: 20).isActive = true
        let column = NSStackView(views: [check, indented])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = DS.Space.xs
        return column
    }

    // MARK: Time

    private static func date(_ hhmm: String) -> Date {
        let parts = hhmm.split(separator: ":").compactMap { Int($0) }
        var parts2 = Calendar.current.dateComponents([.year, .month, .day], from: Date())
        parts2.hour = parts.first ?? 0
        parts2.minute = parts.count > 1 ? parts[1] : 0
        return Calendar.current.date(from: parts2) ?? Date()
    }

    private static func hhmm(_ date: Date) -> String {
        let parts = Calendar.current.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", parts.hour ?? 0, parts.minute ?? 0)
    }

    // MARK: Reading

    /// Read the settings from the gateway, unless there are unsaved edits.
    func load() {
        GatewayAPI.get("/admin/api/settings/telegram") { [weak self] result in
            guard let self, !self.dirty else { return }
            switch result {
            case .failure(let error):
                self.setStatus("Could not read the Telegram settings — \(error.localizedDescription)", .bad)
            case .success(let payload):
                self.apply(payload)
            }
        }
    }

    private func apply(_ payload: [String: Any]) {
        enabledCheck.state = (payload["enabled"] as? Bool ?? true) ? .on : .off
        let saved = payload["token_set"] as? Bool ?? false
        tokenField.placeholderString = saved ? "Saved — paste a new token to replace it" : "123456789:AA…"
        if let chat = payload["chat_id"] as? String, chatField.currentEditor() == nil { chatField.stringValue = chat }

        let events = (payload["events"] as? [[String: Any]]) ?? []
        let keys = events.compactMap { $0["key"] as? String }
        if keys != eventChecks.map({ $0.key }) {
            eventChecks = []
            eventsStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
            for event in events {
                guard let key = event["key"] as? String else { continue }
                eventsStack.addArrangedSubview(eventRow(
                    event["title"] as? String ?? key, event["description"] as? String ?? "", key: key))
            }
        }
        for event in events {
            guard let key = event["key"] as? String,
                  let check = eventChecks.first(where: { $0.key == key })?.button else { continue }
            check.state = (event["enabled"] as? Bool ?? false) ? .on : .off
        }

        if let quiet = payload["quiet_hours"] as? [String: Any] {
            quietCheck.state = (quiet["enabled"] as? Bool ?? false) ? .on : .off
            quietFrom.dateValue = Self.date(quiet["start"] as? String ?? "22:00")
            quietTo.dateValue = Self.date(quiet["end"] as? String ?? "07:00")
        }
        summaryTime.dateValue = Self.date(payload["summary_time"] as? String ?? "08:00")
        let enabled = quietCheck.state == .on
        quietFrom.isEnabled = enabled
        quietTo.isEnabled = enabled

        let status = payload["status"] as? [String: Any]
        if let error = status?["last_error"] as? String, !error.isEmpty {
            setStatus("Last message failed — \(error)", .bad)
        } else if let sent = status?["last_sent_at"] as? Double {
            setStatus("Last message sent \(GatewayAPI.relativeTime(sent)).", .off)
        } else {
            setStatus(saved ? "Saved. No message has been sent yet." : "Not set up yet.", .off)
        }
    }

    private func setStatus(_ text: String, _ health: Health) {
        statusLabel.stringValue = text
        statusLabel.textColor = health == .off ? .secondaryLabelColor : health.text
    }

    // MARK: Actions

    @objc private func changed() {
        dirty = true
        let quiet = quietCheck.state == .on
        quietFrom.isEnabled = quiet
        quietTo.isEnabled = quiet
    }

    func controlTextDidChange(_ notification: Notification) { dirty = true }

    @objc private func save() {
        var body: [String: Any] = [
            "enabled": enabledCheck.state == .on,
            "chat_id": chatField.stringValue.trimmingCharacters(in: .whitespaces),
            "events": Dictionary(uniqueKeysWithValues: eventChecks.map { ($0.key, $0.button.state == .on) }),
            "quiet_hours": [
                "enabled": quietCheck.state == .on,
                "start": Self.hhmm(quietFrom.dateValue),
                "end": Self.hhmm(quietTo.dateValue),
            ] as [String: Any],
            "summary_time": Self.hhmm(summaryTime.dateValue),
        ]
        let token = tokenField.stringValue.trimmingCharacters(in: .whitespaces)
        if !token.isEmpty { body["bot_token"] = token }
        saveButton.isEnabled = false
        GatewayAPI.put("/admin/api/settings/telegram", body: body) { [weak self] result in
            guard let self else { return }
            self.saveButton.isEnabled = true
            switch result {
            case .failure(let error):
                self.setStatus("Not saved — \(error.localizedDescription)", .bad)
            case .success(let payload):
                self.dirty = false
                self.tokenField.stringValue = ""
                self.apply(payload)
                self.setStatus("Saved.", .ok)
            }
        }
    }

    @objc private func sendTest() {
        var body: [String: Any] = [:]
        let token = tokenField.stringValue.trimmingCharacters(in: .whitespaces)
        if !token.isEmpty { body["bot_token"] = token }
        let chat = chatField.stringValue.trimmingCharacters(in: .whitespaces)
        if !chat.isEmpty { body["chat_id"] = chat }
        testButton.isEnabled = false
        setStatus("Sending a test message…", .off)
        GatewayAPI.post("/admin/api/settings/telegram/test", body: body, timeout: 40) { [weak self] result in
            guard let self else { return }
            self.testButton.isEnabled = true
            switch result {
            case .failure(let error):
                self.setStatus("Could not send — \(error.localizedDescription)", .bad)
            case .success(let payload):
                if payload["ok"] as? Bool == true {
                    self.setStatus("Test message sent. Check Telegram.", .ok)
                } else {
                    self.setStatus(payload["error"] as? String ?? "Telegram did not accept the message.", .bad)
                }
            }
        }
    }
}
