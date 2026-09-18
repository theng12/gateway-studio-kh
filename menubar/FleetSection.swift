import AppKit

/// Fleet — every location this gateway can send work to.
///
/// Studio Hub's topology puts the list of locations on the GenStudio side:
/// "GenStudio connects once to each location's controller using its address/IP
/// and Hub token. Each controller manages its joined nodes; nodes do not contact
/// GenStudio directly." This gateway *is* the GenStudio endpoint Story Studio
/// talks to, so this is the page that holds that list — Story Studio never
/// learns about a second address.
///
/// The local Mac is always the first location, so a single-Mac install shows
/// something true without any configuration.
final class FleetSection: NSObject, AppSection, NSTableViewDataSource, NSTableViewDelegate {
    static let shared = FleetSection()

    let title = "Fleet"
    let symbol = "network"
    /// The gateway polls every controller for this page, so it is refreshed on
    /// demand rather than on the host's timer.
    let refreshesAutomatically = false

    private var summaryLabel = NSTextField(labelWithString: "Loading…")
    private var pulseLabel = NSTextField(labelWithString: "")
    private var table: NSTableView!
    /// Flattened machine rows. A table virtualises, so this can hold hundreds or
    /// thousands without building that many views.
    private var locations: [[String: Any]] = []
    private var locationTable: NSTableView!
    private var machines: [[String: Any]] = []
    private var runningRows: [[String: Any]] = []
    private var runningTable: NSTableView!
    private var runningHeading = NSTextField(labelWithString: "")
    private var statusLabel = NSTextField(labelWithString: "")
    private var labelField = NSTextField(string: "")
    private var urlField = NSTextField(string: "")
    private var tokenField = NSSecureTextField(string: "")
    private var addStatus = NSTextField(labelWithString: "")
    private var adding = false

    lazy var view: NSView = buildContent()

    func activated() {
        reload()
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let root = PageKit.page()

        root.addArrangedSubview(PageKit.heading(
            "Fleet",
            "Every location this gateway can send work to, and what each one is doing now."
        ))

        summaryLabel.font = .systemFont(ofSize: 12)
        summaryLabel.textColor = .secondaryLabelColor
        let refresh = NSButton(title: "Refresh", target: self, action: #selector(reload))
        refresh.bezelStyle = .rounded
        refresh.controlSize = .small
        let bar = PageKit.row([summaryLabel, NSView(), refresh], spacing: 8)
        root.addArrangedSubview(bar)
        bar.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56).isActive = true

        pulseLabel.font = .systemFont(ofSize: 11.5)
        pulseLabel.textColor = .tertiaryLabelColor
        root.addArrangedSubview(pulseLabel)

        // Three tables, not a stack of cards. The fleet has to hold hundreds of
        // locations and machines, and a stack would build a view for every one
        // of them; a table only builds the rows you can see.
        locationTable = NSTableView()
        locationTable.dataSource = self
        locationTable.delegate = self
        locationTable.rowHeight = 22
        locationTable.usesAlternatingRowBackgroundColors = true
        for (identifier, title, width) in [
            ("llocation", "Location", 180),
            ("lrole", "Role", 110),
            ("lhub", "Hub", 90),
            ("lmachines", "Machines", 90),
            ("lstatus", "Status", 380),
        ] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
            column.title = title
            column.width = CGFloat(width)
            column.minWidth = 60
            locationTable.addTableColumn(column)
        }
        let locationScroll = NSScrollView()
        locationScroll.documentView = locationTable
        locationScroll.hasVerticalScroller = true
        locationScroll.borderType = .bezelBorder
        locationScroll.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(PageKit.sectionTitle("Locations"))
        root.addArrangedSubview(locationScroll)
        NSLayoutConstraint.activate([
            locationScroll.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56),
            locationScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 90),
        ])

        table = NSTableView()
        table.dataSource = self
        table.delegate = self
        table.rowHeight = 22
        table.usesAlternatingRowBackgroundColors = true
        for (identifier, title, width) in [
            ("state", "State", 96),
            ("machine", "Machine", 150),
            ("location", "Location", 140),
            ("jobs", "Jobs", 150),
            ("avg", "Median", 80),
            ("hardware", "Hardware", 210),
            ("modalities", "Modalities", 240),
        ] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
            column.title = title
            column.width = CGFloat(width)
            column.minWidth = 60
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
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 220),
        ])

        statusLabel.font = .systemFont(ofSize: 11.5)
        statusLabel.textColor = .secondaryLabelColor
        root.addArrangedSubview(statusLabel)

        // Work in flight, also as a table, and for the same reason: on a busy
        // fleet this is the longest list on the page.
        runningHeading = PageKit.sectionTitle("Running now")
        root.addArrangedSubview(runningHeading)

        runningTable = NSTableView()
        runningTable.dataSource = self
        runningTable.delegate = self
        runningTable.rowHeight = 22
        runningTable.usesAlternatingRowBackgroundColors = true
        for (identifier, title, width) in [
            ("rlocation", "Location", 150),
            ("rmodality", "Modality", 110),
            ("rmodel", "Model", 300),
            ("rprogress", "Progress", 200),
        ] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
            column.title = title
            column.width = CGFloat(width)
            column.minWidth = 60
            runningTable.addTableColumn(column)
        }
        let runningScroll = NSScrollView()
        runningScroll.documentView = runningTable
        runningScroll.hasVerticalScroller = true
        runningScroll.borderType = .bezelBorder
        runningScroll.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(runningScroll)
        NSLayoutConstraint.activate([
            runningScroll.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56),
            runningScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 110),
        ])

        root.addArrangedSubview(PageKit.sectionTitle("Add a location"))
        root.addArrangedSubview(PageKit.help(
            "Another Mac running Studio Hub. Standalone or controller, both work: "
            + "A new machine can be added while it is still standalone and keep working "
            + "after it is promoted to a controller; nothing here depends on the role. "
            + "Use its address and its Hub service token. A controller reports its own "
            + "nodes — this gateway never contacts them directly."
        ))

        labelField.placeholderString = "Name (e.g. Blue Room)"
        labelField.widthAnchor.constraint(equalToConstant: 220).isActive = true
        urlField.placeholderString = "http://10.0.0.5:47873"
        urlField.widthAnchor.constraint(equalToConstant: 260).isActive = true
        tokenField.placeholderString = "Hub service token"
        tokenField.widthAnchor.constraint(equalToConstant: 260).isActive = true
        let addButton = NSButton(title: "Add location", target: self, action: #selector(addLocation))
        addButton.bezelStyle = .rounded

        root.addArrangedSubview(PageKit.formRow("Name", labelField))
        root.addArrangedSubview(PageKit.formRow("Address", urlField))
        root.addArrangedSubview(PageKit.formRow("Token", tokenField))
        root.addArrangedSubview(PageKit.formRow("", addButton))

        addStatus.font = .systemFont(ofSize: 11.5)
        addStatus.textColor = .secondaryLabelColor
        root.addArrangedSubview(PageKit.formRow("", addStatus))

        // A scrolling page, and it has to be.
        //
        // This page is taller than a window can be: three tables with minimum
        // heights, four section headings and an add-location form. Pinned
        // directly to the host, those required heights propagated outwards and
        // **grew the window** — measured at 1162pt tall on a 1080pt screen, so
        // the bottom edge and the resize corner were off-screen and the window
        // could not be restored. A page's height must never be able to resize the
        // window; a scroll view gives the content somewhere to go instead, and
        // keeps every part of it reachable.
        //
        // The document view must be flipped, or AppKit scrolls to the bottom of
        // a page whose origin is at the bottom-left.
        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(root)

        let pageScroll = NSScrollView()
        pageScroll.hasVerticalScroller = true
        pageScroll.drawsBackground = false
        pageScroll.translatesAutoresizingMaskIntoConstraints = false
        pageScroll.documentView = document

        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: document.topAnchor),
            root.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            root.bottomAnchor.constraint(equalTo: document.bottomAnchor),

            document.leadingAnchor.constraint(equalTo: pageScroll.contentView.leadingAnchor),
            document.trailingAnchor.constraint(equalTo: pageScroll.contentView.trailingAnchor),
            document.topAnchor.constraint(equalTo: pageScroll.contentView.topAnchor),
        ])

        let container = NSView()
        container.addSubview(pageScroll)
        NSLayoutConstraint.activate([
            pageScroll.topAnchor.constraint(equalTo: container.topAnchor),
            pageScroll.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            pageScroll.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            pageScroll.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        return container
    }

    // MARK: Data

    @objc func reload() {
        GatewayAPI.get("/admin/api/fleet") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.stringValue = "Could not read the fleet — \(error)"
                self.machines = []
                self.table.reloadData()
            case .success(let payload):
                self.render(payload)
            }
        }
    }

    private func render(_ payload: [String: Any]) {
        let summary = payload["summary"] as? [String: Any] ?? [:]
        let locationRows = payload["locations"] as? [[String: Any]] ?? []

        let up = summary["locations_up"] as? Int ?? 0
        let total = summary["locations"] as? Int ?? locationRows.count
        let online = summary["machines_online"] as? Int ?? 0
        let offline = summary["machines_offline"] as? Int ?? 0
        let completed = summary["jobs_completed"] as? Int ?? 0
        let failed = summary["jobs_failed"] as? Int ?? 0
        let running = summary["running_jobs"] as? Int ?? 0

        var line = "\(up) of \(total) location\(total == 1 ? "" : "s") reachable"
        line += " · \(online) machine\(online == 1 ? "" : "s") online"
        if offline > 0 { line += ", \(offline) offline" }
        line += " · \(completed) job\(completed == 1 ? "" : "s") completed"
        if failed > 0 { line += ", \(failed) failed" }
        if running > 0 { line += " · \(running) running now" }
        summaryLabel.stringValue = line

        // The pulse is the Hub's own view of what every machine is doing, so it
        // is shown verbatim rather than re-derived here.
        let pulse = summary["pulse"] as? [String: Int] ?? [:]
        let active = pulse.filter { $0.value > 0 && $0.key != "offline" }
        pulseLabel.stringValue = active.isEmpty
            ? (offline > 0 ? "\(offline) machine\(offline == 1 ? "" : "s") not reporting"
                           : "No machines are reporting.")
            : active.sorted { $0.key < $1.key }
                .map { "\($0.value) \($0.key.replacingOccurrences(of: "_", with: " "))" }
                .joined(separator: " · ")

        // The table is fed from the flat machine list the gateway joins for us,
        // so a machine is one row no matter how many locations it came from.
        locations = locationRows
        machines = payload["machines"] as? [[String: Any]] ?? []
        runningRows = payload["running"] as? [[String: Any]] ?? []
        locationTable.reloadData()
        table.reloadData()
        runningTable.reloadData()
        runningHeading.stringValue = runningRows.isEmpty ? "Running now — nothing" : "Running now"
        statusLabel.stringValue = machines.isEmpty
            ? "No machines are reporting."
            : "\(machines.count) machine\(machines.count == 1 ? "" : "s") across "
              + "\(locationRows.count) location\(locationRows.count == 1 ? "" : "s")."
    }

    // MARK: Tables

    func numberOfRows(in tableView: NSTableView) -> Int {
        if tableView === runningTable { return runningRows.count }
        if tableView === locationTable { return locations.count }
        return machines.count
    }

    func tableView(
        _ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int
    ) -> NSView? {
        guard let column = tableColumn else { return nil }
        let isRunning = tableView === runningTable
        let source = isRunning ? runningRows : (tableView === locationTable ? locations : machines)
        guard row < source.count else { return nil }
        let item = source[row]

        let cell = NSTableCellView()
        let label = NSTextField(labelWithString: "")
        label.font = .systemFont(ofSize: 11.5)
        label.lineBreakMode = .byTruncatingTail
        label.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(label)
        cell.textField = label
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])

        if tableView === locationTable {
            let reachable = item["reachable"] as? Bool ?? false
            switch column.identifier.rawValue {
            case "llocation":
                label.stringValue = (item["local"] as? Bool == true ? "" : "")
                    + (item["label"] as? String ?? "—")
            case "lrole":
                label.stringValue = (item["role"] as? String) ?? "—"
            case "lhub":
                label.stringValue = (item["version"] as? String) ?? "—"
            case "lmachines":
                let count = (item["machines"] as? [[String: Any]])?.count ?? 0
                label.stringValue = reachable ? "\(count)" : "—"
            case "lstatus":
                // A location that is down has to say why; it is the difference
                // between fixing a token and fixing a network.
                label.stringValue = reachable
                    ? ((item["url"] as? String) ?? "")
                    : ((item["error"] as? String) ?? "Not reachable")
                label.textColor = reachable ? .secondaryLabelColor : .systemOrange
            default:
                break
            }
            return cell
        }

        if isRunning {
            switch column.identifier.rawValue {
            case "rlocation": label.stringValue = item["location"] as? String ?? "—"
            case "rmodality": label.stringValue = item["modality"] as? String ?? "—"
            case "rmodel": label.stringValue = item["model"] as? String ?? "—"
            case "rprogress":
                let running = item["running"] as? Int ?? 0
                let queued = item["queued"] as? Int ?? 0
                let total = item["total"] as? Int ?? 0
                var text = "\(running) running, \(queued) queued of \(total)"
                if item["stalled"] as? Bool == true { text += " · stalled" }
                label.stringValue = text
            default: break
            }
            return cell
        }

        switch column.identifier.rawValue {
        case "state":
            // The Hub's own wording, and its own limitation notice when it could
            // not measure something.
            var state = (item["state"] as? String)?.replacingOccurrences(of: "_", with: " ")
                ?? ((item["reachable"] as? Bool == true) ? "up" : "offline")
            if let limitation = item["limitation"] as? String, !limitation.isEmpty {
                state += " ·"
            }
            label.stringValue = state
            label.textColor = item["reachable"] as? Bool == false ? .systemOrange : .labelColor
        case "machine":
            label.stringValue = item["label"] as? String ?? "—"
        case "location":
            label.stringValue = item["location"] as? String ?? "—"
        case "jobs":
            let completed = item["completed"] as? Int
            let failed = item["failed"] as? Int
            if completed == nil && failed == nil {
                label.stringValue = "—"
            } else {
                label.stringValue = "\(completed ?? 0) done"
                    + ((failed ?? 0) > 0 ? ", \(failed!) failed" : "")
            }
        case "avg":
            // Median when the Hub measured one; otherwise the overview's average,
            // labelled as an average so the two are never confused.
            if let median = item["median_runtime_s"] as? Double {
                label.stringValue = format(median) + "s med"
            } else if let average = item["average_seconds"] as? Double {
                label.stringValue = format(average) + "s avg"
            } else {
                label.stringValue = "—"
            }
        case "hardware":
            var parts: [String] = []
            if let chip = item["chip"] as? String, !chip.isEmpty { parts.append(chip) }
            if let memory = item["memory_gb"] as? Double { parts.append(format(memory) + " GB") }
            if let free = item["disk_free_gb"] as? Double { parts.append(format(free) + " GB free") }
            label.stringValue = parts.isEmpty ? "—" : parts.joined(separator: " · ")
        case "modalities":
            let modalities = item["modalities"] as? [String: Int] ?? [:]
            label.stringValue = modalities.isEmpty
                ? "—"
                : modalities.sorted { $0.key < $1.key }
                    .map { "\($0.value) \($0.key)" }.joined(separator: ", ")
        default:
            break
        }
        return cell
    }

    private func format(_ value: Double) -> String {
        value >= 100 ? String(format: "%.0f", value) : String(format: "%.1f", value)
    }

    // MARK: Actions

    @objc private func addLocation() {
        guard !adding else { return }
        let url = urlField.stringValue.trimmingCharacters(in: .whitespaces)
        guard !url.isEmpty else {
            addStatus.stringValue = "An address is required."
            return
        }
        adding = true
        addStatus.stringValue = "Adding…"
        GatewayAPI.post("/admin/api/fleet/controllers", body: [
            "label": labelField.stringValue.trimmingCharacters(in: .whitespaces),
            "url": url,
            "token": tokenField.stringValue.trimmingCharacters(in: .whitespaces),
        ]) { [weak self] result in
            guard let self else { return }
            self.adding = false
            switch result {
            case .failure(let error):
                self.addStatus.stringValue = "Could not add the location — \(error)"
            case .success:
                self.labelField.stringValue = ""
                self.urlField.stringValue = ""
                self.tokenField.stringValue = ""
                self.addStatus.stringValue = "Added. Checking it now…"
                self.reload()
            }
        }
    }

    @objc private func testController(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        addStatus.stringValue = "Testing…"
        GatewayAPI.post("/admin/api/fleet/controllers/\(id)/test", body: [:]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.addStatus.stringValue = "Test failed — \(error)"
            case .success(let payload):
                let reachable = payload["reachable"] as? Bool ?? false
                if reachable {
                    let role = payload["role"] as? String ?? "unknown role"
                    let version = payload["version"] as? String ?? "unknown version"
                    let machines = payload["machines"] as? Int ?? 0
                    let nodes = payload["nodes"] as? Int ?? 0
                    self.addStatus.stringValue =
                        "Answered as \(role) on Hub \(version) — \(machines) machines, \(nodes) nodes."
                } else {
                    self.addStatus.stringValue =
                        "No answer — \(payload["error"] as? String ?? "unknown reason")"
                }
            }
        }
    }

    @objc private func removeController(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        GatewayAPI.delete("/admin/api/fleet/controllers/\(id)") { [weak self] result in
            guard let self else { return }
            if case .failure(let error) = result {
                self.addStatus.stringValue = "Could not remove it — \(error)"
            }
            self.reload()
        }
    }
}
