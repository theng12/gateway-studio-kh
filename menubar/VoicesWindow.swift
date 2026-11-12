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
import UniformTypeIdentifiers

final class VoicesSection: NSObject, AppSection, NSTableViewDataSource, NSTableViewDelegate,
                           NSSearchFieldDelegate {
    static let shared = VoicesSection()

    /// The provider id this Mac's own reference clips are published under.
    ///
    /// Asked of a row's **`provider_id`**, never of its `id`. The published id is
    /// *composed* from the provider id, so `id.hasPrefix("studio-hub-local")` gives
    /// the right answer by coincidence of how ids are spelled — and would go wrong
    /// the day a location or a cloud provider is named something that starts the
    /// same way. Which provider a voice belongs to is a field; it is not a prefix.
    private static let localProviderId = "studio-hub-local"

    let title = "Voices"
    let symbol = "waveform"
    // Browsing is interactive: a timed refresh would rebuild the table
    // under the cursor and fight the checkbox just clicked.
    let refreshesAutomatically = false

    private var table: NSTableView!
    private var providerPopup: NSPopUpButton!
    private var libraryCheck: NSButton!
    private var searchField: NSSearchField!
    private var addByIdField: NSTextField!
    private var addByOwnerField: NSTextField!
    private var summaryLabel: NSTextField!
    private var detailLabel: NSTextField!
    /// "Add to My Account" in the More menu: only meaningful while searching the provider library.
    private var addMenuItem: NSMenuItem?
    private var rows: [[String: Any]] = []
    private var providers: [(id: String, name: String, kind: String)] = []
    private var libraryMode = false
    private var sound: NSSound?
    private var searchTimer: Timer?
    /// A slow search must not repaint over a newer one.
    private var reloadGeneration = 0

    /// The section's content, built once and hosted by the main window.
    /// Lazy so it can be built by a method on self.
    lazy var view: NSView = buildContent()

    func activated() {
        reload()
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let root = PageKit.page()

        // Stocking the library is a different act from ticking voices on and off, so it is the page's one
        // primary action, beside Refresh in the heading rather than among the selection actions.
        PageKit.addFull(PageKit.heading(
            "Voices",
            "Only the voices you enable here are offered to Story Studio.",
            actions: [
                PageKit.primaryButton("Add Voices…", target: self, action: #selector(pickVoices)),
                PageKit.button("Refresh", target: self, action: #selector(refreshVoices)),
            ]
        ), to: root)

        providerPopup = NSPopUpButton()
        providerPopup.target = self
        providerPopup.action = #selector(filtersChanged)
        providerPopup.controlSize = .small

        searchField = NSSearchField()
        searchField.placeholderString = "Search voices…"
        searchField.delegate = self
        searchField.controlSize = .small
        PageKit.softWidth(searchField, 220, min: 120)

        libraryCheck = NSButton(
            checkboxWithTitle: "Search the provider's public library",
            target: self, action: #selector(filtersChanged)
        )
        libraryCheck.controlSize = .small

        // ---- toolbar: what to show, then what to do with the rows that are ticked.
        //
        // Bulk controls belong above the table, not in a footer: the table is the part that scrolls, so a
        // footer moved Enable/Disable off screen exactly when a long list made them most useful.
        // "Select all shown" acts on the filtered rows the owner is looking at, which is the point of search.
        // The rarer per-voice acts live in one "More" menu so the row fits the window at its minimum width.
        summaryLabel = NSTextField(labelWithString: "Loading…")
        summaryLabel.font = TextRole.secondary.font
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.lineBreakMode = .byTruncatingTail
        summaryLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let filters = PageKit.toolbar([providerPopup, searchField, libraryCheck, NSView(), summaryLabel])
        PageKit.addFull(filters, to: root)

        let actions = PageKit.toolbar([
            button("Select All Shown", #selector(selectAllShown)),
            button("Select None", #selector(selectNone)),
            button("Enable Selected", #selector(enableSelected)),
            button("Disable Selected", #selector(disableSelected)),
            NSView(),
            moreMenu(),
        ])
        actions.spacing = DS.Space.s
        PageKit.addFull(actions, to: root)

        // ---- Add a voice by its id, without searching for it.
        //
        // The library search only finds what the provider will *search* for, and a
        // voice the owner already knows the id of — from a share link, or another
        // app's config — could not be saved at all: `addSelected` only ever acts on
        // rows in the table. The gateway API has always accepted an explicit id, so
        // this is the field that was missing rather than the capability.
        addByIdField = NSTextField()
        addByIdField.placeholderString = "Voice ID (from a share link)"
        addByIdField.controlSize = .small
        PageKit.softWidth(addByIdField, 240, min: 120)

        // ElevenLabs is the one provider that needs more than an id: its library
        // import route is `/v1/voices/add/{owner_id}/{voice_id}`.
        addByOwnerField = NSTextField()
        addByOwnerField.placeholderString = "Owner ID (ElevenLabs only)"
        addByOwnerField.controlSize = .small
        PageKit.softWidth(addByOwnerField, 200, min: 120)

        let byIdBar = PageKit.toolbar([
            PageKit.label("Add by ID", .secondary, colour: .secondaryLabelColor),
            addByIdField, addByOwnerField,
            button("Add by ID", #selector(addByID)), NSView(),
        ])
        PageKit.addFull(byIdBar, to: root)

        table = EscapeClearingTableView()
        table.dataSource = self
        table.delegate = self
        table.rowHeight = DS.Size.row
        // Separators only, no zebra; the card around the table already says where it ends.
        table.usesAlternatingRowBackgroundColors = false
        table.backgroundColor = .clear
        table.allowsMultipleSelection = true
        // Right-click a row to remove it; acts on the clicked row (`removeClicked`).
        let rowMenu = NSMenu()
        let removeItem = NSMenuItem(title: "Remove Voice…", action: #selector(removeClicked), keyEquivalent: "")
        removeItem.target = self
        rowMenu.addItem(removeItem)
        table.menu = rowMenu
        table.style = .fullWidth
        table.columnAutoresizingStyle = .noColumnAutoresizing
        table.intercellSpacing = NSSize(width: DS.Space.xs, height: 0)
        // The first two columns hold a checkbox and a play button; Voice and Description take the spare width.
        for (id, title, width, flexes) in [
            ("on", "", 34, false),
            ("preview", "", 30, false),
            ("name", "Voice", 210, true),
            // Length and import date: without them two voices with one name look identical.
            ("clip", "Clip", 150, true),
            ("language", "Language", 80, false),
            ("fleet", "Fleet", 80, false),
            ("details", "Description", 280, true),
            ("source", "Source", 80, false),
        ] as [(String, String, Int, Bool)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            column.title = title
            column.width = CGFloat(width)
            column.minWidth = flexes ? 120 : CGFloat(width)
            table.addTableColumn(column)
        }

        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        let card = TableCard()
        card.hold(table: scroll)
        // Voice and Description take the spare width; the checkbox, play button and short columns keep theirs.
        card.onLayout = { [weak self] in
            guard let self else { return }
            PageKit.fitColumns(of: self.table, in: scroll, flex: ["name": (1, 210), "clip": (0.5, 150), "details": (1.4, 280), "language": (0.3, 80), "fleet": (0.3, 80), "source": (0.3, 80)])
        }
        PageKit.addFull(card, to: root)
        // A *required* floor was the whole reason this page could grow the window: the natural height came
        // to ~630pt against ~532pt of content at the 860x560 minimum, and AppKit satisfies a required
        // height by enlarging the window — measured at 1162pt on the Fleet page once already. The table is
        // the thing that should absorb spare space, so its floor is only there to stop it collapsing.
        card.heightAnchor.constraint(greaterThanOrEqualToConstant: 140).isActive = true

        detailLabel = PageKit.help(
            "Local voices are yours and are on by default. Provider voices are opt-in — "
            + "tick the ones Story Studio should offer.")
        PageKit.addFull(detailLabel, to: root)

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

    /// The per-voice acts that are used less often, in one pull-down menu.
    private func moreMenu() -> NSView {
        let more = NSPopUpButton(frame: .zero, pullsDown: true)
        more.controlSize = .small
        more.addItem(withTitle: "More")
        // Permission to clone and distribute is granted deliberately, never by a stray click, and it is
        // per voice because some voices are private.
        func add(_ title: String, _ action: Selector) {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            more.menu?.addItem(item)
        }
        add("Allow Fleet Sharing…", #selector(allowSharing))
        add("Share with Fleet", #selector(shareWithFleet))
        add("Make Private", #selector(makePrivate))
        more.menu?.addItem(.separator())
        add("Remove Voice…", #selector(removeSelected))
        add("Restore Removed Voice…", #selector(restoreRemoved))
        more.menu?.addItem(.separator())
        add("Add to My Account", #selector(addSelected))
        addMenuItem = more.menu?.items.last
        // Three different things can happen to a recording — used whole, cut to a shorter aligned window,
        // or refused — and from the outside they look the same. This is where the app says which, per model.
        add("Reference…", #selector(checkReference))
        add("Language and Transcript…", #selector(editTranscript))
        add("Stop Preview", #selector(stopPreview))
        return more
    }

    private func button(_ title: String, _ action: Selector) -> NSButton {
        PageKit.button(title, target: self, action: action)
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

    /// Escape clears the search field, in this page's own context (owner rule 2026-11-12):
    /// the keyboard never leaves the owner in a filtered list with no way out but the pointer.
    func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
        guard commandSelector == #selector(NSResponder.cancelOperation(_:)) else { return false }
        if !searchField.stringValue.isEmpty {
            searchField.stringValue = ""
            searchChanged()
        }
        return true
    }

    @objc private func refreshVoices() {
        summaryLabel.stringValue = "Discovering voices…"
        GatewayAPI.post("/admin/api/voices/refresh") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.textColor = Health.warn.text
                self.summaryLabel.stringValue = "Could not refresh voices — \(error.localizedDescription)"
            case .success(let payload):
                let failures = (payload["outcomes"] as? [String: Any] ?? [:]).compactMap { provider, value -> String? in
                    guard let outcome = value as? [String: Any],
                          let error = outcome["error"] as? String,
                          !error.isEmpty else { return nil }
                    return "\(provider): \(error)"
                }
                if !failures.isEmpty {
                    self.summaryLabel.textColor = Health.warn.text
                    self.summaryLabel.stringValue =
                        "Could not discover voices — \(failures.joined(separator: "; "))"
                } else {
                    self.reload()
                }
            }
        }
    }

    /// Stock the catalogue from audio files, or a folder, the owner picks.
    ///
    /// Files are the common case — one clip is one voice — so they can be picked one by one
    /// (⌘-click for several). Picking only a folder still works the way it always did: a
    /// folder of per-voice folders, or a folder of loose audio files.
    @objc private func pickVoices() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = [.audio]
        panel.prompt = "Add Voices"
        panel.message =
            "Pick one or more audio files — each becomes a voice — or a folder of them."
        // Off for a language the transcriber cannot understand (Khmer): those clips clone from
        // their audio alone, and wrong words would make VoxCPM2 read them.
        let transcribe = NSButton(checkboxWithTitle: "Transcribe the clips (turn off for Khmer and other "
                                  + "languages the transcriber cannot understand)", target: nil, action: nil)
        transcribe.state = .on
        panel.accessoryView = transcribe
        panel.isAccessoryViewDisclosed = true
        guard panel.runModal() == .OK, !panel.urls.isEmpty else { return }

        var body: [String: Any] = ["annotate": true,
                                   "include_reference_transcript": transcribe.state == .on]
        if panel.urls.count == 1, panel.urls[0].hasDirectoryPath {
            body["folder"] = panel.urls[0].path
        } else {
            body["files"] = panel.urls.map(\.path)
        }
        addVoices(body, earlier: nil)
    }

    /// Post one add, then ask about every name that is already taken and post the answers.
    ///
    /// The gateway adds nothing under a taken name on its own: a second "Chris" that looks
    /// exactly like the first is how the duplicates happened. `earlier` is the first pass,
    /// whose additions the second pass sees again as "already here" and must not report so.
    private func addVoices(_ body: [String: Any], earlier: [String: Any]?) {
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.stringValue = "Adding voices…"
        GatewayAPI.post("/admin/api/voices/add", body: body, timeout: 120) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.textColor = Health.warn.text
                self.summaryLabel.stringValue = "Could not add them — \(error.localizedDescription)"
            case .success(let payload):
                let conflicts = payload["conflicts"] as? [[String: Any]] ?? []
                if earlier == nil, !conflicts.isEmpty {
                    self.reload()
                    var resolutions: [String: String] = [:]
                    for conflict in conflicts {
                        guard let source = conflict["source"] as? String else { continue }
                        resolutions[source] = self.askAboutTakenName(conflict)
                    }
                    var again = body
                    again["resolutions"] = resolutions
                    self.addVoices(again, earlier: payload)
                    return
                }
                var imported = payload["imported"] as? [[String: Any]] ?? []
                var skipped = payload["skipped"] as? [[String: Any]] ?? []
                if let earlier {
                    let first = earlier["imported"] as? [[String: Any]] ?? []
                    let firstSources = Set(first.compactMap { $0["source"] as? String })
                    imported = first + imported
                    skipped = skipped.filter { !firstSources.contains($0["source"] as? String ?? "") }
                }
                self.reportAdded(imported: imported, skipped: skipped,
                                 notes: (earlier?["notes"] as? [String] ?? [])
                                     + (payload["notes"] as? [String] ?? []),
                                 annotation: payload["annotation"] as? [String: Any])
            }
        }
    }

    /// "A voice named X already exists": keep both, replace the existing clip, or skip.
    /// Returns the gateway's answer for that clip (`keep_both`, `replace:<id>`, `skip`).
    private func askAboutTakenName(_ conflict: [String: Any]) -> String {
        let name = conflict["name"] as? String ?? "?"
        let existing = conflict["existing"] as? [[String: Any]] ?? []
        let alert = NSAlert()
        alert.messageText = conflict["message"] as? String ?? "A voice named \u{201C}\(name)\u{201D} already exists."
        alert.informativeText =
            "The clip you picked (\(Self.clipLabel(conflict["duration_seconds"], nil))) is a different "
            + "recording from the existing " + existing.map {
                "\u{201C}\($0["display_name"] as? String ?? name)\u{201D} (\(Self.clipLabel($0["duration_seconds"], $0["imported_at"])))"
            }.joined(separator: " and ")
            + ".\n\nReplacing keeps that voice's id, so channels using it keep working; its old clip "
            + "is kept in the gateway's voice backups and the new one is transcribed again."
        alert.addButton(withTitle: "Keep Both (name it \(conflict["suggested_name"] as? String ?? "\(name) 2"))")
        for voice in existing {
            alert.addButton(withTitle: existing.count == 1
                ? "Replace the Clip of the Existing \(name)"
                : "Replace \(name) (\(Self.clipLabel(voice["duration_seconds"], voice["imported_at"])))")
        }
        alert.addButton(withTitle: "Skip")
        alert.closeOnEscape()
        let index = alert.runModal().rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
        if index == 0 { return "keep_both" }
        if index >= 1, index <= existing.count, let id = existing[index - 1]["id"] as? String {
            return "replace:\(id)"
        }
        return "skip"
    }

    /// "12s · 21 Sep 2026": what tells two voices with one name apart.
    static func clipLabel(_ seconds: Any?, _ importedAt: Any?) -> String {
        var bits: [String] = []
        if let seconds = seconds as? Double { bits.append("\(Int(seconds.rounded()))s") }
        if let stamp = importedAt as? Double {
            bits.append("added " + DateFormatter.localizedString(
                from: Date(timeIntervalSince1970: stamp), dateStyle: .medium, timeStyle: .none))
        }
        return bits.isEmpty ? "length not known" : bits.joined(separator: " · ")
    }

    /// Say what was added, then follow the transcription that makes them usable.
    private func reportAdded(
        imported: [[String: Any]], skipped: [[String: Any]], notes: [String],
        annotation: [String: Any]?
    ) {
        reload()
        let names = imported.compactMap { $0["display_name"] as? String }
        var line = names.isEmpty
            ? "Nothing new was added."
            : "Added \(names.count) voice\(names.count == 1 ? "" : "s"): "
              + names.prefix(4).joined(separator: ", ")
              + (names.count > 4 ? " +\(names.count - 4) more" : "")
        // The same recording again is not a problem, just a fact; anything else skipped is named.
        let duplicates = skipped.filter { $0["duplicate"] as? Bool == true }
        let others = skipped.filter { $0["duplicate"] as? Bool != true }
        if !duplicates.isEmpty {
            line += " · \(duplicates.count) already here (same recording): "
                + duplicates.compactMap { $0["name"] as? String }.prefix(3).joined(separator: ", ")
        }
        if !others.isEmpty {
            line += " · skipped " + others.compactMap { $0["name"] as? String }
                .prefix(3).joined(separator: ", ")
        }
        if !notes.isEmpty {
            line += " · " + (notes.count == 1 ? "one note" : "\(notes.count) notes")
        }
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.stringValue = line

        // Not a failure, and not silence either: the common one is "that name is
        // already here with different audio, so this was added as a second voice
        // rather than replacing it". The owner has to choose which to keep, and he
        // cannot choose what he is not told.
        if !notes.isEmpty {
            let alert = NSAlert()
            alert.messageText = notes.count == 1
                ? "One thing to know about that import"
                : "\(notes.count) things to know about that import"
            alert.informativeText = notes.joined(separator: "\n\n")
            alert.alertStyle = .informational
            alert.addButton(withTitle: "OK")
            alert.closeOnEscape()
            alert.runModal()
        }

        guard let annotation else { return }
        if let error = annotation["error"] as? String, !error.isEmpty {
            // The usual one: no Hub, so there is nothing to transcribe with. A
            // voice without segments cannot clone, so this is not cosmetic.
            summaryLabel.textColor = Health.warn.text
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
            guard let self else { return }
            guard case .success(let state) = result else {
                if case .failure(let error) = result {
                    self.summaryLabel.textColor = Health.warn.text
                    self.summaryLabel.stringValue =
                        "Could not check transcription progress — \(error.localizedDescription). Retrying…"
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
                    self?.followAnnotation()
                }
                return
            }
            let done = state["done"] as? Int ?? 0
            let total = state["total"] as? Int ?? 0
            let current = state["current"] as? String ?? ""

            if state["running"] as? Bool == true {
                self.summaryLabel.textColor = .secondaryLabelColor
                self.summaryLabel.stringValue =
                    "Making them usable — transcribing \(done + 1) of \(total)"
                    + (current.isEmpty ? "…" : ": \(current)")
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
                    self?.followAnnotation()
                }
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
                self.summaryLabel.textColor = Health.warn.text
                let why = failed.prefix(2).map {
                    "\($0["name"] as? String ?? "?") (\($0["detail"] as? String ?? "?"))"
                }.joined(separator: "; ")
                self.summaryLabel.stringValue =
                    "\(ok) transcribed, \(failed.count) could not be: \(why)"
            }
        }
    }

    private func selectedVoiceIDs() -> Set<String> {
        guard table != nil else { return [] }
        return Set(table.selectedRowIndexes.compactMap {
            $0 < rows.count ? rows[$0]["id"] as? String : nil
        })
    }

    func reload(
        preservingSelection selection: Set<String>? = nil,
        completion: (() -> Void)? = nil
    ) {
        let preserved = selection ?? selectedVoiceIDs()
        let providerId = selectedProviderId()
        let library = libraryCheck.state == .on && supportsLibrary(providerId)
        addMenuItem?.isEnabled = library
        reloadGeneration += 1
        let generation = reloadGeneration

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
            guard self.reloadGeneration == generation else { return completion?() ?? () }
            self.libraryMode = library
            switch result {
            case .success(let payload):
                self.rows = payload["voices"] as? [[String: Any]] ?? []
                self.loadProviders(from: payload)
                self.summaryLabel.textColor = .secondaryLabelColor
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
                self.summaryLabel.textColor = Health.warn.text
                self.summaryLabel.stringValue = "Could not load voices — \(error.localizedDescription)"
            }
            self.table.reloadData()
            var indexes = IndexSet()
            for (index, row) in self.rows.enumerated() {
                if let id = row["id"] as? String, preserved.contains(id) {
                    indexes.insert(index)
                }
            }
            self.table.selectRowIndexes(indexes, byExtendingSelection: false)
            completion?()
        }
    }

    private func loadProviders(from payload: [String: Any]) {
        guard let list = payload["providers"] as? [[String: Any]] else { return }
        let fresh = list.compactMap { row -> (id: String, name: String, kind: String)? in
            guard let id = row["id"] as? String, let name = row["name"] as? String else { return nil }
            return (id: id, name: name, kind: row["kind"] as? String ?? "")
        }
        // Rebuilt whenever the set changes, so a provider added after launch appears —
        // keeping whichever one is selected.
        guard fresh.map({ $0.id }) != providers.map({ $0.id }) else { return }
        let chosen = selectedProviderId()
        providers = fresh
        defer {
            if let item = providerPopup.itemArray.first(where: { $0.representedObject as? String == chosen }) {
                providerPopup.select(item)
            }
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

    /// Select every row currently listed — which is the filtered set, not the whole
    /// library. The owner searches or narrows by provider, then this plus Enable
    /// selected is the bulk act the page was missing.
    @objc private func selectAllShown() {
        guard !rows.isEmpty else { return }
        table.selectRowIndexes(IndexSet(0..<rows.count), byExtendingSelection: false)
    }

    @objc private func selectNone() {
        table.deselectAll(nil)
    }

    private func setEnabled(_ enabled: Bool) {
        let ids = selectedRows().compactMap { $0["id"] as? String }
        guard !ids.isEmpty else { return }
        let selection = Set(ids)
        GatewayAPI.post("/admin/api/voices/enabled", body: ["items": ids, "enabled": enabled]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.reload(preservingSelection: selection) {
                    self.summaryLabel.textColor = Health.warn.text
                    self.summaryLabel.stringValue =
                        "Could not change those voices — \(error.localizedDescription)"
                }
            case .success(let payload):
                let changed = payload["changed"] as? Int ?? 0
                self.reload(preservingSelection: selection) {
                    if changed != ids.count {
                        self.summaryLabel.textColor = Health.warn.text
                        self.summaryLabel.stringValue =
                            "Changed \(changed) of \(ids.count) voices; the rest were not changed."
                    }
                }
            }
        }
    }

    /// Push allowed voices to every location, and say what happened at each.
    ///
    /// Controllers only — the gateway never talks to nodes, and each controller
    /// fans out to its own. A node that is switched off is reported as waiting, not
    /// as a failure: calling it an error would send someone to a site that was
    /// never broken.
    @objc private func shareWithFleet() {
        let ids = selectedRows()
            .filter { ($0["provider_id"] as? String) == Self.localProviderId }
            .compactMap { $0["id"] as? String }
        guard !ids.isEmpty else {
            summaryLabel.textColor = Health.warn.text
            summaryLabel.stringValue = "Select the voices to share first."
            return
        }
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.stringValue = "Sharing \(ids.count) voice\(ids.count == 1 ? "" : "s") with the fleet…"
        GatewayAPI.post("/admin/api/voices/share", body: ["items": ids]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.textColor = Health.warn.text
                self.summaryLabel.stringValue = "Could not share them — \(error.localizedDescription)"
            case .success(let payload):
                let results = payload["results"] as? [[String: Any]] ?? []
                let ok = results.filter { $0["ok"] as? Bool == true }
                let bad = results.filter { $0["ok"] as? Bool == false }
                if let first = ok.first {
                    self.summaryLabel.textColor = .secondaryLabelColor
                    var line = "Shared \(ok.count) of \(results.count) transfers: "
                        + "\(first["voice"] as? String ?? "?") at \(first["location"] as? String ?? "?")"
                        + " — \(first["detail"] as? String ?? "")"
                    if let segments = first["segments"] as? Int { line += " · \(segments) timings kept" }
                    if !bad.isEmpty {
                        let named = bad.map {
                            "\($0["location"] as? String ?? "?") (\($0["detail"] as? String ?? "no reason given"))"
                        }.joined(separator: "; ")
                        line += " · could not take it: \(named)"
                        self.summaryLabel.textColor = Health.warn.text
                    }
                    self.summaryLabel.stringValue = line
                } else {
                    self.summaryLabel.textColor = Health.warn.text
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
        let chosen = selectedRows().filter { ($0["provider_id"] as? String) == Self.localProviderId }
        let ids = chosen.compactMap { $0["id"] as? String }
        guard !ids.isEmpty else {
            summaryLabel.textColor = Health.warn.text
            summaryLabel.stringValue =
                "Select the voices to share first — only this Mac's own voices can be shared."
            return
        }
        // The permission state is published at the top level of the row, from the
        // voice's own folder metadata — not inside `raw`, which is the provider's
        // answer and knows nothing about what the owner allowed.
        let already = chosen.filter {
            ($0["permission_acknowledged"] as? Bool) == true
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
        alert.addButton(withTitle: "Allow Sharing")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        GatewayAPI.post("/admin/api/voices/shared",
                        body: ["items": ids, "acknowledged": true]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.textColor = Health.warn.text
                self.summaryLabel.stringValue = "Could not record it — \(error.localizedDescription)"
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
        let ids = selectedRows()
            .filter { ($0["provider_id"] as? String) == Self.localProviderId }
            .compactMap { $0["id"] as? String }
        guard !ids.isEmpty else { return }
        GatewayAPI.post("/admin/api/voices/shared",
                        body: ["items": ids, "acknowledged": false]) { [weak self] result in
            guard let self else { return }
            if case .failure(let error) = result {
                self.summaryLabel.textColor = Health.warn.text
                self.summaryLabel.stringValue = "Could not change it — \(error.localizedDescription)"
            } else {
                self.summaryLabel.textColor = .secondaryLabelColor
                self.summaryLabel.stringValue =
                    "\(ids.count) voice\(ids.count == 1 ? " is" : "s are") private again."
            }
            self.reload()
        }
    }



    // MARK: Remove and restore

    /// A voice id carries `::` and may carry `/`; this client builds URLs by concatenation.
    private static func pathComponent(_ voiceId: String) -> String {
        let unreserved = CharacterSet(charactersIn:
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return voiceId.addingPercentEncoding(withAllowedCharacters: unreserved) ?? voiceId
    }

    @objc private func removeSelected() {
        let chosen = selectedRows()
        if chosen.count == 1 { confirmRemove(chosen[0]); return }
        // Several at once: provider voices only (the old ElevenLabs list, 2026-10-24). This Mac's
        // own voices go one at a time, because each one's clip moves to the backups.
        guard chosen.count > 1,
              !chosen.contains(where: { ($0["provider_id"] as? String) == Self.localProviderId }) else {
            report(chosen.isEmpty ? "Select the voices to remove."
                                  : "Remove this Mac's own voices one at a time.", ok: false)
            return
        }
        let ids = chosen.compactMap { $0["id"] as? String }
        let names = chosen.compactMap { $0["display_name"] as? String }
        let alert = NSAlert()
        alert.messageText = "Remove these \(ids.count) voices from this Gateway?"
        alert.informativeText = names.prefix(12).joined(separator: ", ")
            + (names.count > 12 ? ", and \(names.count - 12) more" : "")
            + "\n\nThey leave this list and Story Studio. Nothing is deleted at the provider, and "
            + "More → Restore Removed Voice… brings any of them back."
        alert.addButton(withTitle: "Remove")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        removeProviderVoices(ids, confirmed: false)
    }

    private func removeProviderVoices(_ ids: [String], confirmed: Bool) {
        GatewayAPI.post("/admin/api/voices/remove", body: ["items": ids, "confirm_in_use": confirmed]) {
            [weak self] result in
            guard let self else { return }
            if !confirmed, case .failure(let error) = result,
               case GatewayAPIError.refused(409, let detail)? = error as? GatewayAPIError {
                // Some were narrated lately: say which, and ask once more.
                let again = NSAlert()
                again.alertStyle = .warning
                again.messageText = "Some of these voices were used for narration lately"
                again.informativeText = detail
                    + "\n\nA Story Studio channel still set to one of them will fail its next narration."
                again.addButton(withTitle: "Remove Anyway")
                again.addButton(withTitle: "Cancel")
                guard again.runModal() == .alertFirstButtonReturn else { return }
                self.removeProviderVoices(ids, confirmed: true)
                return
            }
            self.reload {
                switch result {
                case .failure(let error):
                    self.report("Could not remove them — \(error.localizedDescription)", ok: false)
                case .success:
                    self.report("Removed \(ids.count) voices. More → Restore Removed Voice… brings any back.",
                                ok: true)
                }
            }
        }
    }

    /// The row's context menu acts on the row that was right-clicked, not the selection.
    @objc private func removeClicked() {
        let row = table.clickedRow
        guard row >= 0, row < rows.count else { return }
        confirmRemove(rows[row])
    }

    /// Remove one voice after saying exactly which one — and, when Story Studio has used it
    /// lately, asking a second time and naming who used it.
    ///
    /// This Mac's own voices are moved to the gateway's voice backups, never deleted, and
    /// More → Restore Removed Voice… brings them back under the same id.
    private func confirmRemove(_ voice: [String: Any]) {
        guard let voiceId = voice["id"] as? String else { return }
        let name = voice["display_name"] as? String ?? voiceId
        let local = (voice["provider_id"] as? String) == Self.localProviderId
        let encoded = Self.pathComponent(voiceId)

        let first = NSAlert()
        first.messageText = "Remove \u{201C}\(name)\u{201D}?"
        first.informativeText = (local
            ? "Clip: \(Self.clipLabel(voice["duration_seconds"], voice["imported_at"]))\nID: \(voiceId)\n\n"
              + "It leaves this list and Story Studio. Its clip, transcript and timings move to the "
              + "gateway's voice backups; More → Restore Removed Voice… brings it back under the same id."
            : "ID: \(voiceId)\n\nIt is removed from this gateway, not from the provider.")
        first.addButton(withTitle: "Remove")
        first.addButton(withTitle: "Cancel")
        guard first.runModal() == .alertFirstButtonReturn else { return }

        GatewayAPI.get("/admin/api/voices/\(encoded)/usage") { [weak self] result in
            guard let self else { return }
            let used = (try? result.get())?["used_by"] as? [[String: Any]] ?? []
            var confirmed = false
            if !used.isEmpty {
                let second = NSAlert()
                second.alertStyle = .warning
                second.messageText = "\u{201C}\(name)\u{201D} was used for narration in the last 30 days"
                second.informativeText = used.map { use in
                    let who = (use["caller"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "an unnamed caller"
                    let when = (use["last_used_at"] as? Double).map {
                        DateFormatter.localizedString(from: Date(timeIntervalSince1970: $0),
                                                      dateStyle: .medium, timeStyle: .short)
                    } ?? "?"
                    return "\(who): \(use["jobs"] as? Int ?? 0) jobs, last \(when)"
                }.joined(separator: "\n")
                    + "\n\nA Story Studio channel still set to this voice will fail its next narration."
                second.addButton(withTitle: "Remove Anyway")
                second.addButton(withTitle: "Cancel")
                guard second.runModal() == .alertFirstButtonReturn else { return }
                confirmed = true
            }
            GatewayAPI.delete("/admin/api/voices/\(encoded)" + (confirmed ? "?confirm_in_use=true" : "")) {
                [weak self] result in
                guard let self else { return }
                self.reload {
                    switch result {
                    case .failure(let error):
                        self.summaryLabel.textColor = Health.warn.text
                        self.summaryLabel.stringValue = "Could not remove \(name) — \(error.localizedDescription)"
                    case .success:
                        self.summaryLabel.textColor = .secondaryLabelColor
                        self.summaryLabel.stringValue = local
                            ? "Removed \(name). Its files are in the gateway's voice backups."
                            : "Removed \(name)."
                    }
                }
            }
        }
    }

    /// Bring back a voice that was removed, under its own id.
    @objc private func restoreRemoved() {
        GatewayAPI.get("/admin/api/voices/removed") { [weak self] result in
            guard let self else { return }
            let payload = (try? result.get()) ?? [:]
            let removed = payload["voices"] as? [[String: Any]] ?? []
            let providerVoices = payload["provider_voices"] as? [[String: Any]] ?? []
            guard !removed.isEmpty || !providerVoices.isEmpty else {
                self.summaryLabel.textColor = .secondaryLabelColor
                self.summaryLabel.stringValue = "No removed voices to restore."
                return
            }
            let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 380, height: 26))
            for voice in removed {
                let when = (voice["removed_at"] as? Double).map {
                    DateFormatter.localizedString(from: Date(timeIntervalSince1970: $0),
                                                  dateStyle: .medium, timeStyle: .short)
                } ?? "?"
                picker.addItem(withTitle: "\(voice["display_name"] as? String ?? "?") — "
                               + "\(Self.clipLabel(voice["duration_seconds"], voice["imported_at"])) — removed \(when)")
                picker.lastItem?.representedObject = ["backup": voice["backup"] ?? ""]
            }
            for voice in providerVoices {
                picker.addItem(withTitle: "\(voice["display_name"] as? String ?? "?") — "
                               + "\(voice["provider_id"] as? String ?? "provider")")
                picker.lastItem?.representedObject = ["voice_id": voice["id"] ?? ""]
            }
            let alert = NSAlert()
            alert.messageText = "Restore a removed voice"
            alert.informativeText = "It comes back under the same id, so channels that used it work again."
            alert.accessoryView = picker
            alert.addButton(withTitle: "Restore")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn,
                  let body = picker.selectedItem?.representedObject as? [String: Any] else { return }
            GatewayAPI.post("/admin/api/voices/restore", body: body) { [weak self] result in
                guard let self else { return }
                self.reload {
                    switch result {
                    case .failure(let error):
                        self.summaryLabel.textColor = Health.warn.text
                        self.summaryLabel.stringValue = "Could not restore it — \(error.localizedDescription)"
                    case .success(let payload):
                        self.summaryLabel.textColor = .secondaryLabelColor
                        self.summaryLabel.stringValue =
                            "Restored \(payload["display_name"] as? String ?? "the voice")."
                    }
                }
            }
        }
    }

    /// Save a provider voice the owner already has the id of.
    ///
    /// The library search can only find what the provider will search for, so a
    /// voice known from a share link had no way in. The gateway API has always
    /// accepted an explicit id — this is the missing field, not a missing
    /// capability.
    ///
    /// A provider must be chosen: an id alone is ambiguous, and "All providers"
    /// is not a provider. ElevenLabs additionally needs the library voice's
    /// **owner id**, because its import route is
    /// `/v1/voices/add/{owner_id}/{voice_id}` — refusing locally with that
    /// sentence beats a 422 from the far end.
    @objc private func addByID() {
        let voiceId = addByIdField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !voiceId.isEmpty else {
            report("Type the voice id first.", ok: false)
            return
        }
        let providerId = selectedProviderId()
        guard !providerId.isEmpty else {
            report("Choose the provider that owns that voice — an id alone does not say.", ok: false)
            return
        }
        if providerKind(providerId) == "elevenlabs"
            && addByOwnerField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            report("ElevenLabs needs the voice's owner id as well — it is in the same share link.", ok: false)
            return
        }
        var body: [String: Any] = ["provider_id": providerId, "provider_voice_id": voiceId]
        let owner = addByOwnerField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !owner.isEmpty { body["owner_id"] = owner }

        summaryLabel.stringValue = "Saving that voice…"
        summaryLabel.textColor = .secondaryLabelColor
        GatewayAPI.post("/admin/api/voices/import", body: body) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report("Could not save that voice — \(error.localizedDescription)", ok: false)
            case .success(let payload):
                let name = ((payload["voice"] as? [String: Any])?["display_name"] as? String) ?? voiceId
                self.addByIdField.stringValue = ""
                self.addByOwnerField.stringValue = ""
                self.report("Saved \(name). It is on — tick it off if you do not want it offered.", ok: true)
                self.refreshVoices()
            }
        }
    }

    /// The kind behind a provider id, for the one place that needs it.
    private func providerKind(_ providerId: String) -> String {
        providers.first(where: { $0.id == providerId })?.kind ?? ""
    }

    /// What will actually happen to this voice's recording on each local model.
    ///
    /// The gateway answers this from the Hub's own catalogue, so every figure in
    /// the report — a model's floor, target and maximum, whether it needs a
    /// transcript — is the Hub's statement rather than ours. Where the Hub states
    /// nothing the report says *unverified* instead of inventing a window.
    @objc private func checkReference() {
        let rows = selectedRows()
        guard rows.count == 1, let voiceId = rows[0]["id"] as? String else {
            report("Pick exactly one voice — this reports on its recording.", ok: false)
            return
        }
        // A voice id carries `::` and may carry `/`, and this client builds URLs
        // by concatenation, so everything outside the unreserved set is escaped.
        let unreserved = CharacterSet(charactersIn:
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        let encoded = voiceId.addingPercentEncoding(withAllowedCharacters: unreserved) ?? voiceId
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.stringValue = "Checking how this recording is used…"
        GatewayAPI.get("/admin/api/voices/\(encoded)/reference") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report("Could not check it — \(error.localizedDescription)", ok: false)
            case .success(let payload):
                self.showReference(payload)
            }
        }
    }

    /// A voice's language label, and whether its reference transcript is sent or it clones
    /// from the audio alone.
    ///
    /// Transcript off is for a recording the transcriber cannot understand (Khmer): wrong words
    /// make VoxCPM2 read them. On sends exactly the words in the box, so they must be what the
    /// clip says. The language is what Story Studio shows beside the voice and searches on.
    @objc private func editTranscript() {
        let rows = selectedRows().filter { ($0["provider_id"] as? String) == Self.localProviderId }
        guard rows.count == 1, let voiceId = rows[0]["id"] as? String else {
            report("Pick exactly one of this Mac's own voices.", ok: false)
            return
        }
        GatewayAPI.get("/admin/api/voices/\(Self.pathComponent(voiceId))/reference") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report("Could not read it — \(error.localizedDescription)", ok: false)
            case .success(let payload):
                self.askTranscript(voiceId, payload)
            }
        }
    }

    private func askTranscript(_ voiceId: String, _ payload: [String: Any]) {
        let clip = payload["clip"] as? [String: Any] ?? [:]
        // A code (km, en) keeps the Language column short; any short name is accepted too.
        let language = NSComboBox(frame: NSRect(x: 0, y: 0, width: 200, height: 26))
        language.addItems(withObjectValues: ["km", "en", "th", "vi", "zh", "ja", "ko", "fr", "es", "de"])
        language.placeholderString = "km for Khmer, en for English"
        language.stringValue = payload["language"] as? String ?? ""
        let languageRow = NSStackView(views: [NSTextField(labelWithString: "Language"), language])
        languageRow.spacing = 8
        let include = NSButton(checkboxWithTitle: "Include reference transcript", target: nil, action: nil)
        include.state = ((clip["include_reference_transcript"] as? Bool) ?? true) ? .on : .off
        let text = NSTextView(frame: NSRect(x: 0, y: 0, width: 460, height: 140))
        text.isRichText = false
        text.font = TextRole.body.font
        text.string = clip["transcript_text"] as? String ?? ""
        text.isVerticallyResizable = true
        text.textContainer?.widthTracksTextView = true
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 460, height: 140))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.documentView = text
        let stack = NSStackView(views: [languageRow, include, scroll])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.frame = NSRect(x: 0, y: 0, width: 460, height: 210)

        let alert = NSAlert()
        alert.messageText = "Language and transcript — \((payload["display_name"] as? String) ?? "this voice")"
        alert.informativeText = "Off: the voice clones from its audio alone and is never transcribed — "
            + "use this for Khmer or any language the transcriber cannot understand. "
            + "On: the words below are sent with the clip, so they must be exactly what it says. "
            + "Models that need a transcript refuse an audio-only voice."
        alert.accessoryView = stack
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let on = include.state == .on
        let words = text.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if on && words.isEmpty {
            report("Type the words spoken in the clip, or turn the transcript off.", ok: false)
            return
        }
        var body: [String: Any] = ["voice_id": voiceId, "include": on,
                                   "language": language.stringValue.trimmingCharacters(in: .whitespaces)]
        if on { body["transcript"] = words }
        GatewayAPI.post("/admin/api/voices/settings", body: body) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report("Could not save it — \(error.localizedDescription)", ok: false)
            case .success:
                self.report(on ? "Saved. The transcript is included with this voice."
                               : "Saved. This voice clones from its audio alone.", ok: true)
            }
            self.reload()
        }
    }

    private func showReference(_ payload: [String: Any]) {
        let name = (payload["display_name"] as? String) ?? "This voice"
        let clip = payload["clip"] as? [String: Any] ?? [:]
        let models = payload["models"] as? [[String: Any]] ?? []

        // A provider voice has no clip here, so there is nothing to report per
        // model. Say that, rather than showing a table that would have to invent
        // a verdict for every model.
        if let note = payload["note"] as? String, models.isEmpty {
            let alert = NSAlert()
            alert.messageText = name
            alert.informativeText = note
            alert.alertStyle = .informational
            alert.addButton(withTitle: "OK")
            alert.closeOnEscape()
            alert.runModal()
            report("No local reference clip for this voice.", ok: true)
            return
        }

        // "Not measured" rather than a plausible number: a voice imported before
        // its duration was recorded genuinely does not state one.
        func length(_ value: Any?) -> String {
            guard let seconds = value as? Double else { return "not measured" }
            return String(format: "%.2fs", seconds)
        }

        var lines = [
            "\(name) — clip \(length(clip["duration_seconds"])), "
            + "speech \(length(clip["speech_seconds"])), "
            + "\(clip["segments"] as? Int ?? 0) timed segments, "
            + "transcript \(((clip["has_transcript"] as? Bool) ?? false) ? "yes" : "no")",
            "",
        ]
        let width = models.map { (($0["display_name"] as? String) ?? "").count }.max() ?? 0
        for model in models {
            let title = (model["display_name"] as? String) ?? ""
            let indent = String(repeating: " ", count: max(0, width - title.count))
            lines.append("\(title)\(indent)  \((model["verdict"] as? String) ?? "")")
            if let warning = model["warning"] as? String, !warning.isEmpty {
                lines.append("\(String(repeating: " ", count: width))  ⚠ \(warning)")
            }
        }
        lines.append("")
        lines.append("A window is cut at whole timed segments and the transcript is rewritten "
                     + "to match it. Nothing here shortens the recording you loaded — the "
                     + "clip stays whole and each model takes what its own contract allows.")

        let text = NSTextView(frame: NSRect(x: 0, y: 0, width: 560, height: 330))
        text.isEditable = false
        text.font = TextRole.mono.font
        text.string = lines.joined(separator: "\n")
        text.isVerticallyResizable = true
        text.textContainer?.widthTracksTextView = true
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 560, height: 330))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.documentView = text

        let alert = NSAlert()
        alert.messageText = "How each local model uses this recording"
        alert.informativeText = ""
        alert.alertStyle = .informational
        alert.accessoryView = scroll
        alert.addButton(withTitle: "OK")
        alert.closeOnEscape()
        alert.runModal()
        report("Checked \(models.count) local models.", ok: true)
    }

    /// Say what happened, and make failure look like failure.
    private func report(_ message: String, ok: Bool) {
        summaryLabel.textColor = ok ? .secondaryLabelColor : Health.bad.text
        summaryLabel.stringValue = message
    }

    @objc private func addSelected() {
        let rows = selectedRows()
        guard !rows.isEmpty else {
            report("Select a voice to add first.", ok: false)
            return
        }
        // Build every body before sending, so a malformed row cannot leave the
        // count below the number of requests actually in flight.
        let bodies: [[String: Any]] = rows.compactMap { row in
            guard let providerId = row["provider_id"] as? String,
                  let voiceId = row["provider_voice_id"] as? String else { return nil }
            var body: [String: Any] = [
                "provider_id": providerId,
                "provider_voice_id": voiceId,
                "display_name": row["display_name"] as? String ?? voiceId,
            ]
            // ElevenLabs addresses a library voice by (owner, voice). The owner id
            // is published at the top level for every source — `raw` only carries
            // it for lanes whose provider states one, and a library voice's owner
            // is the thing that makes the import addressable at all.
            if let owner = row["owner_id"] as? String, !owner.isEmpty {
                body["owner_id"] = owner
            }
            return body
        }
        guard !bodies.isEmpty else {
            report("Those rows do not name a provider voice to import.", ok: false)
            return
        }

        report("Adding \(bodies.count) voice\(bodies.count == 1 ? "" : "s")…", ok: true)
        var failures: [String] = []
        var finished = 0
        for body in bodies {
            let label = body["display_name"] as? String ?? "voice"
            // The result used to be discarded (`{ _ in self?.reload() }`), so a
            // refused import — 403 for a library voice that needs an owner id, 409
            // for an id already holding different audio — looked exactly like a
            // successful one: the table refreshed and nothing new was there. The
            // gateway's own sentence is the only thing that says which it was, so
            // it is carried back rather than dropped.
            GatewayAPI.post("/admin/api/voices/import", body: body) { [weak self] result in
                guard let self else { return }
                if case .failure(let error) = result {
                    failures.append("\(label) — \(error.localizedDescription)")
                }
                finished += 1
                guard finished == bodies.count else { return }
                if failures.isEmpty {
                    self.report("Added \(finished) voice\(finished == 1 ? "" : "s").", ok: true)
                } else {
                    self.report("Added \(finished - failures.count) of \(finished) — "
                                + "\(failures.count) refused.", ok: false)
                    // The sentences go in a dialog, not the one-line label: three
                    // refusals' worth of text in a label is either clipped (so the
                    // reason is unreadable, which is the bug again) or grown into
                    // the window's required height, which is how the Fleet page
                    // once pushed its own bottom edge off-screen.
                    let alert = NSAlert()
                    alert.messageText = failures.count == 1
                        ? "One voice could not be added"
                        : "\(failures.count) voices could not be added"
                    alert.informativeText = failures.joined(separator: "\n\n")
                    alert.alertStyle = .warning
                    alert.addButton(withTitle: "OK")
                    alert.closeOnEscape()
                    alert.runModal()
                }
                self.reload()
            }
        }
    }

    @objc private func stopPreview() {
        sound?.stop()
        sound = nil
    }

    private func preview(_ voiceId: String) {
        let path = "/admin/api/voices/\(voiceId.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "")/preview"
        guard GatewayAPI.url(path) != nil else {
            summaryLabel.textColor = Health.warn.text
            summaryLabel.stringValue = "Bad preview URL."
            return
        }

        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.stringValue = "Fetching preview…"
        GatewayAPI.download(path) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.textColor = Health.warn.text
                switch error {
                case .missingResource:
                    self.summaryLabel.stringValue = "No preview is available for that voice."
                case .unauthorized:
                    self.summaryLabel.stringValue = "Preview unavailable — the gateway rejected the API key."
                case .transport(let detail):
                    self.summaryLabel.stringValue =
                        detail.isEmpty ? "Could not fetch the preview." : "Could not fetch the preview — \(detail)"
                case .emptyResponse:
                    self.summaryLabel.stringValue = "The gateway returned an empty preview."
                default:
                    self.summaryLabel.stringValue = "Could not fetch the preview — \(error.localizedDescription)"
                }
            case .success(let data):
                guard let sound = NSSound(data: data) else {
                    self.summaryLabel.textColor = Health.warn.text
                    self.summaryLabel.stringValue = "The preview response was not valid audio."
                    return
                }
                self.sound?.stop()
                self.sound = sound
                if sound.play() {
                    self.summaryLabel.textColor = .secondaryLabelColor
                    self.summaryLabel.stringValue = "Playing preview…"
                } else {
                    self.summaryLabel.textColor = Health.warn.text
                    self.summaryLabel.stringValue = "The preview could not be played."
                }
            }
        }
    }

    // MARK: Table

    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        SeparatorRowView()
    }

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
        field.font = TextRole.secondary.font
        field.lineBreakMode = .byTruncatingTail
        switch identifier {
        case "name":
            field.stringValue = voice["display_name"] as? String ?? "?"
            field.font = TextRole.secondary.font
        case "clip":
            if (voice["provider_id"] as? String) == Self.localProviderId {
                field.stringValue = Self.clipLabel(voice["duration_seconds"], voice["imported_at"])
                field.textColor = .secondaryLabelColor
            } else {
                field.stringValue = "—"
                field.textColor = .tertiaryLabelColor
            }
        case "language":
            field.stringValue = (voice["language"] as? String ?? "").uppercased()
            field.textColor = .secondaryLabelColor
        case "fleet":
            // Only this Mac's own voices can be shared, and only once the owner has
            // allowed it. "Private" has to be visible, or he cannot tell which of
            // his voices would be copied to another machine.
            let mine = (voice["provider_id"] as? String) == Self.localProviderId
            let allowed = (voice["permission_acknowledged"] as? Bool) == true
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
            if let tags = voice["tags"] as? [String], !tags.isEmpty { bits.append("Tags: " + tags.joined(separator: ", ")) }
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
            field.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
            field.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -DS.Space.xs),
            field.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    @objc private func toggleRow(_ sender: NSButton) {
        guard sender.tag < rows.count, let id = rows[sender.tag]["id"] as? String else { return }
        let enabled = sender.state == .on
        rows[sender.tag]["enabled"] = enabled
        // Deliberately no reload on success: a full refresh would rebuild the
        // table and fight the checkbox the user just clicked. The next reload
        // shows the corrected totals.
        //
        // But a *failed* write has to be undone. The checkbox was flipped before
        // the request went out and the result was discarded, so a refused change
        // left the box showing a state the gateway never accepted — and this is the
        // one control whose whole meaning is "is this voice offered?".
        GatewayAPI.post("/admin/api/voices/enabled", body: ["items": [id], "enabled": enabled]) {
            result in
            if case .failure(let error) = result {
                // By id, not by the row number the click had: a reload may have moved it.
                if let current = self.rows.firstIndex(where: { $0["id"] as? String == id }) {
                    self.rows[current]["enabled"] = !enabled
                }
                sender.state = enabled ? .off : .on
                self.summaryLabel.stringValue = "Could not change that voice — \(error.localizedDescription)"
                self.summaryLabel.textColor = Health.bad.text
            }
        }
    }

    @objc private func playRow(_ sender: NSButton) {
        guard sender.tag < rows.count, let id = rows[sender.tag]["id"] as? String else { return }
        preview(id)
    }
}
