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
/// tab": Groups (one card per controller group, with every node in it),
/// Settings / Locations, Machines, Local Models and Jobs / Generations. The tab bar, the confirmation banner and the
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
    /// **But it re-reads when the owner comes back to the window.** This page is
    /// the one the owner leaves open while he walks to another Mac and fixes
    /// something; a page that kept showing the fleet from before that fix read as
    /// though the fix had not worked. It is read-only, so re-reading costs him
    /// nothing but the request.
    let refreshesOnReturn = true

    // MARK: State

    private var snapshot: FleetSnapshot?
    private var locations: [FleetLocation] = []
    /// The selected inner tab. Held here, not in the segmented control alone, so a
    /// reload — which rebuilds none of the tabs — never drops the owner back to
    /// the first one.
    private var selectedTab: FleetTab = .groups
    private var filter = FleetFilter()
    private var failedRefreshProviderIDs: [String] = []
    private var adding = false
    /// A Refresh is running: reads that land meanwhile must not replace its "Refreshing…" line.
    private var refreshing = false
    /// The location the last add created, kept on the page until a fleet read carries it, so no
    /// read that raced the add can make it vanish again.
    private var justAdded: FleetLocation?
    /// Reads left while a just-added location is still pending (one every 3 s).
    private var pendingReadsLeft = 0
    /// A publish is in flight. The button is disabled until the gateway answers,
    /// because the body carries the whole artifact and the gateway then pushes it
    /// to every controller.
    private var publishing = false
    /// The default JSON timeout is 15 s — an answer, not a gigabyte in flight. The
    /// gateway holds the whole base64 artifact in one body and pushes it to each
    /// controller sequentially, each with its own 600 s ceiling, so the wait can
    /// be tens of minutes.
    private static let publishTimeout: TimeInterval = 3600

    // MARK: Shell furniture

    private let tabControl = NSSegmentedControl(
        labels: FleetTab.allCases.map { $0.shortTitle },
        trackingMode: .selectOne, target: nil, action: nil
    )
    private let banner = FleetBanner(frame: .zero)
    private let locationPopup = NSPopUpButton()
    /// **The owner's own filter** (2026-10-29): everything, offline now, or the flapping Macs. The
    /// words and what they mean live in `FleetHealth.swift`, so the browser console and this page
    /// cannot decide them differently.
    private let healthPopup = NSPopUpButton()
    /// **The threshold every "Problematic" badge on this page is decided by**, shown with the control
    /// that changes it. A rule the app applies but never states is the kind of hidden setting the owner
    /// has to guess at; this one is a number he picked, so it sits on the page as a number.
    private let thresholdPopup = NSPopUpButton()
    private let searchField = NSSearchField()
    private let content = NSView()

    // MARK: Tabs

    private lazy var groupsTab = FleetGroupsTab(frame: .zero)
    private lazy var locationsTab = FleetLocationsTab(frame: .zero)
    private lazy var machinesTab = FleetMachinesTab(frame: .zero)
    private let startupBar = FleetStartupBar(frame: .zero)
    private let macTrashBar = FleetMacTrashBar(frame: .zero)
    private lazy var modelsTab = FleetModelsTab(frame: .zero)
    private lazy var jobsTab = FleetJobsTab(frame: .zero)
    private lazy var storyTab = FleetStoryTab(frame: .zero)

    lazy var view: NSView = buildContent()

    func activated() {
        reload()
        // The plain read is served from the gateway's cache while it re-sweeps in the
        // background, so coming back after fixing a Mac shows the old picture. Read
        // once more shortly after; repeated activations collapse into one.
        NSObject.cancelPreviousPerformRequests(withTarget: self, selector: #selector(reload), object: nil)
        perform(#selector(reload), with: nil, afterDelay: 3)
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let container = NSView()

        let top = NSStackView()
        top.orientation = .vertical
        top.alignment = .leading
        top.spacing = DS.Space.m
        top.edgeInsets = NSEdgeInsets(
            top: PageKit.pagePadding, left: PageKit.pagePadding,
            bottom: 0, right: PageKit.pagePadding
        )
        top.translatesAutoresizingMaskIntoConstraints = false

        top.addArrangedSubview(PageKit.heading(
            "Fleet",
            "Every group of Macs this gateway can send work to, and how each one is doing."
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

        // Startup control (FleetStartup.swift): the per-machine buttons and the bulk bar are injected here,
        // and a change reloads the page from the gateway.
        let reloadAfterStartup: () -> Void = { [weak self] in self?.reload(force: true) }
        startupBar.onChange = reloadAfterStartup
        machinesTab.startupAccessory.addArrangedSubview(startupBar)
        // macOS's own Trash setting (FleetMacTrash.swift): the per-Mac "Turn on" and the bulk bar.
        macTrashBar.onChange = reloadAfterStartup
        machinesTab.startupAccessory.addArrangedSubview(macTrashBar)
        machinesTab.macTrashCell = { row in
            FleetMacTrashCell(machine: row, done: reloadAfterStartup)
        }
        // **The health filter and its threshold live on the tab they filter**, in the accessory strip the
        // Startup and Trash bars already use. Putting them in the shell's own filter row widened the
        // page's minimum, and `test_window_size` caught the window growing on every section.
        machinesTab.startupAccessory.addArrangedSubview(PageKit.row([
            PageKit.label("Show", .secondary, colour: .secondaryLabelColor),
            healthPopup,
            PageKit.label("machines", .secondary, colour: .secondaryLabelColor),
        ], spacing: DS.Space.xs))
        // "Problematic = [4] or more drops in 24 hours": the app's own verdict, and its knob, in one row.
        thresholdPopup.target = self
        thresholdPopup.action = #selector(thresholdChanged)
        thresholdPopup.controlSize = .small
        thresholdPopup.isEnabled = false
        let thresholdRow = PageKit.row([
            PageKit.label("Problematic =", .secondary, colour: .secondaryLabelColor),
            thresholdPopup,
            PageKit.label("or more drops in 24 hours", .secondary, colour: .secondaryLabelColor),
        ], spacing: DS.Space.xs)
        machinesTab.startupAccessory.addArrangedSubview(thresholdRow)
        let resetCounters = NSButton(title: "Reset Job Counters…", target: self, action: #selector(resetCounters))
        resetCounters.controlSize = .small
        resetCounters.toolTip = "Count every Mac's done and failed jobs from now on. Nothing is deleted."
        machinesTab.startupAccessory.addArrangedSubview(resetCounters)
        machinesTab.warningList.onHide = { [weak self] kind in
            guard let self else { return }
            self.setHiddenWarningKinds(Set(self.machinesTab.hiddenWarningKinds).union([kind]))
        }
        machinesTab.warningList.onShowHidden = { [weak self] in self?.setHiddenWarningKinds([]) }
        machinesTab.startupCell = { [weak self] row in
            FleetStartup.cell(for: row, window: { self?.view.window }, done: reloadAfterStartup)
        }

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
        select(selectedTab)
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

        healthPopup.target = self
        healthPopup.action = #selector(healthFilterChanged)
        healthPopup.controlSize = .small
        for filter in FleetHealth.Filter.allCases {
            healthPopup.addItem(withTitle: filter.title)
        }
        healthPopup.widthAnchor.constraint(equalToConstant: 150).isActive = true

        searchField.placeholderString = "Filter machines, models, jobs…"
        searchField.delegate = self
        searchField.controlSize = .small
        searchField.widthAnchor.constraint(equalToConstant: 240).isActive = true

        let row = PageKit.row([
            PageKit.label("Show", .secondary, colour: .secondaryLabelColor),
            locationPopup,
            searchField,
            NSView(),
        ], spacing: DS.Space.s)
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
        case .groups: return groupsTab
        case .locations: return locationsTab
        case .machines: return machinesTab
        case .models: return modelsTab
        case .jobs: return jobsTab
        case .story: return storyTab
        }
    }

    /// Point every tab's buttons at the shell. The tabs are views; the network,
    /// the alerts and the banner all belong here.
    private func wireTabs() {
        groupsTab.onRefresh = { [weak self] in self?.refreshEverything() }
        groupsTab.onRefreshGroup = { [weak self] location in
            if let providerID = location.providerId, !providerID.isEmpty { self?.refreshProviders([providerID]) }
            else { self?.reload(force: true) }
        }
        groupsTab.onTest = { [weak self] location in self?.testController(location) }
        groupsTab.onRename = { [weak self] location in self?.renameController(location) }
        groupsTab.onCredentials = { [weak self] location in self?.editCredentials(location) }
        groupsTab.onRemove = { [weak self] location in self?.removeController(location) }
        groupsTab.onChangeAddress = { [weak self] location in self?.changeAddress(location) }
        groupsTab.onResume = { [weak self] location in self?.resumeController(location) }
        groupsTab.onPause = { [weak self] location in self?.pauseController(location) }
        // Startup control (FleetStartup.swift): the per-node "…" menu and the banner's bulk button.
        groupsTab.startupMenu = { [weak self] machine in
            FleetStartup.menuButton(for: machine, window: { self?.view.window }, done: { self?.reload(force: true) })
        }
        groupsTab.onEnableAutostart = { [weak self] machines in
            FleetStartup.enableEverywhere(machines: machines) { self?.reload(force: true) }
        }

        locationsTab.onRefresh = { [weak self] in self?.refreshEverything() }
        locationsTab.onRetryFailed = { [weak self] in self?.retryFailedRefresh() }
        locationsTab.onAddLocation = { [weak self] label, url, token in
            self?.addLocation(label: label, url: url, token: token)
        }
        locationsTab.onPublishRelease = { [weak self] in self?.beginPublishRelease() }
        locationsTab.onStopRollout = { [weak self] in self?.rolloutAction("stop") }
        locationsTab.onResumeRollout = { [weak self] in self?.rolloutAction("resume") }
        locationsTab.onTest = { [weak self] location in self?.testController(location) }
        locationsTab.onRename = { [weak self] location in self?.renameController(location) }
        locationsTab.onNetwork = { [weak self] location in FleetNetwork.editLocation(location) { self?.reload() } }
        modelsTab.onInstallEverywhere = { [weak self] package, label in
            FleetNetwork.installEverywhere(packageId: package, label: label, confirm: true) { self?.reload() }
        }
        modelsTab.spreadView.onStop = { [weak self] plan in FleetNetwork.stop(plan) { self?.reload() } }
        locationsTab.onCredentials = { [weak self] location in self?.editCredentials(location) }
        locationsTab.onRemove = { [weak self] location in self?.removeController(location) }
        locationsTab.onChangeAddress = { [weak self] location in self?.changeAddress(location) }
        locationsTab.onResume = { [weak self] location in self?.resumeController(location) }
        locationsTab.onPause = { [weak self] location in self?.pauseController(location) }

        jobsTab.onRefresh = { [weak self] in self?.refreshEverything() }
        jobsTab.onCancel = { [weak self] job in self?.cancelJob(job) }
        jobsTab.onRetry = { [weak self] job, item in self?.retryJobItem(job, item: item) }
        storyTab.report = { [weak self] kind, title, detail in self?.report(kind, title: title, detail: detail) }
    }

    // MARK: Tabs

    @objc private func tabChanged() {
        select(FleetTab(rawValue: tabControl.selectedSegment) ?? .groups)
    }

    func select(_ tab: FleetTab) {
        selectedTab = tab
        tabControl.selectedSegment = tab.rawValue
        for candidate in FleetTab.allCases {
            view(for: candidate).isHidden = candidate != tab
        }
        // The location and search filter narrows the three tables; Groups and Locations have no use for it
        // (Groups has its own search).
        filterRowHolder.isHidden = ![.machines, .models, .jobs].contains(tab)
        if tab == .story { storyTab.reload() }
    }

    /// Locations tab: add a location through the form (used by the render harness).
    func renderAdd(label: String, url: String, token: String) {
        locationsTab.pressAdd(label: label, url: url, token: token)
    }

    /// Groups tab: show only the groups that need attention (used by the render harness).
    func showProblemsOnly(_ value: Bool) {
        groupsTab.setProblemsOnly(value)
    }

    // MARK: Data

    /// Refresh each location's reachability **and re-read its models**.
    ///
    /// The button used to call `reload()` alone — `/admin/api/fleet`, the
    /// reachability probe — so a location whose models had never been read still
    /// showed none afterwards, and pressing Refresh looked like it did nothing.
    /// "No models" reads as "it has nothing" rather than "nobody has asked it
    /// yet", which is exactly what this page exists to tell apart; the web console
    /// has "Re-discover all providers" and this had only a button that said "Refresh".
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
            reload(force: true)
            return
        }
        refreshProviders(providerIds)
    }

    @objc func reload() {
        reload(completion: nil)
    }

    /// `force` is the Refresh button. A plain `reload()` reads the gateway's
    /// cached snapshot, which is what makes polling a dashboard cheap; forcing
    /// is the one path that waits for a real fan-out, so it is given a longer
    /// timeout than the 15 s a cached read needs.
    private func reload(force: Bool = false, completion: (() -> Void)? = nil) {
        let path = force ? "/admin/api/fleet?force=1" : "/admin/api/fleet"
        GatewayAPI.get(path, timeout: force ? 45 : 15) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.render(error: "Could not read the fleet — \(Self.sentence(error, within: force ? 45 : 15)).")
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
        refreshing = true
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
            // Each location on its own clock: a controller that takes 20 s to list its models
            // (measured on the Terranash Macs) is slow, not failed, and the others' counts show as
            // soon as each lands rather than after the slowest one.
            GatewayAPI.post("/admin/api/providers/\(encoded)/refresh", timeout: Self.refreshTimeout) { [weak self] result in
                guard let self else { return }
                switch result {
                case .failure(let error):
                    failures.append((providerID, Self.sentence(error, within: Self.refreshTimeout)))
                case .success(let payload):
                    if let error = payload["error"] as? String, !error.isEmpty {
                        failures.append((providerID, error))
                    }
                }
                finished += 1
                guard finished == providerIDs.count else {
                    self.reload()
                    return
                }
                self.refreshing = false
                // Forced: the owner pressed Refresh, so the page must show what
                // the fleet says *now*, not the cached snapshot the poller last
                // wrote. This is the one read that waits for a fan-out.
                self.reload(force: true) { [weak self] in
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
        // **"Could not discover" keeps its capital, and that is a contract, not a
        // style choice.** `tests/test_task4_native.py` reads this sentence to prove
        // the page reports a discovery failure rather than silently showing no
        // models; a rewrite that lowercased it turned the suite red for three days
        // while the behaviour was fine. If the wording must change, change the
        // check with it — in the same commit.
        setStatus(
            "Refreshed \(succeeded). Could not discover at \(failures.count) "
            + "location\(failures.count == 1 ? "" : "s") — \(details)",
            colour: Health.warn.text
        )
        report(
            .warning,
            title: "Refreshed \(succeeded) of \(total) location\(total == 1 ? "" : "s")",
            detail: details
        )
    }

    private func render(_ payload: [String: Any]) {
        guard var snapshot = FleetSnapshot.from(payload) else {
            // A 200 with a shape this build cannot read: say so rather than drawing
            // an empty fleet, because "the answer is nothing" and "I could not read
            // the answer" must not look the same.
            render(error: "The gateway's fleet answer could not be read.")
            return
        }
        if let added = justAdded {
            if snapshot.locations.contains(where: { $0.id == added.id }) {
                justAdded = nil
            } else {
                snapshot.locations.append(added)
            }
        }
        show(snapshot)
    }

    private func show(_ snapshot: FleetSnapshot) {
        self.snapshot = snapshot
        self.locations = snapshot.locations
        FleetNetwork.names = Set(snapshot.locations.compactMap(\.network) + snapshot.machines.compactMap(\.network)).sorted()
        // A successful read replaces whatever the status line said before (an old
        // error, "Loading…", "Refreshed N locations.") with how fresh the picture is.
        if !adding && !publishing && !refreshing {
            setStatus(snapshot.generatedAt.map { "As of \(GatewayAPI.clock($0))" } ?? "")
        }

        var providerIDs: [String: String] = [:]
        for location in snapshot.locations {
            if let id = location.id, let provider = location.providerId {
                providerIDs[id] = provider
            }
        }
        groupsTab.update(snapshot: snapshot)
        locationsTab.update(locations: snapshot.locations, machines: snapshot.machines)
        locationsTab.updateRelease(locations: snapshot.locations, state: snapshot.release)
        if !publishing { locationsTab.setRolloutRunning(snapshot.release?.rollout?.isRunning == true) }
        rebuildLocationPopup()
        applyFilter()
    }

    private func render(error: String) {
        // **A failed read never hides the locations the page already shows.** The plain read
        // timing out (a fan-out as slow as the slowest controller) used to clear every table, so a
        // location just added — and every other one — vanished until a later read. The last read
        // stays, and the status line says it is the last read.
        if let snapshot, !snapshot.locations.isEmpty {
            let when = snapshot.generatedAt.map { " from \(GatewayAPI.clock($0))" } ?? ""
            setStatus("\(error) Showing the last read\(when).", colour: Health.warn.text)
            return
        }
        let empty = FleetSnapshot()
        snapshot = nil
        locations = []
        groupsTab.showError(error)
        locationsTab.update(locations: [])
        locationsTab.updateRelease(
            locations: [], state: nil,
            unavailable: "The release status could not be read; the Fleet read failed."
        )
        machinesTab.update(machines: [], studios: [:], filter: filter)
        startupBar.update(machines: [])
        macTrashBar.update(machines: [])
        modelsTab.update(models: [], workers: [], filter: filter)
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

    /// Hide or show whole kinds of warning (macOS automatic updates, Time Machine). Applied here at once and
    /// saved on the gateway, which refuses anything that is not hideable (disk space never is).
    private func setHiddenWarningKinds(_ kinds: Set<String>) {
        snapshot?.fleetWarnings = FleetWarningSetting(hidden: kinds.sorted())
        applyFilter()
        GatewayAPI.put("/admin/api/settings/fleet-warnings", body: ["hidden": kinds.sorted()]) { [weak self] result in
            // A refusal or a failure puts the page back to what the gateway really holds.
            if case .failure = result { self?.reload() }
        }
    }

    private func applyFilter() {
        guard let snapshot else { return }
        machinesTab.hiddenWarningKinds = Set(snapshot.fleetWarnings?.hidden ?? [])
        machinesTab.summary = snapshot.summary
        machinesTab.update(
            machines: snapshot.machines, studios: snapshot.studiosByMachine, filter: filter)
        startupBar.update(machines: snapshot.machines)
        macTrashBar.update(machines: snapshot.machines)   // "Turn On for All Macs That Are Off"
        FleetTrash.everyMac = snapshot.machines   // "Trash on All Macs…" (FleetTrash.swift)
        updateThreshold(snapshot.fleetHealth)
        modelsTab.update(
            models: snapshot.allLocalModels,
            workers: snapshot.locations.flatMap { $0.workers },
            filter: filter, installs: snapshot.modelInstalls)
        followSpread(snapshot.modelInstalls)
        jobsTab.update(snapshot: snapshot, filter: filter)
        if selectedTab == .story { storyTab.reload() }
    }

    /// While an "Install on every Mac" runs, re-read the (cached) fleet every few seconds so its card moves; this
    /// page otherwise reads only on demand. Stops by itself when nothing runs or the page is closed.
    private var spreadReadPending = false
    private func followSpread(_ installs: [FleetModelSpread]) {
        guard installs.contains(where: \.running), !spreadReadPending else { return }
        spreadReadPending = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            guard let self else { return }
            self.spreadReadPending = false
            if self.modelsTab.window != nil { self.reload() }
        }
    }

    private func setStatus(_ text: String, colour: NSColor = .secondaryLabelColor) {
        groupsTab.setStatus(text, colour: colour)
        locationsTab.setStatus(text, colour: colour)
    }

    // MARK: Filters

    @objc private func locationFilterChanged() {
        filter.locationID = locationPopup.selectedItem?.representedObject as? String
        applyFilter()
    }

    /// Offline now / problematic / everything. Read off the popup by tag-free index, because the words
    /// are `FleetHealth.Filter`'s own and the order here is `allCases`.
    /// Offer the numbers the Gateway will accept, and select the one it is using.
    ///
    /// A gateway older than 0.2.209 publishes no threshold: the popup then says so and stays disabled,
    /// rather than offering a control that would be refused.
    private func updateThreshold(_ setting: FleetHealthSetting?) {
        guard let setting, let current = setting.flappingDropsDay,
              let low = setting.min, let high = setting.max else {
            if thresholdPopup.numberOfItems == 0 {
                thresholdPopup.addItem(withTitle: "not reported")
            }
            thresholdPopup.isEnabled = false
            return
        }
        // The range can be wide (`1…100`); the list offers a sensible band around it and keeps the
        // current value even when it falls outside, so the popup never misreports what is in force.
        let values = Array(Set([low, low + 1, low + 2, 3, 4, 5, 6, 8, 10, current]
            .filter { $0 >= low && $0 <= high })).sorted()
        if thresholdPopup.itemArray.compactMap({ $0.representedObject as? Int }) != values {
            thresholdPopup.removeAllItems()
            for value in values {
                thresholdPopup.addItem(withTitle: "\(value)")
                thresholdPopup.lastItem?.representedObject = value
            }
        }
        thresholdPopup.isEnabled = true
        if let index = values.firstIndex(of: current) { thresholdPopup.selectItem(at: index) }
    }

    @objc private func thresholdChanged() {
        guard let value = thresholdPopup.selectedItem?.representedObject as? Int else { return }
        GatewayAPI.put("/admin/api/settings/fleet-health", body: ["flapping_drops_day": value]) { [weak self] result in
            switch result {
            case .success: self?.reload(force: true)
            // A refusal puts the popup back to what the Gateway really holds.
            case .failure: self?.reload()
            }
        }
    }

    @objc private func healthFilterChanged() {
        let index = healthPopup.indexOfSelectedItem
        let filters = FleetHealth.Filter.allCases
        filter.health = index >= 0 && index < filters.count ? filters[index] : .all
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
        // One click stops a whole batch and there is no resuming it, only per-item retry.
        guard PageKit.confirm(
            "Stop \(what)?",
            "\(job.running ?? 0) running, \(job.queued ?? 0) queued at \(job.location ?? "this location"). "
                + "Queued items are cancelled; a worker that already took its item cannot be promised to stop.",
            ok: "Stop batch"
        ) else { return }
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

    // MARK: Release publishing

    struct PublishReleaseSheetForm {
        let view: NSView
        let versionField: NSTextField
        let buildField: NSTextField
        let allAtOnce: NSButton
        let forceFull: NSButton
        let forceFullHint: NSTextField

        init(version: String, build: String, filename: String) {
            versionField = NSTextField(string: version)
            versionField.placeholderString = "0.6.50"
            versionField.widthAnchor.constraint(equalToConstant: 220).isActive = true

            buildField = NSTextField(string: build)
            buildField.placeholderString = "abc1234"
            buildField.widthAnchor.constraint(equalToConstant: 220).isActive = true

            let hint = PageKit.help(
                "Read from \(filename) — check both. They are signed and "
                + "cannot be corrected without another push."
            )
            hint.preferredMaxLayoutWidth = 320
            hint.widthAnchor.constraint(equalToConstant: 320).isActive = true

            allAtOnce = NSButton(
                checkboxWithTitle: "All at once (emergency): every controller now",
                target: nil, action: nil
            )

            forceFull = NSButton(
                checkboxWithTitle: "Send the whole app (not just the changed files)",
                target: nil, action: nil
            )
            forceFull.state = .off

            forceFullHint = PageKit.help("Use when a Mac is too far behind for a files-only update.")
            forceFullHint.preferredMaxLayoutWidth = 320
            forceFullHint.widthAnchor.constraint(equalToConstant: 320).isActive = true

            let stack = NSStackView(views: [
                PageKit.formRow("Version", versionField),
                PageKit.formRow("Build", buildField),
                hint,
                allAtOnce,
                forceFull,
                forceFullHint,
            ])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = DS.Space.m
            stack.frame = NSRect(x: 0, y: 0, width: 340, height: 240)
            self.view = stack
        }
    }

    /// Pick a `.dmg`, name its version and build, then sign and push it.
    ///
    /// The version and build are **asked for**, not taken from the filename: they
    /// are what the gateway signs, a wrong one is signed exactly like a right one,
    /// and the filename is only a hint. It pre-fills what can be read and leaves
    /// the rest blank rather than guessing a version.
    private func beginPublishRelease() {
        guard !publishing else { return }

        let panel = NSOpenPanel()
        panel.title = "Choose a Studio Hub release"
        panel.prompt = "Choose"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        // `allowedContentTypes` is the modern spelling, but it needs the
        // UniformTypeIdentifiers framework and a newer deployment target; this app
        // targets macOS 11. `ponytail:` swap it when the floor rises.
        panel.allowedFileTypes = ["dmg"]
        guard panel.runModal() == .OK, let url = panel.url else { return }

        let guessed = parseReleaseName(url.lastPathComponent)
        let sheet = PublishReleaseSheetForm(version: guessed.version, build: guessed.build, filename: url.lastPathComponent)

        let alert = NSAlert()
        alert.messageText = "Publish \(url.lastPathComponent)?"
        alert.informativeText =
            "This gateway signs the DMG and rolls it out one location at a time: this Mac's Studio Hub, "
            + "then each controller in turn, waiting for each to come back on the new version. Each "
            + "group's Macs then update themselves one by one, after finishing their current work."
        alert.accessoryView = sheet.view
        alert.addButton(withTitle: "Publish")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = sheet.versionField
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let version = sheet.versionField.stringValue.trimmingCharacters(in: .whitespaces)
        let build = sheet.buildField.stringValue.trimmingCharacters(in: .whitespaces)
        guard !version.isEmpty, !build.isEmpty else {
            report(
                .error,
                title: "A version and a build are required",
                detail: "Enter both, then publish again. Nothing was pushed."
            )
            return
        }

        // Warn *before* a 1.1 GB push when a controller already holds something
        // newer. Studio Hub's updater has no ordering, so it would take the older
        // build; the gateway refuses that too, but catching it here saves the
        // round trip and lets the owner decide with the labels in front of them.
        let older = downgrades(of: version)
        if !older.isEmpty {
            let lines = older.map { "• \($0.label) holds \($0.held)" }.joined(separator: "\n")
            let confirm = NSAlert()
            confirm.messageText = "Publish \(version) over a newer release?"
            confirm.informativeText =
                "These controllers already hold a newer version:\n\(lines)\n\n"
                + "Publishing \(version) would take them backwards. Continue only if "
                + "that is what you want."
            confirm.alertStyle = .warning
            confirm.addButton(withTitle: "Publish Anyway")
            confirm.addButton(withTitle: "Cancel")
            guard confirm.runModal() == .alertFirstButtonReturn else { return }
            publishRelease(
                path: url.path, version: version, build: build,
                filename: url.lastPathComponent, allowDowngrade: true,
                allAtOnce: sheet.allAtOnce.state == .on,
                forceFull: sheet.forceFull.state == .on
            )
            return
        }

        publishRelease(
            path: url.path, version: version, build: build, filename: url.lastPathComponent,
            allAtOnce: sheet.allAtOnce.state == .on, forceFull: sheet.forceFull.state == .on
        )
    }

    /// The controllers that already hold a **newer** version than `version`.
    ///
    /// Mirrors `gateway/release_publisher.py::is_downgrade`: the backend guard is
    /// the enforcement, and this only powers the warning. A version with no
    /// dotted-number order is never treated as a downgrade — refusing on a guess
    /// would be worse than not refusing.
    private func downgrades(of version: String) -> [(label: String, held: String)] {
        locations.compactMap { location in
            guard location.local != true, location.reachable != false else { return nil }
            guard let held = location.release?.version, isNewer(held, than: version) else { return nil }
            return (label: location.label ?? location.id ?? "Controller", held: held)
        }
    }

    /// `1.2.3` as comparable numbers, or `nil` when it is not dotted numbers.
    private func versionParts(_ version: String?) -> [Int]? {
        let text = (version ?? "").trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return nil }
        var numbers: [Int] = []
        for part in text.split(separator: ".", omittingEmptySubsequences: false) {
            guard !part.isEmpty, part.allSatisfy(\.isNumber), let value = Int(part) else { return nil }
            numbers.append(value)
        }
        return numbers
    }

    private func isNewer(_ candidate: String?, than version: String) -> Bool {
        guard let held = versionParts(candidate), let target = versionParts(version) else { return false }
        let count = max(held.count, target.count)
        let paddedHeld = held + Array(repeating: 0, count: count - held.count)
        let paddedTarget = target + Array(repeating: 0, count: count - target.count)
        // `[Int]` is not Comparable; lexicographic order is the same comparison.
        // True when the target is below what is held, i.e. the held one is newer.
        return paddedTarget.lexicographicallyPrecedes(paddedHeld)
    }

    /// `StudioHub-0.6.50-abc1234.dmg` → version `0.6.50`, build `abc1234`.
    ///
    /// Conservative on purpose: anything that does not clearly fit leaves both
    /// fields blank. A wrong version is signed exactly like a right one, so a
    /// guess here is worse than an empty field.
    private func parseReleaseName(_ name: String) -> (version: String, build: String) {
        let stem = (name as NSString).deletingPathExtension
        let parts = stem.split(separator: "-").map(String.init)
        guard parts.count >= 3 else { return ("", "") }
        let version = parts[1]
        let build = parts[2]
        let versionLooksRight = version.contains(where: \.isNumber)
            && version.allSatisfy { $0.isNumber || $0 == "." }
        let buildLooksRight = !build.isEmpty
            && build.allSatisfy { $0.isLetter || $0.isNumber }
        guard versionLooksRight, buildLooksRight else { return ("", "") }
        return (version, build)
    }

    private func controllerCountDescription() -> String {
        let count = locations.filter { $0.local != true }.count
        guard count > 0 else { return "no controllers" }
        return "\(count) controller\(count == 1 ? "" : "s")"
    }

    private func publishRelease(
        path: String, version: String, build: String, filename: String,
        allowDowngrade: Bool = false, allAtOnce: Bool = false, forceFull: Bool = false
    ) {
        publishing = true
        locationsTab.setPublishing(
            true,
            message: allAtOnce
                ? "Signing \(filename) and pushing it to \(controllerCountDescription()) at once. "
                    + "This can take a while."
                : "Starting the rollout of \(filename)…"
        )
        setStatus("Signing and pushing \(filename)…")
        var body: [String: Any] = [
            "artifact_path": path,
            "version": version,
            "build": build,
            "filename": filename,
        ]
        // Only sent when the owner chose to override the newer-version guard.
        if allowDowngrade { body["allow_downgrade"] = true }
        if allAtOnce { body["mode"] = "all_at_once" }
        if forceFull { body["force_full"] = true }
        GatewayAPI.post(
            "/admin/api/release/publish",
            body: body,
            timeout: Self.publishTimeout
        ) { [weak self] result in
            guard let self else { return }
            self.publishing = false
            self.locationsTab.setPublishing(false, message: "")
            switch result {
            case .failure(let error):
                self.report(
                    .error,
                    title: "Could not publish the release",
                    detail: error.localizedDescription
                )
                self.setStatus("The release could not be published.", colour: Health.bad.text)
            case .success(let payload):
                if let rollout = (payload["rollout"] as? [String: Any]).flatMap(FleetRollout.from) {
                    self.report(
                        .success,
                        title: "Rolling out \(version) (\(build))",
                        detail: rollout.summary ?? "One location at a time; the progress is shown below."
                    )
                    self.setStatus("Rolling out \(version) (\(build)), one location at a time.")
                } else {
                    self.reportPublish(payload, version: version, build: build)
                }
            }
            // The held-release column is what a controller reports now, so it is
            // re-read after the push rather than filled from the push's own answer.
            self.reload()
        }
    }

    /// The Machines tab's "Reset Job Counters…": every Mac counts done/failed from now on. A baseline the
    /// Gateway keeps and every Hub counts from; no Hub's job history is touched.
    @objc private func resetCounters() {
        let sure = NSAlert()
        sure.messageText = "Reset the job counters on every Mac?"
        sure.informativeText = "The Machines tab will count done and failed jobs from now on. "
            + "Nothing is deleted: every Hub keeps its full job history. "
            + "A Mac on an older Studio Hub keeps counting everything until it updates, and says so."
        sure.addButton(withTitle: "Reset Counters")
        sure.addButton(withTitle: "Cancel")
        guard sure.runModal() == .alertFirstButtonReturn else { return }
        GatewayAPI.post("/admin/api/fleet/counters/reset", body: [:]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report(.error, title: "Could not reset the counters", detail: error.localizedDescription)
            case .success(let payload):
                let since = (payload["counters_since"] as? Double).map(FleetFormat.stamp) ?? "now"
                self.report(.success, title: "Counters reset", detail: "Jobs are counted since \(since).")
                self.reload(force: true)
            }
        }
    }

    /// Stop or resume the paced rollout. Stopping lets a push in flight finish; nothing is undone.
    private func rolloutAction(_ action: String) {
        GatewayAPI.post("/admin/api/release/rollout/\(action)", body: [:]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report(.error, title: "Could not \(action) the rollout", detail: error.localizedDescription)
            case .success(let payload):
                let rollout = (payload["rollout"] as? [String: Any]).flatMap(FleetRollout.from)
                self.report(.success, title: action == "stop" ? "Rollout stopped" : "Rollout resumed",
                            detail: rollout?.summary ?? "")
            }
            self.reload()
        }
    }

    private func reportPublish(_ payload: [String: Any], version: String, build: String) {
        guard let push = FleetPushReport.from(payload) else {
            report(
                .warning,
                title: "The release was pushed, but its answer could not be read",
                detail: "Reload the page to see what each controller holds."
            )
            return
        }
        let total = push.outcomes.count
        let refusals = push.refusals
        let accepted = total - refusals.count
        if total == 0 {
            report(
                .warning,
                title: "Signed \(version) (\(build)) — no controllers to send it to",
                detail: "Add a location, then publish again."
            )
            setStatus("Signed \(version) (\(build)); no controllers to send it to.", colour: Health.warn.text)
            return
        }
        if push.allAccepted {
            report(
                .success,
                title: "Published \(version) (\(build))",
                detail: "All \(total) controller\(total == 1 ? "" : "s") accepted it and read back "
                    + "the same release."
            )
            setStatus("Published \(version) (\(build)) to \(total) controller\(total == 1 ? "" : "s").")
            return
        }
        // Each controller's own sentence, verbatim, one per refusal. A 409 already
        // carries the App Update Key public-key path and comparison fingerprint.
        let details = refusals.map { refusal -> String in
            let name = refusal.outcome.label
                ?? locations.first { $0.id == refusal.id }?.label
                ?? refusal.id
            let detail = (refusal.outcome.detail ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return detail.isEmpty ? "\(name): refused without a reason" : "\(name): \(detail)"
        }.joined(separator: "\n")
        report(
            .warning,
            title: "Published to \(accepted) of \(total) controller\(total == 1 ? "" : "s")",
            detail: details
        )
        setStatus(
            "Published \(version) (\(build)) to \(accepted) of \(total) controllers.",
            colour: Health.warn.text
        )
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
        case .warning: colour = Health.warn.text
        case .error: colour = Health.bad.text
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
        let host = URL(string: url)?.host ?? url
        banner.clear()
        locationsTab.setAdding(true, message: "Connecting to \(host)…")
        setStatus("Adding \(label.isEmpty ? url : label) — connecting to \(host)…")
        // The add probes the controller, reads its models and checks the token, each against a
        // controller that may take 20 s to answer; 15 s reported a failure for an add that worked.
        GatewayAPI.post("/admin/api/fleet/controllers", body: [
            "label": label,
            "url": url,
            "token": token,
        ], timeout: Self.addTimeout) { [weak self] result in
            guard let self else { return }
            self.adding = false
            self.locationsTab.setAdding(false, message: "")
            switch result {
            case .failure(let error):
                // The gateway's (or the controller's) own sentence, verbatim.
                self.report(
                    .error,
                    title: "Could not add the location",
                    detail: Self.sentence(error, within: Self.addTimeout)
                )
            case .success(let payload):
                let name = label.isEmpty ? url : label
                self.locationsTab.clearAddForm()
                self.insertJustAdded(payload["location"] as? [String: Any])
                self.describeCredential(
                    payload["credential"] as? [String: Any],
                    verb: "Added \(name)",
                    discovered: payload["discovered"] as? Int,
                    discoveryError: payload["discovery_error"] as? String,
                    enableSeed: payload["enable_seed"] as? [String: Any]
                )
                self.pendingReadsLeft = 20
                self.reload { [weak self] in self?.followPending() }
            }
        }
    }

    /// The row the add answered with, on the page at once: "Just added — reading its Macs…".
    private func insertJustAdded(_ row: [String: Any]?) {
        guard let row, let added = FleetSnapshot.from(["locations": [row]])?.locations.first else { return }
        justAdded = added
        var shown = snapshot ?? FleetSnapshot()
        if !shown.locations.contains(where: { $0.id == added.id }) {
            shown.locations.append(added)
            show(shown)
        }
    }

    /// Re-read (a cache read: cheap) every 3 s while a location is still pending, so its Macs and
    /// models fill in by themselves once the gateway's first fan-out that includes it lands.
    private func followPending() {
        guard pendingReadsLeft > 0, locations.contains(where: { $0.pending == true }) else {
            pendingReadsLeft = 0
            return
        }
        pendingReadsLeft -= 1
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            self?.reload { [weak self] in self?.followPending() }
        }
    }

    private static let addTimeout: TimeInterval = 90
    private static let refreshTimeout: TimeInterval = 60

    /// An error as a plain sentence: a timeout says how long it waited, anything else is the
    /// gateway's (or the controller's) own sentence.
    static func sentence(_ error: Error, within seconds: TimeInterval) -> String {
        if (error as? URLError)?.code == .timedOut { return "didn't answer within \(Int(seconds)) s" }
        return error.localizedDescription
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
        discoveryError: String? = nil,
        enableSeed: [String: Any]? = nil
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
        // **What a newly added location switched on, in the gateway's own sentence**
        // (`fleet.seed_enabled_installed`: the models its Hub reports installed, the
        // owner's ruled-out ones left off, nothing copied from this Mac). `nil` for a
        // re-add (its own decisions are deliberately kept), so it is guarded, and a
        // failed seed says so rather than reading as "nothing to switch on".
        if let enableSeed {
            if let sentence = enableSeed["sentence"] as? String, !sentence.isEmpty {
                tail += " " + sentence
            } else if let error = enableSeed["error"] as? String, !error.isEmpty {
                tail += " Its installed models could not be switched on — \(error)"
            }
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

        // 292 + the eye = the 320 the captions wrap at.
        func field(_ placeholder: String) -> SecretField {
            SecretField(placeholder: placeholder, width: 292)
        }
        func caption(_ text: String) -> NSTextField {
            let caption = NSTextField(labelWithString: text)
            caption.font = TextRole.caption.font
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
        stack.spacing = DS.Space.xs
        stack.frame = NSRect(x: 0, y: 0, width: 320, height: 230)

        let alert = NSAlert()
        alert.messageText = "Credentials for \(label)"
        alert.informativeText =
            "Leave a field empty to keep what is already stored. This does not disturb the "
            + "location's name, address or models."
        alert.accessoryView = stack
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = fleet.activeField
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

    /// **Handover: point this location at the Mac that replaces it.**
    ///
    /// Not Remove + Add — that makes a second location and orphans this one's credential and
    /// every per-model enable decision. The row id, and so the provider id Story's presets and
    /// your ticks hang on, is kept. The sheet tests the *new* address first (a candidate probe,
    /// nothing stored) and only re-points once you confirm.
    private func changeAddress(_ location: FleetLocation) {
        guard let id = location.id else { return }
        let label = location.label ?? id

        let address = NSTextField(string: location.url ?? "")
        address.placeholderString = "http://100.64.0.9:47873"
        address.translatesAutoresizingMaskIntoConstraints = false
        address.widthAnchor.constraint(equalToConstant: 320).isActive = true
        let token = SecretField(placeholder: "Fleet token (optional — empty keeps the stored one)", width: 292)
        let result = NSTextField(wrappingLabelWithString: "Not tested yet.")
        result.font = TextRole.caption.font
        result.textColor = .secondaryLabelColor
        result.maximumNumberOfLines = 4
        result.preferredMaxLayoutWidth = 320
        let body: () -> [String: Any] = {
            var body: [String: Any] = ["url": address.stringValue.trimmingCharacters(in: .whitespaces)]
            let secret = token.stringValue.trimmingCharacters(in: .whitespaces)
            if !secret.isEmpty { body["token"] = secret }
            return body
        }
        // The address the last *successful* test answered at; a confirm on anything else asks first.
        var testedOK: String?
        // The sheet is modal, so the test reports into the sheet itself.
        let test = FleetClosureButton(title: "Test This Address") {
            let sent = body()
            result.stringValue = "Testing…"
            result.textColor = .secondaryLabelColor
            GatewayAPI.post("/admin/api/fleet/controllers/\(id)/test", body: sent) { outcome in
                switch outcome {
                case .failure(let error):
                    result.stringValue = "Test failed — \(error.localizedDescription)"
                    result.textColor = Health.bad.text
                case .success(let payload):
                    if payload["reachable"] as? Bool == true {
                        testedOK = sent["url"] as? String
                        let role = payload["role"] as? String ?? "unknown role"
                        let version = payload["version"] as? String ?? "unknown version"
                        result.stringValue = "Answered as \(role) on Hub \(version): "
                            + "\(payload["machines"] as? Int ?? 0) machines, "
                            + "\(payload["nodes"] as? Int ?? 0) nodes."
                        result.textColor = Health.ok.text
                    } else {
                        result.stringValue = "No answer — \(payload["error"] as? String ?? "unknown reason")"
                        result.textColor = Health.warn.text
                    }
                }
            }
        }
        let stack = NSStackView(views: [address, token, test, result])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.tight
        stack.frame = NSRect(x: 0, y: 0, width: 320, height: 130)

        let alert = NSAlert()
        alert.messageText = "Change the address of \(label)"
        alert.informativeText =
            "Use this when a controller is replaced (for example a node promoted to head). Its "
            + "credential, its models and every enable decision stay with the location; the old "
            + "identity is dropped and the next probe reads the new one."
        alert.accessoryView = stack
        alert.addButton(withTitle: "Change Address")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = address

        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let request = body()
        guard let url = request["url"] as? String, !url.isEmpty else {
            report(.error, title: "An address is required", detail: "The address was not changed.")
            return
        }
        if testedOK != url {
            let sure = NSAlert()
            sure.messageText = "This address has not answered a test"
            sure.informativeText = "Change \(label) to \(url) anyway? Routing to it will fail until that Mac answers."
            sure.alertStyle = .warning
            sure.addButton(withTitle: "Cancel")
            sure.addButton(withTitle: "Change Anyway")
            guard sure.runModal() == .alertSecondButtonReturn else { return }
        }
        setStatus("Changing the address of \(label)…")
        GatewayAPI.put("/admin/api/fleet/controllers/\(id)/address", body: request) { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .failure(let error):
                self.report(.error, title: "Could not change the address", detail: error.localizedDescription)
            case .success(let payload):
                let discovered = payload["discovered"] as? Int
                let tail = discovered.map { " It now offers \($0) model\($0 == 1 ? "" : "s")." }
                    ?? (payload["discovery_error"] as? String).map { " Its models could not be read yet — \($0)" }
                    ?? ""
                self.report(
                    .success,
                    title: "\(label) now answers at \(url)",
                    detail: "Credential and enable decisions kept." + tail
                )
                self.reload()
            }
        }
    }

    /// Pause a location by hand (this Mac included): it gets no new work, jobs already running there finish,
    /// and nothing is removed. Kept across restarts; only Resume lifts it.
    private func pauseController(_ location: FleetLocation) {
        guard let id = location.id else { return }
        let label = location.label ?? id
        setStatus("Pausing \(label)…")
        GatewayAPI.post("/admin/api/fleet/controllers/\(id)/pause", body: [:]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report(.error, title: "Could not pause \(label)", detail: error.localizedDescription)
            case .success:
                self.report(
                    .success,
                    title: "Paused \(label)",
                    detail: "It gets no new work; jobs already running there finish. Resume it whenever you want it back."
                )
                self.reload()
            }
        }
    }

    /// Resume a location: one the owner paused by hand, or one the gateway paused after a long outage.
    private func resumeController(_ location: FleetLocation) {
        guard let id = location.id else { return }
        let label = location.label ?? id
        setStatus("Resuming \(label)…")
        GatewayAPI.post("/admin/api/fleet/controllers/\(id)/resume", body: [:]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.report(.error, title: "Could not resume \(label)", detail: error.localizedDescription)
            case .success:
                self.report(
                    .success,
                    title: "Resumed \(label)",
                    detail: "Routing may use it again as soon as it answers; nothing else changed."
                )
                self.reload()
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
            case .success: return Health.ok.text
            case .warning: return Health.warn.text
            case .error: return Health.bad.text
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
        layer?.cornerRadius = DS.Radius.card
        layer?.borderWidth = 1

        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 15, weight: .semibold)
        icon.setContentHuggingPriority(.required, for: .horizontal)

        titleLabel.font = TextRole.rowTitle.font
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        detailLabel.font = TextRole.secondary.font
        detailLabel.textColor = .secondaryLabelColor
        // Up to six lines so a publish can name each refusing controller in its
        // own sentence. The Release section below carries every refusal in full,
        // so a fleet larger than the banner can show is never silently dropped.
        detailLabel.maximumNumberOfLines = 6
        detailLabel.lineBreakMode = .byWordWrapping
        detailLabel.preferredMaxLayoutWidth = 0
        detailLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let text = NSStackView(views: [titleLabel, detailLabel])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = DS.Space.xs
        text.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        // Wrap at the available width while retaining the existing readable measure.
        let detailWidth = detailLabel.widthAnchor.constraint(equalTo: text.widthAnchor)
        detailWidth.priority = .defaultHigh
        NSLayoutConstraint.activate([
            detailWidth,
            detailLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
            titleLabel.widthAnchor.constraint(lessThanOrEqualTo: text.widthAnchor),
        ])

        let row = NSStackView(views: [icon, text])
        row.orientation = .horizontal
        row.alignment = .top
        row.spacing = DS.Space.s
        row.translatesAutoresizingMaskIntoConstraints = false
        addSubview(row)
        // Stack insets do not contribute reliably to this wrapping row's fitting
        // height. Anchor the padding, as CardView.hold does for other Fleet notices.
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: topAnchor, constant: DS.Space.m),
            row.leadingAnchor.constraint(equalTo: leadingAnchor, constant: DS.Space.l),
            row.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -DS.Space.l),
            row.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -DS.Space.m),
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
