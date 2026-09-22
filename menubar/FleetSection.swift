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
    private var retryFailedButton: NSButton!
    private var failedRefreshProviderIDs: [String] = []
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
        summaryLabel.lineBreakMode = .byWordWrapping
        summaryLabel.maximumNumberOfLines = 3
        // Refresh means **refresh**: reachability *and* models. See
        // `refreshEverything`.
        let refresh = NSButton(title: "Refresh", target: self,
                               action: #selector(refreshEverything))
        refresh.bezelStyle = .rounded
        refresh.controlSize = .small
        retryFailedButton = NSButton(
            title: "Retry failed",
            target: self,
            action: #selector(retryFailedRefresh)
        )
        retryFailedButton.bezelStyle = .rounded
        retryFailedButton.controlSize = .small
        retryFailedButton.isHidden = true
        let bar = PageKit.row(
            [summaryLabel, NSView(), retryFailedButton, refresh],
            spacing: 8
        )
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
            ("llocation", "Location", 150),
            ("lrole", "Role", 80),
            ("lhub", "Hub", 70),
            ("lmachines", "Machines", 80),
            ("lstatus", "Status", 220),
            ("lactions", "Actions", 340),
        ] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
            column.title = title
            column.width = CGFloat(width)
            // The Actions column holds four buttons that are ~277pt wide together.
            // At `minWidth = 60` it could be squeezed to 60, and because AppKit's
            // default autoresizing style makes the *last* column absorb the
            // difference, "Remove" was pushed outside the clip view and could
            // neither be seen nor clicked at the default window width — the buttons
            // were live and unreachable, which is the same defect as wiring them to
            // nothing. A measured floor plus a horizontal scroller means they can
            // always be reached, at any window size.
            column.minWidth = identifier == "lactions" ? 285 : 60
            locationTable.addTableColumn(column)
        }
        locationTable.columnAutoresizingStyle = .noColumnAutoresizing
        let locationScroll = NSScrollView()
        locationScroll.documentView = locationTable
        locationScroll.hasVerticalScroller = true
        // Columns keep their widths and the pane scrolls, rather than the last
        // column being squeezed out of reach. The table is wider than the pane at
        // anything under ~1200pt, and unreachable controls are worse than a
        // scrollbar.
        locationScroll.hasHorizontalScroller = true
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
        runningTable.columnAutoresizingStyle = .noColumnAutoresizing
        let runningScroll = NSScrollView()
        runningScroll.documentView = runningTable
        runningScroll.hasVerticalScroller = true
        runningScroll.hasHorizontalScroller = true
        runningScroll.borderType = .bezelBorder
        runningScroll.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(runningScroll)
        NSLayoutConstraint.activate([
            runningScroll.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56),
            runningScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 110),
        ])

        root.addArrangedSubview(PageKit.sectionTitle("Add a location"))
        root.addArrangedSubview(PageKit.help(
            "Another Mac running Studio Hub. Standalone or controller, both work. "
            + "A new machine can be added while it is still standalone and keep working "
            + "after it is promoted to a controller; nothing here depends on the role. "
            + "Enter its address and its Fleet token — the Fleet token is the one this "
            + "gateway needs to send work there and collect the results. A controller "
            + "reports its own nodes — this gateway never contacts them directly. Every "
            + "location below can be renamed at any time: a name is a label, and no "
            + "address, credential or model depends on it. On the other Mac, Studio Hub "
            + "must be told to answer on a network this one can reach: in its Settings, "
            + "stop the services, tick its Tailscale network under "
            + "\u{201C}Listening on\u{201D}, and start them again. It answers only that Mac "
            + "itself by default, and nothing here can change that from a distance."
        ))
        // The Hub's own Connections page labels these two and says what each is for.
        // A person pasting a token here has to be able to tell which one this page
        // wants before they paste it: both reach every route the gateway calls, so
        // the wrong one works silently until the day it matters.
        root.addArrangedSubview(PageKit.help(
            "Two tokens, two jobs.\n\n"
            + "Hub token — the Mac's own key.\n"
            + "Who it is for: the person who owns that Mac, using that Mac.\n"
            + "What it does: change settings, install or remove models, delete output, "
            + "manage shared voices.\n"
            + "Never send it to GatewayKH unless GatewayKH will manage shared voices on "
            + "that Mac. Anyone who holds it can do everything on that Mac.\n\n"
            + "Fleet token — the work badge for the group.\n"
            + "Who it is for: GatewayKH, and every Mac that joins a controller.\n"
            + "What it allows: send work, follow the job, download the result, "
            + "acknowledge it, cancel it or discard an uncertain one, and read the model "
            + "and fleet lists.\n"
            + "What it cannot do: change settings, install or remove models, delete "
            + "output, manage shared voices, or make a Mac join or leave.\n"
            + "This is the token to enter here."
        ))
        // Joining is the Mac's own act, on the Mac itself. Nothing on this side can
        // enlist or dismiss a machine, and saying so here stops "why can't GatewayKH
        // just add it?" from becoming a support question.
        root.addArrangedSubview(PageKit.help(
            "Joining is that Mac's own decision, made from its own app with the "
            + "controller's Fleet token. GatewayKH cannot join or unjoin a Mac."
        ))

        labelField.placeholderString = "Name (e.g. Blue Room)"
        labelField.widthAnchor.constraint(equalToConstant: 220).isActive = true
        urlField.placeholderString = "http://10.0.0.5:47873"
        urlField.widthAnchor.constraint(equalToConstant: 260).isActive = true
        tokenField.placeholderString = "the controller's Fleet token"
        tokenField.widthAnchor.constraint(equalToConstant: 260).isActive = true
        let addButton = NSButton(title: "Add location", target: self, action: #selector(addLocation))
        addButton.bezelStyle = .rounded

        root.addArrangedSubview(PageKit.formRow("Name", labelField))
        root.addArrangedSubview(PageKit.formRow("Address", urlField))
        root.addArrangedSubview(PageKit.formRow(
            "Fleet token",
            [
                tokenField,
                PageKit.help(
                    "The token this gateway needs to send work to that Mac and "
                    + "collect the results."
                ),
            ]
        ))
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

    /// Refresh each location's reachability **and re-read its models**.
    ///
    /// The button used to call `reload()` alone — `/admin/api/fleet`, the
    /// reachability probe — so a location whose models had never been read still
    /// showed none afterwards, and pressing Refresh looked like it did nothing.
    /// "No models" reads as "it has nothing" rather than "nobody has asked it
    /// yet", which is exactly what this page exists to tell apart; the web console
    /// has "Refresh all models" and this had only a button that said "Refresh".
    ///
    /// Deliberately the *locations* on this page, not `/refresh-all`: that would
    /// also re-read every cloud provider, and the client's 15-second timeout is
    /// shorter than that takes. A location's refresh is one Hub read.
    @objc private func refreshEverything() {
        // The provider id, not the location id: a row carries both and they are
        // different strings (`local` versus `studio-hub-local`). The gateway
        // publishes it precisely so no client has to derive it.
        let providerIds = uniqueProviderIDs(from: locations)
        guard !providerIds.isEmpty else {
            failedRefreshProviderIDs = []
            retryFailedButton?.isHidden = true
            reload()
            return
        }
        refreshProviders(providerIds)
    }

    @objc func reload() {
        reload(completion: nil)
    }

    private func reload(completion: (() -> Void)?) {
        GatewayAPI.get("/admin/api/fleet") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summaryLabel.stringValue = "Could not read the fleet — \(error)"
                // Clear *every* table, not just the machines one. The Locations
                // and Running-now tables kept their previous contents, so an error
                // banner sat above rows still claiming a reachability that had not
                // been measured this time. Stale numbers presented as current are
                // the same failure as inventing them.
                self.machines = []
                self.locations = []
                self.runningRows = []
                self.table.reloadData()
                self.runningTable.reloadData()
                self.statusLabel.stringValue = ""
            case .success(let payload):
                self.render(payload)
            }
            completion?()
        }
    }

    @objc private func retryFailedRefresh() {
        let providerIDs = failedRefreshProviderIDs
        guard !providerIDs.isEmpty else { return }
        refreshProviders(providerIDs)
    }

    private func uniqueProviderIDs(from rows: [[String: Any]]) -> [String] {
        var seen = Set<String>()
        return rows.compactMap { $0["provider_id"] as? String }
            .filter { !$0.isEmpty && seen.insert($0).inserted }
    }

    private func refreshProviders(_ providerIDs: [String]) {
        let providerIDs = providerIDs.filter { !$0.isEmpty }
        guard !providerIDs.isEmpty else { return }
        retryFailedButton?.isHidden = true
        summaryLabel.textColor = .secondaryLabelColor
        summaryLabel.stringValue =
            "Refreshing \(providerIDs.count) location\(providerIDs.count == 1 ? "" : "s")…"

        let labels = Dictionary(
            uniqueKeysWithValues: locations.compactMap { row -> (String, String)? in
                guard let providerID = row["provider_id"] as? String,
                      !providerID.isEmpty else { return nil }
                return (providerID, row["label"] as? String ?? providerID)
            }
        )
        var finished = 0
        var failures: [(providerID: String, detail: String)] = []

        for providerID in providerIDs {
            let encoded = providerID.addingPercentEncoding(
                withAllowedCharacters: .alphanumerics) ?? providerID
            GatewayAPI.post("/admin/api/providers/\(encoded)/refresh") { [weak self] result in
                guard let self else { return }
                switch result {
                case .failure(let error):
                    failures.append((providerID, error.localizedDescription))
                case .success(let payload):
                    if let error = payload["error"] as? String, !error.isEmpty {
                        failures.append((providerID, error))
                    }
                }
                finished += 1
                guard finished == providerIDs.count else { return }
                self.reload { [weak self] in
                    self?.reportRefresh(
                        total: providerIDs.count,
                        failures: failures,
                        labels: labels
                    )
                }
            }
        }
    }

    private func reportRefresh(
        total: Int,
        failures: [(providerID: String, detail: String)],
        labels: [String: String]
    ) {
        failedRefreshProviderIDs = failures.map { $0.providerID }
        retryFailedButton?.isHidden = failures.isEmpty
        summaryLabel.textColor = failures.isEmpty ? .secondaryLabelColor : .systemOrange
        let succeeded = total - failures.count
        guard !failures.isEmpty else {
            summaryLabel.stringValue =
                "Refreshed \(total) location\(total == 1 ? "" : "s")."
            return
        }
        let details = failures.map {
            "\(labels[$0.providerID] ?? $0.providerID): \($0.detail)"
        }.joined(separator: "; ")
        summaryLabel.stringValue =
            "Refreshed \(succeeded); Could not discover at \(failures.count) " +
            "location\(failures.count == 1 ? "" : "s") — \(details)"
    }

    private func render(_ payload: [String: Any]) {
        let summary = payload["summary"] as? [String: Any] ?? [:]
        let locationRows = payload["locations"] as? [[String: Any]] ?? []

        let up = summary["locations_up"] as? Int ?? 0
        let total = summary["locations"] as? Int ?? locationRows.count
        let online = summary["machines_online"] as? Int ?? 0
        let offline = summary["machines_offline"] as? Int ?? 0
        // Neither online nor offline: the Hub reported no state for that machine.
        // Shown rather than dropped, so the sentence accounts for every machine in
        // the table below it instead of leaving an unexplained gap.
        let unknown = summary["machines_unknown"] as? Int ?? 0
        let completed = summary["jobs_completed"] as? Int ?? 0
        let failed = summary["jobs_failed"] as? Int ?? 0
        let running = summary["running_jobs"] as? Int ?? 0

        var line = "\(up) of \(total) location\(total == 1 ? "" : "s") reachable"
        line += " · \(online) machine\(online == 1 ? "" : "s") online"
        if offline > 0 { line += ", \(offline) offline" }
        if unknown > 0 { line += ", \(unknown) with no state reported" }
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
            // The actions column carries buttons, not text, so it is built before
            // the label cell below rather than inside its switch.
            if column.identifier.rawValue == "lactions" {
                return locationActions(for: item)
            }
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

    /// Test / Token / Rename / Remove, per location.
    ///
    /// This Mac gets Rename only: it has no address to test and nothing to remove,
    /// and its credential comes from its own Hub rather than from here. Its name is
    /// its own, like any other location's.
    private func locationActions(for item: [String: Any]) -> NSView {
        let isLocal = item["local"] as? Bool == true
        let id = item["id"] as? String ?? ""

        func button(_ title: String, _ action: Selector) -> NSButton {
            let button = NSButton(title: title, target: self, action: action)
            button.identifier = NSUserInterfaceItemIdentifier(id)
            button.bezelStyle = .rounded
            button.controlSize = .small
            return button
        }

        var views: [NSView] = []
        if !isLocal {
            views.append(button("Test", #selector(testController(_:))))
        }
        views.append(button("Rename", #selector(renameController(_:))))
        if !isLocal {
            // One credentials action, not two. Five buttons did not fit this cell,
            // and AppKit left the overflow out of the view hierarchy entirely — so
            // **Remove** was unreachable from the app while its endpoint worked.
            // Merging the tokens also puts the pair in one place, which is where
            // the difference between them belongs.
            views.append(button("Credentials…", #selector(editCredentials(_:))))
            views.append(button("Remove", #selector(removeController(_:))))
        }

        let cell = NSTableCellView()
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.spacing = 4
        stack.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            stack.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
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
            case .success(let payload):
                self.labelField.stringValue = ""
                self.urlField.stringValue = ""
                self.tokenField.stringValue = ""
                self.describe(
                    payload["credential"] as? [String: Any],
                    verb: "Added",
                    discovered: payload["discovered"] as? Int,
                    discoveryError: payload["discovery_error"] as? String
                )
                self.reload()
            }
        }
    }

    /// Rename any location, at any time.
    ///
    /// A name here is a display label, never an identity: the address, the
    /// credential and every model already discovered from that location stay
    /// exactly as they are, so this can never cost a re-entry of the token.
    @objc private func renameController(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        let current = locations.first { ($0["id"] as? String) == id }?["label"] as? String ?? ""

        let field = NSTextField(string: current)
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)

        let alert = NSAlert()
        alert.messageText = "Rename this location"
        alert.informativeText =
            "Only the name changes. Its address, its credential and its models are untouched."
        alert.accessoryView = field
        alert.addButton(withTitle: "Rename")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field

        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let name = field.stringValue.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else {
            addStatus.stringValue = "A name is required."
            return
        }

        addStatus.stringValue = "Renaming…"
        GatewayAPI.patch("/admin/api/fleet/controllers/\(id)", body: ["label": name]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.addStatus.stringValue = "Could not rename it — \(error)"
            case .success:
                self.addStatus.stringValue = "Renamed to “\(name)”."
                self.reload()
            }
        }
    }

    /// Render the gateway's read on a pasted credential, and what adding found.
    ///
    /// Both of a controller's credentials reach every route this gateway calls, so
    /// an over-privileged one works perfectly and is never noticed. That makes
    /// saying which one was pasted, at the moment it was pasted, the whole value of
    /// the check.
    private func describe(
        _ credential: [String: Any]?,
        verb: String,
        discovered: Int? = nil,
        discoveryError: String? = nil
    ) {
        // Discovered models arrive DISABLED, and nothing on this page says so.
        // Saying it here is the difference between "the location has no models"
        // and "you have not enabled any yet".
        var tail = ""
        if let discovered {
            tail = discovered == 0
                ? " It reported no models."
                : " Discovered \(discovered) model\(discovered == 1 ? "" : "s") — enable the ones "
                  + "you want in Models; nothing is available until you do."
        } else if let discoveryError {
            tail = " Its models could not be read yet — \(discoveryError)"
        }

        guard let credential else {
            addStatus.textColor = .systemOrange
            addStatus.stringValue = "\(verb).\(tail)"
            return
        }
        let scope = credential["scope"] as? String ?? "unknown"
        let detail = credential["detail"] as? String ?? ""
        switch scope {
        case "fleet":
            addStatus.textColor = .secondaryLabelColor
            addStatus.stringValue = "\(verb). \(detail)\(tail)"
        case "operator", "rejected":
            addStatus.textColor = .systemOrange
            addStatus.stringValue = "⚠︎ \(verb), but \(detail)\(tail)"
        default:
            addStatus.textColor = .secondaryLabelColor
            let lead = detail.isEmpty ? "\(verb)." : "\(verb). \(detail)"
            addStatus.stringValue = "\(lead)\(tail)"
        }
    }

    /// Both of a location's credentials, in one place, each labelled with what it is for.
    ///
    /// They are different values on purpose — the Hub refuses shared-voice routes to
    /// a fleet token so a node's credential can never administer the controller that
    /// owns it — and that difference is exactly what belongs in one explanation
    /// rather than two separate buttons.
    @objc private func editCredentials(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        let label = locations.first { ($0["id"] as? String) == id }?["label"] as? String ?? id

        func field(_ placeholder: String) -> NSSecureTextField {
            let field = NSSecureTextField(string: "")
            field.placeholderString = placeholder
            field.translatesAutoresizingMaskIntoConstraints = false
            field.widthAnchor.constraint(equalToConstant: 320).isActive = true
            return field
        }
        func caption(_ text: String) -> NSTextField {
            let caption = NSTextField(labelWithString: text)
            caption.font = .systemFont(ofSize: 11)
            caption.textColor = .secondaryLabelColor
            caption.lineBreakMode = .byWordWrapping
            // Both captions spell out what a token is for; the Fleet one lists the
            // full allowlist, so it needs more than the three lines it used to get.
            caption.maximumNumberOfLines = 6
            caption.preferredMaxLayoutWidth = 320
            return caption
        }

        let fleet = field("the controller's Fleet token")
        let hub = field("the controller's Hub token (optional)")
        let stack = NSStackView(views: [
            caption("Fleet token — the work badge for the group, from the controller's "
                    + "Connections page. It sends work, follows the job, downloads and "
                    + "acknowledges the result, cancels or discards it, and reads the model "
                    + "and fleet lists. This is the one that runs work."),
            fleet,
            caption("Hub token — the Mac's own key, from the controller's Connections page. "
                    + "Kept for shared-voice administration only: it creates, lists or removes "
                    + "shared voices on that controller. Never used to run work. Leave empty "
                    + "if you do not share voices."),
            hub,
        ])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 5
        stack.frame = NSRect(x: 0, y: 0, width: 320, height: 230)

        let alert = NSAlert()
        alert.messageText = "Credentials for \(label)"
        alert.informativeText =
            "Leave a field empty to keep what is already stored. This does not disturb the "
            + "location's name, address or models."
        alert.accessoryView = stack
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = fleet
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let fleetToken = fleet.stringValue.trimmingCharacters(in: .whitespaces)
        let hubToken = hub.stringValue.trimmingCharacters(in: .whitespaces)
        guard !fleetToken.isEmpty || !hubToken.isEmpty else { return }

        addStatus.textColor = .secondaryLabelColor
        addStatus.stringValue = "Saving…"

        // Fleet token first: its reply is the one that reports scope and discovery.
        let finish: () -> Void = { [weak self] in
            guard let self else { return }
            guard !hubToken.isEmpty else { self.reload(); return }
            GatewayAPI.put("/admin/api/fleet/controllers/\(id)/hub-token",
                           body: ["token": hubToken]) { [weak self] result in
                guard let self else { return }
                if case .failure(let error) = result {
                    self.addStatus.textColor = .systemOrange
                    self.addStatus.stringValue = "Saved the fleet token, but not the Hub token — \(error)"
                } else {
                    self.addStatus.textColor = .secondaryLabelColor
                    self.addStatus.stringValue =
                        "Saved. \(label) keeps its fleet token for work, and can now be given shared voices."
                }
                self.reload()
            }
        }

        guard !fleetToken.isEmpty else {
            finish()
            return
        }
        GatewayAPI.put("/admin/api/fleet/controllers/\(id)/credential",
                       body: ["token": fleetToken]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.addStatus.textColor = .systemOrange
                self.addStatus.stringValue = "Could not save it — \(error)"
            case .success(let payload):
                self.describe(
                    payload["credential"] as? [String: Any],
                    verb: "Saved",
                    discovered: payload["discovered"] as? Int
                )
                finish()
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
                    self.describe(
                        payload["credential"] as? [String: Any],
                        verb: "Answered as \(role) on Hub \(version) — \(machines) machines, \(nodes) nodes"
                    )
                } else {
                    // The reason is the gateway's, and it distinguishes a refused
                    // connection (Studio Hub answering this Mac only) from a bad
                    // token from a silent network — three problems with three
                    // different fixes, one of which otherwise costs a site visit.
                    self.addStatus.textColor = .systemOrange
                    self.addStatus.stringValue =
                        "No answer — \(payload["error"] as? String ?? "unknown reason")"
                }
            }
        }
    }

    /// Remove a location, after saying what that costs.
    ///
    /// Destructive and not undoable from here: it forgets the credential as well as
    /// the row, so putting it back means pasting a token again. A stray click on a
    /// table row should not do that, and the confirmation names the location so
    /// there is no doubt which one is going.
    @objc private func removeController(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        let label = locations.first { ($0["id"] as? String) == id }?["label"] as? String ?? id

        let alert = NSAlert()
        alert.messageText = "Remove \(label)?"
        alert.informativeText =
            "This gateway stops talking to that controller and forgets its credentials. Nothing on "
            + "the controller itself changes, and its models stop being offered here until you add "
            + "it again and paste a token."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Remove")
        alert.addButton(withTitle: "Cancel")
        // Cancel is the default, so a stray Return does not remove a location.
        alert.buttons.first?.keyEquivalent = ""
        alert.buttons.last?.keyEquivalent = "\r"
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        GatewayAPI.delete("/admin/api/fleet/controllers/\(id)") { [weak self] result in
            guard let self else { return }
            if case .failure(let error) = result {
                self.addStatus.textColor = .systemOrange
                self.addStatus.stringValue = "Could not remove it — \(error)"
            } else {
                self.addStatus.textColor = .secondaryLabelColor
                self.addStatus.stringValue = "Removed \(label)."
            }
            self.reload()
        }
    }
}
