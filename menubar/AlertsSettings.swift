import AppKit

/// The Alerts card on the Settings page: what the notifier has open now, and what it recently
/// resolved.
///
/// Read from `/admin/api/alerts`, which serves the notifier's own state file — the same incidents it
/// messages the owner about, not a second opinion derived from the fleet. Telegram is where a
/// message goes; this is where it can be read without a phone, and where it still is after the
/// message has scrolled away.
final class AlertsSettings: NSObject {
    private let summary = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    private let currentStack = NSStackView()
    private let recentTitle = PageKit.label("Recently cleared", .rowTitle)
    private let recentStack = NSStackView()

    lazy var rows: [NSView] = build()

    private func build() -> [NSView] {
        for stack in [currentStack, recentStack] {
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = DS.Space.s
        }
        currentStack.addArrangedSubview(PageKit.loading("Reading the alerts…"))
        recentTitle.isHidden = true
        recentStack.isHidden = true
        return [
            PageKit.help(
                "What the Gateway has noticed and told your phone about: a Mac behind the published "
                + "Studio Hub, a site with no Mac free for a model, a Story update that stopped, a "
                + "lane with work waiting and nothing running, and the Gateway itself going quiet. "
                + "This list works whether or not Telegram is set up — that is only where the "
                + "messages are sent."
            ),
            summary,
            currentStack,
            recentTitle,
            recentStack,
        ]
    }

    /// Read the list. The Settings page is not a live one, so this runs when it is opened.
    func load() {
        GatewayAPI.get("/admin/api/alerts") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.summary.stringValue = "Could not read the alerts — \(error.localizedDescription)"
                self.summary.textColor = Health.bad.text
            case .success(let payload):
                self.apply(payload)
            }
        }
    }

    private func apply(_ payload: [String: Any]) {
        let current = (payload["current"] as? [[String: Any]]) ?? []
        let recent = (payload["recent"] as? [[String: Any]]) ?? []
        let configured = payload["configured"] as? Bool ?? false
        if current.isEmpty {
            summary.stringValue = configured
                ? "Nothing needs attention right now."
                : "Nothing needs attention right now. Telegram is not set up, so nothing would be sent to your phone."
            summary.textColor = .secondaryLabelColor
        } else {
            summary.stringValue = "\(current.count) thing\(current.count == 1 ? "" : "s") "
                + "need\(current.count == 1 ? "s" : "") attention."
            summary.textColor = Health.warn.text
        }
        fill(currentStack, current, cleared: false)
        fill(recentStack, Array(recent.prefix(8)), cleared: true)
        recentTitle.isHidden = recent.isEmpty
        recentStack.isHidden = recent.isEmpty
    }

    private func fill(_ stack: NSStackView, _ rows: [[String: Any]], cleared: Bool) {
        stack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        for row in rows {
            let text = row["text"] as? String ?? ""
            let line = cleared
                ? "\(text) — cleared \(GatewayAPI.relativeTime(row["resolved_at"] as? Double))"
                : "\(row["icon"] as? String ?? "•") \(text) — open for \(Self.age(row["at"] as? Double))"
            let label = PageKit.label(
                line, .secondary,
                colour: cleared ? .secondaryLabelColor : .labelColor, wrapping: true)
            label.maximumNumberOfLines = 3
            stack.addArrangedSubview(label)
        }
    }

    /// "3h" / "12m": how long an alert has been open. The trailing "ago" of a relative time reads
    /// wrong beside "open for", so this is the same measurement without it.
    private static func age(_ epoch: Double?) -> String {
        guard let epoch, epoch > 0 else { return "unknown" }
        let seconds = Date().timeIntervalSince1970 - epoch
        if seconds < 3600 { return "\(Int(max(seconds, 0) / 60))m" }
        if seconds < 86_400 { return "\(Int(seconds / 3600))h" }
        return "\(Int(seconds / 86_400))d"
    }
}
