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
///
/// **The page is a shell around five inner tabs**, because the owner said the
/// flat page was "a bit flat" and asked for "different tabs inside the fleet
/// tab": Overview, Settings / Locations, Machines, Local Models and
/// Jobs / Generations. The tab bar, the confirmation banner and the
/// location/search filters are the only parts that belong to every tab; each tab
/// builds its own body (see `FleetTabs.swift`).
final class FleetSection: NSObject, AppSection, NSTableViewDataSource, NSTableViewDelegate,
    NSSearchFieldDelegate {
    static let shared = FleetSection()

    let title = "Fleet"
    let symbol = "network"
    /// The gateway polls every controller for this page, so it is refreshed on
    /// demand rather than on the host's timer.
    let refreshesAutomatically = false

    // MARK: State

    private var snapshot: FleetSnapshot?
    private var locations: [FleetLocation] = []
    /// The selected inner tab. Held here, not in the segmented control alone, so a
    /// reload — which rebuilds none of the tabs — never drops the owner back to
    /// Overview.
    private var selectedTab: FleetTab = .overview
    private var filter = FleetFilter()
    private var failedRefreshProviderIDs: [String] = []
    private var adding = false

    // MARK: Shell furniture

    private let tabControl = NSSegmentedControl(
        labels: FleetTab.allCases.map { $0.shortTitle },
        trackingMode: .selectOne, target: nil, action: nil
    )
    private let banner = FleetBanner(frame: .zero)
    private let locationPopup = NSPopUpButton()
    private let searchField = NSSearchField()
    private let content = NSView()

    // MARK: Tabs

    private lazy var overviewTab = FleetOverviewTab(frame: .zero)
    private lazy var locationsTab = FleetLocationsTab(frame: .zero)
    private lazy var machinesTab = FleetMachinesTab(frame: .zero)
    private lazy var modelsTab = FleetModelsTab(frame: .zero)
    private lazy var jobsTab = FleetJobsTab(frame: .zero)

    lazy var view: NSView = buildContent()

    func activated() {
        reload()
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let container = NSView()

        let top = NSStackView()
        top.orientation = .vertical
        top.alignment = .leading
        top.spacing = 10
        top.edgeInsets = NSEdgeInsets(
            top: PageKit.pagePadding, left: PageKit.pagePadding,
            bottom: 6, right: PageKit.pagePadding
        )
        top.translatesAutoresizingMaskIntoConstraints = false

        top.addArrangedSubview(PageKit.heading(
            "Fleet",
            "Every location this gateway can send work to, and what each one is doing now."
        ))

        tabControl.segmentStyle = .rounded
        tabControl.controlSize = .small
        tabControl.target = self
        tabControl.action = #selector(tabChanged)
        tabControl.selectedSegment = selectedTab.rawValue
        top.addArrangedSubview(tabControl)

        // The confirmation banner sits directly under the tab bar, where an action
        // just taken is impossible to miss. The old page reported everything into
        // one small line at the very bottom, which is why adding a controller "kinda
        // shows but there's no visual confirmation".
        top.addArrangedSubview(banner)
        pinWidth(banner, to: top)

        top.addArrangedSubview(filterRow())
        pinWidth(filterRowHolder, to: top)

        container.addSubview(top)

        for tab in FleetTab.allCases {
            let child = view(for: tab)
            child.translatesAutoresizingMaskIntoConstraints = false
            child.isHidden = tab != selectedTab
            content.addSubview(child)
            NSLayoutConstraint.activate([
                child.topAnchor.constraint(equalTo: content.topAnchor),
                child.leadingAnchor.constraint(equalTo: content.leadingAnchor),
                child.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                child.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            ])
        }
        content.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(content)

        NSLayoutConstraint.activate([
            top.topAnchor.constraint(equalTo: container.topAnchor),
            top.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            top.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            content.topAnchor.constraint(equalTo: top.bottomAnchor),
            content.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            content.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            content.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])

        wireTabs()
        return container
    }

    /// The row is kept so the shell can constrain its width after the stack has
    /// been laid out; `filterRow()` builds and stores it.
    private var filterRowHolder = NSView()

    private func filterRow() -> NSView {
        locationPopup.target = self
        locationPopup.action = #selector(locationFilterChanged)
        locationPopup.controlSize = .small
        locationPopup.addItem(withTitle: "All locations")
        locationPopup.widthAnchor.constraint(equalToConstant: 200).isActive = true

        searchField.placeholderString = "Filter machines, models, jobs…"
        searchField.delegate = self
        searchField.controlSize = .small
        searchField.widthAnchor.constraint(equalToConstant: 240).isActive = true

        let row = PageKit.row([
            PageKit.label("Show", size: 11.5, colour: .secondaryLabelColor),
            locationPopup,
            searchField,
            NSView(),
        ], spacing: 8)
        filterRowHolder = row
        return row
    }

    private func pinWidth(_ view: NSView, to host: NSView) {
        view.widthAnchor.constraint(
            equalTo: host.widthAnchor, constant: -PageKit.pagePadding * 2
        ).isActive = true
    }

    private func view(for tab: FleetTab) -> NSView {
        switch tab {
        case .overview: return overviewTab
        case .locations: return locationsTab
        case .machines: return machinesTab
        case .models: return modelsTab
        case .jobs: return jobsTab
        }
    }

    /// Point every tab's buttons at the shell. The tabs are views; the network,
    /// the alerts and the banner all belong here.
    private func wireTabs() {
        overviewTab.onOpenTab = { [weak self] tab in self?.select(tab) }
        overviewTab.onRefreshLocation = { [weak self] providerID in
            self?.refreshProviders([providerID])
        }

        locationsTab.onRefresh = { [weak self] in self?.refreshEverything() }
        locationsTab.onRetryFailed = { [weak self] in self?.retryFailedRefresh() }
        locationsTab.onAddLocation = { [weak self] label, url, token in
            self?.addLocation(label: label, url: url, token: token)
        }
        locationsTab.onTest = { [weak self] location in self?.testController(location) }
        locationsTab.onRename = { [weak self] location in self?.renameController(location) }
        locationsTab.onCredentials = { [weak self] location in self?.editCredentials(location) }
        locationsTab.onRemove = { [weak self] location in self?.removeController(location) }

        jobsTab.onRefresh = { [weak self] in self?.refreshEverything() }
        jobsTab.onCancel = { [weak self] job in self?.cancelJob(job) }
        jobsTab.onRetry = { [weak self] job, item in self?.retryJobItem(job, item: item) }
    }

    // MARK: Tabs

    @objc private func tabChanged() {
        select(FleetTab(rawValue: tabControl.selectedSegment) ?? .overview)
    }

    private func select(_ tab: FleetTab) {
        selectedTab = tab
        tabControl.selectedSegment = tab.rawValue
        for candidate in FleetTab.allCases {
            view(for: candidate).isHidden = candidate != tab
        }
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
        let providerIds = uniqueProviderIDs()
        guard !providerIds.isEmpty else {
            failedRefreshProviderIDs = []
            locationsTab.setRetryFailedVisible(false)
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
                self.render(error: "Could not read the fleet — \(error)")
            case .success(let payload):
                self.render(payload)
            }
            completion?()
        }
    }

    private func uniqueProviderIDs() -> [String] {
        var seen = Set<String>()
        return locations.compactMap { $0.providerId }
            .filter { !$0.isEmpty && seen.insert($0).inserted }
    }

    @objc private func retryFailedRefresh() {
        let providerIDs = failedRefreshProviderIDs
        guard !providerIDs.isEmpty else { return }
        refreshProviders(providerIDs)
    }

    private func refreshProviders(_ providerIDs: [String]) {
        let providerIDs = providerIDs.filter { !$0.isEmpty }
        guard !providerIDs.isEmpty else { return }
        locationsTab.setRetryFailedVisible(false)
        setStatus("Refreshing \(providerIDs.count) location\(providerIDs.count == 1 ? "" : "s")…")
        banner.show(.idle, title: "", detail: "")
        banner.clear()

        let labels = Dictionary(
            uniqueKeysWithValues: locations.compactMap { row -> (String, String)? in
                guard let providerID = row.providerId, !providerID.isEmpty else { return nil }
                return (providerID, row.label ?? providerID)
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
        locationsTab.setRetryFailedVisible(!failures.isEmpty)
        let succeeded = total - failures.count
        guard !failures.isEmpty else {
            setStatus("Refreshed \(total) location\(total == 1 ? "" : "s").")
            report(
                .success,
                title: "Refreshed \(total) location\(total == 1 ? "" : "s")",
                detail: "Reachability and models were re-read. Discovered models arrive "
                    + "disabled — enable the ones you want on the Models page."
            )
            return
        }
        let details = failures.map {
            "\(labels[$0.providerID] ?? $0.providerID): \($0.detail)"
        }.joined(separator: "; ")
        setStatus(
            "Refreshed \(succeeded); could not discover at \(failures.count) "
            + "location\(failures.count == 1 ? "" : "s") — \(details)",
            colour: .systemOrange
        )
        report(
            .warning,
            title: "Refreshed \(succeeded) of \(total) location\(total == 1 ? "" : "s")",
            detail: details
        )
    }

    private func render(_ payload: [String: Any]) {
        guard let snapshot = FleetSnapshot.from(payload) else {
            // A 200 with a shape this build cannot read: say so rather than drawing
            // an empty fleet, because "the answer is nothing" and "I could not read
            // the answer" must not look the same.
            render(error: "The gateway's fleet answer could not be read.")
            return
        }
        self.snapshot = snapshot
        self.locations = snapshot.locations

        var providerIDs: [String: String] = [:]
        for location in snapshot.locations {
            if let id = location.id, let provider = location.providerId {
                providerIDs[id] = provider
            }
        }
        overviewTab.setProviderIDs(providerIDs)
        overviewTab.update(snapshot: snapshot, filter: filter)
        locationsTab.update(locations: snapshot.locations)
        rebuildLocationPopup()
        applyFilter()
    }

    private func render(error: String) {
        let empty = FleetSnapshot()
        snapshot = nil
        locations = []
        overviewTab.update(snapshot: empty, filter: filter)
        overviewTab.setStatus(error, colour: .systemOrange)
        locationsTab.update(locations: [])
        machinesTab.update(machines: [], studios: [:], filter: filter)
        modelsTab.update(models: [], filter: filter)
        jobsTab.update(snapshot: empty, filter: filter)
        // A location filter that outlived its locations would hide the next read's
        // rows for a reason nothing on the page explains.
        rebuildLocationPopup()
    }

    private func rebuildLocationPopup() {
        let previous = locationPopup.selectedItem?.representedObject as? String
        locationPopup.removeAllItems()
        locationPopup.addItem(withTitle: "All locations")
        locationPopup.lastItem?.representedObject = nil
        for location in locations {
            locationPopup.addItem(withTitle: location.label ?? location.id ?? "Location")
            locationPopup.lastItem?.representedObject = location.id
        }
        if let previous,
           let index = locationPopup.itemArray.firstIndex(where: {
               ($0.representedObject as? String) == previous
           }) {
            locationPopup.selectItem(at: index)
        } else {
            locationPopup.selectItem(at: 0)
        }
        filter.locationID = locationPopup.selectedItem?.representedObject as? String
    }

    private func applyFilter() {
        guard let snapshot else { return }
        overviewTab.update(snapshot: snapshot, filter: filter)
        machinesTab.update(
            machines: snapshot.machines, studios: snapshot.studiosByMachine, filter: filter)
        modelsTab.update(models: snapshot.allLocalModels, filter: filter)
        jobsTab.update(snapshot: snapshot, filter: filter)
    }

    private func setStatus(_ text: String, colour: NSColor = .secondaryLabelColor) {
        overviewTab.setStatus(text, colour: colour)
        locationsTab.setStatus(text, colour: colour)
    }

    // MARK: Filters

    @objc private func locationFilterChanged() {
        filter.locationID = locationPopup.selectedItem?.representedObject as? String
        applyFilter()
    }

    func controlTextDidChange(_ notification: Notification) {
        filter.query = searchField.stringValue
        applyFilter()
    }

    /// Ask a location's Hub to stop a running batch.
    ///
    /// **The Hub owns the work, so this reports a request, not a result.** Its
    /// answer counts what was still queued and got cancelled, what worker was
    /// signalled, and what is still pending — and the banner says all three,
    /// because "a worker was told" and "it stopped" are different facts. Nothing
    /// is marked cancelled here; the reload that follows shows what the Hub did.
    private func cancelJob(_ job: FleetJob) {
        guard let batchId = job.batchId, let locationId = job.locationId else { return }
        let encodedLocation = locationId.addingPercentEncoding(withAllowedCharacters: .alphanumerics)
            ?? locationId
        let encodedBatch = batchId.addingPercentEncoding(withAllowedCharacters: .alphanumerics)
            ?? batchId
        let what = "\(FleetFormat.words(job.modality)) · \(job.model ?? job.label ?? batchId)"
        setStatus("Asking the Hub to stop \(what)…")
        GatewayAPI.post(
            "/admin/api/fleet/controllers/\(encodedLocation)/jobs/\(encodedBatch)/cancel",
            body: ["modality": job.modality ?? ""]
        ) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report(
                    .error,
                    title: "Could not ask the Hub to stop it",
                    detail: "\(what) — \(error.localizedDescription)"
                )
            case .success(let payload):
                // **The adapter wraps the Hub's answer.** `result` is
                // `{ok, status, route, answer}` — the Hub's own body is nested one
                // level down. Reading the counts off `result` yielded `nil` for all
                // three, so a real cancellation reported "0 queued cancelled, 0
                // signalled, 0 pending" while the Hub had done exactly what was
                // asked. The counts come from `answer`.
                let result = payload["result"] as? [String: Any] ?? [:]
                let hub = result["answer"] as? [String: Any] ?? [:]
                if result["ok"] as? Bool == false {
                    let why = (result["error"] as? String)
                        ?? (hub["detail"] as? String)
                        ?? "the Hub did not accept the request"
                    self.report(
                        .warning,
                        title: "Could not stop it",
                        detail: "\(what) — \(why)"
                    )
                } else {
                    let queued = (hub["queued_cancelled"] as? Int) ?? 0
                    let signalled = (hub["running_signalled"] as? Int) ?? 0
                    let pending = (hub["running_pending"] as? Int) ?? 0
                    self.report(
                        .success,
                        title: "Asked the Hub to stop it",
                        detail: "\(what) — \(queued) queued cancelled, \(signalled) worker(s) "
                            + "signalled, \(pending) still pending. A worker that already took the "
                            + "work cannot be promised to stop."
                    )
                }
            }
            self.reload()
        }
    }

    /// Ask a location's Hub to re-queue one failed item of a batch.
    ///
    /// **The Hub is the authority on whether it may be retried.** Only an item in
    /// state `error` is accepted; a cancelled batch and an `uncertain` item are
    /// refused with their own sentence, and the refusal is shown as the Hub wrote
    /// it — an item that must be discarded first is a different situation from a
    /// retry that failed.
    private func retryJobItem(_ job: FleetJob, item: FleetJobError) {
        guard let batchId = job.batchId, let locationId = job.locationId,
              let index = item.itemIndex else { return }
        let encodedLocation = locationId.addingPercentEncoding(withAllowedCharacters: .alphanumerics)
            ?? locationId
        let encodedBatch = batchId.addingPercentEncoding(withAllowedCharacters: .alphanumerics)
            ?? batchId
        let what = "\(FleetFormat.words(job.modality)) · \(job.model ?? job.label ?? batchId) "
            + "item \(index)"
        setStatus("Asking the Hub to retry \(what)…")
        GatewayAPI.post(
            "/admin/api/fleet/controllers/\(encodedLocation)/jobs/\(encodedBatch)"
            + "/items/\(index)/retry",
            body: ["modality": job.modality ?? ""]
        ) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report(.error, title: "Could not ask the Hub to retry it",
                            detail: "\(what) — \(error.localizedDescription)")
            case .success(let payload):
                let outcome = payload["result"] as? [String: Any] ?? [:]
                let hub = outcome["answer"] as? [String: Any] ?? [:]
                if outcome["ok"] as? Bool == false {
                    let why = (outcome["error"] as? String)
                        ?? (hub["detail"] as? String)
                        ?? (hub["message"] as? String)
                        ?? "the Hub refused the retry"
                    self.report(.warning, title: "The Hub refused the retry",
                                detail: "\(what) — \(why)")
                } else {
                    let state = (hub["state"] as? String) ?? "queued"
                    self.report(.success, title: "Asked the Hub to retry it",
                                detail: "\(what) is \(state) again.")
                }
            }
            self.reload()
        }
    }

    // MARK: Banner

    /// Show one action's outcome, in the banner and in the Locations tab's own
    /// status line. Every action on this page reports through here; the banner is
    /// the visible confirmation the owner asked for.
    private func report(_ kind: FleetBanner.Kind, title: String, detail: String) {
        banner.show(kind, title: title, detail: detail)
        let colour: NSColor
        switch kind {
        case .success: colour = .secondaryLabelColor
        case .warning: colour = .systemOrange
        case .error: colour = .systemRed
        case .idle: colour = .secondaryLabelColor
        }
        locationsTab.setAddStatus(detail.isEmpty ? title : "\(title) — \(detail)", colour: colour)
    }

    // MARK: Actions

    private func addLocation(label: String, url: String, token: String) {
        guard !adding else { return }
        guard !url.isEmpty else {
            report(.error, title: "An address is required", detail: "Enter the controller's address, e.g. http://10.0.0.5:47873.")
            return
        }
        adding = true
        setStatus("Adding \(label.isEmpty ? url : label)…")
        GatewayAPI.post("/admin/api/fleet/controllers", body: [
            "label": label,
            "url": url,
            "token": token,
        ]) { [weak self] result in
            guard let self else { return }
            self.adding = false
            switch result {
            case .failure(let error):
                self.report(
                    .error,
                    title: "Could not add the location",
                    detail: error.localizedDescription
                )
            case .success(let payload):
                let name = label.isEmpty ? url : label
                self.locationsTab.clearAddForm()
                self.describeCredential(
                    payload["credential"] as? [String: Any],
                    verb: "Added \(name)",
                    discovered: payload["discovered"] as? Int,
                    discoveryError: payload["discovery_error"] as? String
                )
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
    private func describeCredential(
        _ credential: [String: Any]?,
        verb: String,
        discovered: Int? = nil,
        discoveryError: String? = nil
    ) {
        // Discovered models arrive DISABLED. Saying it here is the difference
        // between "the location has no models" and "you have not enabled any yet".
        var tail = ""
        if let discovered {
            tail = discovered == 0
                ? " It reported no models."
                : " Discovered \(discovered) model\(discovered == 1 ? "" : "s") — enable the ones "
                  + "you want on the Models page; nothing is available until you do."
        } else if let discoveryError {
            tail = " Its models could not be read yet — \(discoveryError)"
        }

        guard let credential else {
            report(.warning, title: verb, detail: tail)
            return
        }
        let scope = credential["scope"] as? String ?? "unknown"
        let detail = credential["detail"] as? String ?? ""
        switch scope {
        case "fleet":
            report(.success, title: verb, detail: detail + tail)
        case "operator", "rejected":
            report(
                .warning,
                title: "\(verb) — check which token was pasted",
                detail: detail + tail
            )
        default:
            let lead = detail.isEmpty ? "" : detail
            report(.success, title: verb, detail: lead + tail)
        }
    }

    /// Rename any location, at any time.
    ///
    /// A name here is a display label, never an identity: the address, the
    /// credential and every model already discovered from that location stay
    /// exactly as they are, so this can never cost a re-entry of the token.
    private func renameController(_ location: FleetLocation) {
        guard let id = location.id else { return }
        let current = location.label ?? ""

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
            report(.error, title: "A name is required", detail: "The location was not renamed.")
            return
        }

        setStatus("Renaming \(current)…")
        GatewayAPI.patch("/admin/api/fleet/controllers/\(id)", body: ["label": name]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report(.error, title: "Could not rename it", detail: error.localizedDescription)
            case .success:
                self.report(
                    .success,
                    title: "Renamed to “\(name)”",
                    detail: "Its address, credential and models are untouched."
                )
                self.reload()
            }
        }
    }

    /// Both of a location's credentials, in one place, each labelled with what it is for.
    ///
    /// They are different values on purpose — the Hub refuses shared-voice routes to
    /// a fleet token so a node's credential can never administer the controller that
    /// owns it — and that difference is exactly what belongs in one explanation
    /// rather than two separate buttons.
    private func editCredentials(_ location: FleetLocation) {
        guard let id = location.id else { return }
        let label = location.label ?? id

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

        setStatus("Saving credentials for \(label)…")

        // Fleet token first: its reply is the one that reports scope and discovery.
        let finish: () -> Void = { [weak self] in
            guard let self else { return }
            guard !hubToken.isEmpty else {
                self.reload()
                return
            }
            GatewayAPI.put("/admin/api/fleet/controllers/\(id)/hub-token",
                           body: ["token": hubToken]) { [weak self] result in
                guard let self else { return }
                if case .failure(let error) = result {
                    self.report(
                        .warning,
                        title: "Saved the fleet token, but not the Hub token",
                        detail: error.localizedDescription
                    )
                } else {
                    self.report(
                        .success,
                        title: "Saved credentials for \(label)",
                        detail: "It keeps its fleet token for work, and can now be given shared voices."
                    )
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
                self.report(.error, title: "Could not save it", detail: error.localizedDescription)
            case .success(let payload):
                self.describeCredential(
                    payload["credential"] as? [String: Any],
                    verb: "Saved credentials for \(label)",
                    discovered: payload["discovered"] as? Int
                )
                finish()
            }
        }
    }

    private func testController(_ location: FleetLocation) {
        guard let id = location.id else { return }
        let label = location.label ?? id
        setStatus("Testing \(label)…")
        GatewayAPI.post("/admin/api/fleet/controllers/\(id)/test", body: [:]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report(.error, title: "Test failed for \(label)", detail: error.localizedDescription)
            case .success(let payload):
                let reachable = payload["reachable"] as? Bool ?? false
                if reachable {
                    let role = payload["role"] as? String ?? "unknown role"
                    let version = payload["version"] as? String ?? "unknown version"
                    let machines = payload["machines"] as? Int ?? 0
                    let nodes = payload["nodes"] as? Int ?? 0
                    self.describeCredential(
                        payload["credential"] as? [String: Any],
                        verb: "\(label) answered as \(role) on Hub \(version)",
                        discovered: nil,
                        discoveryError: nil
                    )
                    self.locationsTab.setAddStatus(
                        "\(machines) machines, \(nodes) nodes reported.",
                        colour: .secondaryLabelColor
                    )
                } else {
                    // The reason is the gateway's, and it distinguishes a refused
                    // connection (Studio Hub answering this Mac only) from a bad
                    // token from a silent network — three problems with three
                    // different fixes, one of which otherwise costs a site visit.
                    self.report(
                        .error,
                        title: "No answer from \(label)",
                        detail: payload["error"] as? String ?? "unknown reason"
                    )
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
    private func removeController(_ location: FleetLocation) {
        guard let id = location.id else { return }
        let label = location.label ?? id

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
                self.report(.error, title: "Could not remove \(label)", detail: error.localizedDescription)
            } else {
                self.report(
                    .success,
                    title: "Removed \(label)",
                    detail: "Its models are no longer offered here."
                )
            }
            self.reload()
        }
    }
}

// MARK: - Confirmation banner

/// The page's visible confirmation: an icon, a one-line title and a secondary
/// detail line, green for success and orange/red for anything that needs fixing.
///
/// The Fleet page used to report every action into one small line at the bottom,
/// which the owner could not see while looking at the controls — "there's no
/// visual confirmation". This is that confirmation, and it is deliberately hard
/// to miss.
final class FleetBanner: NSView {
    enum Kind {
        case idle, success, warning, error

        var tint: NSColor {
            switch self {
            case .success: return .systemGreen
            case .warning: return .systemOrange
            case .error: return .systemRed
            case .idle: return .secondaryLabelColor
            }
        }

        var symbol: String {
            switch self {
            case .success: return "checkmark.circle.fill"
            case .warning: return "exclamationmark.triangle.fill"
            case .error: return "xmark.octagon.fill"
            case .idle: return "info.circle"
            }
        }
    }

    private let icon = NSImageView()
    private let titleLabel = NSTextField(labelWithString: "")
    private let detailLabel = NSTextField(wrappingLabelWithString: "")
    private var hideTimer: Timer?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.cornerRadius = 6
        layer?.borderWidth = 1

        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 15, weight: .semibold)
        icon.setContentHuggingPriority(.required, for: .horizontal)

        titleLabel.font = .systemFont(ofSize: 12.5, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingTail
        detailLabel.font = .systemFont(ofSize: 11.5)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.maximumNumberOfLines = 2
        detailLabel.lineBreakMode = .byWordWrapping
        detailLabel.preferredMaxLayoutWidth = 520

        let text = NSStackView(views: [titleLabel, detailLabel])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 2
        text.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let row = NSStackView(views: [icon, text])
        row.orientation = .horizontal
        row.alignment = .top
        row.spacing = 9
        row.edgeInsets = NSEdgeInsets(top: 9, left: 12, bottom: 9, right: 12)
        row.translatesAutoresizingMaskIntoConstraints = false
        addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: topAnchor),
            row.leadingAnchor.constraint(equalTo: leadingAnchor),
            row.trailingAnchor.constraint(equalTo: trailingAnchor),
            row.bottomAnchor.constraint(equalTo: bottomAnchor),
            icon.widthAnchor.constraint(equalToConstant: 18),
        ])
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// **The tint has to be re-resolved on an appearance switch.** `NSColor.cgColor`
    /// snapshots whichever appearance was current when it was read, so a banner
    /// coloured once kept its light-appearance tint in dark mode — measured
    /// offscreen as white label text on a pale card, which is unreadable.
    /// `updateLayer` runs inside the effective appearance, so the banner follows
    /// the Mac and its text stays legible in both.
    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.backgroundColor = kind.tint.withAlphaComponent(0.10).cgColor
        layer?.borderColor = kind.tint.withAlphaComponent(0.35).cgColor
    }

    func show(_ kind: Kind, title: String, detail: String) {
        hideTimer?.invalidate()
        guard kind != .idle, !title.isEmpty || !detail.isEmpty else {
            clear()
            return
        }
        self.kind = kind
        icon.image = NSImage(systemSymbolName: kind.symbol, accessibilityDescription: nil)
        icon.contentTintColor = kind.tint
        titleLabel.stringValue = title
        titleLabel.textColor = kind.tint == .secondaryLabelColor ? .labelColor : kind.tint
        detailLabel.stringValue = detail
        detailLabel.isHidden = detail.isEmpty
        needsDisplay = true  // re-runs `updateLayer` with the new kind's tint
        isHidden = false
        // A success is confirmation, not a permanent notice; leave a failure up
        // until the next action so it cannot be missed.
        if kind == .success {
            hideTimer = Timer.scheduledTimer(withTimeInterval: 12, repeats: false) { [weak self] _ in
                self?.clear()
            }
        }
    }

    func clear() {
        hideTimer?.invalidate()
        hideTimer = nil
        isHidden = true
    }

    private var kind: Kind = .idle
}
