import AppKit
import UniformTypeIdentifiers

/// Settings → Backups: what is backed up, where it goes, Back Up Now, the optional nightly run,
/// and Import / Restore. Everything is done by the gateway (`gateway/backup.py`); this only asks.
final class BackupPanel: NSObject {
    private var contentsLabel: NSTextField!
    private var lastLabel: NSTextField!
    private var statusLabel: NSTextField!
    private var nightlyCheck: NSButton!
    private var timeField: NSTextField!
    private var keepField: NSTextField!
    private var backUpButton: NSButton!
    private var placeChecks: [String: NSButton] = [:]
    private var placeLabels: [String: NSTextField] = [:]

    private static let places: [(kind: String, title: String)] = [
        ("icloud", "iCloud Drive  (KH Backups)"),
        ("ssd", "Attached KH-STUDIO drives  (KH Backups)"),
        ("local", "This Mac  (~/Developer/_backups)"),
    ]

    func rows() -> [NSView] {
        contentsLabel = PageKit.label("Reading…", .secondary, colour: .secondaryLabelColor, wrapping: true)
        lastLabel = PageKit.label("—", .secondary, colour: .secondaryLabelColor, wrapping: true)
        statusLabel = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
        var placeRows: [NSView] = []
        for place in Self.places {
            let check = NSButton(checkboxWithTitle: place.title, target: self, action: #selector(saveSettings))
            let label = PageKit.label("", .caption, colour: .secondaryLabelColor, wrapping: true)
            placeChecks[place.kind] = check
            placeLabels[place.kind] = label
            let stack = NSStackView(views: [check, label])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 2
            placeRows.append(stack)
        }
        nightlyCheck = NSButton(checkboxWithTitle: "Back up every night at", target: self, action: #selector(saveSettings))
        timeField = NSTextField(string: "")
        timeField.placeholderString = "03:00"
        timeField.target = self
        timeField.action = #selector(saveSettings)
        timeField.widthAnchor.constraint(equalToConstant: 64).isActive = true
        keepField = NSTextField(string: "")
        keepField.target = self
        keepField.action = #selector(saveSettings)
        keepField.widthAnchor.constraint(equalToConstant: 48).isActive = true
        backUpButton = PageKit.primaryButton("Back Up Now", target: self, action: #selector(backUpNow), regular: true)

        return [
            PageKit.help(
                "One file with what cannot be recreated: the two signing keys, the API keys (.env and "
                + "credentials), the fleet roster and the voices library. Jobs, generated files and caches "
                + "are left out. The file is not encrypted — only you can read it on this Mac, so keep "
                + "copies somewhere private."
            ),
            PageKit.formRow("Contents", contentsLabel),
            PageKit.formRow("Save to", placeRows),
            PageKit.formRow("Schedule", [
                PageKit.row([nightlyCheck, timeField]),
                PageKit.row([
                    PageKit.label("Keep the last", .secondary, colour: .secondaryLabelColor),
                    keepField,
                    PageKit.label("backups in each place; older ones go to the trash.", .secondary,
                                  colour: .secondaryLabelColor),
                ]),
            ] as [NSView]),
            PageKit.formRow("Last backup", lastLabel),
            PageKit.row([
                backUpButton,
                PageKit.button("Import / Restore…", target: self, action: #selector(importBackup), regular: true),
            ]),
            statusLabel,
        ]
    }

    func reload() {
        GatewayAPI.get("/admin/api/backup") { [weak self] result in
            switch result {
            case .success(let payload): self?.show(payload)
            case .failure(let error):
                self?.contentsLabel?.stringValue = "Could not read the backup settings — \(error.localizedDescription)"
            }
        }
    }

    // MARK: Showing

    private static func size(_ value: Any?) -> String {
        ByteCountFormatter.string(fromByteCount: Int64((value as? Double) ?? Double(value as? Int ?? 0)), countStyle: .file)
    }

    private static func when(_ value: Any?) -> String? {
        guard let seconds = value as? Double else { return nil }
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter.string(from: Date(timeIntervalSince1970: seconds))
    }

    private func show(_ payload: [String: Any]) {
        let settings = payload["settings"] as? [String: Any] ?? [:]
        let chosen = settings["destinations"] as? [String: Bool] ?? [:]
        nightlyCheck.state = (settings["nightly"] as? Bool ?? false) ? .on : .off
        if timeField.currentEditor() == nil { timeField.stringValue = settings["time"] as? String ?? "03:00" }
        if keepField.currentEditor() == nil { keepField.stringValue = "\(settings["keep"] as? Int ?? 10)" }

        if let included = payload["included"] as? [String: Any] {
            let files = included["files"] as? Int ?? 0
            var text = "\(files) files, \(Self.size(included["bytes"])) — signing keys, API keys, fleet roster, voices"
            if let missing = included["missing"] as? [String], !missing.isEmpty {
                text += "\nNot on this Mac (skipped): " + missing.joined(separator: ", ")
            }
            contentsLabel.stringValue = text
        }

        let rows = payload["destinations"] as? [[String: Any]] ?? []
        for place in Self.places {
            placeChecks[place.kind]?.state = (chosen[place.kind] ?? true) ? .on : .off
            let mine = rows.filter { ($0["kind"] as? String) == place.kind }
            var lines: [String] = []
            for row in mine {
                let name = place.kind == "ssd" ? "\(row["label"] as? String ?? "Drive"): " : ""
                if !(row["available"] as? Bool ?? false) {
                    let last = Self.when(row["last_success_at"]).map { ", last copy \($0)" } ?? ""
                    lines.append(place.kind == "ssd" ? "\(name)not attached\(last)"
                                 : "iCloud Drive is not turned on for this Mac.")
                } else if let error = row["last_error"] as? String {
                    lines.append("\(name)last attempt failed — \(error)")
                } else if let last = Self.when(row["last_success_at"]) {
                    let count = row["count"] as? Int ?? 0
                    lines.append("\(name)last backup \(last), \(Self.size(row["last_bytes"])) · \(count) kept")
                } else {
                    lines.append("\(name)no backup yet")
                }
            }
            if place.kind == "ssd" && !mine.contains(where: { $0["available"] as? Bool ?? false }) {
                lines.insert("No KH-STUDIO drive is attached; the next backup after one is plugged in copies to it.", at: 0)
            }
            placeLabels[place.kind]?.stringValue = lines.joined(separator: "\n")
        }

        let running = payload["running"] as? Bool ?? false
        backUpButton.isEnabled = !running
        if let last = payload["last_run"] as? [String: Any] {
            let at = Self.when(last["started_at"]) ?? "—"
            let how = (last["reason"] as? String) == "nightly" ? "nightly" : "by hand"
            if last["ok"] as? Bool ?? false {
                lastLabel.stringValue = "\(at) (\(how)) — \(last["name"] as? String ?? ""), \(Self.size(last["bytes"]))"
            } else {
                lastLabel.stringValue = "\(at) (\(how)) — not everywhere: \(last["error"] as? String ?? "see the places above")"
            }
        } else {
            lastLabel.stringValue = running ? "Backing up…" : "Never"
        }
    }

    // MARK: Actions

    @objc private func saveSettings() {
        var destinations: [String: Bool] = [:]
        for (kind, check) in placeChecks { destinations[kind] = check.state == .on }
        var body: [String: Any] = ["nightly": nightlyCheck.state == .on, "destinations": destinations]
        let time = timeField.stringValue.trimmingCharacters(in: .whitespaces)
        if !time.isEmpty { body["time"] = time }
        if let keep = Int(keepField.stringValue.trimmingCharacters(in: .whitespaces)) { body["keep"] = keep }
        GatewayAPI.put("/admin/api/settings/backup", body: body) { [weak self] result in
            switch result {
            case .success(let payload):
                self?.statusLabel.stringValue = "Saved."
                self?.show(payload)
            case .failure(let error):
                self?.statusLabel.stringValue = "Not saved — \(error.localizedDescription)"
                self?.reload()
            }
        }
    }

    @objc private func backUpNow() {
        backUpButton.isEnabled = false
        statusLabel.stringValue = "Backing up…"
        GatewayAPI.post("/admin/api/backup/run", body: [:], timeout: 300) { [weak self] result in
            guard let self else { return }
            self.backUpButton.isEnabled = true
            switch result {
            case .failure(let error):
                self.statusLabel.stringValue = "Backup failed — \(error.localizedDescription)"
            case .success(let payload):
                let run = payload["result"] as? [String: Any] ?? [:]
                let places = (run["destinations"] as? [String: [String: Any]] ?? [:]).values
                let saved = places.filter { $0["ok"] as? Bool ?? false }.compactMap { $0["label"] as? String }.sorted()
                self.statusLabel.stringValue = (run["ok"] as? Bool ?? false)
                    ? "Backed up to \(saved.joined(separator: ", "))."
                    : "Backup incomplete — \(run["error"] as? String ?? "see the places above")"
                self.show(payload)
            }
        }
    }

    @objc private func importBackup() {
        let panel = NSOpenPanel()
        panel.title = "Choose a backup to restore"
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        if let gzip = UTType(filenameExtension: "gz") { panel.allowedContentTypes = [gzip] }
        panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Developer/_backups")
        guard panel.runModal() == .OK, let url = panel.url else { return }
        statusLabel.stringValue = "Checking \(url.lastPathComponent)…"
        GatewayAPI.post("/admin/api/backup/inspect", body: ["path": url.path], timeout: 120) { [weak self] result in
            guard let self else { return }
            guard case .success(let info) = result else {
                if case .failure(let error) = result {
                    self.statusLabel.stringValue = "Cannot restore — \(error.localizedDescription)"
                }
                return
            }
            let files = info["files"] as? Int ?? 0
            guard PageKit.confirm(
                "Restore the backup made \(info["created_at"] as? String ?? "?") on \(info["host"] as? String ?? "?")?",
                "\(files) files (\(Self.size(info["bytes"]))): signing keys, API keys, fleet roster and voices. "
                    + "Every current file it replaces is moved to ~/Developer/_trash first; files that are not in "
                    + "the backup are left alone. Restart the gateway and Studio Hub afterwards.",
                ok: "Restore"
            ) else {
                self.statusLabel.stringValue = ""
                return
            }
            GatewayAPI.post("/admin/api/backup/restore", body: ["path": url.path], timeout: 120) { [weak self] result in
                guard let self else { return }
                switch result {
                case .failure(let error):
                    self.statusLabel.stringValue = "Restore failed — \(error.localizedDescription)"
                case .success(let done):
                    let written = done["written"] as? Int ?? 0
                    let safety = (done["safety_copy"] as? String).map { " Replaced files are in \($0)." } ?? ""
                    self.statusLabel.stringValue = written == 0
                        ? "Nothing to restore — every file already matches the backup."
                        : "Restored \(written) file\(written == 1 ? "" : "s").\(safety) Restart the gateway and Studio Hub."
                    self.reload()
                }
            }
        }
    }
}
