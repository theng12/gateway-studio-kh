// Remote final rendering, from the Gateway Fleet page (owner decision 2026-10-23).
//
// Lists the Macs that publish `video.final_render` - same local network first, because that is where to
// start - and gives each one a single switch: "Allow remote rendering (untested)". Off by default for
// every Mac. The Gateway keeps the choice and relays it to that Mac's Studio Hub through the controller;
// this file only draws what `/admin/api/fleet/remote-rendering` returns and sends one boolean per Mac.
//
// **The switch is not the whole story, and the sheet says so.** A Mac renders only when it is switched on
// here AND its own Studio Hub has confirmed it accepts unqualified work. "On here, waiting for the Mac"
// is a real state (an older Studio Hub never leaves it), shown as such rather than as a plain "On".

import AppKit

struct FleetRenderWorker: Equatable {
    let controllerId: String
    let machineId: String
    let name: String
    let chip: String?
    let memoryGB: Double?
    let idleState: String
    let network: String
    let ownerNetwork: String?
    let address: String?
    let authority: String
    let optedIn: Bool
    let hubAccepting: Bool
    let suggested: Bool
    let note: String?

    init?(_ row: [String: Any]) {
        guard let controller = row["controller_id"] as? String, !controller.isEmpty,
              let machine = row["machine_id"] as? String, !machine.isEmpty else { return nil }
        controllerId = controller
        machineId = machine
        let label = (row["machine"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        name = (label?.isEmpty == false ? label : nil) ?? machine
        chip = row["chip"] as? String
        memoryGB = (row["memory_gb"] as? NSNumber)?.doubleValue
        idleState = row["idle_state"] as? String ?? "not_ready"
        network = row["network"] as? String ?? "unknown"
        ownerNetwork = (row["owner_network"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        address = row["address"] as? String
        authority = row["authority"] as? String ?? "none"
        optedIn = row["opted_in"] as? Bool ?? false
        hubAccepting = row["hub_accepting"] as? Bool ?? false
        suggested = row["suggested"] as? Bool ?? false
        note = row["note"] as? String
    }

    /// A Mac that already renders on a qualification has nothing to switch.
    var alreadyQualified: Bool { authority == "production" }

    var networkText: String {
        // The owner's own name (Fleet → Networks) wins: it is where the Mac really is.
        if let ownerNetwork { return "Network \(ownerNetwork)" }
        switch network {
        case "same_network": return "Same network"
        case "other_lan": return "Another local network"
        case "tailscale": return "Tailscale"
        default: return "Network unknown"
        }
    }

    var idleText: String {
        switch idleState {
        case "idle": return "Idle"
        case "busy": return "Busy"
        case "offline": return "Offline"
        default: return "Not ready"
        }
    }

    /// Where this Mac stands, in the words the page uses: the switch and the Mac's own confirmation.
    var statusText: String {
        if alreadyQualified { return "Already qualified" }
        if optedIn && hubAccepting { return "On, confirmed by the Mac" }
        if optedIn { return "On here, waiting for the Mac's Studio Hub" }
        return "Off"
    }

    var detailLine: String {
        var parts: [String] = []
        if let chip = chip, !chip.isEmpty { parts.append(chip) }
        if let memoryGB = memoryGB { parts.append(String(format: "%.0f GB", memoryGB)) }
        parts.append(idleText)
        parts.append(networkText + (address.map { " (\($0))" } ?? ""))
        parts.append(statusText)
        return parts.joined(separator: " · ")
    }
}

struct FleetRenderJob: Equatable {
    let id: String
    let machine: String
    let state: String
    let unqualified: Bool
    let createdAt: Double?

    init?(_ row: [String: Any]) {
        guard let id = row["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        machine = row["machine"] as? String ?? row["machine_id"] as? String ?? "a Mac"
        state = row["state"] as? String ?? "unknown"
        unqualified = row["unqualified"] as? Bool ?? false
        createdAt = (row["created_at"] as? NSNumber)?.doubleValue
    }

    /// The row's line. An unqualified render says so; a qualified one is not labelled at all.
    var line: String {
        let when = createdAt.map { FleetRenderClock.text($0) + " · " } ?? ""
        return when + machine + " · " + state.replacingOccurrences(of: "_", with: " ")
            + (unqualified ? " · unqualified" : "")
    }
}

/// The few places a time is drawn, kept out of the model so the model stays plain data.
enum FleetRenderClock {
    static func text(_ epoch: Double) -> String {
        let formatter = DateFormatter()
        formatter.dateStyle = .short
        formatter.timeStyle = .short
        return formatter.string(from: Date(timeIntervalSince1970: epoch))
    }
}

/// A flipped document view, so a short list sits at the top of the scroll area, not the bottom.
private final class FleetRenderDocument: NSView {
    override var isFlipped: Bool { true }
}

/// One checkbox per Mac. Nothing is sent until Save; `changes` is exactly the Macs whose box moved.
final class FleetRemoteRenderForm: NSObject {
    let workers: [FleetRenderWorker]
    private let boxes: [NSButton]
    let view: NSView

    init(workers: [FleetRenderWorker], jobs: [FleetRenderJob]) {
        let explanation = PageKit.label(
            "Untested. A Mac you allow here is asked to render final videos without a qualification record. "
                + "If a render on it fails, that one episode waits for recovery - it is not rendered on this Mac "
                + "instead. Macs on this Mac's own network are listed first.",
            .secondary, colour: .secondaryLabelColor, wrapping: true
        )
        explanation.preferredMaxLayoutWidth = 540

        let rows = NSStackView()
        rows.orientation = .vertical
        rows.alignment = .leading
        rows.spacing = DS.Space.m
        if workers.isEmpty {
            rows.addArrangedSubview(PageKit.label(
                "No Mac is publishing final rendering. A Mac appears here once its Studio Hub is online.",
                .body, colour: .secondaryLabelColor, wrapping: true))
        }
        var made: [NSButton] = []
        for (index, worker) in workers.enumerated() {
            let box = NSButton(checkboxWithTitle: "Allow remote rendering (untested) - " + worker.name,
                               target: nil, action: nil)
            box.state = worker.optedIn ? .on : .off
            box.isEnabled = !worker.alreadyQualified
            box.tag = index
            box.setAccessibilityLabel("Allow remote rendering on \(worker.name)")
            box.lineBreakMode = .byTruncatingTail
            made.append(box)
            let prefix = worker.suggested ? "Suggested: same network. " : ""
            let detail = PageKit.label(prefix + worker.detailLine, .secondary,
                                       colour: .secondaryLabelColor, wrapping: true)
            detail.preferredMaxLayoutWidth = 520
            var views: [NSView] = [box, detail]
            if let note = worker.note {
                let line = PageKit.label(note, .caption, colour: .secondaryLabelColor, wrapping: true)
                line.preferredMaxLayoutWidth = 520
                views.append(line)
            }
            let row = NSStackView(views: views)
            row.orientation = .vertical
            row.alignment = .leading
            row.spacing = DS.Space.xs
            rows.addArrangedSubview(row)
        }

        if !jobs.isEmpty {
            rows.addArrangedSubview(PageKit.label("Recent renders", .rowTitle))
            let unqualified = jobs.contains { $0.unqualified }
            let text = PageKit.label(
                jobs.prefix(5).map { $0.line }.joined(separator: "\n")
                    + (unqualified ? "\n\"unqualified\" = sent to a Mac you allowed above." : ""),
                .caption, colour: .secondaryLabelColor, wrapping: true)
            text.preferredMaxLayoutWidth = 540
            rows.addArrangedSubview(text)
        }

        rows.translatesAutoresizingMaskIntoConstraints = false
        let document = FleetRenderDocument(frame: NSRect(x: 0, y: 0, width: 540, height: 10))
        document.addSubview(rows)
        NSLayoutConstraint.activate([
            rows.leadingAnchor.constraint(equalTo: document.leadingAnchor, constant: DS.Space.s),
            rows.topAnchor.constraint(equalTo: document.topAnchor, constant: DS.Space.s),
            rows.widthAnchor.constraint(equalToConstant: 540 - 2 * DS.Space.s),
        ])
        document.layoutSubtreeIfNeeded()
        document.frame = NSRect(x: 0, y: 0, width: 540, height: rows.fittingSize.height + 2 * DS.Space.s)

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 560, height: 280))
        scroll.documentView = document
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.drawsBackground = false

        let stack = NSStackView(views: [explanation, scroll])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.m
        stack.widthAnchor.constraint(equalToConstant: 560).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 560).isActive = true
        scroll.heightAnchor.constraint(equalToConstant: 280).isActive = true
        stack.layoutSubtreeIfNeeded()
        stack.frame = NSRect(origin: .zero, size: stack.fittingSize)

        self.workers = workers
        boxes = made
        view = stack
        super.init()
    }

    /// The Macs whose switch the owner moved, with the value to send. A qualified Mac is never in it.
    var changes: [(worker: FleetRenderWorker, enabled: Bool)] {
        var moved: [(worker: FleetRenderWorker, enabled: Bool)] = []
        for (worker, box) in zip(workers, boxes) {
            let wanted = box.state == .on
            if !worker.alreadyQualified && wanted != worker.optedIn {
                moved.append((worker: worker, enabled: wanted))
            }
        }
        return moved
    }
}

enum FleetRemoteRender {
    private static func switchPath(_ worker: FleetRenderWorker) -> String? {
        guard let place = worker.controllerId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let node = worker.machineId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
        else { return nil }
        return "/admin/api/fleet/controllers/\(place)/machines/\(node)/render-optin"
    }

    /// Open the sheet: read the page's data, show it, send what changed.
    static func show(done: @escaping () -> Void) {
        GatewayAPI.get("/admin/api/fleet/remote-rendering", timeout: 30) { result in
            DispatchQueue.main.async {
                switch result {
                case .failure(let error):
                    FleetStartup.show("Remote final rendering unavailable", error.localizedDescription)
                case .success(let payload):
                    let workers = (payload["workers"] as? [[String: Any]] ?? []).compactMap(FleetRenderWorker.init)
                    let jobs = (payload["recent_jobs"] as? [[String: Any]] ?? []).compactMap(FleetRenderJob.init)
                    present(workers: workers, jobs: jobs, done: done)
                }
            }
        }
    }

    private static func present(workers: [FleetRenderWorker], jobs: [FleetRenderJob],
                                done: @escaping () -> Void) {
        let form = FleetRemoteRenderForm(workers: workers, jobs: jobs)
        let alert = NSAlert()
        alert.messageText = "Remote final rendering"
        alert.informativeText = "Choose which Macs may render final videos for Story Studio."
        alert.accessoryView = form.view
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Close")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        apply(form.changes.map { ($0.worker, $0.enabled) }, at: 0, problems: [], done: done)
    }

    /// One Mac at a time, so a refusal is named against the Mac it belongs to.
    private static func apply(_ changes: [(FleetRenderWorker, Bool)], at index: Int, problems: [String],
                              done: @escaping () -> Void) {
        guard index < changes.count else {
            if !problems.isEmpty {
                FleetStartup.show("Some changes were not saved", problems.joined(separator: "\n"))
            }
            done()
            return
        }
        let (worker, enabled) = changes[index]
        guard let path = switchPath(worker) else {
            apply(changes, at: index + 1, problems: problems + ["\(worker.name): this row has no machine id"],
                  done: done)
            return
        }
        GatewayAPI.post(path, body: ["enabled": enabled], timeout: 60) { result in
            DispatchQueue.main.async {
                var next = problems
                switch result {
                case .failure(let error):
                    next.append("\(worker.name): \(error.localizedDescription)")
                case .success(let payload):
                    // Switching off always takes effect on the Gateway; only the Mac's own copy may lag.
                    if !enabled, payload["relayed"] as? Bool == false, let detail = payload["detail"] as? String {
                        next.append("\(worker.name): \(detail)")
                    }
                }
                apply(changes, at: index + 1, problems: next, done: done)
            }
        }
    }
}
