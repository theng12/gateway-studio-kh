// The Jobs window: what the gateway has actually run.
//
// The jobs API existed from the start but had no interface, so a failed
// generation was only discoverable by reading a log file. Failure reasons are
// shown inline — that is the whole point of the window.

import Cocoa

final class JobsSection: NSObject, AppSection, NSTableViewDataSource, NSTableViewDelegate {
    static let shared = JobsSection()

    let title = "Jobs"
    let symbol = "list.bullet.rectangle"
    let refreshesAutomatically = true

    private var table: NSTableView!
    private var scroll: NSScrollView!
    private var filterPopup: NSPopUpButton!
    private var summaryLabel: NSTextField!
    private var detailLabel: NSTextField!
    private var rows: [[String: Any]] = []

    /// The section's content, built once and hosted by the main window.
    /// Lazy so it can be built by a method on self.
    lazy var view: NSView = buildContent()

    private let columns: [(id: String, title: String, width: CGFloat)] = [
        ("time", "When", 70),
        ("modality", "Type", 88),
        ("model", "Model", 210),
        ("provider", "Provider", 120),
        ("state", "Result", 84),
        ("duration", "Took", 66),
    ]

    func activated() {
        reload()
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let root = PageKit.page()

        root.addArrangedSubview(PageKit.heading(
            "Jobs",
            "Everything the gateway has run, with the reason when something failed."
        ))

        summaryLabel = NSTextField(labelWithString: "Loading…")
        summaryLabel.font = .systemFont(ofSize: 12)
        summaryLabel.textColor = .secondaryLabelColor

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

        // ---- table
        table = NSTableView()
        table.dataSource = self
        table.delegate = self
        table.rowHeight = 24
        table.usesAlternatingRowBackgroundColors = true
        table.allowsMultipleSelection = false
        table.style = .inset
        for column in columns {
            let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(column.id))
            col.title = column.title
            col.width = column.width
            table.addTableColumn(col)
        }

        scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false

        root.addArrangedSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56),
            // Low on purpose. This was 260 — a *required* height, which is the
            // one thing a page must never propagate outwards: AppKit satisfies it
            // by growing the window. A table is meant to absorb spare space, not
            // to demand it, so the floor is only here to stop it collapsing.
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 140),
        ])

        // ---- detail
        detailLabel = NSTextField(wrappingLabelWithString: "Select a job to see its result.")
        detailLabel.font = .systemFont(ofSize: 11.5)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.isSelectable = true
        detailLabel.translatesAutoresizingMaskIntoConstraints = false
        // A wrapping label's required height is its text, and `error_detail` is
        // arbitrarily long. Without a cap, selecting one failed job grew the
        // window — the same defect as the page-level one, arriving through a
        // single label. Four lines is enough to read a failure and it stays
        // selectable in full.
        detailLabel.maximumNumberOfLines = 4
        detailLabel.lineBreakMode = .byTruncatingTail
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

    // MARK: Data

    @objc func reload() {


        var path = "/admin/api/jobs?limit=200"
        switch filterPopup.indexOfSelectedItem {
        case 1: path += "&state=succeeded"
        case 2: path += "&state=failed"
        case 3: path += "&state=cancelled"
        default: break
        }

        GatewayAPI.get(path) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let payload):
                self.rows = payload["jobs"] as? [[String: Any]] ?? []
                let stats = payload["stats"] as? [String: Any] ?? [:]
                let byState = stats["by_state"] as? [String: Any] ?? [:]
                let succeeded = byState["succeeded"] as? Int ?? 0
                let failed = byState["failed"] as? Int ?? 0
                let total = stats["total"] as? Int ?? self.rows.count
                self.summaryLabel.stringValue =
                    "\(self.rows.count) shown · \(succeeded) succeeded · \(failed) failed · "
                    + "\(total) total · last \(GatewayAPI.relativeTime(stats["last_job_at"] as? Double))"
            case .failure(let error):
                self.rows = []
                self.summaryLabel.stringValue = error.localizedDescription
            }
            self.table.reloadData()
        }
    }

    // MARK: Table

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(
        _ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int
    ) -> NSView? {
        guard let tableColumn, row < rows.count else { return nil }
        let job = rows[row]

        let identifier = tableColumn.identifier
        let field = NSTextField(labelWithString: "")
        field.font = .systemFont(ofSize: 11.5)
        field.lineBreakMode = .byTruncatingTail

        switch identifier.rawValue {
        case "time":
            field.stringValue = GatewayAPI.relativeTime(job["created_at"] as? Double)
            field.textColor = .secondaryLabelColor
        case "modality":
            field.stringValue = (job["modality"] as? String ?? "").capitalized
        case "model":
            field.stringValue = job["model_name"] as? String ?? "?"
        case "provider":
            field.stringValue = job["provider_name"] as? String ?? ""
            field.textColor = .secondaryLabelColor
        case "state":
            let state = job["state"] as? String ?? "?"
            field.stringValue = state
            switch state {
            case "succeeded": field.textColor = .systemGreen
            case "failed": field.textColor = .systemRed
            case "cancelled": field.textColor = .tertiaryLabelColor
            default: field.textColor = .systemYellow
            }
        case "duration":
            field.stringValue = GatewayAPI.duration(job["duration_s"] as? Double)
            field.textColor = .secondaryLabelColor
            field.font = .monospacedDigitSystemFont(ofSize: 11.5, weight: .regular)
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
        let index = table.selectedRow
        guard index >= 0, index < rows.count else {
            detailLabel.stringValue = "Select a job to see its result."
            return
        }
        let job = rows[index]
        var lines: [String] = []
        lines.append("\(job["model_name"] as? String ?? "?") via \(job["provider_name"] as? String ?? "?")")
        lines.append("id \(job["id"] as? String ?? "")")
        if let code = job["error_code"] as? String, !code.isEmpty {
            lines.append("failure: \(code)")
        }
        if let detail = job["error_detail"] as? String, !detail.isEmpty {
            lines.append(detail)
        }
        if job["has_artifact"] as? Bool == true, job["state"] as? String == "succeeded" {
            let kind = job["modality"] as? String ?? ""
            let artifact: String
            switch kind {
            case "image": artifact = "images/\(job["id"] as? String ?? "")/file"
            case "tts": artifact = "jobs/\(job["id"] as? String ?? "")/audio"
            case "transcription": artifact = "transcriptions/\(job["id"] as? String ?? "")/srt"
            case "video": artifact = "videos/\(job["id"] as? String ?? "")/file"
            default: artifact = ""
            }
            if !artifact.isEmpty {
                lines.append("artifact: http://127.0.0.1:\(GatewayController.shared.port)/v1/\(artifact)")
            }
        }
        detailLabel.stringValue = lines.joined(separator: "\n")
    }
}
