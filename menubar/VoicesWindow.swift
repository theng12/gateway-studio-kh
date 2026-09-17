// The Voices window: providers disagree about voices, so this shows the
// difference rather than hiding it.
//
//   Local (this Mac)  your own cloning clips — on by default
//   Account           voices the provider already has for you — opt in
//   Library           the provider's public catalogue — search, then add
//
// Only enabled voices reach Story Studio. That matters most for the library:
// ElevenLabs' public catalogue alone holds tens of thousands of voices, and
// importing all of them would be worse than useless.

import Cocoa

final class VoicesSection: NSObject, AppSection, NSTableViewDataSource, NSTableViewDelegate,
                           NSSearchFieldDelegate {
    static let shared = VoicesSection()

    let title = "Voices"
    let symbol = "waveform"
    // Browsing is interactive: a timed refresh would rebuild the table
    // under the cursor and fight the checkbox just clicked.
    let refreshesAutomatically = false

    private var table: NSTableView!
    private var providerPopup: NSPopUpButton!
    private var libraryCheck: NSButton!
    private var searchField: NSSearchField!
    private var summaryLabel: NSTextField!
    private var detailLabel: NSTextField!
    private var addButton: NSButton!
    private var rows: [[String: Any]] = []
    private var providers: [(id: String, name: String, kind: String)] = []
    private var libraryMode = false
    private var sound: NSSound?
    private var searchTimer: Timer?

    /// The section's content, built once and hosted by the main window.
    /// Lazy so it can be built by a method on self.
    lazy var view: NSView = buildContent()

    func activated() {
        reload()
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: 14, left: 16, bottom: 14, right: 16)
        root.translatesAutoresizingMaskIntoConstraints = false

        providerPopup = NSPopUpButton()
        providerPopup.target = self
        providerPopup.action = #selector(filtersChanged)
        providerPopup.controlSize = .small

        searchField = NSSearchField()
        searchField.placeholderString = "Search voices…"
        searchField.delegate = self
        searchField.controlSize = .small
        searchField.widthAnchor.constraint(equalToConstant: 220).isActive = true

        libraryCheck = NSButton(
            checkboxWithTitle: "Search the provider's public library",
            target: self, action: #selector(filtersChanged)
        )
        libraryCheck.controlSize = .small

        let refresh = NSButton(title: "Refresh", target: self, action: #selector(refreshVoices))
        refresh.bezelStyle = .rounded
        refresh.controlSize = .small

        let bar = NSStackView(views: [providerPopup, searchField, libraryCheck, NSView(), refresh])
        bar.orientation = .horizontal
        bar.spacing = 8
        bar.alignment = .centerY
        root.addArrangedSubview(bar)
        bar.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -32).isActive = true

        summaryLabel = NSTextField(labelWithString: "Loading…")
        summaryLabel.font = .systemFont(ofSize: 11.5)
        summaryLabel.textColor = .secondaryLabelColor
        root.addArrangedSubview(summaryLabel)

        table = NSTableView()
        table.dataSource = self
        table.delegate = self
        table.rowHeight = 30
        table.usesAlternatingRowBackgroundColors = true
        table.allowsMultipleSelection = true
        table.style = .inset
        for (id, title, width) in [
            ("on", "", 34),
            ("preview", "", 30),
            ("name", "Voice", 230),
            ("language", "Language", 80),
            ("details", "Description", 300),
            ("source", "Source", 80),
        ] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            column.title = title
            column.width = CGFloat(width)
            table.addTableColumn(column)
        }

        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -32),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 320),
        ])

        let actions = NSStackView()
        actions.orientation = .horizontal
        actions.spacing = 8
        actions.addArrangedSubview(button("Enable selected", #selector(enableSelected)))
        actions.addArrangedSubview(button("Disable selected", #selector(disableSelected)))
        addButton = button("Add to my account", #selector(addSelected))
        actions.addArrangedSubview(addButton)
        actions.addArrangedSubview(button("Stop preview", #selector(stopPreview)))
        root.addArrangedSubview(actions)

        detailLabel = NSTextField(wrappingLabelWithString:
            "Local voices are yours and are on by default. Provider voices are opt-in — "
            + "tick the ones Story Studio should offer.")
        detailLabel.font = .systemFont(ofSize: 11.5)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(detailLabel)
        detailLabel.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -32).isActive = true

        let container = NSView()
        container.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: container.topAnchor),
            root.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            root.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        return container
    }

    private func button(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        b.controlSize = .small
        return b
    }

    // MARK: Data

    @objc private func filtersChanged() { reload() }

    @objc private func searchChanged() {
        searchTimer?.invalidate()
        searchTimer = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: false) { [weak self] _ in
            self?.reload()
        }
    }

    func controlTextDidChange(_ obj: Notification) { searchChanged() }

    @objc private func refreshVoices() {
        summaryLabel.stringValue = "Discovering voices…"
        GatewayAPI.post("/admin/api/voices/refresh") { [weak self] _ in
            self?.reload()
        }
    }

    func reload() {


        let providerId = selectedProviderId()
        let library = libraryCheck.state == .on && supportsLibrary(providerId)
        libraryMode = library
        addButton?.isEnabled = library

        let query = searchField.stringValue.trimmingCharacters(in: .whitespaces)
        var path: String
        if library {
            path = "/admin/api/voices/library?provider_id=\(providerId)"
            if !query.isEmpty {
                path += "&q=\(query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")"
            }
        } else {
            path = "/admin/api/voices"
            var parts: [String] = []
            if !providerId.isEmpty { parts.append("provider_id=\(providerId)") }
            if !query.isEmpty {
                parts.append("q=\(query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")")
            }
            if !parts.isEmpty { path += "?" + parts.joined(separator: "&") }
        }

        GatewayAPI.get(path) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let payload):
                self.rows = payload["voices"] as? [[String: Any]] ?? []
                self.loadProviders(from: payload)
                if self.libraryMode {
                    self.summaryLabel.stringValue =
                        "\(self.rows.count) voices in the public library"
                        + (self.rows.isEmpty ? " — try another search" : "")
                } else {
                    let summary = payload["summary"] as? [String: Any] ?? [:]
                    let bySource = summary["by_source"] as? [String: Any] ?? [:]
                    let parts = bySource.keys.sorted().map { "\(bySource[$0] as? Int ?? 0) \($0)" }
                    self.summaryLabel.stringValue =
                        "\(summary["enabled"] as? Int ?? 0) of \(summary["discovered"] as? Int ?? 0) "
                        + "voices enabled" + (parts.isEmpty ? "" : " · " + parts.joined(separator: ", "))
                }
            case .failure(let error):
                self.rows = []
                self.summaryLabel.stringValue = error.localizedDescription
            }
            self.table.reloadData()
        }
    }

    private func loadProviders(from payload: [String: Any]) {
        guard providers.isEmpty,
              let list = payload["providers"] as? [[String: Any]] else { return }
        providers = list.compactMap {
            guard let id = $0["id"] as? String, let name = $0["name"] as? String else { return nil }
            return (id: id, name: name, kind: $0["kind"] as? String ?? "")
        }
        providerPopup.removeAllItems()
        providerPopup.addItem(withTitle: "All providers")
        providerPopup.lastItem?.representedObject = ""
        for provider in providers {
            providerPopup.addItem(withTitle: provider.name)
            providerPopup.lastItem?.representedObject = provider.id
        }
    }

    private func selectedProviderId() -> String {
        (providerPopup?.selectedItem?.representedObject as? String) ?? ""
    }

    private func supportsLibrary(_ providerId: String) -> Bool {
        guard let provider = providers.first(where: { $0.id == providerId }) else { return false }
        return provider.kind == "elevenlabs" || provider.kind == "fish"
    }

    // MARK: Actions

    private func selectedRows() -> [[String: Any]] {
        table.selectedRowIndexes.compactMap { $0 < rows.count ? rows[$0] : nil }
    }

    @objc private func enableSelected() { setEnabled(true) }
    @objc private func disableSelected() { setEnabled(false) }

    private func setEnabled(_ enabled: Bool) {
        let ids = selectedRows().compactMap { $0["id"] as? String }
        guard !ids.isEmpty else { return }
        GatewayAPI.post("/admin/api/voices/enabled", body: ["items": ids, "enabled": enabled]) { [weak self] _ in
            self?.reload()
        }
    }

    @objc private func addSelected() {
        for row in selectedRows() {
            guard let providerId = row["provider_id"] as? String,
                  let voiceId = row["provider_voice_id"] as? String else { continue }
            var body: [String: Any] = [
                "provider_id": providerId,
                "provider_voice_id": voiceId,
                "display_name": row["display_name"] as? String ?? voiceId,
            ]
            // ElevenLabs addresses a library voice by (owner, voice).
            if let owner = (row["raw"] as? [String: Any])?["owner_id"] as? String, !owner.isEmpty {
                body["owner_id"] = owner
            }
            GatewayAPI.post("/admin/api/voices/import", body: body) { [weak self] _ in
                self?.reload()
            }
        }
    }

    @objc private func stopPreview() {
        sound?.stop()
        sound = nil
    }

    private func preview(_ voiceId: String) {
        guard let url = GatewayAPI.url(
            "/admin/api/voices/\(voiceId.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "")/preview"
        ) else { return }

        var request = URLRequest(url: url)
        request.timeoutInterval = 60
        let key = EnvFile.apiKey()
        if !key.isEmpty { request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization") }

        summaryLabel.stringValue = "Fetching preview…"
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                guard code == 200, let data, !data.isEmpty else {
                    self.summaryLabel.stringValue = "No preview available for that voice."
                    return
                }
                self.sound?.stop()
                self.sound = NSSound(data: data)
                if self.sound?.play() == true {
                    self.summaryLabel.stringValue = "Playing preview…"
                } else {
                    self.summaryLabel.stringValue = "The preview could not be played."
                }
            }
        }.resume()
    }

    // MARK: Table

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(
        _ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int
    ) -> NSView? {
        guard let tableColumn, row < rows.count else { return nil }
        let voice = rows[row]
        let identifier = tableColumn.identifier.rawValue

        let cell = NSTableCellView()

        if identifier == "on" {
            if libraryMode {
                // Library voices cannot be switched on: they have to be added to
                // the account first. Showing a checkbox would imply otherwise.
                let label = NSTextField(labelWithString: "—")
                label.textColor = .tertiaryLabelColor
                label.translatesAutoresizingMaskIntoConstraints = false
                cell.addSubview(label)
                NSLayoutConstraint.activate([
                    label.centerXAnchor.constraint(equalTo: cell.centerXAnchor),
                    label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                ])
                return cell
            }
            let box = NSButton(checkboxWithTitle: "", target: self, action: #selector(toggleRow(_:)))
            box.state = (voice["enabled"] as? Bool ?? false) ? .on : .off
            box.tag = row
            box.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(box)
            NSLayoutConstraint.activate([
                box.centerXAnchor.constraint(equalTo: cell.centerXAnchor),
                box.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            ])
            return cell
        }

        if identifier == "preview" {
            let play = NSButton(title: "▶", target: self, action: #selector(playRow(_:)))
            play.isBordered = false
            play.tag = row
            play.toolTip = "Preview this voice"
            play.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(play)
            NSLayoutConstraint.activate([
                play.centerXAnchor.constraint(equalTo: cell.centerXAnchor),
                play.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            ])
            return cell
        }

        let field = NSTextField(labelWithString: "")
        field.font = .systemFont(ofSize: 11.5)
        field.lineBreakMode = .byTruncatingTail
        switch identifier {
        case "name":
            field.stringValue = voice["display_name"] as? String ?? "?"
            field.font = .systemFont(ofSize: 12)
        case "language":
            field.stringValue = (voice["language"] as? String ?? "").uppercased()
            field.textColor = .secondaryLabelColor
        case "details":
            var bits = [voice["description"] as? String ?? ""]
            if let gender = voice["gender"] as? String, !gender.isEmpty { bits.append(gender) }
            if let accent = voice["accent"] as? String, !accent.isEmpty { bits.append(accent) }
            field.stringValue = bits.filter { !$0.isEmpty }.joined(separator: " · ")
            field.textColor = .secondaryLabelColor
        case "source":
            field.stringValue = voice["source"] as? String ?? ""
            field.textColor = .secondaryLabelColor
        default:
            break
        }

        field.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(field)
        cell.textField = field
        NSLayoutConstraint.activate([
            field.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            field.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            field.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    @objc private func toggleRow(_ sender: NSButton) {
        guard sender.tag < rows.count, let id = rows[sender.tag]["id"] as? String else { return }
        let enabled = sender.state == .on
        rows[sender.tag]["enabled"] = enabled
        // Deliberately no reload: a full refresh would rebuild the table and
        // fight the checkbox the user just clicked. The next reload shows the
        // corrected totals.
        GatewayAPI.post("/admin/api/voices/enabled", body: ["items": [id], "enabled": enabled])
    }

    @objc private func playRow(_ sender: NSButton) {
        guard sender.tag < rows.count, let id = rows[sender.tag]["id"] as? String else { return }
        preview(id)
    }
}
