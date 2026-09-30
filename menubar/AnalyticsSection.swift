import AppKit

/// Analytics — what this gateway has generated, grouped so the next job is an informed choice.
///
/// **Why this page exists.** The Jobs page answers "what happened to that one job". Nothing
/// answered "which model is actually fast here", "is the whole fleet working or is one Mac doing
/// everything", or "how much have I generated" — the questions the owner asks when deciding where
/// to send the next batch. Every number here already existed in the job record; none of it was
/// readable.
///
/// **Two decisions worth keeping, because both were made against the obvious choice.**
///
/// 1. **No timer, though Jobs has one.** Jobs must poll: it shows a *running* job, whose state
///    changes second to second. Nothing on this page moves faster than a completed generation, and
///    the aggregate groups individual durations rather than taking a cheap SQL summary, so a timer
///    would spend a full scan every few seconds to redraw numbers that change once per render.
///    `refreshesOnReturn` is the useful half and is on: the page is read-only, and coming back to
///    it is exactly when a stale figure misleads.
///
/// 2. **The median is shown beside the mean, not instead of it.** A lane's first item after a
///    model load is far slower than the ones behind it, so a mean over a handful of image jobs
///    describes the cold load rather than the machine. The median answers "what should I expect";
///    the mean is kept beside it so the gap between them is visible rather than hidden.
///
/// **The machine column is the Hub's own answer, not a guess.** `"local"` means the controller ran
/// the job on itself; a node id means a joined Mac did. They are shown with the provider beside
/// them, so a controller's own work is never read as a joined node's — which is the exact
/// confusion this page was asked to clear up.
final class AnalyticsSection: NSObject, AppSection {
    static let shared = AnalyticsSection()

    let title = "Analytics"
    let symbol = "chart.bar.xaxis"

    /// See the class comment: a full aggregate on a timer buys nothing here.
    let refreshesAutomatically = false
    /// Read-only page, so re-reading on return is safe and is when staleness misleads.
    let refreshesOnReturn = true

    private let body = NSStackView()
    private let status = NSTextField(labelWithString: "Reading the job record…")
    private var loading = false
    /// Ids the job record stores, and the names Jobs and Fleet show for them.
    private var modelNames: [String: String] = [:]
    private var providerNames: [String: String] = [:]

    lazy var view: NSView = buildPage()

    func activated() {
        refresh()
    }

    // MARK: - Page

    private func buildPage() -> NSView {
        let page = PageKit.page()

        page.addArrangedSubview(PageKit.heading(
            "Analytics",
            "What this gateway has generated — by type, by model, and by the machine that ran it."
        ))

        let refreshButton = NSButton(
            title: "Refresh", target: self, action: #selector(refreshPressed)
        )
        refreshButton.bezelStyle = .rounded
        refreshButton.controlSize = .small

        status.font = .systemFont(ofSize: 11.5)
        status.textColor = .secondaryLabelColor

        let controls = PageKit.row([status, NSView(), refreshButton])
        controls.setContentHuggingPriority(.defaultLow, for: .horizontal)
        page.addArrangedSubview(controls)

        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = PageKit.blockSpacing
        page.addArrangedSubview(body)

        // The same reason Overview and Fleet scroll: content with a required height pinned
        // straight to the host propagates outwards and grows the window.
        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(page)

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = document

        NSLayoutConstraint.activate([
            page.topAnchor.constraint(equalTo: document.topAnchor),
            page.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            page.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            page.bottomAnchor.constraint(equalTo: document.bottomAnchor),

            document.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            document.trailingAnchor.constraint(equalTo: scroll.contentView.trailingAnchor),
            document.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
        ])

        // `controls` spans the page width so the Refresh button sits on the right edge.
        controls.widthAnchor.constraint(equalTo: page.widthAnchor, constant: -2 * PageKit.pagePadding)
            .isActive = true

        return scroll
    }

    @objc private func refreshPressed() {
        refresh()
    }

    // MARK: - Data

    func refresh() {
        guard !loading else { return }
        loading = true
        status.stringValue = "Reading the job record…"
        GatewayAPI.get("/admin/api/analytics") { [weak self] result in
            guard let self else { return }
            self.loading = false
            switch result {
            case .success(let payload):
                self.loadNames { self.render(payload) }
            case .failure(let error):
                // Name the failure rather than emptying the page: a blank Analytics page
                // reads as "nothing has ever been generated", which is a different and
                // much worse claim than "the read failed".
                self.status.stringValue = "Could not read analytics: \(error.localizedDescription)"
                self.body.setViews([], in: .top)
            }
        }
    }

    /// The job record keys rows by raw model and provider ids, which no other page shows.
    /// Best effort: a failed read leaves the ids, which are still true.
    private func loadNames(then done: @escaping () -> Void) {
        func names(_ payload: [String: Any], _ list: String, _ id: String) -> [String: String] {
            Dictionary(
                (payload[list] as? [[String: Any]] ?? []).compactMap { row in
                    guard let key = row[id] as? String, let name = row["display_name"] as? String else { return nil }
                    return (key, name)
                },
                uniquingKeysWith: { first, _ in first }
            )
        }
        GatewayAPI.get("/admin/api/providers") { [weak self] result in
            if case .success(let payload) = result { self?.providerNames = names(payload, "providers", "id") }
            GatewayAPI.get("/admin/api/models?limit=2000") { [weak self] result in
                if case .success(let payload) = result { self?.modelNames = names(payload, "models", "model_id") }
                done()
            }
        }
    }

    private func render(_ payload: [String: Any]) {
        let totals = payload["totals"] as? [String: Any] ?? [:]
        let sampled = AnalyticsSection.count(totals["jobs"])

        guard sampled > 0 else {
            status.stringValue = ""
            body.setViews([
                PageKit.empty(
                    "Nothing generated yet",
                    "Once jobs finish, their timing, output size and the machine that ran them "
                        + "appear here.",
                    symbol: "chart.bar.xaxis"
                )
            ], in: .top)
            return
        }

        let timed = AnalyticsSection.count(totals["timed_jobs"])
        status.stringValue = timed < sampled
            ? "\(sampled) settled jobs · \(timed) stated a generation time"
            : "\(sampled) settled jobs · all stated a generation time"

        var blocks: [NSView] = []
        blocks.append(summary(totals))

        blocks.append(contentsOf: table(
            title: "By generation type",
            firstHeader: "Type",
            firstWidth: 150,
            rows: rows(from: payload["by_modality"]) { $0 }
        ))
        blocks.append(contentsOf: table(
            title: "By model",
            firstHeader: "Model",
            firstWidth: 300,
            rows: rows(from: payload["by_model"]) { self.modelNames[$0] ?? $0 }
        ))
        blocks.append(contentsOf: table(
            title: "By machine",
            firstHeader: "Machine",
            firstWidth: 300,
            rows: rows(from: payload["by_machine"]) { key in
                // "<machine> @ <provider id>"; the machine is already a name.
                let parts = key.components(separatedBy: " @ ")
                guard parts.count == 2, let name = self.providerNames[parts[1]] else { return key }
                return "\(parts[0]) @ \(name)"
            }
        ))

        if timed < sampled {
            blocks.append(PageKit.help(
                "A dash means that job finished before the Hub started reporting a runtime — "
                    + "those are excluded from the averages rather than counted as zero."
            ))
        }

        body.setViews(blocks, in: .top)
    }

    // MARK: - Blocks

    private func summary(_ totals: [String: Any]) -> NSView {
        let succeeded = AnalyticsSection.count(totals["succeeded"])
        let failed = AnalyticsSection.count(totals["failed"])
        let settled = max(1, succeeded + failed + AnalyticsSection.count(totals["cancelled"]))

        var tiles: [NSView] = [
            tile("Jobs", "\(AnalyticsSection.count(totals["jobs"]))"),
            tile("Succeeded", "\(succeeded)"),
            tile("Failed", "\(failed)"),
            tile("Success rate", String(format: "%.0f%%", 100 * Double(succeeded) / Double(settled))),
            tile("Median", AnalyticsSection.seconds(totals["seconds_median"])),
            tile("Mean", AnalyticsSection.seconds(totals["seconds_mean"])),
            tile("Output", AnalyticsSection.bytes(totals["bytes"])),
        ]
        if AnalyticsSection.count(totals["bytes_jobs"]) == 0 {
            tiles[6] = tile("Output", "—")
        }

        let row = PageKit.row(tiles, spacing: 18)
        row.distribution = .fillEqually
        return row
    }

    private func tile(_ caption: String, _ value: String) -> NSView {
        let valueLabel = PageKit.label(value, size: 19, weight: .semibold)
        let stack = NSStackView(views: [
            PageKit.label(caption.uppercased(), size: 10, weight: .medium,
                          colour: .secondaryLabelColor),
            valueLabel,
        ])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 3
        return stack
    }

    /// Each row is its cells plus the raw id, which the first cell's tooltip carries.
    private func rows(from any: Any?, label: (String) -> String) -> [(cells: [String], id: String)] {
        (any as? [[String: Any]] ?? []).map { row in
            let id = (row["key"] as? String) ?? "unknown"
            let first = (row["label"] as? String) ?? label(id)
            let jobs = AnalyticsSection.count(row["jobs"])
            let ok = AnalyticsSection.count(row["succeeded"])
            let failed = AnalyticsSection.count(row["failed"])
            let timed = AnalyticsSection.count(row["timed_jobs"])
            return ([
                first,
                "\(jobs)",
                "\(ok)",
                "\(failed)",
                timed == 0 ? "—" : AnalyticsSection.seconds(row["seconds_median"]),
                timed == 0 ? "—" : AnalyticsSection.seconds(row["seconds_mean"]),
                AnalyticsSection.count(row["bytes_jobs"]) == 0
                    ? "—" : AnalyticsSection.bytes(row["bytes"]),
            ], id)
        }
    }

    private func table(
        title: String, firstHeader: String, firstWidth: CGFloat, rows: [(cells: [String], id: String)]
    ) -> [NSView] {
        var views: [NSView] = [PageKit.sectionTitle(title)]
        guard !rows.isEmpty else {
            views.append(PageKit.help("Nothing recorded in this group yet."))
            return views
        }

        let widths: [CGFloat] = [firstWidth, 62, 62, 62, 92, 92, 104]
        let headers = [firstHeader, "Jobs", "OK", "Failed", "Median", "Mean", "Output"]
        views.append(tableRow(headers, widths: widths, header: true))
        for row in rows {
            views.append(tableRow(row.cells, widths: widths, header: false, tip: row.id))
        }
        return views
    }

    private func tableRow(_ cells: [String], widths: [CGFloat], header: Bool, tip: String? = nil) -> NSView {
        let views: [NSView] = cells.enumerated().map { index, text in
            let field = PageKit.label(
                text,
                size: header ? 10.5 : 12,
                weight: header ? .semibold : .regular,
                colour: header ? .secondaryLabelColor : .labelColor
            )
            field.alignment = index == 0 ? .left : .right
            // Tail, not middle: the middle is where a long id differs.
            field.lineBreakMode = .byTruncatingTail
            if index == 0 { field.toolTip = tip }
            field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            field.widthAnchor.constraint(equalToConstant: widths[index]).isActive = true
            return field
        }
        let row = PageKit.row(views, spacing: 10)
        row.widthAnchor.constraint(
            equalToConstant: widths.reduce(0, +) + 10 * CGFloat(max(0, widths.count - 1))
        ).isActive = true
        return row
    }

    // MARK: - Formatting

    /// JSON `true`/`false` arrive as `NSNumber` too, so a bare `as? NSNumber` reads a
    /// boolean as 1. A count is only a count when it is not a boolean.
    private static func count(_ any: Any?) -> Int {
        guard let value = any as? NSNumber, !(any is Bool) else { return 0 }
        return value.intValue
    }

    /// `nil` and `0` are different answers and must not render alike: nothing here
    /// prints "0.0 s" for a job that stated no time.
    private static func seconds(_ any: Any?) -> String {
        guard let value = any as? NSNumber, !(any is Bool) else { return "—" }
        let total = value.doubleValue
        if total < 1 { return String(format: "%.2f s", total) }
        if total < 60 { return String(format: "%.1f s", total) }
        let minutes = Int(total) / 60
        let rest = Int(total) % 60
        return "\(minutes)m \(rest)s"
    }

    private static func bytes(_ any: Any?) -> String {
        guard let value = any as? NSNumber, !(any is Bool) else { return "—" }
        let total = value.doubleValue
        if total <= 0 { return "—" }
        let units = ["B", "KB", "MB", "GB", "TB"]
        var size = total
        var unit = 0
        while size >= 1024, unit < units.count - 1 {
            size /= 1024
            unit += 1
        }
        return unit == 0
            ? "\(Int(size)) \(units[unit])"
            : String(format: "%.1f %@", size, units[unit])
    }
}
