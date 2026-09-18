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
final class FleetSection: NSObject, AppSection {
    static let shared = FleetSection()

    let title = "Fleet"
    let symbol = "network"
    /// The gateway polls every controller for this page, so it is refreshed on
    /// demand rather than on the host's timer.
    let refreshesAutomatically = false

    private var summaryLabel = NSTextField(labelWithString: "Loading…")
    private var pulseLabel = NSTextField(labelWithString: "")
    private var locationsStack = NSStackView()
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

        locationsStack.orientation = .vertical
        locationsStack.alignment = .leading
        locationsStack.spacing = 14
        root.addArrangedSubview(locationsStack)

        root.addArrangedSubview(PageKit.sectionTitle("Add a location"))
        root.addArrangedSubview(PageKit.help(
            "A location is a controller: another Mac (or site) running Studio Hub in "
            + "controller mode. Use its address and its Hub service token. Its nodes "
            + "are reported by it — this gateway never contacts them directly."
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

        // Natural height at the top: a list of locations has no reason to stretch.
        let container = NSView()
        container.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: container.topAnchor),
            root.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            root.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor),
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
                self.locationsStack.setViews([], in: .top)
            case .success(let payload):
                self.render(payload)
            }
        }
    }

    private func render(_ payload: [String: Any]) {
        let summary = payload["summary"] as? [String: Any] ?? [:]
        let locations = payload["locations"] as? [[String: Any]] ?? []

        let up = summary["locations_up"] as? Int ?? 0
        let total = summary["locations"] as? Int ?? locations.count
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

        locationsStack.setViews(locations.map { card(for: $0) }, in: .top)
    }

    /// One location, drawn as a card so a down site is obvious at a glance.
    private func card(for location: [String: Any]) -> NSView {
        let card = NSView()
        card.wantsLayer = true
        card.layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
        card.layer?.cornerRadius = 10
        card.layer?.borderWidth = 1
        card.layer?.borderColor = NSColor.separatorColor.cgColor

        let reachable = location["reachable"] as? Bool ?? false
        let isLocal = location["local"] as? Bool ?? false

        let dot = NSView()
        dot.wantsLayer = true
        dot.layer?.cornerRadius = 4
        dot.layer?.backgroundColor =
            (reachable ? NSColor.systemGreen : NSColor.systemOrange).cgColor
        dot.translatesAutoresizingMaskIntoConstraints = false
        dot.widthAnchor.constraint(equalToConstant: 8).isActive = true
        dot.heightAnchor.constraint(equalToConstant: 8).isActive = true

        let name = PageKit.label(
            (location["label"] as? String) ?? "Location", size: 13, weight: .semibold
        )
        // "This Mac" is the local Hub; saying controller/standalone for it is
        // still useful, because that is what decides whether it can own nodes.
        var facts: [String] = []
        if let role = location["role"] as? String, !role.isEmpty { facts.append(role) }
        if let version = location["version"] as? String, !version.isEmpty {
            facts.append("Hub \(version)")
        }
        if let url = location["url"] as? String, !url.isEmpty, !isLocal { facts.append(url) }
        let detail = PageKit.label(
            reachable ? facts.joined(separator: " · ") : "Not reachable",
            size: 11.5, colour: .secondaryLabelColor, wrapping: true
        )

        let header = PageKit.row([dot, name, PageKit.label(
            isLocal ? "this Mac" : "", size: 11, colour: .tertiaryLabelColor
        )], spacing: 8)

        let stack = NSStackView(views: [header, detail])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4

        if !reachable, let error = location["error"] as? String {
            stack.addArrangedSubview(PageKit.help(error))
        }

        // The machine's *state* and its *job counts* live in the Hub's activity
        // report, not in `fleet/overview`. Join them by id so one row answers
        // "is it up, and how much work has it done".
        let activity = location["activity"] as? [String: Any] ?? [:]
        var byMachine: [String: [String: Any]] = [:]
        for entry in activity["machines"] as? [[String: Any]] ?? [] {
            if let id = entry["machine"] as? String { byMachine[id] = entry }
        }
        for machine in location["machines"] as? [[String: Any]] ?? [] {
            let id = machine["machine"] as? String ?? ""
            stack.addArrangedSubview(machineRow(machine, activity: byMachine[id]))
        }
        // A machine the activity report knows about but the overview did not
        // list is still a machine.
        let listed = Set((location["machines"] as? [[String: Any]] ?? [])
            .compactMap { $0["machine"] as? String })
        for (id, entry) in byMachine.sorted(by: { $0.key < $1.key }) where !listed.contains(id) {
            stack.addArrangedSubview(machineRow(["machine": id, "label": id], activity: entry))
        }

        let running = location["running_jobs"] as? [[String: Any]] ?? []
        if !running.isEmpty {
            stack.addArrangedSubview(PageKit.label(
                "Running now", size: 11.5, weight: .semibold
            ))
            for job in running { stack.addArrangedSubview(jobRow(job)) }
        }

        if !isLocal, let id = location["id"] as? String {
            let actions = PageKit.row([
                NSButton(title: "Test", target: self, action: #selector(testController(_:))),
                NSButton(title: "Remove", target: self, action: #selector(removeController(_:))),
            ], spacing: 8)
            // The buttons carry the id, so one action serves every card.
            for case let button as NSButton in actions.arrangedSubviews {
                button.identifier = NSUserInterfaceItemIdentifier(id)
                button.bezelStyle = .rounded
                button.controlSize = .small
            }
            stack.addArrangedSubview(actions)
        }

        stack.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: card.topAnchor, constant: 12),
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -12),
            stack.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -12),
            card.widthAnchor.constraint(lessThanOrEqualToConstant: 900),
        ])
        return card
    }

    /// One machine: is it up, what is it, and how much work has it done.
    ///
    /// Absent values stay absent — a machine that has not reported memory must
    /// not be shown as 0 GB, and a Hub that could not measure its own activity
    /// says so (`limitation`) instead of implying a confident number.
    private func machineRow(_ machine: [String: Any], activity: [String: Any]?) -> NSView {
        var parts: [String] = []
        if let chip = machine["chip"] as? String, !chip.isEmpty { parts.append(chip) }
        if let memory = machine["memory_gb"] as? Double { parts.append(format(memory) + " GB") }
        if let free = machine["disk_free_gb"] as? Double { parts.append(format(free) + " GB free") }

        // Work done, from the Hub's activity report.
        if let activity {
            var work: [String] = []
            if let completed = activity["completed"] as? Int { work.append("\(completed) done") }
            if let failed = activity["failed"] as? Int, failed > 0 {
                work.append("\(failed) failed")
            }
            if let median = activity["median_runtime_s"] as? Double {
                work.append(format(median) + "s median")
            } else if let average = machine["average_seconds"] as? Double {
                // Fall back to the overview's average, which is a different
                // statistic and is labelled as such.
                work.append(format(average) + "s avg")
            }
            if !work.isEmpty { parts.append(work.joined(separator: " · ")) }
        } else if let jobs = machine["jobs"] as? Int {
            parts.append("\(jobs) jobs")
        }

        if let modalities = machine["modalities"] as? [String: Int], !modalities.isEmpty {
            parts.append(modalities.sorted { $0.key < $1.key }
                .map { "\($0.value) \($0.key)" }.joined(separator: ", "))
        }

        var line = "• " + ((machine["label"] as? String) ?? "machine")
        if let state = activity?["state"] as? String {
            line = "• " + state.replacingOccurrences(of: "_", with: " ")
                + " — " + ((machine["label"] as? String) ?? "machine")
        }
        if !(machine["reachable"] as? Bool ?? true) { line = "• offline — " + line.dropFirst(2) }
        if !parts.isEmpty { line += " — " + parts.joined(separator: " · ") }
        if let limitation = activity?["limitation"] as? String, !limitation.isEmpty {
            line += " · \(limitation)"
        }

        let row = PageKit.label(line, size: 11.5, colour: .secondaryLabelColor, wrapping: true)
        return row
    }

    private func jobRow(_ job: [String: Any]) -> NSView {
        let running = job["running"] as? Int ?? 0
        let queued = job["queued"] as? Int ?? 0
        let total = job["total"] as? Int ?? 0
        var text = "• \(job["modality"] as? String ?? "job") · \(job["model"] as? String ?? "")"
        text += " — \(running) running, \(queued) queued of \(total)"
        if let label = job["label"] as? String, !label.isEmpty { text += " · \(label)" }
        if job["stalled"] as? Bool == true { text += " · stalled" }
        return PageKit.label(text, size: 11.5, colour: .secondaryLabelColor, wrapping: true)
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
