import AppKit
import CryptoKit

// MARK: - The lanes

/// One field on a Create form.
///
/// The six workspaces are **one view driven by this data**, not six near-copies
/// of the same page. Studio Hub frames its three implemented workspaces with one
/// shared body "so the three workspace pages cannot drift apart one fix at a
/// time"; six copies here would drift the same way, faster.
struct CreateField {
    enum Kind { case script, text, number, voice }
    let key: String
    let label: String
    let kind: Kind
    var placeholder: String = ""
    var initial: String = ""
    var hint: String = ""
}

private struct CreateDraft: Codable {
    var modelID: String?
    var values: [String: String]
    var filePath: String?
}

private struct CreateAttempt: Codable {
    var key: String
    var modelID: String
    var modality: String
    var body: Data?
    var uploadPath: String?
    var uploadName: String?
    var uploadType: String?
    var uploadBytes: Int?
    var uploadDigest: String?
}

private struct CreateWorkspaceState: Codable {
    var draft: CreateDraft
    var currentJobID: String?
    var attempt: CreateAttempt?
    var unresolvedAttempts: [CreateAttempt]

    private enum CodingKeys: String, CodingKey { case draft, currentJobID, attempt, unresolvedAttempts }

    init(
        draft: CreateDraft, currentJobID: String?, attempt: CreateAttempt?,
        unresolvedAttempts: [CreateAttempt] = []
    ) {
        self.draft = draft
        self.currentJobID = currentJobID
        self.attempt = attempt
        self.unresolvedAttempts = unresolvedAttempts
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        draft = try values.decode(CreateDraft.self, forKey: .draft)
        currentJobID = try values.decodeIfPresent(String.self, forKey: .currentJobID)
        attempt = try values.decodeIfPresent(CreateAttempt.self, forKey: .attempt)
        unresolvedAttempts = try values.decodeIfPresent([CreateAttempt].self, forKey: .unresolvedAttempts) ?? []
    }

    static let empty = CreateWorkspaceState(
        draft: CreateDraft(modelID: nil, values: [:], filePath: nil),
        currentJobID: nil,
        attempt: nil,
        unresolvedAttempts: []
    )
}

private enum CreateStateStore {
    private static let prefix = "gateway.create.state."

    private static var defaults: UserDefaults {
        let name = ProcessInfo.processInfo.environment["GATEWAY_NATIVE_DEFAULTS_SUITE"]
            ?? "kh.gateway.create"
        return UserDefaults(suiteName: name) ?? .standard
    }

    static func load(lane: String) -> CreateWorkspaceState {
        guard let data = defaults.data(forKey: prefix + lane),
              let state = try? JSONDecoder().decode(CreateWorkspaceState.self, from: data)
        else { return .empty }
        return state
    }

    static func save(_ state: CreateWorkspaceState, lane: String) {
        guard let data = try? JSONEncoder().encode(state) else { return }
        defaults.set(data, forKey: prefix + lane)
    }
}

/// A Create lane: Studio Hub's page names and order, wired to cloud models.
struct CreateLane {
    let key: String
    let title: String
    let symbol: String
    /// The gateway modality this lane submits as.
    let modality: String
    let blurb: String
    let fields: [CreateField]
    /// Transcription carries a file rather than a JSON body.
    var isUpload = false
}

enum CreateLanes {
    /// Studio Hub's Create set in its order, with its owner-facing names —
    /// "Subtitle" is what it calls transcription, and so does this.
    ///
    /// It says nothing about generating "locally", because these are cloud models
    /// and those pages say that word about engines on this Mac.
    static let all: [CreateLane] = [
        CreateLane(
            key: "audio", title: "Audio", symbol: "waveform", modality: "tts",
            blurb: "Write a script, choose a voice and generate spoken audio.",
            fields: [
                CreateField(key: "text", label: "Script", kind: .script,
                            placeholder: "Write what should be spoken…"),
                CreateField(key: "voice_id", label: "Voice", kind: .voice,
                            hint: "Every speech lane here needs a voice — the gateway will not invent one."),
            ]
        ),
        CreateLane(
            key: "subtitle", title: "Subtitle", symbol: "text.bubble",
            modality: "transcription",
            blurb: "Import a recording and read its transcript.",
            fields: [], isUpload: true
        ),
        CreateLane(
            key: "image", title: "Image", symbol: "photo", modality: "image",
            blurb: "Describe an image, choose a model and generate it.",
            fields: [
                CreateField(key: "prompt", label: "Prompt", kind: .script,
                            placeholder: "Describe the image…"),
                CreateField(key: "n", label: "How many", kind: .number, initial: "1",
                            hint: "A provider that answers several variations returns all of them."),
            ]
        ),
        CreateLane(
            key: "music", title: "Music", symbol: "music.note", modality: "music",
            blurb: "Describe the music you want.",
            fields: [
                CreateField(key: "prompt", label: "Describe", kind: .script,
                            placeholder: "calm piano, late night…"),
                CreateField(key: "style", label: "Style", kind: .text, placeholder: "Classical"),
                CreateField(key: "title", label: "Title", kind: .text, placeholder: "Peaceful"),
            ]
        ),
        CreateLane(
            key: "video", title: "Video", symbol: "film", modality: "video",
            blurb: "Describe the shot and generate it.",
            fields: [
                CreateField(key: "prompt", label: "Prompt", kind: .script,
                            placeholder: "Describe the shot…"),
            ]
        ),
        CreateLane(
            key: "chat", title: "Chat", symbol: "bubble.left.and.bubble.right",
            modality: "chat",
            blurb: "Ask a model directly.",
            fields: [
                CreateField(key: "prompt", label: "Message", kind: .script,
                            placeholder: "Ask anything…"),
            ]
        ),
    ]
}

// MARK: - Section

/// A Create lane as a sidebar page.
final class CreateSection: NSObject, AppSection {
    static let all: [CreateSection] = CreateLanes.all.map(CreateSection.init)

    let lane: CreateLane
    var title: String { lane.title }
    var symbol: String { lane.symbol }
    /// Nothing here should be re-polled from under the owner: a refresh mid-typing
    /// would fight the field being edited, and a finished job is polled by the
    /// workspace itself until it settles.
    let refreshesAutomatically = false

    init(lane: CreateLane) { self.lane = lane }

    private lazy var workspace = CreateWorkspace(lane: lane)
    var view: NSView { workspace }

    func activated() { workspace.refreshModels() }
}

// MARK: - Workspace

private final class CreateArtifactButton: NSButton {
    let jobID: String
    let assetID: String
    let path: String
    let mediaType: String
    var kept: Bool

    init(
        title: String, jobID: String, assetID: String, path: String, mediaType: String,
        kept: Bool = false, target: AnyObject?, action: Selector
    ) {
        self.jobID = jobID
        self.assetID = assetID
        self.path = path
        self.mediaType = mediaType
        self.kept = kept
        super.init(frame: .zero)
        self.title = title
        self.target = target
        self.action = action
        bezelStyle = .rounded
    }

    required init?(coder: NSCoder) { fatalError("not used") }
}

/// Studio Hub's Create house frame: a heading, then the form on the left and the
/// result on the right, with the output area holding its empty state until there
/// is something to show.
final class CreateWorkspace: NSView {
    private let lane: CreateLane
    private var state: CreateWorkspaceState
    private var fields: [String: NSView] = [:]
    private var modelPopup: NSPopUpButton?
    private var voiceComboBox: NSComboBox?
    private var advancedVoiceField: NSTextField?
    private var voiceChoices: [String] = []
    private var models: [[String: Any]] = []
    private var selectedModelIDHint: String?
    private var pollTimer: Timer?
    private var pollInFlight = false
    private var currentJobID: String?
    private var pendingJobID: String?
    private var submissionInFlight = false
    private var activeAttempt: CreateAttempt?
    private var ambiguousAttempt: CreateAttempt?
    private var hasLoadedModels = false
    private var modelRefreshRequest = 0
    private var voiceRefreshRequest = 0
    private var jobRequestGeneration = 0
    private var draftObservers: [NSObjectProtocol] = []
    private var localArtifactURLs: [String: URL] = [:]
    private var fileButton: NSButton?
    private var fileHelp: NSTextField?
    private var fileURL: URL?
    private var capabilityLabel: NSTextField?
    private var cancelButton: NSButton?
    private var retryAttemptButton: NSButton?
    private var newDraftButton: NSButton?
    private var loadJobButton: NSButton?

    private let resultBox = NSStackView()
    private let statusLabel = PageKit.label("", size: 12, colour: .secondaryLabelColor, wrapping: true)
    private let generateButton = NSButton(title: "Generate", target: nil, action: nil)

    init(lane: CreateLane) {
        self.lane = lane
        self.state = CreateStateStore.load(lane: lane.key)
        self.selectedModelIDHint = state.draft.modelID
        self.currentJobID = state.currentJobID
        self.ambiguousAttempt = state.attempt
        super.init(frame: .zero)
        build()
        restoreDraft()
        observeDraftChanges()
        if ambiguousAttempt != nil {
            setEditingEnabled(false)
            showAttemptResolution()
        } else if let currentJobID {
            statusLabel.stringValue = "Job \(currentJobID.prefix(8)) is selected. Reload it to inspect the result."
            loadJobButton?.isHidden = false
        }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    // MARK: layout

    private func build() {
        let page = PageKit.page()
        page.addArrangedSubview(PageKit.heading(lane.title, lane.blurb))
        page.addArrangedSubview(headerRow())

        let split = NSStackView(views: [formPane(), outputPane()])
        // Keep the form usable at the app's minimum width. The output is a
        // second block below it, so controls never force the window wider or
        // push the primary action outside the viewport.
        split.orientation = .vertical
        split.alignment = .leading
        split.spacing = 20
        split.distribution = .fill
        split.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        page.addArrangedSubview(split)
        split.widthAnchor.constraint(equalTo: page.widthAnchor, constant: -PageKit.pagePadding * 2)
            .isActive = true
        formColumn.widthAnchor.constraint(lessThanOrEqualToConstant: 390).isActive = true
        for case let label as NSTextField in formColumn.arrangedSubviews {
            label.preferredMaxLayoutWidth = 330
            label.widthAnchor.constraint(equalToConstant: 330).isActive = true
        }
        outputColumn.widthAnchor.constraint(equalTo: split.widthAnchor).isActive = true

        // A form has a required height, so the page scrolls rather than growing the
        // window — the rule this app learned the hard way.
        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(page)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = document
        addSubview(scroll)
        NSLayoutConstraint.activate([
            page.topAnchor.constraint(equalTo: document.topAnchor),
            page.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            page.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            page.bottomAnchor.constraint(equalTo: document.bottomAnchor),
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            scroll.topAnchor.constraint(equalTo: topAnchor),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    private func headerRow() -> NSView {
        statusLabel.preferredMaxLayoutWidth = 280
        statusLabel.widthAnchor.constraint(equalToConstant: 280).isActive = true
        let refresh = NSButton(title: "Refresh models", target: self, action: #selector(refreshModels))
        refresh.bezelStyle = .rounded
        loadJobButton = NSButton(title: "Reload selected job", target: self, action: #selector(loadSelectedJob))
        loadJobButton?.bezelStyle = .rounded
        loadJobButton?.isHidden = true
        return PageKit.row([statusLabel, NSView(), loadJobButton!, refresh])
    }

    private let formColumn = NSStackView()
    private let outputColumn = NSStackView()

    private func formPane() -> NSView {
        formColumn.orientation = .vertical
        formColumn.alignment = .leading
        formColumn.spacing = 12

        let picker = NSPopUpButton()
        picker.target = self
        picker.action = #selector(modelChanged)
        picker.widthAnchor.constraint(equalToConstant: 330).isActive = true
        modelPopup = picker
        formColumn.addArrangedSubview(PageKit.formRow("Model", picker))
        formColumn.addArrangedSubview(PageKit.help("Only models switched on for this lane appear here — the same set Story Studio picks from."))

        capabilityLabel = PageKit.help("Capabilities: unknown")
        formColumn.addArrangedSubview(capabilityLabel!)

        if lane.isUpload {
            let choose = NSButton(title: "Choose File…", target: self, action: #selector(pickFile))
            choose.bezelStyle = .rounded
            fileButton = choose
            fileHelp = PageKit.help("No file chosen")
            formColumn.addArrangedSubview(PageKit.formRow("Recording", [choose, fileHelp!]))
        }

        for field in lane.fields {
            let control = makeControl(field)
            fields[field.key] = control
            formColumn.addArrangedSubview(PageKit.formRow(field.label, control))
            if !field.hint.isEmpty {
                formColumn.addArrangedSubview(PageKit.help(field.hint))
            }
        }

        generateButton.target = self
        generateButton.action = #selector(generate)
        generateButton.bezelStyle = .rounded
        generateButton.keyEquivalent = "\r"
        cancelButton = NSButton(title: "Cancel", target: self, action: #selector(cancel))
        cancelButton?.bezelStyle = .rounded
        cancelButton?.isHidden = true
        retryAttemptButton = NSButton(title: "Retry same attempt", target: self, action: #selector(retryAttempt))
        retryAttemptButton?.bezelStyle = .rounded
        retryAttemptButton?.isHidden = true
        newDraftButton = NSButton(title: "Start separate draft", target: self, action: #selector(startNewDraft))
        newDraftButton?.bezelStyle = .rounded
        newDraftButton?.isHidden = true
        let row = PageKit.row([
            generateButton, cancelButton!, retryAttemptButton!, newDraftButton!,
            PageKit.label("", size: 12, colour: .secondaryLabelColor),
        ])
        formColumn.addArrangedSubview(row)
        return formColumn
    }

    private func makeControl(_ field: CreateField) -> NSView {
        switch field.kind {
        case .script:
            let text = NSTextView()
            text.font = .systemFont(ofSize: 13)
            text.isRichText = false
            text.string = field.initial
            text.isVerticallyResizable = true
            text.autoresizingMask = [.width]
            text.textContainerInset = NSSize(width: 4, height: 6)
            let scroll = NSScrollView()
            scroll.documentView = text
            scroll.hasVerticalScroller = true
            scroll.borderType = .bezelBorder
            scroll.translatesAutoresizingMaskIntoConstraints = false
            scroll.widthAnchor.constraint(equalToConstant: 330).isActive = true
            scroll.heightAnchor.constraint(equalToConstant: 120).isActive = true
            return scroll
        case .number, .text:
            let entry = NSTextField(string: field.initial)
            entry.placeholderString = field.placeholder
            entry.font = .systemFont(ofSize: 13)
            entry.widthAnchor.constraint(equalToConstant: field.kind == .number ? 90 : 330).isActive = true
            entry.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            return entry
        case .voice:
            let combo = NSComboBox()
            combo.isEditable = true
            combo.completes = true
            combo.font = .systemFont(ofSize: 13)
            combo.placeholderString = "Search compatible voices"
            combo.widthAnchor.constraint(equalToConstant: 330).isActive = true
            combo.target = self
            combo.action = #selector(voiceChanged)
            voiceComboBox = combo

            let advanced = NSTextField()
            advanced.placeholderString = "Advanced provider voice ID (exact)"
            advanced.font = .systemFont(ofSize: 13)
            advanced.widthAnchor.constraint(equalToConstant: 330).isActive = true
            advancedVoiceField = advanced

            let stack = NSStackView(views: [combo, advanced])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 6
            return stack
        }
    }

    private func outputPane() -> NSView {
        outputColumn.orientation = .vertical
        outputColumn.alignment = .leading
        outputColumn.spacing = 12
        resultBox.orientation = .vertical
        resultBox.alignment = .leading
        resultBox.spacing = 10
        resultBox.edgeInsets = NSEdgeInsets(top: 14, left: 14, bottom: 14, right: 14)
        resultBox.wantsLayer = true
        resultBox.layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
        resultBox.layer?.cornerRadius = 6
        let preview = PageKit.sectionTitle(lane.title)
        outputColumn.addArrangedSubview(preview)
        outputColumn.addArrangedSubview(resultBox)
        resultBox.widthAnchor.constraint(equalTo: outputColumn.widthAnchor).isActive = true
        resultBox.addArrangedSubview(
            PageKit.empty("Nothing generated yet",
                          "Fill in the form and press Generate. Anything you make here is temporary until you save it or keep it.",
                          symbol: lane.symbol)
        )
        return outputColumn
    }

    // MARK: data

    private func restoreDraft() {
        let values = state.draft.values
        for field in lane.fields {
            guard let value = values[field.key] else { continue }
            setValue(value, for: field.key)
        }
        if let advanced = values["voice_advanced_id"] {
            advancedVoiceField?.stringValue = advanced
        }
        if let path = state.draft.filePath {
            let url = URL(fileURLWithPath: path)
            if FileManager.default.fileExists(atPath: url.path) {
                fileURL = url
                fileButton?.title = url.lastPathComponent
                fileHelp?.stringValue = url.path
            }
        }
        updateCapabilities()
        updateGenerateEnabled()
    }

    private func observeDraftChanges() {
        let center = NotificationCenter.default
        draftObservers.append(center.addObserver(
            forName: NSControl.textDidChangeNotification, object: nil, queue: .main
        ) { [weak self] notification in
            guard let self, self.ownsControl(notification.object) else { return }
            self.persistState()
        })
        draftObservers.append(center.addObserver(
            forName: NSText.didChangeNotification, object: nil, queue: .main
        ) { [weak self] notification in
            guard let self, self.ownsControl(notification.object) else { return }
            self.persistState()
        })
    }

    private func ownsControl(_ object: Any?) -> Bool {
        guard let object = object as? NSView else { return false }
        return fields.values.contains { contains(object, in: $0) }
    }

    private func contains(_ object: NSView, in view: NSView) -> Bool {
        view === object || view.subviews.contains { contains(object, in: $0) }
    }

    private func persistState() {
        var values: [String: String] = [:]
        for field in lane.fields {
            values[field.key] = rawValue(of: field.key)
        }
        if let advancedVoiceField, !advancedVoiceField.stringValue.isEmpty {
            values["voice_advanced_id"] = advancedVoiceField.stringValue
        }
        state.draft = CreateDraft(
            modelID: selectedModelIDHint ?? selectedModelID(),
            values: values,
            filePath: fileURL?.path
        )
        state.currentJobID = currentJobID
        state.attempt = ambiguousAttempt ?? activeAttempt
        CreateStateStore.save(state, lane: lane.key)
    }

    private func rawValue(of key: String) -> String {
        if key == "voice_id" {
            if let advancedVoiceField, !advancedVoiceField.stringValue.isEmpty {
                return advancedVoiceField.stringValue
            }
            return selectedVoiceID()
        }
        if let scroll = fields[key] as? NSScrollView,
           let text = scroll.documentView as? NSTextView {
            return text.string
        }
        return (fields[key] as? NSTextField)?.stringValue ?? ""
    }

    private func setValue(_ value: String, for key: String) {
        if key == "voice_id" {
            voiceComboBox?.stringValue = value
            return
        }
        if let scroll = fields[key] as? NSScrollView,
           let text = scroll.documentView as? NSTextView {
            text.string = value
        } else if let entry = fields[key] as? NSTextField {
            entry.stringValue = value
        }
    }

    @objc func refreshModels() {
        modelRefreshRequest += 1
        let requestID = modelRefreshRequest
        if currentJobID == nil && !submissionInFlight {
            statusLabel.stringValue = "loading models…"
        }
        GatewayAPI.get("/admin/api/create/models") { [weak self] result in
            guard let self else { return }
            guard self.modelRefreshRequest == requestID else { return }
            switch result {
            case .failure(let error):
                if self.currentJobID == nil && !self.submissionInFlight {
                    self.statusLabel.stringValue = error.localizedDescription
                }
            case .success(let payload):
                let byModality = payload["by_modality"] as? [String: Any] ?? [:]
                let rows = byModality[self.lane.modality] as? [[String: Any]] ?? []
                let previousModelID = self.selectedModelID()
                self.models = rows
                self.modelPopup?.removeAllItems()
                if rows.isEmpty {
                    self.selectedModelIDHint = nil
                    self.modelPopup?.addItem(withTitle: "No model switched on")
                    self.modelPopup?.lastItem?.isEnabled = false
                    if self.currentJobID == nil && !self.submissionInFlight {
                        self.statusLabel.stringValue =
                            "No model is switched on for this lane. Enable one on the Providers page and it appears here."
                    }
                } else {
                    for row in rows {
                        let provider = row["provider_name"] as? String ?? ""
                        let name = row["display_name"] as? String ?? ""
                        let free = (row["free_lane"] as? Bool == true) ? "  ·  free" : ""
                        self.modelPopup?.addItem(withTitle: "\(provider) — \(name)\(free)")
                        self.modelPopup?.lastItem?.representedObject = row["id"] as? String
                    }

                    var selectionLost = false
                    var needsSelection = false
                    if let previousModelID,
                       let index = rows.firstIndex(where: { $0["id"] as? String == previousModelID }) {
                        self.selectedModelIDHint = previousModelID
                        self.modelPopup?.selectItem(at: index)
                    } else if !self.hasLoadedModels && previousModelID == nil {
                        self.modelPopup?.selectItem(at: 0)
                        self.selectedModelIDHint = rows[0]["id"] as? String
                    } else {
                        self.selectedModelIDHint = nil
                        selectionLost = previousModelID != nil
                        needsSelection = true
                        self.modelPopup?.insertItem(withTitle: "Select a model", at: 0)
                        self.modelPopup?.item(at: 0)?.isEnabled = false
                        self.modelPopup?.selectItem(at: 0)
                    }
                    if self.currentJobID == nil && !self.submissionInFlight {
                        self.statusLabel.stringValue = selectionLost
                            ? "Your selected model is no longer available. Select a model to continue."
                            : needsSelection
                            ? "Select a model to continue."
                            : "\(rows.count) model\(rows.count == 1 ? "" : "s") available"
                    }
                    self.hasLoadedModels = true
                }
                self.modelPopup?.isEnabled = !rows.isEmpty
                self.updateCapabilities()
                if let modelID = self.selectedModelID(), self.lane.modality == "tts" {
                    self.refreshVoices(modelID: modelID)
                }
                self.persistState()
                self.updateGenerateEnabled()
            }
        }
    }

    @objc private func modelChanged() {
        selectedModelIDHint = modelPopup?.selectedItem?.representedObject as? String
        persistState()
        updateCapabilities()
        if let modelID = selectedModelID(), lane.modality == "tts" {
            refreshVoices(modelID: modelID)
        }
        updateGenerateEnabled()
    }

    @objc private func voiceChanged() {
        if let index = voiceComboBox?.indexOfSelectedItem,
           index >= 0, index < voiceChoices.count {
            voiceComboBox?.stringValue = voiceChoices[index]
        }
        persistState()
    }

    private func refreshVoices(modelID: String) {
        guard lane.modality == "tts", let combo = voiceComboBox else { return }
        voiceRefreshRequest += 1
        let requestID = voiceRefreshRequest
        let current = rawValue(of: "voice_id")
        combo.isEnabled = false
        GatewayAPI.get("/v1/voices?model_id=\(queryValue(modelID))") { [weak self] result in
            guard let self, self.voiceRefreshRequest == requestID else { return }
            switch result {
            case .failure:
                self.voiceChoices = []
                combo.removeAllItems()
                combo.stringValue = current
                combo.isEnabled = true
                self.capabilityLabel?.stringValue += "\nCompatible voices could not be loaded; enter an exact provider ID if needed."
            case .success(let payload):
                let rows = payload["voices"] as? [[String: Any]] ?? []
                self.voiceChoices = rows.compactMap { $0["id"] as? String }
                combo.removeAllItems()
                for row in rows {
                    let name = row["display_name"] as? String ?? row["id"] as? String ?? "Voice"
                    let source = row["source"] as? String ?? "source unknown"
                    combo.addItem(withObjectValue: "\(name) · \(source)")
                }
                combo.stringValue = current
                combo.isEnabled = true
                if let index = self.voiceChoices.firstIndex(of: current), index < combo.numberOfItems {
                    combo.selectItem(at: index)
                } else if !current.isEmpty {
                    self.capabilityLabel?.stringValue += "\nVoice \(current) is retained; it is not in this model's compatible list."
                }
            }
            self.setEditingEnabled(self.ambiguousAttempt == nil && !self.submissionInFlight && self.pendingJobID == nil)
            self.persistState()
        }
    }

    private func queryValue(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? value
    }

    private func selectedModelRow() -> [String: Any]? {
        guard let id = selectedModelID() else { return nil }
        return models.first { ($0["id"] as? String) == id || ($0["model_id"] as? String) == id }
    }

    private func unknown(_ value: Any?) -> String {
        guard let value, !(value is NSNull) else { return "unknown" }
        if let int = value as? Int { return "\(int)" }
        if let double = value as? Double { return "\(double)" }
        if let bool = value as? Bool { return bool ? "supported" : "not supported" }
        if let string = value as? String, !string.isEmpty { return string }
        return "unknown"
    }

    private func source(_ row: [String: Any], _ key: String) -> String {
        let value = unknown(row[key])
        return value == "unknown" ? "source unknown" : value
    }

    private func updateCapabilities() {
        guard let label = capabilityLabel else { return }
        guard let row = selectedModelRow() else {
            label.stringValue = "Capabilities: unknown"
            return
        }
        var lines = ["Capabilities"]
        if lane.modality == "tts" {
            lines.append("Input characters: \(unknown(row["maximum_input_characters"])) (\(source(row, "maximum_input_characters_source")))")
            let cloning = (row["supports_voice_cloning"] as? Bool).map { $0 ? "yes" : "no" } ?? "unknown"
            lines.append("Voice cloning: \(cloning)")
            lines.append("Speed: \(unknown(row["minimum_speed"]))–\(unknown(row["maximum_speed"]))")
        } else if lane.modality == "chat" {
            lines.append("Context window: \(unknown(row["context_window_tokens"])) tokens (\(source(row, "context_window_tokens_source")))")
            lines.append("Output tokens: \(unknown(row["max_output_tokens"])) (\(source(row, "max_output_tokens_source")))")
        } else {
            lines.append("Prompt characters: \(unknown(row["max_prompt_characters"])) (\(source(row, "max_prompt_characters_source")))")
        }
        label.stringValue = lines.joined(separator: "\n")
    }

    private func updateGenerateEnabled() {
        generateButton.isEnabled = selectedModelID() != nil
            && modelPopup?.isEnabled == true
            && !submissionInFlight
            && pendingJobID == nil
            && ambiguousAttempt == nil
    }

    private func selectedModelID() -> String? {
        (modelPopup?.selectedItem?.representedObject as? String) ?? selectedModelIDHint
    }

    @objc private func pickFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        fileURL = url
        fileButton?.title = url.lastPathComponent
        fileHelp?.stringValue = url.path
        persistState()
    }

    private func value(of key: String) -> String {
        if key == "voice_id" {
            if let advancedVoiceField, !advancedVoiceField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return advancedVoiceField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            }
            return selectedVoiceID().trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if let scroll = fields[key] as? NSScrollView,
           let text = scroll.documentView as? NSTextView {
            return text.string.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if let entry = fields[key] as? NSTextField {
            return entry.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return ""
    }

    private func selectedVoiceID() -> String {
        if let index = voiceComboBox?.indexOfSelectedItem,
           index >= 0, index < voiceChoices.count {
            return voiceChoices[index]
        }
        return voiceComboBox?.stringValue ?? ""
    }

    // MARK: generate

    private func setEditingEnabled(_ enabled: Bool) {
        for view in fields.values {
            setEditingEnabled(enabled, in: view)
        }
        modelPopup?.isEnabled = enabled && !models.isEmpty
        fileButton?.isEnabled = enabled
        advancedVoiceField?.isEnabled = enabled
        if enabled {
            retryAttemptButton?.isHidden = true
            newDraftButton?.isHidden = true
        }
        updateGenerateEnabled()
    }

    private func setEditingEnabled(_ enabled: Bool, in view: NSView) {
        if let control = view as? NSControl { control.isEnabled = enabled }
        if let text = view as? NSTextView { text.isEditable = enabled }
        for child in view.subviews { setEditingEnabled(enabled, in: child) }
    }

    private func showAttemptResolution(message: String? = nil) {
        statusLabel.stringValue = message
            ?? "Submission outcome is unknown. Retry the same attempt or start a new draft; do not submit another request."
        retryAttemptButton?.isHidden = false
        newDraftButton?.isHidden = false
        cancelButton?.isHidden = true
        loadJobButton?.isHidden = true
        updateGenerateEnabled()
    }

    @objc private func startNewDraft() {
        if let unresolved = ambiguousAttempt {
            state.unresolvedAttempts.append(unresolved)
        }
        ambiguousAttempt = nil
        activeAttempt = nil
        state.attempt = nil
        setEditingEnabled(true)
        statusLabel.stringValue = "Separate draft started. The previous submission outcome remains unknown; check Jobs/provider before generating again."
        persistState()
    }

    @objc private func retryAttempt() {
        guard let attempt = ambiguousAttempt, !submissionInFlight else { return }
        jobRequestGeneration += 1
        activeAttempt = attempt
        submissionInFlight = true
        retryAttemptButton?.isHidden = true
        newDraftButton?.isHidden = true
        setEditingEnabled(false)
        statusLabel.stringValue = "retrying the same attempt…"
        persistState()
        submit(attempt)
    }

    private func makeAttempt(modelID: String) -> CreateAttempt? {
        let key = UUID().uuidString
        if lane.isUpload {
            guard let url = fileURL,
                  let data = try? Data(contentsOf: url), !data.isEmpty else {
                statusLabel.stringValue = "Choose a recording first."
                return nil
            }
            return CreateAttempt(
                key: key, modelID: modelID, modality: lane.modality, body: nil,
                uploadPath: url.path, uploadName: url.lastPathComponent,
                uploadType: "application/octet-stream", uploadBytes: data.count,
                uploadDigest: digest(data)
            )
        }

        var params: [String: Any] = [:]
        for field in lane.fields {
            let raw = value(of: field.key)
            if raw.isEmpty { continue }
            params[field.key] = field.kind == .number ? (Int(raw) ?? 1) : raw
        }
        if lane.modality == "chat" {
            params["messages"] = [["role": "user", "content": params["prompt"] as? String ?? ""]]
        }
        let body: [String: Any] = [
            "modality": lane.modality, "model_id": modelID,
            "params": params, "idempotency_key": key,
        ]
        guard JSONSerialization.isValidJSONObject(body),
              let data = try? JSONSerialization.data(withJSONObject: body) else {
            statusLabel.stringValue = "The form could not be encoded for submission."
            return nil
        }
        return CreateAttempt(
            key: key, modelID: modelID, modality: lane.modality, body: data,
            uploadPath: nil, uploadName: nil, uploadType: nil,
            uploadBytes: nil, uploadDigest: nil
        )
    }

    private func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func refuseChangedUpload(_ attempt: CreateAttempt, reason: String) {
        submissionInFlight = false
        activeAttempt = nil
        ambiguousAttempt = attempt
        setEditingEnabled(false)
        showAttemptResolution(message: "Cannot retry this attempt — \(reason) The unresolved key was kept; start a separate draft only if you accept that outcome remains unknown.")
        persistState()
    }

    private func submit(_ attempt: CreateAttempt) {
        if let uploadPath = attempt.uploadPath, let name = attempt.uploadName {
            guard let data = try? Data(contentsOf: URL(fileURLWithPath: uploadPath)) else {
                refuseChangedUpload(attempt, reason: "The saved upload is missing.")
                return
            }
            guard attempt.uploadBytes == data.count,
                  attempt.uploadDigest == digest(data) else {
                refuseChangedUpload(attempt, reason: "The saved upload changed since the attempt was recorded.")
                return
            }
            GatewayAPI.upload(
                "/admin/api/create/transcription",
                fileField: "file", filename: name, data: data,
                fields: ["model_id": attempt.modelID, "idempotency_key": attempt.key]
            ) { [weak self] result in
                self?.handleSubmit(result, attemptKey: attempt.key)
            }
            return
        }
        guard let body = attempt.body,
              let object = try? JSONSerialization.jsonObject(with: body),
              let payload = object as? [String: Any] else {
            handleSubmit(.failure(GatewayAPIError.malformed), attemptKey: attempt.key)
            return
        }
        GatewayAPI.post("/admin/api/create", body: payload) { [weak self] result in
            self?.handleSubmit(result, attemptKey: attempt.key)
        }
    }

    @objc private func generate() {
        guard !submissionInFlight, pendingJobID == nil, ambiguousAttempt == nil else { return }
        guard let modelID = selectedModelID() else {
            statusLabel.stringValue = "Select a model before generating."
            return
        }
        if let row = selectedModelRow() {
            let limitKey = lane.modality == "tts" ? "maximum_input_characters" : "max_prompt_characters"
            let textKey = lane.modality == "tts" ? "text" : "prompt"
            if let limit = row[limitKey] as? Int, limit >= 0,
               value(of: textKey).unicodeScalars.count > limit {
                statusLabel.stringValue = "This model accepts at most \(limit) characters (\(source(row, limitKey + "_source"))). Shorten the text or choose another model."
                return
            }
        }
        for field in lane.fields where field.kind == .number {
            if Int(value(of: field.key)).map({ $0 > 0 }) != true {
                statusLabel.stringValue = "\(field.label) must be a positive whole number."
                return
            }
        }
        guard let attempt = makeAttempt(modelID: modelID) else { return }
        jobRequestGeneration += 1
        activeAttempt = attempt
        submissionInFlight = true
        setEditingEnabled(false)
        updateGenerateEnabled()
        statusLabel.stringValue = "submitting…"
        persistState()
        submit(attempt)
    }

    private func isAmbiguous(_ error: Error) -> Bool {
        if error is URLError || (error as NSError).domain == NSURLErrorDomain {
            return true
        }
        guard let error = error as? GatewayAPIError else { return false }
        switch error {
        case .transport, .invalidResponse, .emptyResponse, .malformed, .unsafeRedirect:
            return true
        case .refused(let code, _):
            return code == 408 || code >= 500
        default:
            // Task4's current error contract uses `.refused`; keep the older
            // `.http(code)` spelling safe too without adding a test-only enum
            // case that the production transport does not expose.
            let description = String(reflecting: error)
            guard let open = description.firstIndex(of: "("),
                  description[..<open].hasSuffix("http") else { return false }
            let first = description.index(after: open)
            let digits = description[first...].prefix { $0.isNumber }
            guard let code = Int(digits) else { return false }
            return code == 408 || code >= 500
        }
    }

    private func handleSubmit(_ result: Result<[String: Any], Error>, attemptKey: String) {
        // A refresh started before this result must not overwrite its message.
        modelRefreshRequest += 1
        guard activeAttempt?.key == attemptKey else { return }
        switch result {
        case .failure(let error):
            submissionInFlight = false
            if isAmbiguous(error), let attempt = activeAttempt {
                ambiguousAttempt = attempt
                activeAttempt = nil
                setEditingEnabled(false)
                showAttemptResolution()
            } else {
                activeAttempt = nil
                ambiguousAttempt = nil
                state.attempt = nil
                setEditingEnabled(true)
                statusLabel.stringValue = error.localizedDescription
                persistState()
            }
        case .success(let job):
            guard let id = job["id"] as? String else {
                submissionInFlight = false
                if let attempt = activeAttempt {
                    ambiguousAttempt = attempt
                    activeAttempt = nil
                    setEditingEnabled(false)
                    showAttemptResolution()
                }
                return
            }
            submissionInFlight = false
            activeAttempt = nil
            ambiguousAttempt = nil
            state.attempt = nil
            currentJobID = id
            let state = job["state"] as? String ?? ""
            let terminal = ["succeeded", "failed", "cancelled"].contains(state)
            pendingJobID = terminal ? nil : id
            render(job)
            cancelButton?.isHidden = terminal
            loadJobButton?.isHidden = false
            persistState()
            if terminal {
                setEditingEnabled(true)
                updateGenerateEnabled()
            } else {
                setEditingEnabled(false)
                startPolling(id)
            }
        }
    }

    @objc private func loadSelectedJob() {
        guard let id = currentJobID else { return }
        jobRequestGeneration += 1
        let requestGeneration = jobRequestGeneration
        statusLabel.stringValue = "loading selected job…"
        GatewayAPI.get("/admin/api/create/status/\(id)") { [weak self] result in
            guard let self else { return }
            guard self.jobRequestGeneration == requestGeneration,
                  self.currentJobID == id else { return }
            switch result {
            case .failure(let error):
                self.statusLabel.stringValue = "Cannot load this job — \(error.localizedDescription)"
            case .success(let job):
                guard (job["id"] as? String ?? id) == id else { return }
                self.render(job)
                let state = job["state"] as? String ?? ""
                let terminal = ["succeeded", "failed", "cancelled"].contains(state)
                self.pendingJobID = terminal ? nil : id
                self.cancelButton?.isHidden = terminal
                self.setEditingEnabled(terminal && self.ambiguousAttempt == nil)
                self.persistState()
                self.statusLabel.stringValue = terminal
                    ? "Job reloaded; outputs were reattached from the gateway."
                    : "Job \(id.prefix(8)) is still \(state)."
            }
        }
    }

    @objc private func cancel() {
        guard let id = pendingJobID ?? currentJobID, !id.isEmpty else { return }
        cancelButton?.isEnabled = false
        statusLabel.stringValue = "requesting cancellation…"
        GatewayAPI.post("/admin/api/create/status/\(id)/cancel") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.cancelButton?.isEnabled = true
                self.statusLabel.stringValue = "Could not request cancellation — \(error.localizedDescription)"
            case .success(let job):
                self.render(job)
                let state = job["state"] as? String ?? ""
                let confirmed = state == "cancelled" || job["cancel_state"] as? String == "confirmed"
                if confirmed {
                    self.pendingJobID = nil
                    self.cancelButton?.isHidden = true
                    self.cancelButton?.isEnabled = true
                    self.setEditingEnabled(true)
                    self.persistState()
                } else if job["cancel_requested"] as? Bool == true {
                    self.statusLabel.stringValue = "Cancellation requested. It may still complete; any outputs will be kept."
                    self.cancelButton?.isEnabled = false
                    self.pendingJobID = id
                    self.schedulePoll(id)
                } else {
                    self.cancelButton?.isEnabled = true
                }
            }
        }
    }

    private func startPolling(_ id: String) {
        pollTimer?.invalidate()
        pollInFlight = false
        schedulePoll(id)
    }

    private func schedulePoll(_ id: String) {
        guard pendingJobID == id else { return }
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: false) { [weak self] _ in
            self?.pollTimer = nil
            self?.poll(id)
        }
    }

    private func poll(_ id: String) {
        guard pendingJobID == id, !pollInFlight else { return }
        pollInFlight = true
        GatewayAPI.get("/admin/api/create/status/\(id)") { [weak self] result in
            guard let self, self.currentJobID == id, self.pendingJobID == id else { return }
            self.pollInFlight = false
            switch result {
            case .failure(let error):
                self.statusLabel.stringValue =
                    "Cannot check this job — \(error.localizedDescription). Retrying; do not submit again."
                self.schedulePoll(id)
            case .success(let job):
                self.render(job)
                let state = job["state"] as? String ?? ""
                if ["succeeded", "failed", "cancelled"].contains(state) {
                    self.pendingJobID = nil
                    self.cancelButton?.isHidden = true
                    self.setEditingEnabled(true)
                    self.persistState()
                    self.updateGenerateEnabled()
                } else {
                    self.cancelButton?.isHidden = false
                    self.schedulePoll(id)
                }
            }
        }
    }

    // MARK: render

    private func clearResult() {
        for view in resultBox.arrangedSubviews { view.removeFromSuperview() }
    }

    private func render(_ job: [String: Any]) {
        let state = job["state"] as? String ?? ""
        let jobID = job["id"] as? String ?? currentJobID ?? ""
        let cancellationRequested = job["cancel_requested"] as? Bool == true
        statusLabel.stringValue = cancellationRequested && state != "cancelled"
            ? "Cancellation requested — this job may still complete."
            : state
        clearResult()

        if state == "failed" {
            resultBox.addArrangedSubview(PageKit.label(
                job["failure_reason"] as? String ?? job["error_detail"] as? String ?? "the generation failed",
                size: 13, colour: .systemRed, wrapping: true))
            return
        }
        if state == "cancelled" {
            resultBox.addArrangedSubview(PageKit.label(
                "The generation was cancelled. No automatic replay was made.",
                size: 13, colour: .secondaryLabelColor, wrapping: true))
            return
        }
        if state != "succeeded" {
            let spin = NSProgressIndicator()
            spin.style = .spinning
            spin.startAnimation(nil)
            resultBox.addArrangedSubview(PageKit.row([spin,
                PageKit.label(
                    cancellationRequested
                        ? "Cancellation requested; the provider may still finish."
                        : "\(state)…",
                    size: 13, colour: .secondaryLabelColor, wrapping: true)]))
            return
        }

        if lane.modality == "chat" {
            let text = NSTextView()
            text.isEditable = false
            text.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
            text.string = job["output_text"] as? String ?? ""
            let scroll = NSScrollView()
            scroll.documentView = text
            scroll.hasVerticalScroller = true
            scroll.borderType = .bezelBorder
            scroll.translatesAutoresizingMaskIntoConstraints = false
            scroll.widthAnchor.constraint(lessThanOrEqualToConstant: 520).isActive = true
            scroll.heightAnchor.constraint(equalToConstant: 260).isActive = true
            resultBox.addArrangedSubview(scroll)
            return
        }

        let outputs = job["outputs"] as? [[String: Any]] ?? []
        if outputs.isEmpty {
            resultBox.addArrangedSubview(PageKit.empty(
                "No result", "The job finished but produced no artifact.", symbol: lane.symbol))
            return
        }
        if outputs.count > 1 {
            resultBox.addArrangedSubview(PageKit.label(
                "\(outputs.count) results — every one the model returned",
                size: 11.5, colour: .secondaryLabelColor))
        }
        for output in outputs {
            resultBox.addArrangedSubview(artifactRow(jobID: jobID, output: output))
        }
    }

    private func artifactRow(jobID: String, output: [String: Any]) -> NSView {
        let assetID = output["asset_id"] as? String ?? "0"
        let path = output["url"] as? String
            ?? "/admin/api/artifacts/\(jobID)/outputs/\(assetID)"
        let present = output["present"] as? Bool ?? true
        let kept = output["kept"] as? Bool ?? false
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8

        let media = output["media_type"] as? String ?? "media type unknown"
        let bytes = (output["bytes"] as? Int).map { "\($0) bytes" } ?? "size unknown"
        let state = present ? (kept ? "Pinned" : "Available") : "Missing file"
        stack.addArrangedSubview(PageKit.label(
            "Asset \(assetID) · \(media) · \(bytes) · \(state)",
            size: 13, colour: present ? .labelColor : .systemRed, wrapping: true))

        if !present {
            stack.addArrangedSubview(PageKit.help(
                "The gateway no longer has this file. Reload the selected job after reattachment; no network request was made for the missing output."))
            return stack
        }

        let icon = NSImageView()
        icon.image = NSImage(systemSymbolName: lane.symbol, accessibilityDescription: nil)
        icon.contentTintColor = .secondaryLabelColor
        stack.addArrangedSubview(PageKit.row([icon, PageKit.label(
            "\(lane.title) result · asset \(assetID)", size: 13, colour: .secondaryLabelColor)]))

        let preview = CreateArtifactButton(
            title: "Preview", jobID: jobID, assetID: assetID, path: path, mediaType: media,
            kept: kept, target: self, action: #selector(previewArtifact(_:)))
        let open = CreateArtifactButton(
            title: "Open", jobID: jobID, assetID: assetID, path: path, mediaType: media,
            kept: kept, target: self, action: #selector(openArtifact(_:)))
        let save = CreateArtifactButton(
            title: "Save…", jobID: jobID, assetID: assetID, path: path, mediaType: media,
            kept: kept, target: self, action: #selector(saveArtifact(_:)))
        let keep = CreateArtifactButton(
            title: kept ? "Unpin" : "Pin", jobID: jobID, assetID: assetID, path: path, mediaType: media,
            kept: kept, target: self, action: #selector(keepArtifact(_:)))
        stack.addArrangedSubview(PageKit.row([preview, open, save, keep]))
        return stack
    }

    // MARK: artifact actions

    private func artifactKey(_ sender: CreateArtifactButton) -> String {
        "\(sender.jobID)/\(sender.assetID)"
    }

    private func safeAdminPath(_ path: String) -> String? {
        guard path.hasPrefix("/"), !path.hasPrefix("//"),
              URL(string: path)?.scheme == nil else { return nil }
        return path
    }

    private func downloadArtifact(
        _ sender: CreateArtifactButton,
        completion: @escaping (Data, URL) -> Void
    ) {
        let key = artifactKey(sender)
        if let data = localArtifactData[key], let url = localArtifactURLs[key] {
            completion(data, url)
            return
        }
        guard let path = safeAdminPath(sender.path) else {
            statusLabel.stringValue = "The gateway returned an invalid artifact path."
            return
        }
        statusLabel.stringValue = "downloading asset \(sender.assetID)…"
        let generation = jobRequestGeneration
        GatewayAPI.download(path) { [weak self] result in
            guard let self, self.currentJobID == sender.jobID,
                  self.jobRequestGeneration == generation else { return }
            switch result {
            case .failure(let error):
                self.statusLabel.stringValue = error.localizedDescription == "The requested file is no longer available."
                    ? "Asset \(sender.assetID) is missing from the gateway."
                    : "Could not download asset \(sender.assetID) — \(error.localizedDescription)"
            case .success(let data):
                let ext = PageKit.artifactExtension(sender.mediaType)
                let url = FileManager.default.temporaryDirectory
                    .appendingPathComponent("gateway-kh-\(sender.jobID)-\(sender.assetID).\(ext)")
                DispatchQueue.global(qos: .userInitiated).async {
                    guard (try? data.write(to: url, options: .atomic)) != nil else {
                        DispatchQueue.main.async {
                            self.statusLabel.stringValue = "Could not keep the downloaded asset locally."
                        }
                        return
                    }
                    DispatchQueue.main.async {
                        self.localArtifactData[key] = data
                        self.localArtifactURLs[key] = url
                        guard self.currentJobID == sender.jobID,
                              self.jobRequestGeneration == generation else { return }
                        self.statusLabel.stringValue = "Downloaded locally: \(url.path)"
                        completion(data, url)
                    }
                }
            }
        }
    }

    private var localArtifactData: [String: Data] = [:]

    @objc private func previewArtifact(_ sender: CreateArtifactButton) {
        downloadArtifact(sender) { [weak self] data, url in
            guard let self else { return }
            if self.lane.modality == "image", let image = NSImage(data: data) {
                let view = NSImageView(image: image)
                view.imageScaling = .scaleProportionallyDown
                view.translatesAutoresizingMaskIntoConstraints = false
                view.widthAnchor.constraint(lessThanOrEqualToConstant: 520).isActive = true
                view.heightAnchor.constraint(lessThanOrEqualToConstant: 340).isActive = true
                self.resultBox.addArrangedSubview(view)
            } else if self.lane.modality == "transcription" {
                self.showTextPreview(String(data: data, encoding: .utf8) ?? "The transcript came back empty.")
            } else {
                NSWorkspace.shared.open(url)
            }
        }
    }

    private func showTextPreview(_ value: String) {
        let text = NSTextView()
        text.isEditable = false
        text.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        text.string = value.isEmpty ? "The transcript came back empty." : value
        let scroll = NSScrollView()
        scroll.documentView = text
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.widthAnchor.constraint(lessThanOrEqualToConstant: 520).isActive = true
        scroll.heightAnchor.constraint(equalToConstant: 260).isActive = true
        resultBox.addArrangedSubview(scroll)
    }

    @objc private func openArtifact(_ sender: CreateArtifactButton) {
        downloadArtifact(sender) { _, url in NSWorkspace.shared.open(url) }
    }

    @objc private func saveArtifact(_ sender: CreateArtifactButton) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "\(lane.key)-\(sender.jobID.prefix(8))-\(sender.assetID).\(PageKit.artifactExtension(sender.mediaType))"
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        downloadArtifact(sender) { [weak self] data, _ in
            guard let self else { return }
            let statusLabel = self.statusLabel
            let filename = destination.lastPathComponent
            DispatchQueue.global(qos: .userInitiated).async {
                let success = (try? data.write(to: destination, options: .atomic)) != nil
                DispatchQueue.main.async {
                    statusLabel.stringValue = success
                        ? "Saved \(filename)."
                        : "Could not save \(filename)."
                }
            }
        }
    }

    @objc private func keepArtifact(_ sender: CreateArtifactButton) {
        let keep = !sender.kept
        sender.isEnabled = false
        GatewayAPI.post(
            "/admin/api/artifacts/keep",
            body: ["job_id": sender.jobID, "asset_id": sender.assetID, "keep": keep]
        ) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                sender.isEnabled = true
                self.statusLabel.stringValue = error.localizedDescription
            case .success:
                sender.kept = keep
                sender.title = keep ? "Unpin" : "Pin"
                sender.isEnabled = true
                self.statusLabel.stringValue = keep
                    ? "Pinned — retention will protect this output."
                    : "Unpinned — retention may remove this output."
            }
        }
    }
}
