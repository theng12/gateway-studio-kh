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
    /// The app's own bundle. Only this process may touch the owner's real saved state.
    private static let appBundleID = "kh.gateway.menubar"
    /// Everything else -- a test binary, a fixture, a build run from a shell -- keeps its state in
    /// memory. A fixture id ("mock-job") once reached the owner's real Create page because the
    /// only isolation was a test remembering to set an environment variable, and a test that forgot
    /// wrote to the production suite. Now forgetting is harmless: there is no path to it.
    private static var memory: [String: Data] = [:]

    private static var defaults: UserDefaults? {
        if let name = ProcessInfo.processInfo.environment["GATEWAY_NATIVE_DEFAULTS_SUITE"] {
            return UserDefaults(suiteName: name)
        }
        return Bundle.main.bundleIdentifier == appBundleID ? UserDefaults(suiteName: "kh.gateway.create") : nil
    }

    static func load(lane: String) -> CreateWorkspaceState {
        let data = defaults != nil ? defaults?.data(forKey: prefix + lane) : memory[prefix + lane]
        guard let data, let state = try? JSONDecoder().decode(CreateWorkspaceState.self, from: data)
        else { return .empty }
        return state
    }

    static func save(_ state: CreateWorkspaceState, lane: String) {
        guard let data = try? JSONEncoder().encode(state) else { return }
        if let defaults { defaults.set(data, forKey: prefix + lane) } else { memory[prefix + lane] = data }
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
    /// What the result pane is called, and what it says before there is a result.
    var previewTitle = "Preview"
    var emptyTitle = "Nothing generated yet"
    var emptyDetail = "Fill in the form and press Generate. Anything you make here is temporary until you save it or keep it."
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
            ],
            previewTitle: "Preview", emptyTitle: "Your audio will appear here",
            emptyDetail: "Write a script, pick a voice and generate. You can play it here, then save it."
        ),
        CreateLane(
            key: "subtitle", title: "Subtitle", symbol: "text.bubble",
            modality: "transcription",
            blurb: "Import a recording and read its transcript.",
            fields: [], isUpload: true,
            previewTitle: "Transcript", emptyTitle: "Your transcript will appear here",
            emptyDetail: "Choose a recording and generate. The words come back with their timestamps, ready to copy or save as SRT."
        ),
        CreateLane(
            key: "image", title: "Image", symbol: "photo", modality: "image",
            blurb: "Describe an image, choose a model and generate it.",
            fields: [
                CreateField(key: "prompt", label: "Prompt", kind: .script,
                            placeholder: "Describe the image…"),
                CreateField(key: "n", label: "How many", kind: .number, initial: "1",
                            hint: "A provider that answers several variations returns all of them."),
            ],
            previewTitle: "Preview", emptyTitle: "Your image will appear here",
            emptyDetail: "Describe an image and generate it. Several results show as thumbnails under the large preview."
        ),
        CreateLane(
            key: "music", title: "Music", symbol: "music.note", modality: "music",
            blurb: "Describe the music you want.",
            fields: [
                CreateField(key: "prompt", label: "Describe", kind: .script,
                            placeholder: "calm piano, late night…"),
                CreateField(key: "style", label: "Style", kind: .text, placeholder: "Classical"),
                CreateField(key: "title", label: "Title", kind: .text, placeholder: "Peaceful"),
            ],
            previewTitle: "Preview", emptyTitle: "Your music will appear here",
            emptyDetail: "Describe the music and generate it. You can play it here, then save it."
        ),
        CreateLane(
            key: "video", title: "Video", symbol: "film", modality: "video",
            blurb: "Describe the shot and generate it.",
            fields: [
                CreateField(key: "prompt", label: "Prompt", kind: .script,
                            placeholder: "Describe the shot…"),
            ],
            previewTitle: "Preview", emptyTitle: "Your video will appear here",
            emptyDetail: "Describe the shot and generate it. Video can take a few minutes; it plays here when it is ready."
        ),
        CreateLane(
            key: "chat", title: "Chat", symbol: "bubble.left.and.bubble.right",
            modality: "chat",
            blurb: "Ask a model directly.",
            fields: [
                CreateField(key: "prompt", label: "Message", kind: .script,
                            placeholder: "Ask anything…"),
            ],
            previewTitle: "Conversation", emptyTitle: "No messages yet",
            emptyDetail: "The first message starts the conversation."
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
    private var restoredJob = false
    var view: NSView { workspace }

    func activated() {
        workspace.refreshModels()
        // The result this page was showing when the app last closed comes back once, when the
        // page is first opened, rather than waiting for a "Reload" click.
        if !restoredJob {
            restoredJob = true
            workspace.restoreSelectedJob()
        }
    }
}

// MARK: - Workspace

/// One output of the job on screen.
private struct CreateOutput {
    let jobID: String
    let assetID: String
    let path: String
    let mediaType: String
    let bytes: Int?
    let present: Bool
    var kept: Bool

    var key: String { "\(jobID)/\(assetID)" }
    var facts: String {
        let size = bytes.map { "\($0) bytes" } ?? "size unknown"
        return "Asset \(assetID) · \(mediaType.isEmpty ? "media type unknown" : mediaType) · \(size) · "
            + (present ? (kept ? "Pinned" : "Available") : "Missing file")
    }
}

/// Studio Hub's generator page: the form on the left, the result on the right, one shared frame
/// (`CreatePageLayout`) for all six lanes. What differs per lane is only the fields and the preview.
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
    private var savedArtifactURLs: [String: URL] = [:]
    private var fileButton: NSButton?
    private var fileHelp: NSTextField?
    private var fileURL: URL?
    /// Why the chosen file cannot be sent, said before it is read rather than after the upload.
    private var fileProblem: String?
    /// The gateway refuses a transcription upload past this (api.py `_MAX_UPLOAD_BYTES`).
    private static let maxUploadBytes = 25 * 1024 * 1024
    private var capabilityLabel: NSTextField?
    private var cancelButton: NSButton?
    private var retryAttemptButton: NSButton?
    private var newDraftButton: NSButton?

    private let layout: CreatePageLayout
    private let statusLabel = PageKit.label("", size: 12, colour: .secondaryLabelColor, wrapping: true)
    private let generateButton = NSButton(title: "Generate", target: nil, action: nil)
    // The result pane's actions.
    private let saveButton = NSButton(title: "Save…", target: nil, action: nil)
    private let copyButton = NSButton(
        image: NSImage(systemSymbolName: "doc.on.doc", accessibilityDescription: "Copy") ?? NSImage(),
        target: nil, action: nil)
    private let moreButton = NSPopUpButton(frame: .zero, pullsDown: true)
    private var openItem: NSMenuItem!
    private var revealItem: NSMenuItem!
    private var pinItem: NSMenuItem!
    private var reloadItem: NSMenuItem!
    private var clearItem: NSMenuItem!

    /// The outputs of the job on screen, and which one Save / Open / Pin act on.
    private var outputs: [CreateOutput] = []
    private var selectedOutput = 0
    private var gallery: ImageGalleryView?
    /// What Copy puts on the pasteboard: the transcript, or the model's last reply.
    private var copyableText: String?
    private var chatTurns: [ChatTurn] = []
    private var chatRecordedJobs: Set<String> = []

    init(lane: CreateLane) {
        self.lane = lane
        self.layout = CreatePageLayout(title: lane.title, blurb: lane.blurb, previewTitle: lane.previewTitle)
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
        }
        updateActions()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    // MARK: layout

    private func build() {
        layout.translatesAutoresizingMaskIntoConstraints = false
        addSubview(layout)
        NSLayoutConstraint.activate([
            layout.topAnchor.constraint(equalTo: topAnchor),
            layout.leadingAnchor.constraint(equalTo: leadingAnchor),
            layout.trailingAnchor.constraint(equalTo: trailingAnchor),
            layout.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
        buildForm()
        buildPreviewActions()
        showMessage(lane.emptyTitle, lane.emptyDetail, symbol: lane.symbol)
    }

    /// A titled block of the form: the title in Studio Hub's `.headline` weight, the control at the
    /// full width of the column, and an optional hint under it.
    private func section(_ title: String, _ control: NSView, hint: String = "", fullWidth: Bool = true) -> NSView {
        let head = PageKit.label(title, size: 13, weight: .semibold)
        let stack = NSStackView(views: [head, control])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        if fullWidth { control.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true }
        if !hint.isEmpty {
            let help = wrapped(PageKit.help(hint))
            stack.addArrangedSubview(help)
            help.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        return stack
    }

    /// A wrapping label is as wide as its column, so it breaks where the column ends.
    private func wrapped(_ label: NSTextField) -> NSTextField {
        label.preferredMaxLayoutWidth = 0
        return label
    }

    private func divider() -> NSView {
        let line = NSBox()
        line.boxType = .separator
        return line
    }

    private func buildForm() {
        if lane.isUpload {
            let choose = NSButton(title: "Choose File…", target: self, action: #selector(pickFile))
            choose.bezelStyle = .rounded
            fileButton = choose
            fileHelp = PageKit.help("No file chosen")
            let column = NSStackView(views: [choose, fileHelp!])
            column.orientation = .vertical
            column.alignment = .leading
            column.spacing = 6
            fileHelp!.preferredMaxLayoutWidth = 0
            fileHelp!.widthAnchor.constraint(equalTo: column.widthAnchor).isActive = true
            layout.addForm(section("Recording", column))
        }

        for field in lane.fields {
            let control = makeControl(field)
            fields[field.key] = control
            layout.addForm(section(field.label, control, hint: field.hint, fullWidth: field.kind != .number))
        }

        generateButton.target = self
        generateButton.action = #selector(generate)
        generateButton.bezelStyle = .rounded
        generateButton.controlSize = .large
        generateButton.keyEquivalent = "\r"
        // Studio Hub's `.borderedProminent`: the one control the page is built around is the
        // accent colour, also while the window is not key.
        generateButton.bezelColor = .controlAccentColor
        generateButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 110).isActive = true
        cancelButton = NSButton(title: "Cancel", target: self, action: #selector(cancel))
        cancelButton?.bezelStyle = .rounded
        cancelButton?.controlSize = .large
        cancelButton?.isHidden = true
        retryAttemptButton = NSButton(title: "Retry same attempt", target: self, action: #selector(retryAttempt))
        retryAttemptButton?.bezelStyle = .rounded
        retryAttemptButton?.isHidden = true
        newDraftButton = NSButton(title: "Start separate draft", target: self, action: #selector(startNewDraft))
        newDraftButton?.bezelStyle = .rounded
        newDraftButton?.isHidden = true
        let actions = NSStackView(views: [
            PageKit.row([generateButton, cancelButton!]), retryAttemptButton!, newDraftButton!,
        ])
        actions.orientation = .vertical
        actions.alignment = .leading
        actions.spacing = 8
        layout.addForm(actions)
        layout.addForm(wrapped(statusLabel))

        layout.addForm(divider())
        let picker = NSPopUpButton()
        picker.target = self
        picker.action = #selector(modelChanged)
        modelPopup = picker
        let refresh = NSButton(
            image: NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "Refresh models") ?? NSImage(),
            target: self, action: #selector(refreshModels))
        refresh.bezelStyle = .rounded
        refresh.toolTip = "Refresh models"
        let pickerRow = PageKit.row([picker, refresh], spacing: 8)
        picker.setContentHuggingPriority(NSLayoutConstraint.Priority(1), for: .horizontal)
        capabilityLabel = PageKit.help("Capabilities: unknown")
        let manage = NSButton(title: "Manage \(lane.title) Models", target: self, action: #selector(openModels))
        manage.bezelStyle = .rounded
        let modelColumn = NSStackView(views: [pickerRow, manage])
        modelColumn.orientation = .vertical
        modelColumn.alignment = .leading
        modelColumn.spacing = 10
        pickerRow.widthAnchor.constraint(equalTo: modelColumn.widthAnchor).isActive = true
        let modelBlock = section(
            "Model", modelColumn,
            hint: "Only models switched on for this lane appear here — the same set Story Studio picks from.")
        layout.addForm(modelBlock)
        layout.addForm(wrapped(capabilityLabel!))
    }

    private func buildPreviewActions() {
        saveButton.target = self
        saveButton.action = #selector(saveArtifact)
        saveButton.bezelStyle = .rounded
        copyButton.target = self
        copyButton.action = #selector(copyText)
        copyButton.bezelStyle = .rounded
        copyButton.toolTip = "Copy the transcript"

        moreButton.addItem(withTitle: "")
        moreButton.item(at: 0)?.image = NSImage(systemSymbolName: "ellipsis.circle", accessibilityDescription: "More")
        moreButton.isBordered = false
        moreButton.autoenablesItems = false
        func item(_ title: String, _ action: Selector) -> NSMenuItem {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            moreButton.menu?.addItem(item)
            return item
        }
        openItem = item("Open", #selector(openArtifact))
        revealItem = item("Show in Finder", #selector(revealArtifact))
        pinItem = item("Pin", #selector(keepArtifact))
        clearItem = item("Clear Conversation", #selector(clearConversation))
        reloadItem = item("Reload Result", #selector(loadSelectedJob))
        _ = item("All Jobs", #selector(openJobs))
        for view in [copyButton, saveButton, moreButton] { layout.previewActions.addArrangedSubview(view) }
    }

    private func makeControl(_ field: CreateField) -> NSView {
        switch field.kind {
        case .script:
            let text = NSTextView()
            text.font = .systemFont(ofSize: 14)
            text.isRichText = false
            text.string = field.initial
            text.isVerticallyResizable = true
            text.autoresizingMask = [.width]
            text.textContainerInset = NSSize(width: 6, height: 8)
            let scroll = NSScrollView()
            scroll.documentView = text
            scroll.hasVerticalScroller = true
            scroll.borderType = .bezelBorder
            scroll.translatesAutoresizingMaskIntoConstraints = false
            scroll.heightAnchor.constraint(equalToConstant: 170).isActive = true
            return scroll
        case .number, .text:
            let entry = NSTextField(string: field.initial)
            entry.placeholderString = field.placeholder
            entry.font = .systemFont(ofSize: 13)
            // A count is a short number; it should not stretch across the column.
            if field.kind == .number { entry.widthAnchor.constraint(equalToConstant: 90).isActive = true }
            return entry
        case .voice:
            let combo = NSComboBox()
            combo.isEditable = true
            combo.completes = true
            combo.font = .systemFont(ofSize: 13)
            combo.placeholderString = "Search compatible voices"
            combo.target = self
            combo.action = #selector(voiceChanged)
            voiceComboBox = combo

            let advanced = NSTextField()
            advanced.placeholderString = "Advanced provider voice ID (exact)"
            advanced.font = .systemFont(ofSize: 13)
            advancedVoiceField = advanced

            let stack = NSStackView(views: [combo, advanced])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 6
            combo.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            advanced.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            return stack
        }
    }

    @objc private func openModels() { MainWindow.shared.show(section: "models") }
    @objc private func openJobs() { MainWindow.shared.show(section: "jobs") }

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
                showFile(url)
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

    /// Whether a message about the models may take the status line: not while a job is running or an
    /// unresolved submission is waiting for a decision. (A *finished* job's "succeeded" does not
    /// count: a page with no model to run must say so.)
    private var modelStatusIsFree: Bool {
        pendingJobID == nil && !submissionInFlight && ambiguousAttempt == nil
    }

    @objc func refreshModels() {
        modelRefreshRequest += 1
        let requestID = modelRefreshRequest
        if modelStatusIsFree {
            statusLabel.stringValue = "loading models…"
        }
        GatewayAPI.get("/admin/api/create/models") { [weak self] result in
            guard let self else { return }
            guard self.modelRefreshRequest == requestID else { return }
            switch result {
            case .failure(let error):
                if self.modelStatusIsFree {
                    self.statusLabel.stringValue = error.localizedDescription
                }
            case .success(let payload):
                let byModality = payload["by_modality"] as? [String: Any] ?? [:]
                let rows = byModality[self.lane.modality] as? [[String: Any]] ?? []
                let previousModelID = self.selectedModelID()
                self.models = rows
                self.modelPopup?.removeAllItems()
                if rows.isEmpty {
                    // The remembered choice is kept: switch the model back on and it is selected again.
                    self.modelPopup?.addItem(withTitle: "No model switched on")
                    self.modelPopup?.lastItem?.isEnabled = false
                    if self.modelStatusIsFree {
                        // Say what is wrong and where to fix it -- not "no longer available", which
                        // reads as a model that vanished rather than one that was never switched on.
                        self.statusLabel.stringValue =
                            "No \(self.lane.title.lowercased()) models are switched on. Open Models and switch one on — it appears here."
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
                    // A model can be renamed without being removed: the catalogue lists the
                    // ids it used to publish under `legacy_ids`. A saved choice that matches one
                    // is the same model, so it is kept under its current id rather than reported
                    // as gone (Subtitle showed "no longer available" for exactly this).
                    if let previousModelID,
                       let index = rows.firstIndex(where: {
                           $0["id"] as? String == previousModelID
                               || ($0["legacy_ids"] as? [String])?.contains(previousModelID) == true
                       }) {
                        self.selectedModelIDHint = rows[index]["id"] as? String
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
                    if self.modelStatusIsFree {
                        self.statusLabel.stringValue = selectionLost
                            ? "\(previousModelID ?? "The model you chose") is no longer switched on. Select another model to continue, or open Models to switch it back on."
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
            let low = unknown(row["minimum_speed"]), high = unknown(row["maximum_speed"])
            lines.append("Speed: " + (low == "unknown" && high == "unknown" ? "unknown" : "\(low)–\(high)"))
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
            && fileProblem == nil
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
        showFile(url)
        updateGenerateEnabled()
        persistState()
    }

    /// Show the path, or say the file is too big — from its size on disk, without reading it.
    private func showFile(_ url: URL) {
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0
        fileProblem = size > Self.maxUploadBytes
            ? "This file is \(size / (1024 * 1024)) MB; the gateway accepts at most "
                + "\(Self.maxUploadBytes / (1024 * 1024)) MB. Choose a shorter or compressed recording."
            : nil
        fileHelp?.stringValue = fileProblem ?? url.path
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

    /// Read and hash a recording off the main thread: it can be tens of megabytes.
    /// `data` is nil when the file is unreadable, empty or past the upload limit.
    private func readUpload(_ url: URL, then done: @escaping (Data?, String?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0
            let data = size > Self.maxUploadBytes ? nil : try? Data(contentsOf: url)
            let hash = data.map { self.digest($0) }
            DispatchQueue.main.async { done(data?.isEmpty == false ? data : nil, hash) }
        }
    }

    private func makeAttempt(modelID: String) -> CreateAttempt? {
        let key = UUID().uuidString
        var params: [String: Any] = [:]
        for field in lane.fields {
            let raw = value(of: field.key)
            if raw.isEmpty { continue }
            params[field.key] = field.kind == .number ? (Int(raw) ?? 1) : raw
        }
        if lane.modality == "chat" {
            // The whole conversation so far, so the model answers in context.
            params["messages"] = chatHistory() + [["role": "user", "content": params["prompt"] as? String ?? ""]]
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
            readUpload(URL(fileURLWithPath: uploadPath)) { [weak self] data, hash in
                guard let self, self.activeAttempt?.key == attempt.key else { return }
                guard let data else {
                    self.refuseChangedUpload(attempt, reason: "The saved upload is missing.")
                    return
                }
                guard attempt.uploadBytes == data.count, attempt.uploadDigest == hash else {
                    self.refuseChangedUpload(attempt, reason: "The saved upload changed since the attempt was recorded.")
                    return
                }
                GatewayAPI.upload(
                    "/admin/api/create/transcription",
                    fileField: "file", filename: name, data: data,
                    fields: ["model_id": attempt.modelID, "idempotency_key": attempt.key]
                ) { [weak self] result in
                    self?.handleSubmit(result, attemptKey: attempt.key)
                }
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
        guard let modelID = selectedModelID(), selectedModelRow() != nil else {
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
        if lane.isUpload {
            guard let url = fileURL else {
                statusLabel.stringValue = "Choose a recording first."
                return
            }
            if let problem = fileProblem {
                statusLabel.stringValue = problem
                return
            }
            generateButton.isEnabled = false
            statusLabel.stringValue = "reading the recording…"
            readUpload(url) { [weak self] data, hash in
                guard let self else { return }
                guard let data, let hash else {
                    self.statusLabel.stringValue = "Choose a recording first."
                    self.updateGenerateEnabled()
                    return
                }
                self.begin(CreateAttempt(
                    key: UUID().uuidString, modelID: modelID, modality: self.lane.modality, body: nil,
                    uploadPath: url.path, uploadName: url.lastPathComponent,
                    uploadType: "application/octet-stream", uploadBytes: data.count,
                    uploadDigest: hash
                ))
            }
            return
        }
        guard let attempt = makeAttempt(modelID: modelID) else { return }
        if lane.modality == "chat" {
            chatTurns.append(ChatTurn(role: "user", text: value(of: "prompt")))
            showConversation(waiting: true)
        }
        begin(attempt)
    }

    /// The completed turns, as chat messages. A message that got no reply is left out, with the
    /// failure that followed it, so a failed try never becomes part of what the model is asked.
    private func chatHistory() -> [[String: String]] {
        var messages: [[String: String]] = []
        for (index, turn) in chatTurns.enumerated() where !turn.failed {
            let next = index + 1 < chatTurns.count ? chatTurns[index + 1] : nil
            if turn.role == "user", next?.failed == true { continue }
            messages.append(["role": turn.role == "user" ? "user" : "assistant", "content": turn.text])
        }
        return messages
    }

    /// The message field empties once the message is on the wire and in the transcript.
    private func clearPrompt() {
        setValue("", for: "prompt")
    }

    private func begin(_ attempt: CreateAttempt) {
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
            if lane.modality == "chat" { clearPrompt() }
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

    /// Re-attach the job this page was showing when the app last closed.
    func restoreSelectedJob() {
        guard currentJobID != nil, pendingJobID == nil, !submissionInFlight, ambiguousAttempt == nil else { return }
        loadJob(automatic: true)
    }

    @objc private func loadSelectedJob() { loadJob(automatic: false) }

    private func loadJob(automatic: Bool) {
        guard let id = currentJobID else { return }
        jobRequestGeneration += 1
        let requestGeneration = jobRequestGeneration
        if !automatic { statusLabel.stringValue = "loading selected job…" }
        GatewayAPI.get("/admin/api/create/status/\(id)") { [weak self] result in
            guard let self else { return }
            guard self.jobRequestGeneration == requestGeneration,
                  self.currentJobID == id else { return }
            switch result {
            case .failure(let error):
                // A saved id the gateway has never heard of (pruned, deleted, or never real) is not
                // a result to offer "reloading": forget it and show the empty page.
                if let refused = error as? GatewayAPIError, case .refused(404, _) = refused {
                    self.forgetJob()
                    return
                }
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
                if terminal {
                    if !automatic { self.statusLabel.stringValue = "Job reloaded; outputs were reattached from the gateway." }
                } else {
                    self.statusLabel.stringValue = "Job \(id.prefix(8)) is still \(state)."
                    self.startPolling(id)
                }
            }
        }
    }

    /// Drop a selected job the gateway does not know, and go back to the empty page.
    private func forgetJob() {
        currentJobID = nil
        pendingJobID = nil
        outputs = []
        persistState()
        showMessage(lane.emptyTitle, lane.emptyDetail, symbol: lane.symbol)
        updateActions()
        setEditingEnabled(ambiguousAttempt == nil)
        refreshModels()
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

    /// A message centred in the result card: the empty state, a running job, a failure.
    private func messageView(
        _ title: String, _ detail: String, symbol: String,
        spinning: Bool = false, colour: NSColor = .secondaryLabelColor
    ) -> NSView {
        let icon: NSView
        if spinning {
            let spin = NSProgressIndicator()
            spin.style = .spinning
            spin.startAnimation(nil)
            icon = spin
        } else {
            let image = NSImageView()
            image.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
            image.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 34, weight: .light)
            image.contentTintColor = colour
            icon = image
        }
        let heading = PageKit.label(title, size: 15, weight: .semibold, colour: colour == .secondaryLabelColor ? .labelColor : colour)
        var views = [icon, heading]
        if !detail.isEmpty {
            let words = PageKit.label(detail, size: 13, colour: .secondaryLabelColor, wrapping: true)
            words.alignment = .center
            words.preferredMaxLayoutWidth = 0
            words.widthAnchor.constraint(lessThanOrEqualToConstant: 440).isActive = true
            views.append(words)
        }
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        let container = NSView()
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: container.leadingAnchor),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: container.trailingAnchor),
            stack.topAnchor.constraint(greaterThanOrEqualTo: container.topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor),
        ])
        return container
    }

    private func showMessage(
        _ title: String, _ detail: String, symbol: String,
        spinning: Bool = false, colour: NSColor = .secondaryLabelColor
    ) {
        layout.setPreview(messageView(title, detail, symbol: symbol, spinning: spinning, colour: colour))
    }

    private func showConversation(waiting: Bool = false) {
        outputs = []
        copyableText = chatTurns.last { $0.role != "user" && !$0.failed }?.text
        layout.setCaption("")
        if chatTurns.isEmpty && !waiting {
            showMessage(lane.emptyTitle, lane.emptyDetail, symbol: lane.symbol)
        } else {
            var footer: NSView?
            if waiting {
                let spin = NSProgressIndicator()
                spin.style = .spinning
                spin.controlSize = .small
                spin.startAnimation(nil)
                footer = PageKit.row([spin, PageKit.label("The model is answering…", size: 13, colour: .secondaryLabelColor)])
            }
            let view = ConversationView(turns: chatTurns, footer: footer)
            layout.setPreview(view)
            DispatchQueue.main.async { view.scrollToEnd() }
        }
        updateActions()
    }

    private func recordChat(_ jobID: String, _ turn: ChatTurn) {
        guard chatRecordedJobs.insert(jobID).inserted else { return }
        chatTurns.append(turn)
    }

    private func render(_ job: [String: Any]) {
        let state = job["state"] as? String ?? ""
        let jobID = job["id"] as? String ?? currentJobID ?? ""
        let cancellationRequested = job["cancel_requested"] as? Bool == true
        let isChat = lane.modality == "chat"
        statusLabel.stringValue = cancellationRequested && state != "cancelled"
            ? "Cancellation requested — this job may still complete."
            : state == "succeeded" ? "Finished." : state

        if state == "failed" {
            let reason = job["failure_reason"] as? String ?? job["error_detail"] as? String ?? "the generation failed"
            if isChat {
                recordChat(jobID, ChatTurn(role: "model", text: reason, failed: true))
                showConversation()
            } else {
                outputs = []
                showMessage("The generation failed", reason, symbol: "exclamationmark.triangle", colour: .systemRed)
                updateActions()
            }
            return
        }
        if state == "cancelled" {
            outputs = []
            copyableText = nil
            showMessage("Cancelled", "The generation was cancelled. No automatic replay was made.", symbol: "xmark.circle")
            updateActions()
            return
        }
        if state != "succeeded" {
            if isChat {
                showConversation(waiting: true)
            } else {
                outputs = []
                showMessage(
                    cancellationRequested ? "Cancellation requested" : "\(state)…",
                    cancellationRequested
                        ? "The provider may still finish; anything it makes is kept."
                        : "This page updates by itself. You can leave and come back.",
                    symbol: lane.symbol, spinning: true)
                updateActions()
            }
            return
        }

        if isChat {
            recordChat(jobID, ChatTurn(role: "model", text: job["output_text"] as? String ?? ""))
            showConversation()
            return
        }

        let raw = job["outputs"] as? [[String: Any]] ?? []
        outputs = raw.map { output in
            let assetID = output["asset_id"] as? String ?? "0"
            return CreateOutput(
                jobID: jobID, assetID: assetID,
                path: output["url"] as? String ?? "/admin/api/artifacts/\(jobID)/outputs/\(assetID)",
                mediaType: output["media_type"] as? String ?? "",
                bytes: output["bytes"] as? Int,
                present: output["present"] as? Bool ?? true,
                kept: output["kept"] as? Bool ?? false)
        }
        selectedOutput = 0
        copyableText = nil
        gallery = nil
        if outputs.isEmpty {
            showMessage("No result", "The job finished but produced no artifact.", symbol: lane.symbol)
            layout.setCaption("")
            updateActions()
        } else if lane.modality == "image" {
            presentGallery()
        } else {
            present(0)
        }
    }

    private func updateCaption() {
        guard outputs.indices.contains(selectedOutput) else { return layout.setCaption("") }
        let prefix = outputs.count > 1 ? "\(outputs.count) results — every one the model returned · " : ""
        layout.setCaption(prefix + outputs[selectedOutput].facts)
    }

    /// Several images: one large, the rest as thumbnails. Every output is fetched, because the
    /// thumbnails are the only way to choose between them.
    private func presentGallery() {
        guard outputs.contains(where: { $0.present }) else {
            showMessage(
                "The files are gone",
                "The gateway no longer has these files. Reload the result after reattachment; no network request was made for the missing outputs.",
                symbol: "exclamationmark.triangle", colour: .systemRed)
            updateCaption()
            updateActions()
            return
        }
        let view = ImageGalleryView(count: outputs.count)
        view.onSelect = { [weak self] index in
            self?.selectedOutput = index
            self?.updateCaption()
            self?.updateActions()
        }
        gallery = view
        layout.setPreview(view)
        updateCaption()
        updateActions()
        let missing = NSImage(systemSymbolName: "exclamationmark.triangle", accessibilityDescription: "Missing")
        for (index, output) in outputs.enumerated() {
            guard output.present else { view.setImage(missing, at: index); continue }
            fetch(output) { [weak view] data, _ in
                view?.setImage(NSImage(data: data) ?? missing, at: index)
            }
        }
    }

    /// Show one output. A model that returned several (music often returns two) gets a picker.
    private func present(_ index: Int) {
        guard outputs.indices.contains(index) else { return }
        selectedOutput = index
        copyableText = nil
        let output = outputs[index]
        updateCaption()
        updateActions()
        guard output.present else {
            layout.setPreview(picker(messageView(
                "The file is gone",
                "The gateway no longer has this file. Reload the result after reattachment; no network request was made for the missing output.",
                symbol: "exclamationmark.triangle", colour: .systemRed)))
            return
        }
        layout.setPreview(picker(messageView("Loading…", "", symbol: lane.symbol, spinning: true)))
        fetch(output) { [weak self] data, url in
            guard let self, self.selectedOutput == index, self.outputs.indices.contains(index),
                  self.outputs[index].key == output.key else { return }
            self.layout.setPreview(self.picker(self.mediaView(output, data, url)))
            self.updateActions()
        }
    }

    private func picker(_ content: NSView) -> NSView {
        guard outputs.count > 1 else { return content }
        let segments = NSSegmentedControl(
            labels: (1...outputs.count).map { "Result \($0)" },
            trackingMode: .selectOne, target: self, action: #selector(pickResult(_:)))
        segments.selectedSegment = selectedOutput
        let stack = NSStackView(views: [segments, content])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        content.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        content.setContentHuggingPriority(NSLayoutConstraint.Priority(1), for: .vertical)
        return stack
    }

    @objc private func pickResult(_ sender: NSSegmentedControl) { present(sender.selectedSegment) }

    private func mediaView(_ output: CreateOutput, _ data: Data, _ url: URL) -> NSView {
        switch lane.modality {
        case "tts", "music":
            let player = AudioPlayerView(url: url)
            if player.canPlay { return player }
        case "video":
            return VideoPreviewView(url: url)
        case "transcription":
            let text = String(data: data, encoding: .utf8) ?? ""
            if text.isEmpty {
                return messageView("The transcript came back empty", "The model heard no speech in this recording.", symbol: "text.bubble")
            }
            copyableText = text
            return TranscriptText.view(text)
        default: break
        }
        return messageView(
            "No preview here",
            "This \(output.mediaType) can't be shown in the page. Use Open or Save… to see it.",
            symbol: lane.symbol)
    }

    // MARK: actions

    private var selected: CreateOutput? { outputs.indices.contains(selectedOutput) ? outputs[selectedOutput] : nil }

    private func updateActions() {
        let has = selected?.present == true
        let isChat = lane.modality == "chat"
        saveButton.isHidden = !has
        copyButton.isHidden = copyableText == nil
        for item in [openItem, revealItem, pinItem] {
            item?.isEnabled = has
            item?.isHidden = isChat
        }
        pinItem?.title = selected?.kept == true ? "Unpin" : "Pin"
        clearItem?.isHidden = !isChat
        clearItem?.isEnabled = !chatTurns.isEmpty && pendingJobID == nil
        reloadItem?.isHidden = isChat
        reloadItem?.isEnabled = currentJobID != nil
    }

    private func safeAdminPath(_ path: String) -> String? {
        guard path.hasPrefix("/"), !path.hasPrefix("//"),
              URL(string: path)?.scheme == nil else { return nil }
        return path
    }

    /// The output's bytes and a local copy of them, downloaded once through the authenticated helper.
    private func fetch(_ output: CreateOutput, completion: @escaping (Data, URL) -> Void) {
        let key = output.key
        if let data = localArtifactData[key], let url = localArtifactURLs[key] {
            completion(data, url)
            return
        }
        guard let path = safeAdminPath(output.path) else {
            statusLabel.stringValue = "The gateway returned an invalid artifact path."
            return
        }
        let generation = jobRequestGeneration
        GatewayAPI.download(path) { [weak self] result in
            guard let self, self.currentJobID == output.jobID,
                  self.jobRequestGeneration == generation else { return }
            switch result {
            case .failure(let error):
                self.statusLabel.stringValue = error.localizedDescription == "The requested file is no longer available."
                    ? "Asset \(output.assetID) is missing from the gateway."
                    : "Could not download asset \(output.assetID) — \(error.localizedDescription)"
                if self.outputs.indices.contains(self.selectedOutput), self.outputs[self.selectedOutput].key == key,
                   self.lane.modality != "image" {
                    self.layout.setPreview(self.picker(self.messageView(
                        "Could not load this result", error.localizedDescription,
                        symbol: "exclamationmark.triangle", colour: .systemRed)))
                }
            case .success(let data):
                let ext = PageKit.artifactExtension(output.mediaType)
                let url = FileManager.default.temporaryDirectory
                    .appendingPathComponent("gateway-kh-\(output.jobID)-\(output.assetID).\(ext)")
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
                        guard self.currentJobID == output.jobID,
                              self.jobRequestGeneration == generation else { return }
                        completion(data, url)
                    }
                }
            }
        }
    }

    private var localArtifactData: [String: Data] = [:]

    @objc private func openArtifact() {
        guard let output = selected, output.present else { return }
        fetch(output) { _, url in NSWorkspace.shared.open(url) }
    }

    /// Show the saved copy in Finder, or the downloaded one if nothing was saved yet.
    @objc private func revealArtifact() {
        guard let output = selected, output.present else { return }
        if let saved = savedArtifactURLs[output.key], FileManager.default.fileExists(atPath: saved.path) {
            NSWorkspace.shared.activateFileViewerSelecting([saved])
        } else {
            fetch(output) { _, url in NSWorkspace.shared.activateFileViewerSelecting([url]) }
        }
    }

    @objc private func saveArtifact() {
        guard let output = selected, output.present else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "\(lane.key)-\(output.jobID.prefix(8))-\(output.assetID).\(PageKit.artifactExtension(output.mediaType))"
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        fetch(output) { [weak self] data, _ in
            guard let self else { return }
            let statusLabel = self.statusLabel
            let filename = destination.lastPathComponent
            DispatchQueue.global(qos: .userInitiated).async {
                let success = (try? data.write(to: destination, options: .atomic)) != nil
                DispatchQueue.main.async {
                    if success { self.savedArtifactURLs[output.key] = destination }
                    statusLabel.stringValue = success
                        ? "Saved \(filename)."
                        : "Could not save \(filename)."
                }
            }
        }
    }

    @objc private func copyText() {
        guard let text = copyableText else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        statusLabel.stringValue = "Copied."
    }

    @objc private func clearConversation() {
        chatTurns = []
        showConversation()
    }

    @objc private func keepArtifact() {
        guard let output = selected, output.present else { return }
        let keep = !output.kept
        let index = selectedOutput
        pinItem.isEnabled = false
        GatewayAPI.post(
            "/admin/api/artifacts/keep",
            body: ["job_id": output.jobID, "asset_id": output.assetID, "keep": keep]
        ) { [weak self] result in
            guard let self else { return }
            self.pinItem.isEnabled = true
            switch result {
            case .failure(let error):
                self.statusLabel.stringValue = error.localizedDescription
            case .success:
                if self.outputs.indices.contains(index), self.outputs[index].key == output.key {
                    self.outputs[index].kept = keep
                    self.updateCaption()
                    self.updateActions()
                }
                self.statusLabel.stringValue = keep
                    ? "Pinned — retention will protect this output."
                    : "Unpinned — retention may remove this output."
            }
        }
    }
}
