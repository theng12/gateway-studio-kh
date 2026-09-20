// The Jobs window: a revisitable library of gateway work and its outputs.
//
// Output bytes always travel through GatewayAPI's authenticated download helper
// and are written to a local file before AppKit opens them. The page deliberately
// keeps the detail column in a scroll view: long provider/recovery text must not
// make the shared window grow past its minimum size.

import Cocoa

final class JobsSection: NSObject, AppSection, NSTableViewDataSource, NSTableViewDelegate {
    static let shared = JobsSection()

    let title = "Jobs"
    let symbol = "list.bullet.rectangle"
    let refreshesAutomatically = true

    private struct OutputRecord {
        let jobID: String
        let assetID: String
        let path: String
        let mediaType: String
        let bytes: Int64?
        let present: Bool
        let kept: Bool
    }

    private enum OutputActionKind {
        case preview
        case save
        case open
        case pin
    }

    private final class OutputAction: NSObject {
        let kind: OutputActionKind
        let output: OutputRecord

        init(kind: OutputActionKind, output: OutputRecord) {
            self.kind = kind
            self.output = output
        }
    }

    private final class OutputButton: NSButton {
        let outputAction: OutputAction

        init(
            title: String,
            target: AnyObject,
            action: Selector,
            outputAction: OutputAction
        ) {
            self.outputAction = outputAction
            super.init(frame: .zero)
            self.title = title
            self.target = target
            self.action = action
        }

        required init?(coder: NSCoder) {
            fatalError("Output buttons are built programmatically.")
        }
    }

    private var table: NSTableView!
    private var tableScroll: NSScrollView!
    private var detailScroll: NSScrollView!
    private var detailStack: NSStackView!
    private var filterPopup: NSPopUpButton!
    private var summaryLabel: NSTextField!
    private var detailStatusLabel: NSTextField!
    private var cancelButton: NSButton?
    private var rows: [[String: Any]] = []
    private var selectedJobID: String?
    private var selectionGeneration = 0
    private var reloadGeneration = 0
    private var suppressSelectionCallback = false

    /// The section's content, built once and hosted by the main window.
    lazy var view: NSView = buildContent()

    private let columns: [(id: String, title: String, width: CGFloat)] = [
        ("time", "When", 70),
        ("modality", "Type", 88),
        ("model", "Model", 190),
        ("state", "State", 140),
        ("outputs", "Outputs", 72),
    ]

    func activated() {
        reload()
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let root = PageKit.page()
        root.addArrangedSubview(PageKit.heading(
            "Jobs",
            "Revisit completed outputs, see why work stopped, and manage retained files."
        ))

        summaryLabel = NSTextField(labelWithString: "Loading…")
        summaryLabel.font = .systemFont(ofSize: 12)
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        filterPopup = NSPopUpButton()
        filterPopup.addItems(withTitles: ["All results", "Succeeded", "Failed", "Cancelled"])
        filterPopup.target = self
        filterPopup.action = #selector(reload)
        filterPopup.controlSize = .small

        let refresh = NSButton(title: "Refresh", target: self, action: #selector(reload))
        refresh.bezelStyle = .rounded
        refresh.controlSize = .small

        let bar = NSStackView(views: [summaryLabel, NSView(), filterPopup, refresh])
        bar.orientation = .horizontal
        bar.spacing = 8
        bar.alignment = .centerY
        root.addArrangedSubview(bar)
        bar.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56).isActive = true

        table = NSTableView()
        table.dataSource = self
        table.delegate = self
        table.rowHeight = 24
        table.usesAlternatingRowBackgroundColors = true
        table.allowsMultipleSelection = false
        table.style = .inset
        for column in columns {
            let tableColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(column.id))
            tableColumn.title = column.title
            tableColumn.width = column.width
            table.addTableColumn(tableColumn)
        }

        tableScroll = NSScrollView()
        tableScroll.documentView = table
        tableScroll.hasVerticalScroller = true
        tableScroll.hasHorizontalScroller = false
        tableScroll.borderType = .bezelBorder
        tableScroll.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(tableScroll)
        NSLayoutConstraint.activate([
            tableScroll.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56),
            tableScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 112),
        ])

        detailScroll = NSScrollView()
        detailScroll.hasVerticalScroller = true
        detailScroll.hasHorizontalScroller = false
        detailScroll.borderType = .bezelBorder
        detailScroll.translatesAutoresizingMaskIntoConstraints = false

        let detailDocument = FlippedView()
        detailDocument.translatesAutoresizingMaskIntoConstraints = false
        detailStack = NSStackView()
        detailStack.orientation = .vertical
        detailStack.alignment = .leading
        detailStack.spacing = 8
        detailStack.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        detailStack.translatesAutoresizingMaskIntoConstraints = false
        detailDocument.addSubview(detailStack)
        detailScroll.documentView = detailDocument
        NSLayoutConstraint.activate([
            detailDocument.widthAnchor.constraint(equalTo: detailScroll.contentView.widthAnchor),
            detailStack.topAnchor.constraint(equalTo: detailDocument.topAnchor),
            detailStack.leadingAnchor.constraint(equalTo: detailDocument.leadingAnchor),
            detailStack.trailingAnchor.constraint(equalTo: detailDocument.trailingAnchor),
            detailStack.bottomAnchor.constraint(equalTo: detailDocument.bottomAnchor),
        ])

        root.addArrangedSubview(detailScroll)
        NSLayoutConstraint.activate([
            detailScroll.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56),
            detailScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 156),
        ])
        renderPlaceholder()

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

    // MARK: Data

    @objc func reload() {
        reloadGeneration += 1
        let requestGeneration = reloadGeneration

        var path = "/admin/api/jobs?limit=200"
        switch filterPopup?.indexOfSelectedItem ?? 0 {
        case 1: path += "&state=succeeded"
        case 2: path += "&state=failed"
        case 3: path += "&state=cancelled"
        default: break
        }

        summaryLabel?.stringValue = "Loading…"
        GatewayAPI.get(path) { [weak self] result in
            guard let self, self.reloadGeneration == requestGeneration else { return }

            switch result {
            case .success(let payload):
                let currentID = self.selectedJobID ?? self.selectedTableJobID()
                self.rows = payload["jobs"] as? [[String: Any]] ?? []
                self.updateSummary(payload: payload)
                self.suppressSelectionCallback = true
                self.table?.reloadData()
                self.suppressSelectionCallback = false
                self.restoreSelection(currentID)
                self.loadKeptState(requestGeneration: requestGeneration)
            case .failure(let error):
                self.rows = []
                self.selectedJobID = nil
                self.selectionGeneration += 1
                self.table?.reloadData()
                self.renderPlaceholder()
                self.summaryLabel?.stringValue = "Could not load jobs: \(error.localizedDescription)"
            }
        }
    }

    private func updateSummary(payload: [String: Any]) {
        let stats = payload["stats"] as? [String: Any] ?? [:]
        let byState = stats["by_state"] as? [String: Any] ?? [:]
        let succeeded = integer(byState["succeeded"]) ?? 0
        let failed = integer(byState["failed"]) ?? 0
        let total = integer(stats["total"]) ?? rows.count
        let last = stats["last_job_at"] as? Double
        summaryLabel.stringValue = "\(rows.count) shown · \(succeeded) succeeded · "
            + "\(failed) failed · \(total) total · last \(GatewayAPI.relativeTime(last))"
    }

    private func loadKeptState(requestGeneration: Int) {
        GatewayAPI.get("/admin/api/artifacts/kept") { [weak self] result in
            guard let self, self.reloadGeneration == requestGeneration else { return }
            guard case .success(let payload) = result,
                  let artifacts = payload["artifacts"] as? [[String: Any]] else { return }

            let currentID = self.selectedJobID ?? self.selectedTableJobID()
            let kept = Set(artifacts.compactMap { entry -> String? in
                guard let jobID = self.string(entry["job_id"]),
                      let assetID = self.string(entry["asset_id"]),
                      !jobID.isEmpty, !assetID.isEmpty else { return nil }
                return self.outputKey(jobID: jobID, assetID: assetID)
            })
            self.rows = self.rows.map { job in
                var updated = job
                let jobID = self.string(job["id"]) ?? ""
                if var outputs = job["outputs"] as? [[String: Any]] {
                    outputs = outputs.map { output in
                        var updatedOutput = output
                        let assetID = self.string(output["asset_id"]) ?? ""
                        updatedOutput["kept"] = kept.contains(
                            self.outputKey(jobID: jobID, assetID: assetID)
                        )
                        return updatedOutput
                    }
                    updated["outputs"] = outputs
                }
                return updated
            }
            self.suppressSelectionCallback = true
            self.table.reloadData()
            self.suppressSelectionCallback = false
            self.restoreSelection(currentID)
        }
    }

    private func selectedTableJobID() -> String? {
        guard let table, table.selectedRow >= 0, table.selectedRow < rows.count else { return nil }
        return string(rows[table.selectedRow]["id"])
    }

    private func restoreSelection(_ jobID: String?) {
        guard let jobID,
              let row = rows.firstIndex(where: { string($0["id"]) == jobID }) else {
            if selectedJobID != nil {
                selectionGeneration += 1
            }
            selectedJobID = nil
            suppressSelectionCallback = true
            table?.deselectAll(nil)
            suppressSelectionCallback = false
            renderPlaceholder()
            return
        }
        suppressSelectionCallback = true
        table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        suppressSelectionCallback = false
        if selectedJobID != jobID {
            selectionGeneration += 1
        }
        selectedJobID = jobID
        renderDetail(rows[row])
        loadJobStatus(jobID: jobID, generation: selectionGeneration)
    }

    // MARK: Table

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(
        _ tableView: NSTableView,
        viewFor tableColumn: NSTableColumn?,
        row: Int
    ) -> NSView? {
        guard let tableColumn, row >= 0, row < rows.count else { return nil }
        let job = rows[row]
        let field = NSTextField(labelWithString: "")
        field.font = .systemFont(ofSize: 11.5)
        field.lineBreakMode = .byTruncatingTail

        switch tableColumn.identifier.rawValue {
        case "time":
            field.stringValue = GatewayAPI.relativeTime(job["created_at"] as? Double)
            field.textColor = .secondaryLabelColor
        case "modality":
            field.stringValue = (string(job["modality"]) ?? "").capitalized
        case "model":
            field.stringValue = string(job["model_name"]) ?? string(job["model_id"]) ?? "?"
        case "state":
            field.stringValue = displayState(job)
            field.textColor = stateColour(job)
        case "outputs":
            let count = (job["outputs"] as? [[String: Any]])?.count ?? 0
            field.stringValue = count == 1 ? "1 file" : "\(count) files"
            field.textColor = .secondaryLabelColor
        default:
            break
        }

        let cell = NSTableCellView()
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

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !suppressSelectionCallback else { return }
        let index = table.selectedRow
        guard index >= 0, index < rows.count else {
            if selectedJobID != nil {
                selectionGeneration += 1
            }
            selectedJobID = nil
            renderPlaceholder()
            return
        }
        let jobID = string(rows[index]["id"])
        if selectedJobID != jobID {
            selectionGeneration += 1
        }
        selectedJobID = jobID
        renderDetail(rows[index])
        if let jobID {
            loadJobStatus(jobID: jobID, generation: selectionGeneration)
        }
    }

    private func loadJobStatus(jobID: String, generation: Int) {
        GatewayAPI.get(statusPath(jobID)) { [weak self] result in
            guard let self,
                  self.selectedJobID == jobID,
                  self.selectionGeneration == generation,
                  let row = self.rows.firstIndex(where: { self.string($0["id"]) == jobID })
            else { return }

            switch result {
            case .success(let payload):
                var merged = self.rows[row]
                let detailKeys = [
                    "state", "cancel_requested", "cancel_state", "retryable",
                    "recovery_required", "recovery_state", "recovery_detail",
                    "upstream_job_id", "late_result_state", "output_text", "outputs",
                    "error_code", "error_detail", "failure_code", "failure_reason",
                ]
                for key in detailKeys where payload[key] != nil {
                    merged[key] = payload[key]
                }
                self.rows[row] = merged
                self.renderDetail(merged)
            case .failure(let error):
                self.setStatus(
                    "Could not load the latest details: \(error.localizedDescription)",
                    error: true
                )
            }
        }
    }

    // MARK: Detail

    private func renderPlaceholder() {
        guard detailStack != nil else { return }
        clearDetail()
        detailStack.addArrangedSubview(PageKit.sectionTitle("Job details"))
        detailStack.addArrangedSubview(PageKit.help("Select a job to inspect its lifecycle and outputs."))
        detailStatusLabel = PageKit.help("")
        detailStack.addArrangedSubview(detailStatusLabel)
        cancelButton = nil
    }

    private func renderDetail(_ job: [String: Any]) {
        guard detailStack != nil else { return }
        clearDetail()

        let jobID = string(job["id"]) ?? ""
        let model = string(job["model_name"]) ?? string(job["model_id"]) ?? "?"
        let provider = string(job["provider_name"]) ?? string(job["provider_id"]) ?? "?"
        detailStack.addArrangedSubview(PageKit.sectionTitle("\(model) · \(provider)"))
        detailStack.addArrangedSubview(PageKit.help("Job \(jobID) · \(displayState(job))"))

        let lifecycle = lifecycleText(job)
        if !lifecycle.isEmpty {
            let lifecycleLabel = PageKit.help(lifecycle)
            lifecycleLabel.textColor = lifecycleColour(job)
            detailStack.addArrangedSubview(lifecycleLabel)
        }

        if let errorCode = string(job["error_code"]), !errorCode.isEmpty {
            detailStack.addArrangedSubview(PageKit.help("Failure: \(errorCode)"))
        }
        if let errorDetail = string(job["error_detail"]), !errorDetail.isEmpty {
            detailStack.addArrangedSubview(PageKit.help(errorDetail))
        }

        if let text = string(job["output_text"]), !text.isEmpty {
            detailStack.addArrangedSubview(PageKit.sectionTitle("Text output"))
            let outputText = NSTextField(wrappingLabelWithString: text)
            outputText.font = .systemFont(ofSize: 12)
            outputText.textColor = .labelColor
            outputText.isSelectable = true
            outputText.preferredMaxLayoutWidth = PageKit.textWidth
            outputText.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            detailStack.addArrangedSubview(outputText)
        }

        let outputs = outputRecords(job)
        detailStack.addArrangedSubview(PageKit.sectionTitle(
            outputs.isEmpty ? "Outputs" : "Outputs · \(outputs.count)"
        ))
        if outputs.isEmpty {
            detailStack.addArrangedSubview(PageKit.help("No output files were recorded for this job."))
        } else {
            for output in outputs {
                addOutputRow(output)
            }
        }

        cancelButton = nil
        if canRequestCancellation(job) {
            let cancel = NSButton(title: "Cancel queued job", target: self, action: #selector(cancelSelectedJob))
            cancel.bezelStyle = .rounded
            cancel.controlSize = .small
            detailStack.addArrangedSubview(cancel)
            cancelButton = cancel
        }

        detailStatusLabel = PageKit.help("")
        detailStack.addArrangedSubview(detailStatusLabel)
    }

    private func clearDetail() {
        for view in detailStack.arrangedSubviews {
            detailStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
    }

    private func addOutputRow(_ output: OutputRecord) {
        var state = output.present ? "Ready" : "Missing file"
        if output.kept { state += " · Pinned" }
        let bytes = output.bytes.map(formatBytes) ?? "size unknown"
        let label = NSTextField(labelWithString: "\(output.assetID) · \(output.mediaType) · \(bytes) · \(state)")
        label.font = .systemFont(ofSize: 11.5)
        label.textColor = output.present ? .secondaryLabelColor : .systemOrange
        label.lineBreakMode = .byTruncatingTail
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        var controls: [NSView] = [label, NSView()]
        if output.present {
            controls.append(outputButton("Preview", kind: .preview, output: output))
            controls.append(outputButton("Save…", kind: .save, output: output))
            controls.append(outputButton("Open", kind: .open, output: output))
        }
        controls.append(outputButton(output.kept ? "Unpin" : "Pin", kind: .pin, output: output))
        let row = NSStackView(views: controls)
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 6
        detailStack.addArrangedSubview(row)
    }

    private func outputButton(
        _ title: String,
        kind: OutputActionKind,
        output: OutputRecord
    ) -> NSButton {
        let button = OutputButton(
            title: title,
            target: self,
            action: #selector(outputAction(_:)),
            outputAction: OutputAction(kind: kind, output: output)
        )
        button.bezelStyle = .rounded
        button.controlSize = .small
        return button
    }

    // MARK: Output actions

    @objc private func outputAction(_ sender: NSButton) {
        guard let sender = sender as? OutputButton else { return }
        let action = sender.outputAction
        switch action.kind {
        case .pin:
            togglePin(action.output, sender: sender)
        case .save:
            let panel = NSSavePanel()
            panel.nameFieldStringValue = "asset-\(safeFilePart(action.output.assetID)).\(fileExtension(action.output.mediaType))"
            panel.canCreateDirectories = false
            panel.begin { [weak self] response in
                guard response == .OK, let destination = panel.url else { return }
                self?.downloadOutput(action.output, destination: destination, opens: false)
            }
        case .preview, .open:
            downloadOutput(action.output, destination: nil, opens: true)
        }
    }

    private func downloadOutput(_ output: OutputRecord, destination: URL?, opens: Bool) {
        guard output.present else {
            setStatus("Cannot open \(output.assetID): the file is missing on disk.", error: true)
            return
        }
        guard output.path.hasPrefix("/") else {
            setStatus("Cannot open \(output.assetID): the gateway returned an invalid path.", error: true)
            return
        }

        let jobID = output.jobID
        let generation = selectionGeneration
        setStatus("Downloading \(output.assetID)…", error: false)
        GatewayAPI.download(output.path) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                guard self.isCurrent(jobID: jobID, generation: generation) else { return }
                self.setStatus("Could not download \(output.assetID): \(error.localizedDescription)", error: true)
            case .success(let data):
                DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                    guard let self else { return }
                    do {
                        let localURL: URL
                        if let destination {
                            try data.write(to: destination, options: [.atomic])
                            localURL = destination
                        } else {
                            let directory = FileManager.default.temporaryDirectory
                                .appendingPathComponent("gateway-kh-jobs", isDirectory: true)
                            try FileManager.default.createDirectory(
                                at: directory,
                                withIntermediateDirectories: true,
                                attributes: nil
                            )
                            localURL = directory.appendingPathComponent(
                                "\(safeFilePart(output.assetID))-\(UUID().uuidString).\(fileExtension(output.mediaType))"
                            )
                            try data.write(to: localURL, options: [.atomic])
                        }
                        DispatchQueue.main.async {
                            guard self.isCurrent(jobID: jobID, generation: generation) else { return }
                            if opens {
                                guard NSWorkspace.shared.open(localURL) else {
                                    self.setStatus("Could not open the local file.", error: true)
                                    return
                                }
                                self.setStatus("Opened a local copy of \(output.assetID).", error: false)
                            } else {
                                self.setStatus("Saved \(output.assetID) to \(localURL.path).", error: false)
                            }
                        }
                    } catch {
                        DispatchQueue.main.async {
                            guard self.isCurrent(jobID: jobID, generation: generation) else { return }
                            self.setStatus("Could not save \(output.assetID): \(error.localizedDescription)", error: true)
                        }
                    }
                }
            }
        }
    }

    private func togglePin(_ output: OutputRecord, sender: NSButton) {
        let keep = !output.kept
        sender.isEnabled = false
        let generation = selectionGeneration
        GatewayAPI.post(
            "/admin/api/artifacts/keep",
            body: ["job_id": output.jobID, "asset_id": output.assetID, "keep": keep]
        ) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success:
                self.updatePin(jobID: output.jobID, assetID: output.assetID, kept: keep)
                if self.isCurrent(jobID: output.jobID, generation: generation),
                   let job = self.rows.first(where: { self.string($0["id"]) == output.jobID }) {
                    self.renderDetail(job)
                }
            case .failure(let error):
                sender.isEnabled = true
                guard self.isCurrent(jobID: output.jobID, generation: generation) else { return }
                self.setStatus("Could not change pin: \(error.localizedDescription)", error: true)
            }
        }
    }

    private func updatePin(jobID: String, assetID: String, kept: Bool) {
        for index in rows.indices where string(rows[index]["id"]) == jobID {
            guard var outputs = rows[index]["outputs"] as? [[String: Any]] else { continue }
            for outputIndex in outputs.indices where string(outputs[outputIndex]["asset_id"]) == assetID {
                outputs[outputIndex]["kept"] = kept
            }
            rows[index]["outputs"] = outputs
        }
        table.reloadData()
    }

    @objc private func cancelSelectedJob() {
        guard let jobID = selectedJobID,
              let job = rows.first(where: { string($0["id"]) == jobID }) else { return }
        guard canRequestCancellation(job) else { return }
        let requestGeneration = selectionGeneration
        cancelButton?.isEnabled = false
        setStatus("Requesting cancellation…", error: false)
        GatewayAPI.post(cancelPath(jobID)) { [weak self] result in
            guard let self,
                  self.isCurrent(jobID: jobID, generation: requestGeneration)
            else { return }
            switch result {
            case .success:
                self.setStatus("Cancellation requested. Refreshing lifecycle state…", error: false)
                self.reload()
            case .failure(let error):
                self.cancelButton?.isEnabled = true
                self.setStatus("Could not cancel job: \(error.localizedDescription)", error: true)
            }
        }
    }

    private func cancelPath(_ jobID: String) -> String {
        "/admin/api/create/status/\(escapedJobID(jobID))/cancel"
    }

    private func statusPath(_ jobID: String) -> String {
        "/admin/api/create/status/\(escapedJobID(jobID))"
    }

    private func escapedJobID(_ jobID: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_."))
        return jobID.addingPercentEncoding(withAllowedCharacters: allowed) ?? jobID
    }

    private func setStatus(_ text: String, error: Bool) {
        guard detailStatusLabel != nil else { return }
        detailStatusLabel.stringValue = text
        detailStatusLabel.textColor = error ? .systemRed : .secondaryLabelColor
    }

    private func isCurrent(jobID: String, generation: Int) -> Bool {
        selectedJobID == jobID && selectionGeneration == generation
    }

    // MARK: Lifecycle and output formatting

    private func outputRecords(_ job: [String: Any]) -> [OutputRecord] {
        guard let rawOutputs = job["outputs"] as? [[String: Any]] else { return [] }
        let jobID = string(job["id"]) ?? ""
        return rawOutputs.compactMap { output in
            guard let assetID = string(output["asset_id"]),
                  let path = string(output["url"]),
                  !assetID.isEmpty,
                  !path.isEmpty else { return nil }
            return OutputRecord(
                jobID: jobID,
                assetID: assetID,
                path: path,
                mediaType: string(output["media_type"]) ?? "application/octet-stream",
                bytes: integer64(output["bytes"]),
                present: output["present"] as? Bool ?? false,
                kept: output["kept"] as? Bool ?? false
            )
        }
    }

    private func lifecycleText(_ job: [String: Any]) -> String {
        let state = string(job["state"]) ?? "unknown"
        var lines: [String] = []
        let cancelRequested = job["cancel_requested"] as? Bool ?? false
        let cancelState = string(job["cancel_state"]) ?? ""
        if cancelRequested {
            if state == "queued" || cancelState == "confirmed" || cancelState == "cancelled" {
                lines.append("Cancellation requested; this job is being stopped before provider completion.")
            } else {
                lines.append("Cancellation requested; the running provider request may still settle.")
            }
        } else if state == "queued" || state == "accepted" {
            lines.append("Queued. Cancellation can be requested before dispatch.")
        }

        let recoveryRequired = job["recovery_required"] as? Bool ?? false
        if recoveryRequired {
            lines.append("Manual recovery required; no automatic replay will be started.")
            if let recoveryState = string(job["recovery_state"]), !recoveryState.isEmpty {
                lines.append("Recovery state: \(recoveryState)")
            }
            if let recoveryDetail = string(job["recovery_detail"]), !recoveryDetail.isEmpty {
                lines.append(recoveryDetail)
            }
            if let upstream = string(job["upstream_job_id"]), !upstream.isEmpty {
                lines.append("Upstream job: \(upstream)")
            }
            if let late = string(job["late_result_state"]), !late.isEmpty {
                lines.append("Late result: \(late)")
            }
            if job["retryable"] as? Bool == true {
                lines.append("Retryable: yes — review the recovery details before starting another request.")
            }
        }
        return lines.joined(separator: "\n")
    }

    private func canRequestCancellation(_ job: [String: Any]) -> Bool {
        let state = string(job["state"]) ?? ""
        let terminal = ["succeeded", "failed", "cancelled"].contains(state)
        return !terminal && (job["cancel_requested"] as? Bool ?? false) == false
    }

    private func displayState(_ job: [String: Any]) -> String {
        if job["cancel_requested"] as? Bool == true {
            return "cancellation requested"
        }
        return string(job["state"]) ?? "unknown"
    }

    private func stateColour(_ job: [String: Any]) -> NSColor {
        switch string(job["state"]) {
        case "succeeded": return .systemGreen
        case "failed": return .systemRed
        case "cancelled": return .tertiaryLabelColor
        default: return .systemYellow
        }
    }

    private func lifecycleColour(_ job: [String: Any]) -> NSColor {
        if job["recovery_required"] as? Bool == true { return .systemOrange }
        if job["cancel_requested"] as? Bool == true { return .systemYellow }
        return .secondaryLabelColor
    }

    private func outputKey(jobID: String, assetID: String) -> String {
        "\(jobID)\u{001F}\(assetID)"
    }

    private func integer(_ value: Any?) -> Int? {
        if let number = value as? NSNumber { return number.intValue }
        if let value = value as? Int { return value }
        return nil
    }

    private func integer64(_ value: Any?) -> Int64? {
        if let number = value as? NSNumber { return number.int64Value }
        if let value = value as? Int { return Int64(value) }
        if let value = value as? Int64 { return value }
        return nil
    }

    private func string(_ value: Any?) -> String? {
        if let value = value as? String { return value }
        if let value = value as? NSNumber { return value.stringValue }
        return nil
    }

    private func formatBytes(_ bytes: Int64) -> String {
        if bytes < 1024 { return "\(bytes) B" }
        if bytes < 1024 * 1024 { return String(format: "%.1f KB", Double(bytes) / 1024) }
        return String(format: "%.1f MB", Double(bytes) / (1024 * 1024))
    }

    private func safeFilePart(_ value: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_."))
        let result = value.unicodeScalars.map { allowed.contains($0) ? String($0) : "_" }.joined()
        return result.isEmpty ? "output" : result
    }

    private func fileExtension(_ mediaType: String) -> String {
        PageKit.artifactExtension(mediaType)
    }
}
