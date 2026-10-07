import Cocoa

/// One Story Studio Mac as `/admin/api/story/instances` reports it (`gateway/story_releases.py`).
struct StoryInstance {
    let id: String
    let machine: String
    let version: String?
    let status: String
    let channels: Int?
    let running: Int?
    let queued: Int?
    let held: Bool
    let phase: String?
    let error: String?
    let ageSeconds: Double?
    let gatewayWaiting: Int
    let gatewayRunning: Int

    static func from(_ row: [String: Any]) -> StoryInstance? {
        guard let id = row["instance_id"] as? String else { return nil }
        let queue = row["queue"] as? [String: Any] ?? [:]
        let update = row["update"] as? [String: Any] ?? [:]
        let jobs = row["gateway_jobs"] as? [String: Any] ?? [:]
        return StoryInstance(
            id: id, machine: row["machine"] as? String ?? id, version: row["version"] as? String,
            status: row["status"] as? String ?? "", channels: row["channels"] as? Int,
            running: queue["running"] as? Int, queued: queue["queued"] as? Int, held: queue["held"] as? Bool ?? false,
            phase: update["phase"] as? String, error: update["error"] as? String,
            ageSeconds: row["age_seconds"] as? Double,
            gatewayWaiting: jobs["waiting"] as? Int ?? 0, gatewayRunning: jobs["running"] as? Int ?? 0)
    }

    var statusText: (String, Health) {
        switch status {
        case "up_to_date": return ("Up to date", .ok)
        case "update_available": return ("Update available", .warn)
        case "updating": return (phase == "draining" ? "Updating… (waiting for jobs)" : "Updating…", .info)
        case "failed": return (phase == "rolled_back" ? "Update rolled back" : "Update failed", .bad)
        case "offline": return ("Offline", .off)
        default: return (status, .off)
        }
    }
}

/// Fleet → Story Studio: every Story Mac on this Gateway, its version against the published Story
/// release, its queue and its jobs here, with "Update Now" per Mac and "Publish a Story Studio
/// Release…". Publishing never pushes: each Mac fetches on its next check-in (about two minutes),
/// verifies the signature, and installs once its queue has drained.
final class FleetStoryTab: FleetTabView {
    /// The shell's banner, so this tab reports the way the other tabs do.
    var report: ((FleetBanner.Kind, String, String) -> Void)?

    private var table: FleetTableController!
    private let releaseLabel = PageKit.label("No Story Studio release is published yet.", .body,
                                             colour: .secondaryLabelColor, wrapping: true)
    private var publishing = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        table = FleetTableController(columns: columns(), rowHeight: 44)
        table.emptyText = "No Story Studio Mac has checked in yet. A Story Mac on a build with remote updates checks in every two minutes."
        table.container.heightAnchor.constraint(equalToConstant: 320).isActive = true

        let publish = PageKit.primaryButton("Publish a Story Studio Release…", target: self, action: #selector(beginPublish))
        let refresh = FleetClosureButton(title: "Refresh") { [weak self] in self?.reload() }
        let header = PageKit.row([PageKit.sectionTitle("Story Studio Macs"), NSView(), refresh, publish])
        page.addArrangedSubview(header)
        fullWidth(header)
        page.addArrangedSubview(releaseLabel)
        page.addArrangedSubview(table.container)
        fullWidth(table.container)
        page.addArrangedSubview(PageKit.label(
            "Signed with the same publisher key as Studio Hub releases. A Mac pauses its queue, lets running "
            + "jobs finish (up to 30 minutes), installs, restarts and resumes. Jobs the owner paused stay paused, "
            + "and a version that fails to start is put back automatically.",
            .body, colour: .secondaryLabelColor, wrapping: true))
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func columns() -> [FleetColumn] {
        func row(_ any: Any) -> StoryInstance? { any as? StoryInstance }
        func count(_ value: Int?) -> String { value.map(String.init) ?? "—" }
        return [
            FleetColumn(id: "machine", title: "Mac", width: 180, text: { row($0)?.machine ?? "" },
                        sortValue: { .text(row($0)?.machine ?? "") }),
            FleetColumn(id: "version", title: "Version", width: 90, text: { row($0)?.version ?? "—" },
                        sortValue: { .text(row($0)?.version ?? "") }),
            FleetColumn(id: "status", title: "Status", width: 190, text: { _ in "" },
                        sortValue: { .text(row($0)?.status ?? "") },
                        cell: { any in
                            guard let r = row(any) else { return NSView() }
                            let (text, health) = r.statusText
                            return fleetBadgeCell(text, health, caption: r.error, tooltip: r.error)
                        }),
            FleetColumn(id: "channels", title: "Channels", width: 70, hideBelow: 760, alignment: .right,
                        text: { count(row($0)?.channels) }, sortValue: { .number(Double(row($0)?.channels ?? -1)) }),
            FleetColumn(id: "queue", title: "Story queue", width: 150, text: { any in
                guard let r = row(any) else { return "" }
                return "\(count(r.running)) running · \(count(r.queued)) queued" + (r.held ? " · paused" : "")
            }),
            FleetColumn(id: "gateway", title: "On this Gateway", width: 140, hideBelow: 860, text: { any in
                guard let r = row(any) else { return "" }
                return "\(r.gatewayRunning) running · \(r.gatewayWaiting) waiting"
            }, sortValue: { .number(Double((row($0)?.gatewayRunning ?? 0) + (row($0)?.gatewayWaiting ?? 0))) }),
            FleetColumn(id: "seen", title: "Checked in", width: 100, hideBelow: 700, text: { any in
                guard let age = row(any)?.ageSeconds else { return "—" }
                return age < 90 ? "\(Int(age)) s ago" : age < 5400 ? "\(Int(age / 60)) min ago" : "\(Int(age / 3600)) h ago"
            }, sortValue: { .number(row($0)?.ageSeconds ?? .infinity) }),
            FleetColumn(id: "actions", title: "", width: 120, flex: 0, sortable: false, text: { _ in "" },
                        cell: { [weak self] any in
                            guard let r = row(any), r.status == "update_available" || r.status == "failed" else { return NSView() }
                            return fleetButtonCell([FleetClosureButton(title: "Update Now") { self?.updateNow(r) }])
                        }),
        ]
    }

    func reload() {
        GatewayAPI.get("/admin/api/story/instances") { [weak self] result in
            guard let self else { return }
            guard case .success(let payload) = result else { return }
            let rows = (payload["instances"] as? [[String: Any]] ?? []).compactMap(StoryInstance.from)
            self.table.setRows(rows)
            if let release = payload["release"] as? [String: Any], let version = release["version"] as? String {
                let file = release["filename"] as? String ?? ""
                self.releaseLabel.stringValue = "Published: Story Studio \(version) (\(file))."
            }
        }
    }

    private func updateNow(_ row: StoryInstance) {
        let path = "/admin/api/story/instances/\(row.id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? row.id)/update"
        GatewayAPI.post(path, body: [:]) { [weak self] result in
            switch result {
            case .failure(let error):
                self?.report?(.error, "Could not ask \(row.machine) to update", error.localizedDescription)
            case .success:
                self?.report?(.success, "\(row.machine) updates on its next check-in",
                              "Within about two minutes it pauses its queue, waits for running jobs, installs and restarts.")
            }
            self?.reload()
        }
    }

    @objc private func beginPublish() {
        guard !publishing else { return }
        let panel = NSOpenPanel()
        panel.title = "Choose a Story Studio release"
        panel.prompt = "Choose"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        // ponytail: `allowedFileTypes` because this app targets macOS 11; swap for UTTypes when the floor rises.
        panel.allowedFileTypes = ["dmg", "zip"]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let sure = NSAlert()
        sure.messageText = "Publish \(url.lastPathComponent) to every Story Studio Mac?"
        sure.informativeText = "The version is read from the app inside. Each Mac installs it after its queue drains, "
            + "unless its owner turned automatic updates off."
        sure.addButton(withTitle: "Publish")
        sure.addButton(withTitle: "Cancel")
        guard sure.runModal() == .alertFirstButtonReturn else { return }
        publishing = true
        report?(.success, "Signing \(url.lastPathComponent)…", "Reading its version and copying it into the Gateway.")
        GatewayAPI.post("/admin/api/story/release/publish", body: ["artifact_path": url.path], timeout: 900) { [weak self] result in
            guard let self else { return }
            self.publishing = false
            switch result {
            case .failure(let error):
                self.report?(.error, "Could not publish the Story Studio release", error.localizedDescription)
            case .success(let payload):
                let version = (payload["descriptor"] as? [String: Any])?["version"] as? String ?? "?"
                self.report?(.success, "Published Story Studio \(version)",
                             "Each Story Mac picks it up on its next check-in, about two minutes.")
            }
            self.reload()
        }
    }
}
