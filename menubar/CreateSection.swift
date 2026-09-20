import AppKit

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

/// Studio Hub's Create house frame: a heading, then the form on the left and the
/// result on the right, with the output area holding its empty state until there
/// is something to show.
final class CreateWorkspace: NSView {
    private let lane: CreateLane
    private var fields: [String: NSView] = [:]
    private var modelPopup: NSPopUpButton?
    private var models: [[String: Any]] = []
    private var pollTimer: Timer?
    private var pollInFlight = false
    private var currentJobID: String?
    private var pendingJobID: String?
    private var submissionInFlight = false
    private var hasLoadedModels = false
    private var modelRefreshRequest = 0
    private var fileButton: NSButton?
    private var fileURL: URL?

    private let resultBox = NSStackView()
    private let statusLabel = PageKit.label("", size: 12, colour: .secondaryLabelColor, wrapping: true)
    private let generateButton = NSButton(title: "Generate", target: nil, action: nil)

    init(lane: CreateLane) {
        self.lane = lane
        super.init(frame: .zero)
        build()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    // MARK: layout

    private func build() {
        let page = PageKit.page()
        page.addArrangedSubview(PageKit.heading(lane.title, lane.blurb))
        page.addArrangedSubview(headerRow())

        let split = NSStackView(views: [formPane(), outputPane()])
        split.orientation = .horizontal
        split.alignment = .top
        split.spacing = 20
        split.distribution = .fill
        // The form column keeps its width and the output column takes what is left,
        // which is the same division HSplitView makes in Studio Hub.
        split.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        page.addArrangedSubview(split)
        split.widthAnchor.constraint(equalTo: page.widthAnchor, constant: -PageKit.pagePadding * 2)
            .isActive = true
        formColumn.widthAnchor.constraint(equalToConstant: 390).isActive = true
        outputColumn.widthAnchor.constraint(greaterThanOrEqualToConstant: 320).isActive = true

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
            scroll.topAnchor.constraint(equalTo: topAnchor),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    private func headerRow() -> NSView {
        let refresh = NSButton(title: "Refresh models", target: self, action: #selector(refreshModels))
        refresh.bezelStyle = .rounded
        return PageKit.row([statusLabel, NSView(), refresh])
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

        if lane.isUpload {
            let choose = NSButton(title: "Choose File…", target: self, action: #selector(pickFile))
            choose.bezelStyle = .rounded
            fileButton = choose
            formColumn.addArrangedSubview(PageKit.formRow("Recording", [choose, PageKit.help("No file chosen")]))
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
        let row = PageKit.row([generateButton, PageKit.label("", size: 12, colour: .secondaryLabelColor)])
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
        case .number, .text, .voice:
            let entry = NSTextField(string: field.initial)
            entry.placeholderString = field.placeholder
            entry.font = .systemFont(ofSize: 13)
            entry.widthAnchor.constraint(equalToConstant: field.kind == .number ? 90 : 330).isActive = true
            return entry
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
                        self.modelPopup?.selectItem(at: index)
                    } else if !self.hasLoadedModels && previousModelID == nil {
                        self.modelPopup?.selectItem(at: 0)
                    } else {
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
                self.updateGenerateEnabled()
            }
        }
    }

    @objc private func modelChanged() { updateGenerateEnabled() }

    private func updateGenerateEnabled() {
        generateButton.isEnabled = selectedModelID() != nil
            && !submissionInFlight
            && pendingJobID == nil
    }

    private func selectedModelID() -> String? {
        modelPopup?.selectedItem?.representedObject as? String
    }

    @objc private func pickFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        fileURL = url
        fileButton?.title = url.lastPathComponent
    }

    private func value(of key: String) -> String {
        if let scroll = fields[key] as? NSScrollView,
           let text = scroll.documentView as? NSTextView {
            return text.string.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if let entry = fields[key] as? NSTextField {
            return entry.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return ""
    }

    // MARK: generate

    @objc private func generate() {
        guard !submissionInFlight, pendingJobID == nil else { return }
        guard let modelID = selectedModelID() else {
            statusLabel.stringValue = "Select a model before generating."
            return
        }
        submissionInFlight = true
        updateGenerateEnabled()
        statusLabel.stringValue = "submitting…"
        let key = UUID().uuidString

        if lane.isUpload {
            guard let url = fileURL, let data = try? Data(contentsOf: url) else {
                statusLabel.stringValue = "Choose a recording first."
                submissionInFlight = false
                updateGenerateEnabled()
                return
            }
            GatewayAPI.upload(
                "/admin/api/create/transcription",
                fileField: "file", filename: url.lastPathComponent, data: data,
                fields: ["model_id": modelID, "idempotency_key": key]
            ) { [weak self] result in self?.handleSubmit(result) }
            return
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
        GatewayAPI.post("/admin/api/create", body: [
            "modality": lane.modality, "model_id": modelID,
            "params": params, "idempotency_key": key,
        ]) { [weak self] result in self?.handleSubmit(result) }
    }

    private func handleSubmit(_ result: Result<[String: Any], Error>) {
        // A refresh started before this result must not overwrite its message.
        modelRefreshRequest += 1
        switch result {
        case .failure(let error):
            submissionInFlight = false
            statusLabel.stringValue = error.localizedDescription
            updateGenerateEnabled()
        case .success(let job):
            guard let id = job["id"] as? String else {
                submissionInFlight = false
                statusLabel.stringValue = GatewayAPI.detail(from: nil)
                updateGenerateEnabled()
                return
            }
            submissionInFlight = false
            currentJobID = id
            let state = job["state"] as? String ?? ""
            let terminal = ["succeeded", "failed", "cancelled"].contains(state)
            pendingJobID = terminal ? nil : id
            render(job)
            if terminal {
                updateGenerateEnabled()
            } else {
                startPolling(id)
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
                    self.updateGenerateEnabled()
                } else {
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
        let jobID = job["id"] as? String ?? ""
        statusLabel.stringValue = state
        clearResult()

        if state == "failed" {
            resultBox.addArrangedSubview(PageKit.label(
                job["failure_reason"] as? String ?? "the generation failed",
                size: 13, colour: .systemRed, wrapping: true))
            return
        }
        if state == "cancelled" {
            resultBox.addArrangedSubview(PageKit.label(
                "The generation was cancelled.", size: 13, colour: .secondaryLabelColor,
                wrapping: true))
            return
        }
        if state != "succeeded" {
            let spin = NSProgressIndicator()
            spin.style = .spinning
            spin.startAnimation(nil)
            resultBox.addArrangedSubview(PageKit.row([spin,
                PageKit.label("\(state)…", size: 13, colour: .secondaryLabelColor)]))
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
            scroll.widthAnchor.constraint(equalToConstant: 520).isActive = true
            scroll.heightAnchor.constraint(equalToConstant: 260).isActive = true
            resultBox.addArrangedSubview(scroll)
            return
        }

        let outputs = job["outputs"] as? [[String: Any]] ?? []
        if lane.modality == "transcription" {
            showTranscript(jobID: jobID, assetID: "0")
            return
        }
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
            let assetID = output["asset_id"] as? String ?? "0"
            resultBox.addArrangedSubview(artifactRow(jobID: jobID, assetID: assetID))
        }
    }

    private func artifactRow(jobID: String, assetID: String) -> NSView {
        let path = "/admin/api/artifacts/\(jobID)/outputs/\(assetID)"
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8

        if lane.modality == "image", let url = GatewayAPI.url(path),
           let image = NSImage(contentsOf: url) {
            let view = NSImageView(image: image)
            view.imageScaling = .scaleProportionallyDown
            view.translatesAutoresizingMaskIntoConstraints = false
            view.widthAnchor.constraint(lessThanOrEqualToConstant: 520).isActive = true
            view.heightAnchor.constraint(lessThanOrEqualToConstant: 340).isActive = true
            stack.addArrangedSubview(view)
        } else {
            // Audio and video get a real Open rather than an embedded player: the
            // file plays in whatever the owner already uses, and nothing here has
            // to reimplement transport controls to be useful.
            let icon = NSImageView()
            icon.image = NSImage(systemSymbolName: lane.symbol, accessibilityDescription: nil)
            icon.contentTintColor = .secondaryLabelColor
            let bits = ["\(lane.title) result", "asset \(assetID)"]
            stack.addArrangedSubview(PageKit.row([icon, PageKit.label(bits.joined(separator: " · "),
                                                                      size: 13, colour: .secondaryLabelColor)]))
        }

        let open = NSButton(title: "Open", target: self, action: #selector(openArtifact(_:)))
        open.bezelStyle = .rounded
        open.identifier = NSUserInterfaceItemIdentifier(path)
        let save = NSButton(title: "Save…", target: self, action: #selector(saveArtifact(_:)))
        save.bezelStyle = .rounded
        save.identifier = NSUserInterfaceItemIdentifier(path)
        let keep = NSButton(title: "Keep", target: self, action: #selector(keepArtifact(_:)))
        keep.bezelStyle = .rounded
        keep.identifier = NSUserInterfaceItemIdentifier("\(jobID)|\(assetID)")
        stack.addArrangedSubview(PageKit.row([open, save, keep]))
        return stack
    }

    private func showTranscript(jobID: String, assetID: String) {
        guard let url = GatewayAPI.url("/admin/api/artifacts/\(jobID)/outputs/\(assetID)") else { return }
        let text = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        let view = NSTextView()
        view.isEditable = false
        view.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        view.string = text.isEmpty ? "The transcript came back empty." : text
        let scroll = NSScrollView()
        scroll.documentView = view
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.widthAnchor.constraint(equalToConstant: 520).isActive = true
        scroll.heightAnchor.constraint(equalToConstant: 260).isActive = true
        resultBox.addArrangedSubview(scroll)
        resultBox.addArrangedSubview(artifactRow(jobID: jobID, assetID: assetID))
    }

    // MARK: artifact actions

    @objc private func openArtifact(_ sender: NSButton) {
        guard let path = sender.identifier?.rawValue, let url = GatewayAPI.url(path) else { return }
        NSWorkspace.shared.open(url)
    }

    @objc private func saveArtifact(_ sender: NSButton) {
        guard let path = sender.identifier?.rawValue, let url = GatewayAPI.url(path) else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "\(lane.key)-\(currentJobID?.prefix(8) ?? "result")"
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        if let data = try? Data(contentsOf: url) {
            try? data.write(to: destination)
        }
    }

    @objc private func keepArtifact(_ sender: NSButton) {
        guard let raw = sender.identifier?.rawValue else { return }
        let parts = raw.split(separator: "|").map(String.init)
        guard parts.count == 2 else { return }
        sender.isEnabled = false
        GatewayAPI.post("/admin/api/artifacts/keep",
                        body: ["job_id": parts[0], "asset_id": parts[1], "keep": true]) { [weak self] result in
            switch result {
            case .failure(let error):
                self?.statusLabel.stringValue = error.localizedDescription
                sender.isEnabled = true
            case .success:
                sender.title = "Kept"
                self?.statusLabel.stringValue = "kept — the 7-day prune will not take it"
            }
        }
    }
}
