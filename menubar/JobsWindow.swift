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
    private var discardButton: NSButton?
    private var rows: [[String: Any]] = []
    /// The last list read as the gateway sent it, to tell whether a job changed.
    private var listRows: [String: [String: Any]] = [:]
    private var keptKeys: Set<String> = []

    /// Asked before anything is discarded. A property so the native test can
    /// answer without a modal alert: the confirmation is part of the behaviour
    /// (abandoning work the Hub may still be running is deliberate), not
    /// decoration around it.
    var confirmDiscard: (() -> Bool)?
    private var selectedJobID: String?
    private var selectionGeneration = 0
    private var reloadGeneration = 0
    private var suppressSelectionCallback = false

    /// The section's content, built once and hosted by the main window.
    lazy var view: NSView = buildContent()

    /// The route filters on one `state`; "needs recovery" is a `failed` job the gateway
    /// still holds for a manual decision, which only the row itself says.
    private let filters: [(title: String, state: String?, recoveryOnly: Bool)] = [
        ("All results", nil, false),
        ("In progress", "running", false),
        ("Waiting in queue", "accepted", false),
        ("Needs recovery", "failed", true),
        ("Succeeded", "succeeded", false),
        ("Failed", "failed", false),
        ("Cancelled", "cancelled", false),
    ]

    private let columns: [(id: String, title: String, width: CGFloat)] = [
        ("time", "When", 70),
        ("modality", "Type", 104),
        ("provider", "Provider", 130),
        ("model", "Model", 180),
        ("state", "State", 130),
        ("outputs", "Outputs", 66),
    ]

    /// Also the host's 5 s timer: a quiet read keeps what is on screen.
    func activated() {
        load(quiet: true)
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let root = PageKit.page()
        PageKit.addFull(PageKit.heading(
            "Jobs",
            "Revisit completed outputs, see why work stopped, and manage retained files.",
            actions: [PageKit.button("Refresh", target: self, action: #selector(reload))]
        ), to: root)

        summaryLabel = NSTextField(labelWithString: "Loading…")
        summaryLabel.font = TextRole.secondary.font
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        filterPopup = NSPopUpButton()
        filterPopup.addItems(withTitles: filters.map { $0.title })
        filterPopup.target = self
        filterPopup.action = #selector(reload)
        filterPopup.controlSize = .small

        PageKit.addFull(PageKit.toolbar([filterPopup, NSView(), summaryLabel]), to: root)

        table = NSTableView()
        table.dataSource = self
        table.delegate = self
        table.rowHeight = DS.Size.row
        // Separators only, no zebra: the card around the table already says where it ends.
        table.usesAlternatingRowBackgroundColors = false
        table.backgroundColor = .clear
        table.allowsMultipleSelection = false
        table.style = .fullWidth
        // Share the width between columns: there is no horizontal scroller, so a
        // wider list must truncate its cells rather than push them past a clipped
        // edge at the window's minimum size.
        table.columnAutoresizingStyle = .noColumnAutoresizing
        table.intercellSpacing = NSSize(width: DS.Space.xs, height: 0)
        for column in columns {
            let tableColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(column.id))
            tableColumn.title = column.title
            tableColumn.width = column.width
            // Provider and Model take the spare width (see `fitColumns`); the rest keep what their contents need.
            tableColumn.minWidth = ["provider", "model"].contains(column.id) ? 100 : column.width
            table.addTableColumn(tableColumn)
        }

        tableScroll = NSScrollView()
        tableScroll.documentView = table
        tableScroll.hasVerticalScroller = true
        tableScroll.hasHorizontalScroller = false
        let tableCard = TableCard()
        tableCard.hold(table: tableScroll)
        tableCard.heightAnchor.constraint(greaterThanOrEqualToConstant: 112).isActive = true
        tableCard.onLayout = { [weak self] in
            guard let self else { return }
            PageKit.fitColumns(of: self.table, in: self.tableScroll, flex: ["provider": (1, 130), "model": (1.4, 180), "state": (0.5, 130)])
        }
        PageKit.addFull(tableCard, to: root)

        detailScroll = NSScrollView()
        detailScroll.hasVerticalScroller = true
        detailScroll.hasHorizontalScroller = false
        detailScroll.borderType = .noBorder
        detailScroll.drawsBackground = false
        detailScroll.wantsLayer = true
        detailScroll.layer?.cornerRadius = DS.Radius.card - 1
        detailScroll.layer?.masksToBounds = true
        detailScroll.translatesAutoresizingMaskIntoConstraints = false

        let detailDocument = FlippedView()
        detailDocument.translatesAutoresizingMaskIntoConstraints = false
        detailStack = NSStackView()
        detailStack.orientation = .vertical
        detailStack.alignment = .leading
        detailStack.spacing = DS.Space.s
        detailStack.edgeInsets = NSEdgeInsets(top: DS.Space.m, left: DS.Space.m, bottom: DS.Space.m, right: DS.Space.m)
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

        let detailCard = CardView()
        detailCard.addSubview(detailScroll)
        NSLayoutConstraint.activate([
            detailScroll.topAnchor.constraint(equalTo: detailCard.topAnchor, constant: 1),
            detailScroll.leadingAnchor.constraint(equalTo: detailCard.leadingAnchor, constant: 1),
            detailScroll.trailingAnchor.constraint(equalTo: detailCard.trailingAnchor, constant: -1),
            detailScroll.bottomAnchor.constraint(equalTo: detailCard.bottomAnchor, constant: -1),
            detailCard.heightAnchor.constraint(greaterThanOrEqualToConstant: 156),
        ])
        PageKit.addFull(detailCard, to: root)
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
        load(quiet: false)
    }

    private func load(quiet: Bool) {
        reloadGeneration += 1
        let requestGeneration = reloadGeneration

        let filter = filters[max(filterPopup?.indexOfSelectedItem ?? 0, 0)]
        var path = "/admin/api/jobs?limit=500"
        if let state = filter.state { path += "&state=\(state)" }

        // A timer-driven read must not wipe the summary or the detail pane.
        if !quiet || rows.isEmpty { summaryLabel?.stringValue = "Loading…" }
        GatewayAPI.get(path) { [weak self] result in
            guard let self, self.reloadGeneration == requestGeneration else { return }

            switch result {
            case .success(let payload):
                var fresh = payload["jobs"] as? [[String: Any]] ?? []
                if filter.recoveryOnly { fresh = fresh.filter { $0["recovery_required"] as? Bool == true } }
                self.adopt(fresh)
                self.updateSummary(payload: payload)
                self.loadKeptState(requestGeneration: requestGeneration)
            case .failure(let error):
                // A failed poll keeps the last table and the selection; the summary says so.
                self.summaryLabel?.stringValue = "Could not load jobs: \(error.localizedDescription)"
                    + (self.rows.isEmpty ? "" : " — showing the last list.")
            }
        }
    }

    /// The row as the list reported it, minus the countdown that changes on every read.
    private func stable(_ row: [String: Any]?) -> NSDictionary? {
        guard var row else { return nil }
        row["retry_in_s"] = nil
        return row as NSDictionary
    }

    /// Install a fresh list. Detail fetched separately (attempts, text output) is carried
    /// over, and the open detail is rebuilt only if the selected job's own row changed —
    /// otherwise a poll would scroll a long answer back to the top and wipe its status line.
    private func adopt(_ fresh: [[String: Any]]) {
        let currentID = selectedJobID ?? selectedTableJobID()
        let before = Dictionary(rows.compactMap { row in string(row["id"]).map { ($0, row) } },
                                uniquingKeysWith: { first, _ in first })
        let listed = listRows
        listRows = Dictionary(fresh.compactMap { row in string(row["id"]).map { ($0, row) } },
                              uniquingKeysWith: { first, _ in first })
        rows = fresh.map { row in
            let id = string(row["id"]) ?? ""
            var merged = before[id] ?? [:]
            if stable(listed[id]) != stable(row) { merged.merge(row) { _, new in new } }
            return withKept(merged)
        }
        suppressSelectionCallback = true
        table?.reloadData()
        suppressSelectionCallback = false
        let unchanged = currentID != nil && currentID == selectedJobID
            && stable(listed[currentID!]) == stable(listRows[currentID!])
        restoreSelection(currentID, redraw: !unchanged)
    }

    private func withKept(_ job: [String: Any]) -> [String: Any] {
        guard var outputs = job["outputs"] as? [[String: Any]] else { return job }
        let jobID = string(job["id"]) ?? ""
        for index in outputs.indices {
            let assetID = string(outputs[index]["asset_id"]) ?? ""
            outputs[index]["kept"] = keptKeys.contains(outputKey(jobID: jobID, assetID: assetID))
        }
        var updated = job
        updated["outputs"] = outputs
        return updated
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

            let kept = Set(artifacts.compactMap { entry -> String? in
                guard let jobID = self.string(entry["job_id"]),
                      let assetID = self.string(entry["asset_id"]),
                      !jobID.isEmpty, !assetID.isEmpty else { return nil }
                return self.outputKey(jobID: jobID, assetID: assetID)
            })
            guard kept != self.keptKeys else { return }
            let currentID = self.selectedJobID ?? self.selectedTableJobID()
            self.keptKeys = kept
            self.rows = self.rows.map(self.withKept)
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

    private func restoreSelection(_ jobID: String?, redraw: Bool = true) {
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
        guard redraw else { return }
        renderDetail(rows[row])
        loadJobStatus(jobID: jobID, generation: selectionGeneration)
    }

    // MARK: Table

    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        SeparatorRowView()
    }

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(
        _ tableView: NSTableView,
        viewFor tableColumn: NSTableColumn?,
        row: Int
    ) -> NSView? {
        guard let tableColumn, row >= 0, row < rows.count else { return nil }
        let job = rows[row]
        let field = NSTextField(labelWithString: "")
        field.font = TextRole.secondary.font
        field.lineBreakMode = .byTruncatingTail

        switch tableColumn.identifier.rawValue {
        case "time":
            field.stringValue = GatewayAPI.relativeTime(job["created_at"] as? Double)
            field.textColor = .secondaryLabelColor
        case "modality":
            field.stringValue = Self.typeName(string(job["modality"]))
        case "provider":
            // The gateway resolves the display name; fall back to the id it stored
            // so a provider removed from the registry still names the job.
            field.stringValue = string(job["provider_name"]) ?? string(job["provider_id"]) ?? "?"
            field.textColor = .secondaryLabelColor
        case "model":
            field.stringValue = string(job["model_name"]) ?? string(job["model_id"]) ?? "?"
        case "state":
            // The state is a badge, like every other status in the app: the word and its colour together.
            let word = job["recovery_required"] as? Bool == true ? "needs recovery" : displayState(job)
            let badge = Badge(word.prefix(1).uppercased() + word.dropFirst(), stateHealth(job))
            let cell = NSTableCellView()
            badge.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(badge)
            NSLayoutConstraint.activate([
                badge.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
                badge.trailingAnchor.constraint(lessThanOrEqualTo: cell.trailingAnchor, constant: -DS.Space.xs),
                badge.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            ])
            return cell
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
            field.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
            field.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -DS.Space.xs),
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
                    "attempts", "attempts_used", "attempts_allowed", "attempt_policy",
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
        discardButton = nil
    }

    private func renderDetail(_ job: [String: Any]) {
        guard detailStack != nil else { return }
        clearDetail()

        let jobID = string(job["id"]) ?? ""
        let model = string(job["model_name"]) ?? string(job["model_id"]) ?? "?"
        let provider = string(job["provider_name"]) ?? string(job["provider_id"]) ?? "?"
        detailStack.addArrangedSubview(PageKit.sectionTitle("\(model) · \(provider)"))
        detailStack.addArrangedSubview(PageKit.help("Job \(jobID) · \(displayState(job))"))

        // Exact times and how long it took: "5m ago" cannot answer "when exactly".
        var times: [String] = []
        if let created = job["created_at"] as? Double { times.append("Created \(Self.stamp(created))") }
        let terminal = ["succeeded", "failed", "cancelled"].contains(string(job["state"]) ?? "")
        if terminal, let updated = job["updated_at"] as? Double { times.append("finished \(Self.stamp(updated))") }
        if let took = job["duration_s"] as? Double { times.append("took \(Self.shortDuration(Int(took.rounded())))") }
        if !times.isEmpty { detailStack.addArrangedSubview(PageKit.help(times.joined(separator: " · "))) }

        // **Who asked.** Not an identity — one API key, shared by every caller — but
        // the difference between Story Studio and a script, which is the question
        // actually asked when a batch fails. Omitted when the request said nothing,
        // rather than drawn as an empty row: "the caller said nothing" and "the
        // caller called itself something" must not look alike here either.
        if let caller = string(job["caller"]), !caller.isEmpty {
            detailStack.addArrangedSubview(PageKit.help("Requested by \(caller)"))
        }
        // The project run this job takes turns with (`<project>/<episode>`); omitted when none was sent.
        if let group = string(job["job_group"]), !group.isEmpty {
            detailStack.addArrangedSubview(PageKit.help("Group \(group)"))
        }

        // **Waiting, not failed.** A job the fleet could not take yet goes back in
        // our own queue rather than being reported as broken, and saying so is the
        // whole point — `accepted` alone reads as "not started", which is a
        // different thing from "the machine was busy and we are trying again".
        // **The reason is not the whole answer.** "The machine is busy" leaves the two questions
        // the owner actually has — *how far back am I* and *when will it try again* — and the
        // gateway publishes both: `queue_position` is a real rank among the waiting jobs of the same
        // modality, and `retry_in_s` is when this gateway makes its next attempt (not a promise about
        // the provider). Each is drawn only when present, so a queue the gateway cannot count does
        // not read as "position zero".
        // The position is published for every `accepted` job, not only a deferred one.
        let waiting = string(job["defer_reason"]) ?? ""
        let position = integer(job["queue_position"]) ?? 0
        if !waiting.isEmpty || position > 0 {
            var line = waiting.isEmpty ? "Queued" : "Waiting for a free machine"
            if position > 0 {
                line += " — position \(position)"
                if let size = integer(job["queue_size"]), size > 0 { line += " of \(size)" }
            }
            if let retry = integer(job["retry_in_s"]), retry > 0 {
                line += ", trying again in \(Self.shortDuration(retry))"
            }
            let label = PageKit.help(waiting.isEmpty ? "\(line)." : "\(line). \(waiting)")
            label.textColor = Health.warn.text
            detailStack.addArrangedSubview(label)
        }

        let lifecycle = lifecycleText(job)
        if !lifecycle.isEmpty {
            let lifecycleLabel = PageKit.help(lifecycle)
            lifecycleLabel.textColor = lifecycleColour(job)
            detailStack.addArrangedSubview(lifecycleLabel)
        }

        // Which machines tried it, and where the retry policy stands (attempt n of N).
        if let attempts = job["attempts"] as? [[String: Any]], !attempts.isEmpty {
            var lines = ["Attempt \(integer(job["attempts_used"]) ?? attempts.count)"
                + (integer(job["attempts_allowed"]).map { " of \($0)" } ?? "")]
            for attempt in attempts {
                let reason = string(attempt["failure_reason"]) ?? string(attempt["failure_code"]) ?? "no reason recorded"
                let at = (attempt["at"] as? Double).map { " · \(Self.stamp($0))" } ?? ""
                lines.append("\(integer(attempt["number"]) ?? 0). \(string(attempt["machine"]) ?? "unknown machine") — \(reason)\(at)")
            }
            detailStack.addArrangedSubview(PageKit.help(lines.joined(separator: "\n")))
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
            outputText.font = TextRole.secondary.font
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
            let cancel = PageKit.button(job["state"] as? String == "running" ? "Cancel Running Job" : "Cancel Queued Job", target: self, action: #selector(cancelSelectedJob))
            detailStack.addArrangedSubview(cancel)
            cancelButton = cancel
        }

        // An uncertain attempt on a Hub location pins that node's machine until
        // it is reconciled or discarded, and an unknown cloud outcome has no
        // upstream to ask at all. The owner is the only one who can decide that
        // waiting is over, so the decision is a button — never a timer.
        discardButton = nil
        if let action = recoveryAction(job) {
            let closing = action == "close_hold"
            let button = PageKit.button(
                closing ? "Close This Hold…" : "Discard Uncertain Attempt…",
                target: self,
                action: #selector(discardSelectedJob)
            )
            button.toolTip = closing
                ? "Close the gateway's own record of this attempt. Nothing is sent upstream — "
                    + "this lane has no discard route — and whatever the provider may still "
                    + "produce is not collected."
                : "Abandon this attempt on the Studio Hub and free the machine it reserved. "
                    + "Whatever the original worker still produces is ignored."
            detailStack.addArrangedSubview(button)
            discardButton = button
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
        label.font = TextRole.secondary.font
        label.textColor = output.present ? .secondaryLabelColor : Health.warn.text
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
        row.spacing = DS.Space.tight
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
        let key = outputKey(jobID: jobID, assetID: assetID)
        if kept { keptKeys.insert(key) } else { keptKeys.remove(key) }
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

    /// What an operator can do about this hold, as the gateway decided it.
    ///
    /// **The gateway owns this, not the view.** It used to be inferred here from
    /// `recovery_context`, which meant a hold the restart sweep recorded (context
    /// null, batch named only in the prose) and every hold on a cloud lane (no
    /// discard route exists) offered no action at all and could never be cleared
    /// from the app. The published action is one of `discard_upstream`
    /// (the Hub is asked, and its answer is the outcome) or `close_hold`
    /// (nothing upstream can be told, so the gateway's own record is closed).
    /// Anything unrecognised offers nothing rather than a button that fails.
    private func recoveryAction(_ job: [String: Any]) -> String? {
        guard job["recovery_required"] as? Bool == true else { return nil }
        guard let action = string(job["recovery_action"]) else { return nil }
        return ["discard_upstream", "close_hold"].contains(action) ? action : nil
    }

    @objc private func discardSelectedJob() {
        guard let jobID = selectedJobID,
              let job = rows.first(where: { string($0["id"]) == jobID }),
              let action = recoveryAction(job) else { return }
        let closing = action == "close_hold"
        let confirm = confirmDiscard ?? (closing ? confirmCloseHoldWithAlert : confirmDiscardWithAlert)
        guard confirm() else { return }
        let requestGeneration = selectionGeneration
        discardButton?.isEnabled = false
        setStatus(
            closing ? "Closing the hold…" : "Discarding the uncertain attempt…",
            error: false
        )
        GatewayAPI.post(discardPath(jobID)) { [weak self] result in
            guard let self,
                  self.isCurrent(jobID: jobID, generation: requestGeneration)
            else { return }
            switch result {
            case .success:
                self.setStatus(
                    closing
                        ? "Hold closed. Nothing was sent upstream; the attempt stays abandoned "
                            + "and its late result is not collected."
                        : "Attempt discarded. The machine is released and the result is ignored.",
                    error: false
                )
                self.reload()
            case .failure(let error):
                self.discardButton?.isEnabled = true
                self.setStatus(
                    closing
                        ? "Could not close the hold: \(error.localizedDescription)"
                        : "Could not discard the attempt: \(error.localizedDescription)",
                    error: true
                )
            }
        }
    }

    /// The owner's confirmation for a **local** close, which contacts nobody.
    ///
    /// Deliberately says what does *not* happen: the provider is not told, so
    /// nothing on its side is released, and this is not a retry.
    private func confirmCloseHoldWithAlert() -> Bool {
        let alert = NSAlert()
        alert.messageText = "Close this hold?"
        alert.informativeText =
            "No discard can be sent for this attempt: the lane that ran it has no such "
            + "route, so the provider is not contacted and nothing on its side changes. "
            + "The gateway stops asking for a manual reconciliation and will not dispatch "
            + "it again. Anything the provider still produces is not collected. This "
            + "cannot be undone."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Close the Hold")
        alert.addButton(withTitle: "Keep It Open")
        return alert.runModal() == .alertFirstButtonReturn
    }

    /// The owner's confirmation, naming what is abandoned. Discarding ignores
    /// whatever the worker may still produce and cannot be undone.
    private func confirmDiscardWithAlert() -> Bool {
        let alert = NSAlert()
        alert.messageText = "Discard this uncertain attempt?"
        alert.informativeText =
            "The Studio Hub will abandon the attempt and release the machine it has been "
            + "holding. Anything the original worker still produces is ignored, and this "
            + "cannot be undone. Reconcile it instead if the result may still matter."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Discard")
        alert.addButton(withTitle: "Keep Waiting")
        return alert.runModal() == .alertFirstButtonReturn
    }

    private func discardPath(_ jobID: String) -> String {
        "/admin/api/jobs/\(escapedJobID(jobID))/discard-uncertain"
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
        detailStatusLabel.textColor = error ? Health.bad.text : .secondaryLabelColor
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

    /// A failed job the Gateway holds because its outcome is unknown is red like any failure, with its own word.
    private func stateHealth(_ job: [String: Any]) -> Health {
        if job["recovery_required"] as? Bool == true { return .bad }
        return Health(state: displayState(job))
    }

    /// "TTS", "Image", "Transcription": the acronym keeps its capitals (`capitalized` made it "Tts").
    static func typeName(_ modality: String?) -> String {
        switch (modality ?? "").lowercased() {
        case "tts": return "TTS"
        case "": return ""
        case let word: return word.prefix(1).uppercased() + word.dropFirst()
        }
    }

    private func lifecycleColour(_ job: [String: Any]) -> NSColor {
        if job["recovery_required"] as? Bool == true { return Health.bad.text }
        if job["cancel_requested"] as? Bool == true { return Health.warn.text }
        return .secondaryLabelColor
    }

    private func outputKey(jobID: String, assetID: String) -> String {
        "\(jobID)\u{001F}\(assetID)"
    }

    private static let stampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "d MMM HH:mm:ss"
        return formatter
    }()

    /// The wall-clock time of an epoch: "5m ago" cannot answer "when exactly".
    private static func stamp(_ epoch: Double) -> String {
        stampFormatter.string(from: Date(timeIntervalSince1970: epoch))
    }

    /// "45s" / "3m 20s" — for a countdown the owner reads at a glance, not a wall clock.
    static func shortDuration(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        let rest = seconds % 60
        return rest == 0 ? "\(minutes)m" : "\(minutes)m \(rest)s"
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
