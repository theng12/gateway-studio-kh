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
        let root = PageKit.page()

        root.addArrangedSubview(PageKit.heading(
            "Voices",
            "Only the voices you enable here are offered to Story Studio."
        ))

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

        // Stocking the library, which is a different act from ticking voices on and
        // off: it belongs beside Refresh, not among the selection actions.
        let addVoices = NSButton(title: "Add voices…", target: self, action: #selector(pickVoices))
        addVoices.bezelStyle = .rounded
        addVoices.controlSize = .small

        let bar = NSStackView(views: [
            providerPopup, searchField, libraryCheck, NSView(), addVoices, refresh,
        ])
        bar.orientation = .horizontal
        bar.spacing = 8
        bar.alignment = .centerY
        root.addArrangedSubview(bar)
        bar.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56).isActive = true

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
            ("name", "Voice", 210),
            ("language", "Language", 70),
            ("fleet", "Fleet", 80),
            ("details", "Description", 290),
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
            scroll.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 320),
        ])

        let actions = NSStackView()
        actions.orientation = .horizontal
        actions.spacing = 8
        actions.addArrangedSubview(button("Enable selected", #selector(enableSelected)))
        actions.addArrangedSubview(button("Disable selected", #selector(disableSelected)))
        // Permission to clone and distribute is granted deliberately, never by a
        // stray click, and it is per voice because some voices are private.
        actions.addArrangedSubview(button("Allow fleet sharing…", #selector(allowSharing)))
        actions.addArrangedSubview(button("Make private", #selector(makePrivate)))
        actions.addArrangedSubview(button("Share with fleet", #selector(shareWithFleet)))
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
        detailLabel.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56).isActive = true

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

    /// Stock the catalogue from a folder the owner picks.
    ///
    /// Both shapes are accepted because both are things people have: a folder of
    /// per-voice folders, and a folder of loose audio files — which is what you get
    /// by copying a clip in and expecting it to become a voice.
    @objc private func pickVoices() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Add voices"
        panel.message =
            "Pick a folder of voices, or a folder of audio files. One voice per folder, "
            + "or one voice per file — either works."
        guard panel.runModal() == .OK, let folder = panel.url else { return }

        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.stringValue = "Adding voices…"
        GatewayAPI.post("/admin/api/voices/add", body: [
            "folder": folder.path,
            "annotate": true,
        ]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.textColor = .systemOrange
                self.summaryLabel.stringValue = "Could not add them — \(error)"
            case .success(let payload):
                let imported = payload["imported"] as? [[String: Any]] ?? []
                let skipped = payload["skipped"] as? [[String: Any]] ?? []
                self.reportAdded(imported: imported, skipped: skipped,
                                 annotation: payload["annotation"] as? [String: Any])
            }
        }
    }

    /// Say what was added, then follow the transcription that makes them usable.
    private func reportAdded(
        imported: [[String: Any]], skipped: [[String: Any]], annotation: [String: Any]?
    ) {
        reload()
        let names = imported.compactMap { $0["display_name"] as? String }
        var line = names.isEmpty
            ? "Nothing new was added."
            : "Added \(names.count) voice\(names.count == 1 ? "" : "s"): "
              + names.prefix(4).joined(separator: ", ")
              + (names.count > 4 ? " +\(names.count - 4) more" : "")
        if !skipped.isEmpty {
            line += " · skipped " + skipped.compactMap { $0["name"] as? String }
                .prefix(3).joined(separator: ", ")
        }
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.stringValue = line

        guard let annotation else { return }
        if let error = annotation["error"] as? String, !error.isEmpty {
            // The usual one: no Hub, so there is nothing to transcribe with. A
            // voice without segments cannot clone, so this is not cosmetic.
            summaryLabel.textColor = .systemOrange
            summaryLabel.stringValue = "\(line) — but they cannot be used yet: \(error)"
            return
        }
        if annotation["running"] as? Bool == true {
            followAnnotation()
        } else if (annotation["total"] as? Int ?? 0) == 0 {
            summaryLabel.stringValue = line + " Every one already has a transcript."
        }
    }

    /// Poll the transcription pass until it finishes, then say what happened.
    private func followAnnotation() {
        GatewayAPI.get("/admin/api/voices/add/status") { [weak self] result in
            guard let self, case .success(let state) = result else { return }
            let done = state["done"] as? Int ?? 0
            let total = state["total"] as? Int ?? 0
            let current = state["current"] as? String ?? ""

            if state["running"] as? Bool == true {
                self.summaryLabel.textColor = .secondaryLabelColor
                self.summaryLabel.stringValue =
                    "Making them usable — transcribing \(done + 1) of \(total)"
                    + (current.isEmpty ? "…" : ": \(current)")
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.followAnnotation() }
                return
            }

            let results = state["results"] as? [[String: Any]] ?? []
            let failed = results.filter { $0["ok"] as? Bool == false }
            let ok = results.count - failed.count
            self.reload()
            if failed.isEmpty {
                self.summaryLabel.textColor = .secondaryLabelColor
                self.summaryLabel.stringValue =
                    "Ready — \(ok) voice\(ok == 1 ? "" : "s") transcribed and usable."
            } else {
                // Name them. "Some failed" is not something anyone can act on.
                self.summaryLabel.textColor = .systemOrange
                let why = failed.prefix(2).map {
                    "\($0["name"] as? String ?? "?") (\($0["detail"] as? String ?? "?"))"
                }.joined(separator: "; ")
                self.summaryLabel.stringValue =
                    "\(ok) transcribed, \(failed.count) could not be: \(why)"
            }
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

    /// Push allowed voices to every location, and say what happened at each.
    ///
    /// Controllers only — the gateway never talks to nodes, and each controller
    /// fans out to its own. A node that is switched off is reported as waiting, not
    /// as a failure: calling it an error would send someone to a site that was
    /// never broken.
    @objc private func shareWithFleet() {
        let ids = selectedRows().compactMap { $0["id"] as? String }
            .filter { $0.hasPrefix("studio-hub-local") }
        guard !ids.isEmpty else {
            summaryLabel.textColor = .systemOrange
            summaryLabel.stringValue = "Select the voices to share first."
            return
        }
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.stringValue = "Sharing \(ids.count) voice\(ids.count == 1 ? "" : "s") with the fleet…"
        GatewayAPI.post("/admin/api/voices/share", body: ["items": ids]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.textColor = .systemOrange
                self.summaryLabel.stringValue = "Could not share them — \(error)"
            case .success(let payload):
                let results = payload["results"] as? [[String: Any]] ?? []
                let ok = results.filter { $0["ok"] as? Bool == true }
                let bad = results.filter { $0["ok"] as? Bool == false }
                if let first = ok.first {
                    self.summaryLabel.textColor = .secondaryLabelColor
                    var line = "Shared \(ok.count) of \(results.count): "
                        + "\(first["voice"] as? String ?? "?") at \(first["location"] as? String ?? "?")"
                        + " — \(first["detail"] as? String ?? "")"
                    if let segments = first["segments"] as? Int { line += " · \(segments) timings kept" }
                    if !bad.isEmpty {
                        line += " · \(bad.count) location\(bad.count == 1 ? "" : "s") could not take it"
                        self.summaryLabel.textColor = .systemOrange
                    }
                    self.summaryLabel.stringValue = line
                } else {
                    self.summaryLabel.textColor = .systemOrange
                    // Name the reason. The common one is a missing Hub token, and
                    // these routes refuse a fleet token on purpose.
                    let why = bad.first?["detail"] as? String ?? "no location accepted it"
                    self.summaryLabel.stringValue = "Nothing was shared — \(why)"
                }
            }
        }
    }

    /// Grant permission to clone and distribute the selected voices.
    ///
    /// Behind a confirmation on purpose. This records the owner's permission, and
    /// he has voices he does not want cloned or distributed — so it states exactly
    /// what is being granted, to how many voices, before anything is written.
    @objc private func allowSharing() {
        let chosen = selectedRows().filter { ($0["id"] as? String)?.hasPrefix("studio-hub-local") == true }
        let ids = chosen.compactMap { $0["id"] as? String }
        guard !ids.isEmpty else {
            summaryLabel.textColor = .systemOrange
            summaryLabel.stringValue =
                "Select the voices to share first — only this Mac's own voices can be shared."
            return
        }
        let already = chosen.filter {
            (($0["raw"] as? [String: Any])?["permission_acknowledged"] as? Bool) == true
        }.count

        let alert = NSAlert()
        alert.messageText = ids.count == 1
            ? "Allow cloning and distribution of this voice?"
            : "Allow cloning and distribution of these \(ids.count) voices?"
        alert.informativeText =
            "This records that you have permission to clone and distribute "
            + (ids.count == 1 ? "this voice" : "these voices")
            + " across the machines in your fleet. Only allow voices you own or are licensed to "
            + "distribute — anything you do not allow stays private and is never copied to "
            + "another machine."
            + (already > 0 ? "\n\n\(already) of them already allow it." : "")
        alert.addButton(withTitle: "Allow sharing")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        GatewayAPI.post("/admin/api/voices/shared",
                        body: ["items": ids, "acknowledged": true]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.textColor = .systemOrange
                self.summaryLabel.stringValue = "Could not record it — \(error)"
            case .success:
                self.summaryLabel.textColor = .secondaryLabelColor
                self.summaryLabel.stringValue =
                    "Recorded your permission for \(ids.count) voice\(ids.count == 1 ? "" : "s")."
            }
            self.reload()
        }
    }

    /// Withdraw permission for the selected voices, in one act and with no dialog:
    /// making something private again is the safe direction.
    @objc private func makePrivate() {
        let ids = selectedRows().compactMap { $0["id"] as? String }
            .filter { $0.hasPrefix("studio-hub-local") }
        guard !ids.isEmpty else { return }
        GatewayAPI.post("/admin/api/voices/shared",
                        body: ["items": ids, "acknowledged": false]) { [weak self] result in
            guard let self else { return }
            if case .failure(let error) = result {
                self.summaryLabel.textColor = .systemOrange
                self.summaryLabel.stringValue = "Could not change it — \(error)"
            } else {
                self.summaryLabel.textColor = .secondaryLabelColor
                self.summaryLabel.stringValue =
                    "\(ids.count) voice\(ids.count == 1 ? " is" : "s are") private again."
            }
            self.reload()
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
        case "fleet":
            // Only this Mac's own voices can be shared, and only once the owner has
            // allowed it. "Private" has to be visible, or he cannot tell which of
            // his voices would be copied to another machine.
            let mine = (voice["id"] as? String)?.hasPrefix("studio-hub-local") == true
            let allowed = (voice["raw"] as? [String: Any])?["permission_acknowledged"] as? Bool == true
            if !mine {
                field.stringValue = "—"
                field.textColor = .tertiaryLabelColor
            } else if allowed {
                field.stringValue = "Shared"
                field.textColor = .secondaryLabelColor
            } else {
                field.stringValue = "Private"
                field.textColor = .tertiaryLabelColor
            }
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
