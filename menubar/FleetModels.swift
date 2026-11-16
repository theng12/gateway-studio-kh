import AppKit

/// The Fleet payload, decoded.
///
/// **Nothing here is required.** The gateway grows this payload one section at a
/// time — the machines list came first, and the workers, local models and issues
/// lists follow — so an older gateway must still fill the parts of the page it
/// *can* fill. A missing key must never crash the app or blank a whole tab, which
/// is why every property is optional, every list defaults to empty, and a single
/// malformed row is dropped rather than taking its whole section with it.
///
/// The decoded shape is the JSON the gateway publishes, snake_case and all. Each
/// field is paired with its hub-side name in the type below so a reader changing
/// one end can see the other.

// MARK: - Snapshot

struct FleetSnapshot: Decodable {
    var generatedAt: Double?
    var summary: FleetSummary?
    var locations: [FleetLocation] = []
    var machines: [FleetMachine] = []
    var running: [FleetJob] = []
    var issues: [FleetIssue] = []
    /// A flat model list, if the gateway also publishes one. The rows normally
    /// arrive nested under their location; both are read and merged.
    var models: [FleetLocalModel] = []
    /// The warning kinds the owner hid; `nil` from a gateway that predates the setting.
    var fleetWarnings: FleetWarningSetting?
    /// Every "Install on every Mac that can run it", newest first (`FleetModelSpread.swift`); `[]` from an older gateway.
    var modelInstalls: [FleetModelSpread] = []
    /// This Gateway's own queue with background work (0.2.165) apart; nil from an older gateway.
    var gatewayQueue: FleetGatewayQueue?
    /// The flapping threshold behind every "Problematic" on this page, with its bounds.
    var fleetHealth: FleetHealthSetting?

    private enum CodingKeys: String, CodingKey {
        case generatedAt, summary, locations, machines, running, issues, models, release, fleetWarnings, modelInstalls,
             gatewayQueue, fleetHealth
    }

    /// An empty snapshot, for the "could not read the fleet" case: every table is
    /// cleared rather than left showing numbers that were not measured this time.
    init() {}

    /// Decode the `[String: Any]` `GatewayAPI` hands back.
    ///
    /// It is re-serialised rather than walked by hand so the field names live in
    /// one place — the `CodingKeys` and property names here — instead of in a
    /// second, parallel set of string lookups that could drift from them.
    static func from(_ payload: [String: Any]) -> FleetSnapshot? {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload) else { return nil }
        let decoder = JSONDecoder()
        // The gateway publishes snake_case; the properties here are camelCase.
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(FleetSnapshot.self, from: data)
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generatedAt = try? c.decode(Double.self, forKey: .generatedAt)
        // Each section is decoded on its own: a summary the gateway renames must
        // not cost the machines table, and one bad location row must not cost
        // the other nine sites.
        summary = try? c.decode(FleetSummary.self, forKey: .summary)
        locations = lenientArray(c, forKey: .locations)
        machines = lenientArray(c, forKey: .machines)
        running = lenientArray(c, forKey: .running)
        issues = lenientArray(c, forKey: .issues)
        models = lenientArray(c, forKey: .models)
        fleetWarnings = try? c.decode(FleetWarningSetting.self, forKey: .fleetWarnings)
        modelInstalls = lenientArray(c, forKey: .modelInstalls)
        gatewayQueue = try? c.decode(FleetGatewayQueue.self, forKey: .gatewayQueue)
        fleetHealth = try? c.decode(FleetHealthSetting.self, forKey: .fleetHealth)
    }
}

/// `gateway_queue`: jobs waiting and running here, and how many of them are background work.
struct FleetGatewayQueue: Decodable {
    struct Background: Decodable {
        var waiting: Int?
        var running: Int?
        var waitingByModality: [String: Int]?
    }
    var waiting: Int?
    var running: Int?
    var background: Background?

    /// "Gateway queue: 4 waiting, 9 running · 312 background music waiting (idle Macs only)".
    var text: String {
        let bg = background?.waiting ?? 0
        let normal = max((waiting ?? 0) - bg, 0)
        var line = "Gateway queue: \(normal) waiting, \(running ?? 0) running"
        let lanes = (background?.waitingByModality ?? [:]).filter { $0.value > 0 }.sorted { $0.key < $1.key }
        if !lanes.isEmpty {
            line += " · " + lanes.map { "\($0.value) background \(FleetFormat.words($0.key).lowercased()) waiting" }
                .joined(separator: ", ") + " (they run only on idle Macs)"
        }
        return line
    }
}

struct FleetSummary: Decodable {
    var locations: Int?
    var locationsUp: Int?
    var machines: Int?
    var machinesOnline: Int?
    var machinesOffline: Int?
    var machinesUnknown: Int?
    var jobsCompleted: Int?
    var jobsFailed: Int?
    /// The owner's "Reset counters" baseline (epoch seconds); nil when never reset.
    var countersSince: Double?
    var runningJobs: Int?
    var probeMachinesHidden: Int?
    var localModelsTotal: Int?
    var modelsAvailable: Int?
    var workersTotal: Int?
    var workersReady: Int?
    var issues: Int?
    /// **Studio Hub versions against the newest release this Gateway published** (`hubLatest`; nil when
    /// nothing was published here). `onLatest` of `versionKnown` Macs that answered with a version.
    var hubLatest: String?
    var onLatest: Int?
    var versionKnown: Int?
    var hubUpdating: Int?
    var hubBehind: Int?
    var hubUnknown: Int?
    /// The Hub's own per-state machine counts. Kept because the pulse line is
    /// read verbatim from the gateway rather than re-derived here.
    var pulse: [String: Int]?
    /// **How the fleet is wired**, in one line the Machines tab draws above the table: how many Macs
    /// sit on a slow Ethernet link, on Wi-Fi, or cannot be read at all (see `fleet._connection_summary`).
    var connections: FleetConnections?
}

/// The fleet-wide link line: `slow_ethernet` Macs (named) under 1 Gb/s or at half duplex,
/// `wifi` Macs, and `unmeasured` ones whose Hub does not publish the block.
struct FleetConnections: Decodable {
    var slowEthernet: Int?
    var slowMachines: [String]?
    var wifi: Int?
    var wifiMachines: [String]?
    var ethernet: Int?
    var unmeasured: Int?
    var total: Int?

    /// "3 Macs on slow Ethernet — check the cable: A, B, C." `nil` when no Mac is slow, so the line
    /// takes no room. The Macs are named because the point of the figure is which cable to replace.
    var slowLine: String? {
        let count = slowEthernet ?? 0
        guard count > 0 else { return nil }
        let named = (slowMachines ?? []).filter { !$0.isEmpty }
        return "\(count) Mac\(count == 1 ? "" : "s") on slow Ethernet — check the cable"
            + (named.isEmpty ? "." : ": " + named.joined(separator: ", ") + ".")
    }
}

// MARK: - Install on every Mac (LAN model sharing, FleetModelSpread.swift)

/// One Mac's line in a spread (`model_installs[].networks[].macs[]`); `text` is the gateway's sentence.
struct FleetSpreadMac: Decodable {
    var locationId: String?
    var machine: String?
    var label: String?
    var state: String?
    var text: String?
    var note: String?
}

struct FleetSpreadNetwork: Decodable {
    var network: String?
    var title: String?
    var done: Int?
    var total: Int?
    var macs: [FleetSpreadMac]?
}

/// One "Install on every Mac that can run it" (`model_installs[]`), newest first.
struct FleetModelSpread: Decodable {
    var id: String?
    var packageId: String?
    var label: String?
    var state: String?
    var done: Int?
    var total: Int?
    var networks: [FleetSpreadNetwork]?

    var running: Bool { state == "running" }
    /// The action's name, on the Install sheet and on Fleet → Models.
    static let actionTitle = "Install on Every Mac That Can Run It"
}

// MARK: - Location

struct FleetLocation: Decodable {
    var id: String?
    var label: String?
    var url: String?
    var local: Bool?
    var reachable: Bool?
    var error: String?
    var role: String?
    var version: String?
    /// How this controller's Studio Hub version compares to the release this
    /// gateway build targets: `match`, `older`, `newer`, or `nil` when the Hub
    /// stated no version. **It is never a gate** — a Hub on another version keeps
    /// working (it auto-updates itself), and this is how the page says so instead
    /// of leaving a drift invisible.
    var hubVersionRelation: String?
    /// The controller's own Mac against the newest published release: `latest`, `updating`, `behind`,
    /// `unknown`, or nil when nothing was published (see `FleetMachine.versionStatus`).
    var versionStatus: String?
    var providerId: String?
    var latencyMs: Int?
    var machines: [FleetMachine] = []
    var workers: [FleetWorker] = []
    var localModels: [FleetLocalModel] = []
    /// The Hub's own per-(machine, model) verdict and reason (0.6.48).
    var modelHealth: [FleetModelHealth] = []
    var machineSummary: FleetMachineSummary?
    /// Normally a count. The gateway published this as an array of batch rows
    /// first, so either shape is accepted rather than failing the whole location.
    var runningJobs: Int?
    var modelsDiscovered: Int?
    var modelsEnabled: Int?
    var modelsPublishable: Int?
    /// **Who has joined this controller** — the Hub's `fleet/nodes` roster, one row
    /// per Mac, passed through by the gateway unchanged.
    ///
    /// Both this and `nodeSummary` were fetched per location since the node join
    /// landed and **never decoded here** (audit H10), so "able to see each member
    /// connected" meant opening Studio Hub on the controller itself. `nodes` is
    /// `[]` on a location whose roster could not be read; `nodeSummary` is `nil`
    /// there, which is the difference the Members column draws as an em dash.
    var nodes: [FleetNode] = []
    var nodeSummary: FleetNodeSummary?
    /// **The pause clock** the gateway keeps per controller (Q7): unreachable for the pause
    /// window and routing skips it — the row, credential and enable decisions stay. Epoch
    /// seconds; `pauseReason` is the gateway's own sentence ("resume it from the Fleet page").
    var pausedAt: Double?
    /// `owner` (paused by hand: grey, nothing wrong) or `outage` (the gateway's automatic pause after a long
    /// silence). `nil` from a gateway that predates the field, which only ever paused for an outage.
    var pausedBy: String?
    var pauseReason: String?
    var unreachableSince: Double?
    /// The controller is draining (finishing its work, taking none): routing skips it.
    var drained: Bool?
    /// Paused by the owner: it gets no new work, and it is not a fault.
    var isPausedByOwner: Bool { pausedAt != nil && pausedBy == "owner" }
    /// Set when the roster shown is the one remembered from before the group went quiet, and for how long.
    var membersFrom: String?
    var membersStaleSeconds: Double?
    /// In the roster, not read yet: just added, its first read still in flight.
    var pending: Bool?
    /// Did not answer in time; `error` says so and what it last reported is kept.
    var slow: Bool?
    /// The network its Macs are on unless one says otherwise (LAN model sharing); `nil` is not set.
    var network: String?
    /// The controller's own reachability history and what it means (see `FleetHistory`); `nil` from an
    /// older Gateway.
    var history: FleetHistory?
    /// Why this site is not answering, in the Gateway's words, with its evidence. Empty from an older Gateway.
    var causes: [FleetCause]?

    private enum CodingKeys: String, CodingKey {
        case drained, membersFrom, membersStaleSeconds, pending, slow, network
        case id, label, url, local, reachable, error, role, version, hubVersionRelation, versionStatus
        case providerId, latencyMs
        case machines, workers, localModels, modelHealth, machineSummary, runningJobs
        case modelsDiscovered, modelsEnabled, modelsPublishable
        case nodes, nodeSummary
        case pausedAt, pausedBy, pauseReason, unreachableSince
        case history, causes
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try? c.decode(String.self, forKey: .id)
        label = try? c.decode(String.self, forKey: .label)
        url = try? c.decode(String.self, forKey: .url)
        local = try? c.decode(Bool.self, forKey: .local)
        reachable = try? c.decode(Bool.self, forKey: .reachable)
        error = try? c.decode(String.self, forKey: .error)
        role = try? c.decode(String.self, forKey: .role)
        version = try? c.decode(String.self, forKey: .version)
        hubVersionRelation = try? c.decode(String.self, forKey: .hubVersionRelation)
        versionStatus = try? c.decode(String.self, forKey: .versionStatus)
        providerId = try? c.decode(String.self, forKey: .providerId)
        latencyMs = try? c.decode(Int.self, forKey: .latencyMs)
        machines = lenientArray(c, forKey: .machines)
        workers = lenientArray(c, forKey: .workers)
        localModels = lenientArray(c, forKey: .localModels)
        modelHealth = lenientArray(c, forKey: .modelHealth)
        machineSummary = try? c.decode(FleetMachineSummary.self, forKey: .machineSummary)
        nodes = lenientArray(c, forKey: .nodes)
        nodeSummary = try? c.decode(FleetNodeSummary.self, forKey: .nodeSummary)
        pausedAt = try? c.decode(Double.self, forKey: .pausedAt)
        pausedBy = try? c.decode(String.self, forKey: .pausedBy)
        pauseReason = try? c.decode(String.self, forKey: .pauseReason)
        unreachableSince = try? c.decode(Double.self, forKey: .unreachableSince)
        drained = try? c.decode(Bool.self, forKey: .drained)
        membersFrom = try? c.decode(String.self, forKey: .membersFrom)
        membersStaleSeconds = try? c.decode(Double.self, forKey: .membersStaleSeconds)
        pending = try? c.decode(Bool.self, forKey: .pending)
        slow = try? c.decode(Bool.self, forKey: .slow)
        network = try? c.decode(String.self, forKey: .network)
        history = try? c.decode(FleetHistory.self, forKey: .history)
        causes = try? c.decode([FleetCause].self, forKey: .causes)
        if let count = try? c.decode(Int.self, forKey: .runningJobs) {
            runningJobs = count
        } else if let rows = try? c.decode([Lenient<FleetJob>].self, forKey: .runningJobs) {
            runningJobs = rows.count
        } else {
            runningJobs = nil
        }
        modelsDiscovered = try? c.decode(Int.self, forKey: .modelsDiscovered)
        modelsEnabled = try? c.decode(Int.self, forKey: .modelsEnabled)
        modelsPublishable = try? c.decode(Int.self, forKey: .modelsPublishable)
    }
}

struct FleetMachineSummary: Decodable {
    var total: Int?
    var reachable: Int?
    var unreachable: Int?
}

// MARK: - Node (a Mac joined to a controller)

/// One Mac in a controller's roster, exactly as the controller states it.
///
/// This is the Hub's `fleet/nodes` row, which the gateway has carried per location
/// since the node join and nothing decoded (audit H10/M21). Every field is optional
/// for the reason the rest of this file gives: an older Hub carries fewer of them
/// (`last_seen_at`, `credential_issued_at` and `has_credential` are new in 0.6.91),
/// and a field the Hub did not state must leave the page able to say so rather than
/// blanking the roster.
///
/// **`state` is read verbatim and never collapsed.** `unreachable` is a Mac that did
/// not answer; `unauthorized` is a Mac that answered and refused *this controller* —
/// a different fault with a different fix, and the one the page used to render as
/// plain "offline".
struct FleetNode: Decodable {
    var id: String?
    var name: String?
    var address: String?
    /// The role the Mac joined with — `agent` on today's Hub. `nil` means the
    /// controller stated none, which is not the same as "not a member".
    var role: String?
    var studios: [FleetNodeStudio] = []
    var joinedAt: FleetScalar?
    /// **The answer to "is this Mac switched off, or gone?"** (audit M20.) The Hub
    /// stamps it on every probe that answers, so a Mac that is off keeps the last
    /// moment it was heard from rather than looking like one that was scrapped.
    var lastSeenAt: FleetScalar?
    /// When the controller issued this node its credential.
    var credentialIssuedAt: FleetScalar?
    /// Whether the controller still holds a credential for this Mac. `nil` on a Hub
    /// that does not state it — never read as `false`.
    var hasCredential: Bool?
    /// `reachable`, `unreachable`, `unauthorized` or `error`.
    var state: String?
    /// The controller's own sentence about reaching this node — quoted, because it
    /// names the fix ("re-join it with the current Fleet Token").
    var detail: String?
    var version: String?

    private enum CodingKeys: String, CodingKey {
        case id, name, address, role, studios, joinedAt, lastSeenAt, credentialIssuedAt
        case hasCredential, state, detail, version
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try? c.decode(String.self, forKey: .id)
        name = try? c.decode(String.self, forKey: .name)
        address = try? c.decode(String.self, forKey: .address)
        role = try? c.decode(String.self, forKey: .role)
        studios = lenientArray(c, forKey: .studios)
        joinedAt = try? c.decode(FleetScalar.self, forKey: .joinedAt)
        lastSeenAt = try? c.decode(FleetScalar.self, forKey: .lastSeenAt)
        credentialIssuedAt = try? c.decode(FleetScalar.self, forKey: .credentialIssuedAt)
        hasCredential = try? c.decode(Bool.self, forKey: .hasCredential)
        state = try? c.decode(String.self, forKey: .state)
        detail = try? c.decode(String.self, forKey: .detail)
        version = try? c.decode(String.self, forKey: .version)
    }

    private var word: String { (state ?? "").lowercased() }

    var isReachable: Bool { word == "reachable" }
    var isUnreachable: Bool { word == "unreachable" }
    /// **A refused credential, not a network fault.** The Mac answered and said no.
    var isUnauthorized: Bool { word == "unauthorized" }
    var isError: Bool { word == "error" }

    /// What the roster says about this Mac, in the page's own words.
    ///
    /// `unauthorized` deliberately does not read as "offline": the two have different
    /// causes and different fixes, and the whole point of the Hub's own state is that
    /// a token problem stops looking like a dead Mac.
    var stateText: String {
        switch word {
        case "reachable": return "reachable"
        case "unreachable": return "offline"
        case "unauthorized": return "credential refused"
        case "error": return "error"
        case "": return "—"
        default: return FleetFormat.words(state)
        }
    }

    /// `last_seen_at`, relative — the columns' own formatting of it. `—` when the
    /// Hub never stamped one, which for a node that has never answered is the truth.
    var lastSeenText: String { FleetFormat.relative(lastSeenAt?.doubleValue) }

    /// What this Mac runs, from the studios it declared when it joined.
    var studioText: String {
        let names = studios.compactMap { $0.modality ?? $0.id }.filter { !$0.isEmpty }
        return names.isEmpty ? "—" : names.joined(separator: ", ")
    }

    /// The Macs needing attention first, then the ones that are fine — the order a
    /// person reads a list in when they are looking for what to act on.
    var attentionRank: Int {
        if isUnauthorized { return 0 }
        if isError { return 1 }
        if isUnreachable { return 2 }
        if isReachable { return 3 }
        return 4
    }
}

/// One studio a node declared when it joined, e.g. `voice` / `transcription`.
struct FleetNodeStudio: Decodable {
    var id: String?
    var modality: String?
}

/// The controller's own count of its roster, per state.
///
/// The numbers are the Hub's (`fleet_nodes.probe`), never re-derived here: a node row
/// can be dropped as malformed without the count changing, and a re-derived total
/// would then disagree with the number the controller itself published.
struct FleetNodeSummary: Decodable {
    var total: Int?
    var reachable: Int?
    var unreachable: Int?
    var unauthorized: Int?
    var error: Int?
    /// Macs evicted from the group. They cannot re-join by themselves — a person has
    /// to re-admit one — so the count is worth showing beside the roster.
    var removed: Int?
}

/// Re-serialise an admin payload and decode it with the gateway's snake_case
/// convention, so the field names live in the `Decodable` types alone.
func fleetDecode<T: Decodable>(_ payload: [String: Any]) -> T? {
    guard JSONSerialization.isValidJSONObject(payload),
          let data = try? JSONSerialization.data(withJSONObject: payload) else { return nil }
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try? decoder.decode(T.self, from: data)
}

// MARK: - Machine

/// One top consumer of a Mac's disk, as the Hub reports it.
struct FleetDiskTop: Decodable {
    var name: String?
    var gb: Double?
}

/// One Hub warning about a Mac's disk, with the kind the gateway classified it as.
struct FleetWarningItem: Decodable {
    var kind: String
    var text: String
}

/// Which kinds of Fleet warning the owner hid (`GET /admin/api/settings/fleet-warnings`, also on the snapshot).
struct FleetWarningSetting: Decodable {
    var hidden: [String] = []
}

/// One Mac's disk as the gateway normalises it (`machine_disk` in `fleet.py`).
///
/// `state` is `green|amber|red|stop`. `stateSource` is `hub` when the Hub stated it and
/// `derived` when the gateway worked it out from the free/total sizes of a Hub that predates
/// the `disk` object; routing only ever acts on a stated `stop`. Every field is optional and
/// decoded on its own, so a Hub that adds or drops one never costs the machine its row.
struct FleetDisk: Decodable {
    var state: String?
    var stateSource: String?
    var freeGb: Double?
    var totalGb: Double?
    var trashGb: Double?
    var swapUsedGb: Double?
    var top: [FleetDiskTop] = []
    var lastSweep: FleetScalar?
    var warnings: [String] = []
    /// The same warnings with what each is about (`disk_space`, `macos_updates`, `time_machine`, `other`), so a
    /// banner can group them by kind. A gateway that predates the field gets a plain split between the two.
    var warningItems: [FleetWarningItem] = []

    private enum CodingKeys: String, CodingKey {
        case state, stateSource, freeGb, totalGb, trashGb, swapUsedGb, top, lastSweep, warnings, warningItems
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = try? c.decode(String.self, forKey: .state)
        stateSource = try? c.decode(String.self, forKey: .stateSource)
        freeGb = try? c.decode(Double.self, forKey: .freeGb)
        totalGb = try? c.decode(Double.self, forKey: .totalGb)
        trashGb = try? c.decode(Double.self, forKey: .trashGb)
        swapUsedGb = try? c.decode(Double.self, forKey: .swapUsedGb)
        top = (try? c.decode([FleetDiskTop].self, forKey: .top)) ?? []
        lastSweep = try? c.decode(FleetScalar.self, forKey: .lastSweep)
        warnings = (try? c.decode([String].self, forKey: .warnings)) ?? []
        warningItems = (try? c.decode([FleetWarningItem].self, forKey: .warningItems))
            ?? warnings.map { FleetWarningItem(kind: Self.guessKind($0), text: $0) }
    }

    private static func guessKind(_ text: String) -> String {
        let lowered = text.lowercased()
        return lowered.contains("free") && !lowered.contains("update") && !lowered.contains("time machine")
            ? "disk_space" : "other"
    }

    var isLow: Bool { ["amber", "red", "stop"].contains((state ?? "").lowercased()) }

    /// "models 61 GB, outputs 20 GB", or nil when the Hub named none.
    var topText: String? {
        let parts = top.compactMap { item -> String? in
            guard let name = item.name, !name.isEmpty else { return nil }
            return item.gb.map { "\(name) \(FleetFormat.gb($0))" } ?? name
        }
        return parts.isEmpty ? nil : parts.joined(separator: ", ")
    }

    /// What the Disk column draws: free space and the state beside it.
    var columnText: String {
        let free = FleetFormat.gb(freeGb)
        guard let state, !state.isEmpty else { return free }
        return free == "—" ? state : "\(free) · \(state)"
    }

    /// The state as the shared status vocabulary: amber is look soon, red and stop are act now.
    var health: Health? {
        switch (state ?? "").lowercased() {
        case "amber": return .warn
        case "red", "stop": return .bad
        case "green": return .ok
        default: return nil
        }
    }

    /// Text colour for the Disk column: plain unless the state is a problem.
    var colour: NSColor {
        guard let health, health != .ok else { return .labelColor }
        return health.text
    }
}

/// One model a Mac can be asked to install (`machines[].installable_models[]`).
struct FleetInstallableModel: Decodable {
    var packageId: String?
    var model: String?
    var label: String?
    var sizeGb: Double?

    var title: String {
        let name = label ?? model ?? packageId ?? "?"
        guard let size = sizeGb, size > 0 else { return name }
        return "\(name) — \(String(format: size < 10 ? "%.1f" : "%.0f", size)) GB"
    }
}

/// The job a node is running right now, **when the Hub states it** (Gateway `machines_flat` republishes
/// `current` and nothing else). Every field is optional: absent means unknown, never idle.
struct FleetCurrentJob: Decodable {
    var batchId: String?
    var model: String?
    var label: String?
    var studio: String?
    var startedAt: Double?
}

/// One Mac's network link as its own Hub reported it (gateway `fleet.machine_connection`).
///
/// **Why this exists.** A failing cable does not take a Mac offline: it *negotiates*, and a Mac
/// linked at 100 Mb/s — or 10 — answers every other field on this page as a healthy machine while
/// its work crawls. The owner measured 1301 at `100baseTX` on an old cable, `10baseT` before that,
/// and nothing here could show it. `kind` is the hardware port the Mac's own OS names for the
/// interface carrying its default route, `speedMbps`/`duplex` are the media that port says it
/// negotiated, and `text` is the Gateway's own sentence so every surface says the same thing.
///
/// **Unknown is not fine.** A Hub older than the block publishes none of it and this is `nil`, which
/// draws as an em dash — never as a gigabit link.
struct FleetConnection: Decodable {
    /// `measured` or `unmeasured`. Anything but `measured` means the Mac's Hub could not read its
    /// link, and `reason` says why in its own words.
    var status: String?
    var reason: String?
    var interface: String?
    /// `ethernet`, `wifi`, `other` — or nil when the Mac could not be read.
    var kind: String?
    var speedMbps: Int?
    /// `full` or `half`; nil when the OS stated none.
    var duplex: String?
    var media: String?
    var linkUp: Bool?
    var wifiUp: Bool?
    var wifiRateMbps: Int?
    /// Ethernet below 1000 Mb/s, or at half duplex: a cable or a port to look at, not a slow Mac.
    var slow: Bool?
    /// The Gateway's one-line sentence: "Ethernet 100 Mb/s half", "Wi-Fi 432 Mb/s", "—".
    var text: String?

    /// The reading could not be made — a different fact from a link whose speed is unknown.
    var measured: Bool { (status ?? "") == "measured" }

    /// What the Network cell draws: the sentence when the link was read, and an em dash when it was
    /// not. Never a number the Mac did not state.
    var display: String {
        guard measured else { return "—" }
        let text = (self.text ?? "").trimmingCharacters(in: .whitespaces)
        return text.isEmpty || text == "—" ? "—" : text
    }

    /// Amber on a slow Ethernet link, grey when the reading is missing, green otherwise. Wi-Fi is
    /// **not** amber: a Mac on Wi-Fi is a different fact and the cell says so instead of warning
    /// about a cable that is not there.
    var health: Health {
        guard measured else { return .off }
        if slow == true { return .warn }
        if (kind ?? "") == "wifi" { return .off }
        return .ok
    }

    /// The hover text: every fact the Mac stated, and the reason when it stated none.
    var tooltip: String {
        guard measured else {
            return reason ?? "This Mac's Hub did not report its link."
        }
        var lines: [String] = []
        if let kind, let interface {
            lines.append("\(kind == "ethernet" ? "Ethernet" : kind == "wifi" ? "Wi-Fi" : "Connection") on \(interface).")
        }
        if let speedMbps, let kind, kind == "ethernet" {
            lines.append("The port negotiated \(speedMbps) Mb/s" + (duplex.map { " \($0) duplex" } ?? "") + ".")
        }
        if let media { lines.append("Media: \(media).") }
        if let reason { lines.append(reason) }
        if wifiUp == true, (kind ?? "") != "wifi" {
            lines.append("Wi-Fi is up as well" + (wifiRateMbps.map { " (\($0) Mb/s)" } ?? "") + ".")
        }
        if slow == true {
            lines.append("Below 1000 Mb/s on Ethernet or at half duplex — check the cable and the switch port.")
        }
        return lines.isEmpty ? "The Mac reported a link with no details." : lines.joined(separator: "\n")
    }
}

/// The owner's threshold for calling a Mac *problematic* (`/admin/api/settings/fleet-health`).
///
/// Published with the bounds beside the value, so the page can offer the numbers the Gateway will
/// accept instead of guessing a range. `nil` from a Gateway older than 0.2.209.
struct FleetHealthSetting: Decodable {
    /// Drops in 24 h at which a Mac is called problematic.
    var flappingDropsDay: Int?
    var `default`: Int?
    var min: Int?
    var max: Int?
    var windowDays: Int?
}

/// One day of the seven-day strip. `offlineFraction` is `nil` for a day nobody observed — which is
/// **not** `0`: "nobody looked" and "it was up all day" must not draw the same bar.
struct FleetHistoryDay: Decodable {
    var date: String?
    var offlineFraction: Double?
}

/// What the Gateway saw when it last lost this Mac or site, in its own words.
struct FleetHistoryDrop: Decodable {
    var at: Double?
    /// `no_hub`, `unreachable`, `site_down`, `refused`, `timeout` … — the Gateway's vocabulary.
    var reason: String?
    /// The same reason as a sentence, or `nil` when it had none.
    var text: String?
    /// How long the Mac stayed unreachable, from the recorded up/down transitions. `nil` for a drop
    /// nobody watched end — "length not measured", never a short one.
    var durationSeconds: Double?
    /// The drop has not ended yet (the Mac is still unreachable).
    var ongoing: Bool?
    /// `after_startup_action` when the startup audit proves one within the Gateway's window,
    /// `by_itself` when it was watched come back without one, `unknown` when neither can be shown.
    var recoveredBy: String?
}

/// One likely cause, **with the evidence it came from**. `evidence` is never empty for a real hint:
/// a cause a reader cannot check is the thing the owner asked not to be given.
struct FleetCause: Decodable {
    var code: String?
    var text: String?
    var evidence: String?
}

/// A Mac's (or a site's) reachability over the last seven days, read by the Gateway on every
/// fan-out and derived fresh for each read.
///
/// Every figure is optional on purpose: `nil` means **not measured** and is drawn as an em dash,
/// never as a zero. `problematic` is the Gateway's verdict against the owner's own threshold, and
/// the app draws it as sent rather than deciding again from a number.
struct FleetHistory: Decodable {
    var observed: Bool?
    var online: Bool?
    var observedSince: Double?
    var observedAt: Double?
    var since: Double?
    var onlineSince: Double?
    var offlineSince: Double?
    /// Drops (up → down) in the last 24 hours and the last seven days. **A drop caused by the whole
    /// site being unreachable is not counted here** — that is the controller's fault, not this Mac's.
    var dropsDay: Int?
    var dropsWeek: Int?
    /// Every transition, both directions, over the same two windows.
    var flipsDay: Int?
    var flipsWeek: Int?
    /// **Each figure carries its own window.** `...Day` covers the last 24 hours and `...Week` the
    /// last seven days; they are never interchangeable, and the page prints the window beside each.
    var offlineSecondsDay: Double?
    var offlineSecondsWeek: Double?
    /// How long its drops lasted (median and longest), and the typical gap between them, from
    /// recorded transitions. `nil` when no drop has a measured end — never a zero.
    var dropMedianSeconds: Double?
    var dropLongestSeconds: Double?
    var dropGapSeconds: Double?
    var dropsCompleted: Int?
    var days: [FleetHistoryDay]?
    /// A recurring shape in the drops ("at about the same time every day"), or nil when there is none.
    var pattern: FleetCause?
    /// `true`, `false`, or `nil` for a subject nobody has observed yet.
    var problematic: Bool?
    /// The evidence behind `problematic` in one line: "4 drops in 24 h (threshold 4) · longest 2 min
    /// · median 1 min". A length nobody measured says so rather than reading as short.
    var problematicBasis: String?
    var lastDrop: FleetHistoryDrop?

    /// Has the Gateway ever observed this subject? `false` means every figure is unknown.
    var measured: Bool { observed == true }
}

/// What this Mac's macOS Trash holds (Studio Hub 0.6.243) — its own column, beside the Hub folder.
///
/// Every retirement a Hub makes ends in Finder's Trash, so a Mac whose Trash is never emptied fills
/// its disk even after the Studio Hub Trash Folder is empty. Both fields are optional: a Hub that
/// states neither leaves them `nil` — unknown, never drawn as 0.
struct FleetMacosTrash: Decodable {
    var count: Int?
    var bytes: Int64?

    private enum CodingKeys: String, CodingKey {
        case count, bytes
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        count = try? c.decode(Int.self, forKey: .count)
        bytes = try? c.decode(Int64.self, forKey: .bytes)
    }
}

struct FleetMachine: Decodable {
    var locationId: String?
    var location: String?
    var locationLocal: Bool?
    var locationReachable: Bool?
    var machine: String?
    var label: String?
    var state: String?
    var reachable: Bool?
    var completed: Int?
    var failed: Int?
    /// From when `completed`/`failed` count: the reset baseline when this Mac's Hub honoured it, nil when
    /// it counts its whole history (no reset, or a Hub too old for one).
    var countedSince: Double?
    var averageSeconds: Double?
    var medianRuntimeS: Double?
    var chip: String?
    var memoryGb: Double?
    /// What is free **now**, beside the total in `memoryGb`. The Hub's own
    /// admission uses this number, and a reader who sees only the total takes it
    /// for free memory — the column says "free" for disk and said nothing here.
    var availableGb: Double?
    /// The Gateway's sentence when this Mac's memory is a fault (see `faultMemoryHealth`); nil when it is not.
    var memoryFault: String?
    var diskFreeGb: Double?
    var diskTotalGb: Double?
    /// The Mac's disk state and top consumers (see `FleetDisk`); nil from a gateway that
    /// predates the field, in which case `diskFreeGb` still draws.
    var disk: FleetDisk?
    /// What the Hub moved into this Mac's macOS Trash (see `FleetMacosTrash`); nil from a Hub older
    /// than 0.6.243 that reports nothing — unknown, never 0.
    var macosTrash: FleetMacosTrash?
    /// How this Mac is connected — its interface, its kind and the media its Ethernet negotiated
    /// (see `FleetConnection`). `nil` from a Hub older than 0.6.206, which is unknown, never "fine".
    var connection: FleetConnection?
    var health: String?
    /// The node roster's own facts, when this machine is a node of a controller.
    /// `nodeDetail` is the controller's sentence about why it cannot reach the
    /// node — quoted, because it is the only thing that distinguishes a machine
    /// that never answered from one whose Hub is not running. `nil` for the
    /// controller's own Mac, which is not a node.
    var nodeState: String?
    var nodeDetail: String?
    var nodeRole: String?
    var nodeAddress: String?
    /// The owner paused this Mac by hand on its controller: no new work, still joined. `nil` when the Hub does
    /// not say (older than 0.6.135), which is never read as "not paused".
    var nodePaused: Bool?
    var nodeLastSeenAt: FleetScalar?
    /// What this Mac says it starts when it starts (see `FleetStartup.swift`). `nil` is unknown.
    var startup: FleetStartupFact?
    /// Why this Mac's Studio Hub last stopped, as the Mac recorded it (see `FleetLastStop.swift`); `nil` is unknown.
    var lastStop: FleetLastStop?
    /// Why the controller could not reach it - `no_hub` (the Mac answers, nothing listens) or `unreachable` - or `nil`.
    var reachReason: String?
    /// Workstation mode on this Mac (see `FleetWorkstation.swift`); `nil` when its Hub is too old for it.
    var workstation: FleetWorkstation?
    /// A Lite Mode change the Gateway keeps for this Mac while it is offline, sent when it is back; `nil` when none.
    var liteModePending: FleetLiteModePending?
    /// Switched off and in Lite Mode (last report, or a change waiting for it): expected, never a fault or offline.
    var liteOff: Bool?
    /// Ranked Studio Hub function ids for this Mac. Empty means every enabled function is equal;
    /// nil means this Gateway or Hub predates ranked work policy.
    var priorityOrder: [String]?
    /// Hub 0.6.156 compatibility field. New clients read `priorityOrder` exclusively.
    var chatPriority: Bool?
    /// What this node is running now, when its Hub says so (Fleet Groups "Doing now"); `nil` is unknown.
    var current: FleetCurrentJob?
    /// The models "Install a model…" offers this Mac (see `FleetModelInstall.swift`); `nil` from an older gateway.
    var installableModels: [FleetInstallableModel]?
    /// The network this Mac is on (its own, else its location's); `nil` is not set. `networkOverride` is its own.
    var network: String?
    var networkOverride: String?
    /// **Job counts per modality**, not a list of models the machine offers. The
    /// table labels the column "Work done by kind" so the two are never confused.
    var modalities: [String: Int]?
    /// The Hub's sentence when it could not fully read this machine's activity.
    var limitation: String?
    /// Hub 0.6.49's machine-readable reason beside that sentence. A closed set:
    /// `reporter_unavailable`, `partial_read`, `stale_snapshot`,
    /// `waiting_for_worker` — or null, which with a null sentence means the machine
    /// reports **fully**, not that nobody checked.
    var limitationCode: String?
    var limitationReason: String?
    var limitationSince: FleetScalar?
    /// The fields genuinely missing because of it, e.g. `["state", "median_runtime"]`.
    var limitationAffects: [String]?
    /// **Why** a Mac needs attention, in a few words the Gateway worked out ("Voice studio not running",
    /// "Last job failed: …"). Only set while `state` is `needs_attention`.
    var attention: String?
    /// This Mac's Studio Hub version, and how it stands against the newest release this Gateway published:
    /// `latest`, `updating` (its controller holds the release; it installs when idle), `behind`, `unknown`
    /// (not answering), or nil when nothing was published.
    var hubVersion: String?
    var versionStatus: String?
    /// This Mac's reachability over the last seven days: offline since, drops, a 7-day strip, and the
    /// `problematic` verdict (see `FleetHealth.swift`). `nil` from a Gateway older than 0.2.209 — which
    /// is unknown, never "fine".
    var history: FleetHistory?
    /// The likely cause, strongest first, each citing its evidence. Empty or absent from an older Gateway.
    var causes: [FleetCause]?
    /// Studios the controller lists for this Mac that the Mac does not run at all (an old roster entry).
    var notInstalledStudios: [String]?

    /// What the Limitation column draws: the Hub's phrase if it gave one, else its
    /// sentence, plus which readings are missing. A machine that reports fully
    /// draws an em dash, never an empty cell that could read as a glitch.
    var limitationText: String {
        var parts: [String] = []
        if let reason = limitationReason, !reason.isEmpty {
            parts.append(reason)
        } else if let limitation, !limitation.isEmpty {
            parts.append(limitation)
        } else {
            return "—"
        }
        if let code = limitationCode, !code.isEmpty {
            parts.append("(\(code))")
        }
        if let affects = limitationAffects, !affects.isEmpty {
            parts.append("missing \(affects.joined(separator: ", "))")
        }
        return parts.joined(separator: " · ")
    }

    /// Why this machine reads the way it does, in the Hub's own words.
    ///
    /// The Hub states a machine's health (`unreachable`, `no_hub`, `degraded` …)
    /// and, for a node, the controller's own sentence about reaching it. Both were
    /// fetched and neither was ever drawn, so eight dead nodes read as eight
    /// identical "offline" rows: the owner could see *that* they were down and
    /// never *why*. `nil` when the Hub stated neither, so an unknown never renders
    /// as a claim.
    ///
    /// **A refused credential is not a health problem, and the health word is not
    /// repeated beside it.** A node the controller marked `unauthorized` often also
    /// reads `unreachable` to the Hub's activity poll — that is the consequence, and
    /// printing both vocabularies for one fault is how the credential sentence ends
    /// up second.
    var stateReason: String? {
        var parts: [String] = []
        if nodeStateIsUnauthorized {
            parts.append("\(label ?? machine ?? "This node") answered but refused this controller's credential.")
        } else if let health, !health.isEmpty, health.lowercased() != "up" {
            parts.append("The Hub reports its health as ‘\(health)’.")
        }
        if let nodeDetail, !nodeDetail.isEmpty {
            parts.append(nodeDetail)
        }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }

    /// **Whether this machine is a node of the controller at all.**
    ///
    /// `machines_flat` joins the roster onto the machine by id and leaves every
    /// node field `nil` for a machine with no roster row — the controller's own Mac.
    /// Any one of them being present means there is a node row, so a Hub too old to
    /// state a `role` still marks its members rather than calling them the controller.
    var isNode: Bool {
        nodeState != nil || nodeRole != nil || nodeAddress != nil
            || nodeDetail != nil || nodeLastSeenAt != nil
    }

    /// The Hub's node state, verbatim and lowercased for comparison.
    private var nodeWord: String { (nodeState ?? "").lowercased() }

    /// The Mac answered and refused *this controller's* credential — a token fault,
    /// not a network one, and its own state so the two are never drawn alike.
    var nodeStateIsUnauthorized: Bool { nodeWord == "unauthorized" }

    /// What the Machines tab's Node column draws: the role the controller recorded,
    /// `member` when it recorded none, and `—` for the controller's own Mac.
    var nodeRoleText: String {
        guard isNode else { return "—" }
        guard let role = nodeRole, !role.trimmingCharacters(in: .whitespaces).isEmpty else {
            return "member"
        }
        return FleetFormat.words(role)
    }

    /// When the controller last heard from this Mac. `—` when it never has, which is
    /// a different fact from "a long time ago" and is what `last_seen_at` is for.
    var nodeLastSeenText: String { FleetFormat.relative(nodeLastSeenAt?.doubleValue) }
}

// MARK: - Disk and memory tiers

/// Disk and memory against the shared tiers. Disk uses the Hub disk governor's own state when the Hub
/// stated one (amber < 12 % free, red < 6 %, stop < 5 GB) and works the same thresholds out from the sizes
/// when it did not. Memory is the owner's rule: amber under 20 % of RAM free, red under 10 % or 1.5 GB.
extension FleetMachine {
    var diskFree: Double? { disk?.freeGb ?? diskFreeGb }
    var diskTotal: Double? { disk?.totalGb ?? diskTotalGb }

    var diskHealth: Health {
        if let stated = disk?.health { return stated }
        guard let free = diskFree, let total = diskTotal, total > 0 else { return .off }
        if free < 5 || free / total < 0.06 { return .bad }
        if free / total < 0.12 { return .warn }
        return .ok
    }

    var memoryHealth: Health {
        guard let available = availableGb, let total = memoryGb, total > 0 else { return .off }
        if available < 1.5 || available / total < 0.10 { return .bad }
        if available / total < 0.20 { return .warn }
        return .ok
    }

    /// The colour of the State badge. A Mac that is working is running a job, so it is amber like any running
    /// state; `activityHealth` (below) is the fault verdict that groups count, and a working Mac is no fault.
    var badgeHealth: Health { (state ?? "").lowercased() == "working" && nodePaused != true && reachable != false
        ? Health(state: "working") : activityHealth }

    /// How this Mac's activity reads: red for a refused credential or an offline Mac, amber when it needs
    /// attention (the Gateway only leaves that verdict standing with a cause it can name), grey for idle or
    /// unknown, green for ready or working. This is the fault verdict groups count; the State badge uses `badgeHealth`.
    var activityHealth: Health {
        let word = (state ?? "").lowercased()
        if nodePaused == true { return .off }   // paused by you: quiet grey, not a fault
        if liteModeResting || liteOff == true { return .off }   // the shop is using it, quiet hours, or switched off
        if nodeStateIsUnauthorized || word == "offline" || reachable == false { return .bad }
        if word == "needs_attention" { return .warn }
        if word == "long_idle" || word.isEmpty { return .off }
        return .ok
    }

    /// The Version badge: "Latest", "Updating…", "Behind — 0.6.126", "Unknown"; nil when the Gateway has
    /// published nothing to compare with (the cell then shows the bare version). `latest` is the newest
    /// published release, for the tooltip.
    func versionBadge(latest: String?) -> (text: String, health: Health, tooltip: String)? {
        let mine = hubVersion ?? "an unstated version"
        let newest = latest.map { "Studio Hub \($0)" } ?? "the newest release"
        switch (versionStatus ?? "").lowercased() {
        case "latest":
            return ("Latest", Health(state: "latest"), "Studio Hub \(mine): the newest release this Gateway published.")
        case "updating":
            return ("Updating…", Health(state: "updating"), "Studio Hub \(mine). Its controller holds \(newest); "
                    + "the Mac installs it by itself as soon as it is idle.")
        case "behind":
            return ("Behind — \(hubVersion ?? "?")", .warn, "Studio Hub \(mine), behind \(newest), and not updating "
                    + "by itself. Open Studio Hub on that Mac, or run “Update this Mac”.")
        case "unknown":
            return ("Unknown", .off, "This Mac is not answering, so its version is not known.")
        default:
            return nil
        }
    }

    /// The state word on a badge: Title Case, and **a refused credential is not "offline"** (different fault,
    /// different fix).
    var stateLabel: String {
        if nodePaused == true { return "Paused" }
        if let resting = workstationRestingLabel { return resting }
        if liteOff == true { return "Off" }
        if nodeStateIsUnauthorized { return "Credential refused" }
        if reachable == false { return "Offline" }
        switch (state ?? "").lowercased() {
        case "long_idle": return "Idle"
        case "offline": return "Offline"
        case "": return reachable == true ? "Ready" : "Unknown"
        default: return FleetFormat.words(state).capitalized
        }
    }

    /// "Apple M4 · 16 GB", the hardware in one phrase.
    var hardwareText: String {
        [chip, memoryGb.map { FleetFormat.gb($0).replacingOccurrences(of: ".0 GB", with: " GB") }]
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// Share of the disk in use, for the bar; `nil` when either size is unknown.
    var diskUsedFraction: Double? {
        guard let free = diskFree, let total = diskTotal, total > 0 else { return nil }
        return 1 - free / total
    }

    var memoryUsedFraction: Double? {
        guard let available = availableGb, let total = memoryGb, total > 0 else { return nil }
        return 1 - available / total
    }
}

// MARK: - Worker

struct FleetWorker: Decodable {
    var id: String?
    var studio: String?
    var modality: String?
    var machine: String?
    var online: Bool?
    var ready: Bool?
    var busy: Bool?
    var drained: Bool?
    var maintenance: Bool?
    var quarantined: Bool?
    var slots: Int?
    var slotsTotal: Int?
    /// Usually epoch seconds, but the Hub could spell it as text; see `FleetScalar`.
    var lastSeenAt: FleetScalar?
    /// What this worker actually serves, from its own catalogue (Hub 0.6.48).
    /// This is the answer to "which workers have this model" rather than an
    /// approximation by studio.
    var models: [String] = []
    var supportedOperations: [String] = []
}

// MARK: - Local model

struct FleetLocalModel: Decodable {
    var model: String?
    /// The Hub's canonical join id (0.6.48). It is what a supply row, a health row
    /// and a batch carry; `model` (the repo) is what this gateway publishes.
    var modelId: String?
    var label: String?
    var family: String?
    var studio: String?
    var modality: String?
    var machine: String?
    var kind: String?
    var sizeGb: Double?
    var minUnifiedMemoryGb: Double?
    var requiredFreeMemoryGb: Double?
    var memoryEligible: Bool?
    var runtimeReady: Bool?
    var available: Bool?
    var loaded: Bool?
    var fit: FleetFit?
    var cacheState: String?
    var catalogError: String?
    var catalogStale: Bool?
    /// A download flag is a **boolean** on today's gateway. It is read as a
    /// `FleetScalar` because reading it as one fixed Swift type would fail the
    /// whole model row the day it becomes a download id — and a dropped row is a
    /// machine that silently disappears from the Models tab.
    var activeDownload: FleetScalar?
    var gated: Bool?
    var dispatchState: String?
    /// What the Hub installs this model by ("Install on every Mac that can run it").
    var packageId: String?
    /// Not in the hub's per-machine row: the location a row belongs to. It is
    /// decoded when the gateway publishes a flat `models` list and injected from
    /// the parent location when the row is nested, so the master table can answer
    /// "how many locations offer this model".
    var locationId: String?
    var location: String?
}

struct FleetFit: Decodable {
    var state: String?
    var label: String?
    var hint: String?
    var actualGb: Double?
    var requiredGb: Double?
}

// MARK: - Job

struct FleetJob: Decodable {
    /// The location's id and provider id: a cancel has to name the controller
    /// whose Hub holds the batch, and a label is not an identity.
    var locationId: String?
    var providerId: String?
    var location: String?
    var locationLocal: Bool?
    /// The Hub's batch id — the handle on work this gateway did not itself create.
    var batchId: String?
    var modality: String?
    var model: String?
    var label: String?
    var total: Int?
    var running: Int?
    var queued: Int?
    var retrying: Int?
    var done: Int?
    /// **The Hub's `error` is a count of failed items, not a sentence.** Measured
    /// live: `"error": 0` beside `"queued"/"running"`. Typed `String?` it decoded
    /// to nothing, so the "Needs attention" card was blank on exactly the batches
    /// it exists for. A lenient scalar reads whichever the Hub states.
    var error: FleetScalar?
    var stalled: Bool?
    var stalledAfterS: Double?
    var noProgressS: Double?
    var nextRetryAt: Double?
    var uncertain: Int?
    var cancelRequested: Int?
    var cancelled: Bool?
    var cancelledItems: Int?
    var routing: String?
    /// The Hub's own reason a batch is being held.
    var governorNote: String?
    /// The canonical model id (0.6.48), the same string a model and health row use.
    var modelId: String?
    /// **Per-item failure text (0.6.48).** The batch's `error` is only a count, so
    /// this is the one place the list route says *what* failed and which item — and
    /// the item index is what a retry addresses. Optional because a 0.6.47 Hub
    /// sends no such key at all, and a non-optional array would drop the row.
    var errors: [FleetJobError]?
    var avgS: Double?
    var startedAt: Double?

    var failedItems: [FleetJobError] { errors ?? [] }

    var errorCount: Int? {
        guard let error else { return nil }
        if case .text(let text) = error { return Int(text) }
        return error.doubleValue.map { Int($0) }
    }

    var errorText: String? {
        guard case .text(let text)? = error, Int(text) == nil, !text.isEmpty else { return nil }
        return text
    }

    /// A stop can be asked for only while the Hub still has the batch and has not
    /// already been asked. Offering it after a request is how a button lies.
    var canCancel: Bool {
        guard let batchId, !batchId.isEmpty,
              let locationId, !locationId.isEmpty else { return false }
        return cancelled != true && (cancelRequested ?? 0) == 0
    }

    /// A failed item can be retried only on a batch the Hub still holds. The Hub
    /// accepts only items in state `error`, so the page offers Retry on exactly
    /// those and lets the Hub refuse the rest with its own words.
    func canRetry(_ item: FleetJobError) -> Bool {
        guard let batchId, !batchId.isEmpty,
              let locationId, !locationId.isEmpty else { return false }
        return cancelled != true && item.itemIndex != nil
    }
}

/// One failed item of a batch, as the Hub's `errors[]` states it (0.6.48).
struct FleetJobError: Decodable {
    var itemIndex: Int?
    var code: String?
    var message: String?

    /// What a card shows for this item: the Hub's message, else its code, else a
    /// plain statement that the item failed — never an empty line.
    var text: String {
        if let message, !message.trimmingCharacters(in: .whitespaces).isEmpty { return message }
        if let code, !code.isEmpty { return code }
        return "this item failed"
    }
}

// MARK: - Model health

/// The Hub's own per-(machine, model) verdict, with its reason (0.6.48).
///
/// `state` is a closed vocabulary from the Hub: ready, download_required,
/// runtime_missing, unsupported_os, unsupported_chip, insufficient_memory,
/// quarantined, unknown. Read verbatim; nothing is mapped to a nicer word, and a
/// missing state stays unknown rather than becoming "fine".
struct FleetModelHealth: Decodable {
    var modelId: String?
    var machine: String?
    var state: String?
    var reason: String?
    var hint: String?
    var observedAt: Double?
    /// Not in the hub's row: the location it belongs to, attached by the client.
    var locationId: String?
    var location: String?

    var isReady: Bool { (state ?? "").lowercased() == "ready" }
}

// MARK: - Issue

struct FleetIssue: Decodable {
    var severity: String?
    var code: String?
    var locationId: String?
    var location: String?
    var subject: String?
    var message: String?
    var action: String?
}

// MARK: - Defensive decoding

/// One element that will not decode becomes `nil` instead of failing the list.
///
/// A single bad row — a string where a number was expected, a shape that moved —
/// used to be able to hide every other location on the page. This is the smallest
/// wrapper that stops that.
struct Lenient<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}

private func lenientArray<T: Decodable, K: CodingKey>(
    _ container: KeyedDecodingContainer<K>, forKey key: K
) -> [T] {
    guard let wrapped = try? container.decode([Lenient<T>].self, forKey: key) else { return [] }
    return wrapped.compactMap { $0.value }
}

/// A value whose JSON type the gateway may change without notice.
///
/// `active_download` is a boolean today and could be a download id tomorrow; a
/// worker's `last_seen_at` is normally an epoch-seconds number but a Hub could
/// spell it as text. Decoding either as one fixed Swift type throws, and because
/// model and worker rows are dropped individually when they fail, the whole
/// machine would vanish from the page. This keeps whichever of the three JSON
/// primitives actually arrived.
enum FleetScalar: Decodable {
    case bool(Bool)
    case number(Double)
    case text(String)

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(Bool.self) { self = .bool(value); return }
        if let value = try? container.decode(Double.self) { self = .number(value); return }
        if let value = try? container.decode(String.self) { self = .text(value); return }
        self = .bool(false)
    }

    var display: String {
        switch self {
        case .bool(let value): return value ? "yes" : "no"
        case .number(let value):
            return value == value.rounded()
                ? String(format: "%.0f", value)
                : String(format: "%.1f", value)
        case .text(let value): return value
        }
    }

    var doubleValue: Double? {
        if case .number(let value) = self { return value }
        if case .text(let value) = self { return Double(value) }
        return nil
    }

    var isTrue: Bool {
        switch self {
        case .bool(let value): return value
        case .number(let value): return value != 0
        case .text(let value): return ["1", "true", "yes", "on"].contains(value.lowercased())
        }
    }
}

// MARK: - Derived views over the snapshot

extension FleetSnapshot {
    /// Every local-model row on the page, with its location attached.
    ///
    /// Rows normally arrive nested under their location. A gateway that also
    /// publishes a flat top-level `models` list is read the same way, and the two
    /// sources are merged by (location, machine, model) so both together do not
    /// double-count a machine.
    var allLocalModels: [FleetLocalModel] {
        var out: [FleetLocalModel] = []
        var seen = Set<String>()

        func key(_ row: FleetLocalModel) -> String {
            "\(row.locationId ?? "")|\(row.machine ?? "")|\(row.model ?? "")"
        }

        for location in locations {
            for var row in location.localModels {
                row.locationId = row.locationId ?? location.id
                row.location = row.location ?? location.label
                let k = key(row)
                if seen.insert(k).inserted { out.append(row) }
            }
        }
        for var row in models {
            if row.location == nil, let id = row.locationId {
                row.location = locations.first { $0.id == id }?.label
            }
            let k = key(row)
            if seen.insert(k).inserted { out.append(row) }
        }
        return out
    }

    /// Every model-health row, with its location attached, keyed for the Models
    /// detail table by (location, machine, model).
    ///
    /// The Hub keys health by the canonical `model_id` while a model row may carry
    /// only the repo; both spellings are indexed so a lookup finds the row either
    /// way, and a health row for a model the Hub names differently is not lost.
    var modelHealthIndex: [String: FleetModelHealth] {
        var index: [String: FleetModelHealth] = [:]
        for location in locations {
            for var row in location.modelHealth {
                row.locationId = row.locationId ?? location.id
                row.location = row.location ?? location.label
                for key in [
                    "\(location.id ?? "")|\(row.machine ?? "")|\(row.modelId ?? "")",
                    "\(row.location ?? "")|\(row.machine ?? "")|\(row.modelId ?? "")",
                ] {
                    index[key] = row
                }
            }
        }
        return index
    }

    /// Worker modalities per machine, keyed by whatever the worker rows name.
    /// This fills the Machines tab's "Studios" column — which workers a machine
    /// actually serves — rather than the job counts, which answer a different
    /// question and are labelled separately. The Hub names a machine by id in
    /// these rows, so the caller looks up by id first and the label second.
    var studiosByMachine: [String: [String]] {
        var map: [String: Set<String>] = [:]
        for location in locations {
            for worker in location.workers {
                guard let machine = worker.machine, !machine.isEmpty,
                      let modality = worker.modality, !modality.isEmpty else { continue }
                map[machine, default: []].insert(modality)
            }
        }
        return map.mapValues { $0.sorted() }
    }
}

extension FleetLocation {
    /// Distinct models this location reports.
    ///
    /// The gateway publishes one row per (model, machine), so "discovered" is the
    /// distinct model count. It is derived only when the gateway does not state
    /// `models_discovered` itself, so a payload with the explicit count still wins.
    var derivedModelsDiscovered: Int {
        Set(localModels.compactMap { $0.model }).count
    }

    /// Distinct models at least one machine can dispatch right now: its package
    /// is `eligible`, or the machine reports it `available`. The fallback for a
    /// gateway that sends the model rows but no per-location offer count.
    var derivedModelsOffered: Int {
        var offered = Set<String>()
        for row in localModels where row.dispatchState == "eligible" || row.available == true {
            if let model = row.model { offered.insert(model) }
        }
        return offered.count
    }

    // MARK: Members

    /// **How many of this controller's joined Macs are answering, out of how many.**
    ///
    /// `nil` — drawn as an em dash — when the controller stated no roster summary at
    /// all: a location that could not be read, or a Hub older than the route. That is
    /// deliberately *not* `0 / 0`, which is a claim about a fleet nobody counted.
    var memberCount: (reachable: Int, total: Int)? {
        guard let summary = nodeSummary, let total = summary.total,
              let reachable = summary.reachable else { return nil }
        return (reachable, total)
    }

    /// What the Members column draws: `reachable / total`, and the refused count
    /// spelled out when there is one, because "4 / 5" does not say which Mac needs a
    /// token re-entered and "1 refused" does.
    var membersText: String {
        guard let count = memberCount else { return "—" }
        let refused = nodeSummary?.unauthorized ?? 0
        return refused > 0 ? "\(count.reachable) / \(count.total) · \(refused) refused"
                           : "\(count.reachable) / \(count.total)"
    }

    /// The roster itself, ordered so the Macs that need someone come first: a refused
    /// credential, then an error, then a Mac that is off, then the ones that are fine.
    var nodesByAttention: [FleetNode] {
        nodes.sorted {
            if $0.attentionRank != $1.attentionRank { return $0.attentionRank < $1.attentionRank }
            return ($0.name ?? $0.id ?? "") < ($1.name ?? $1.id ?? "")
        }
    }

    /// The Members cell's tooltip: the controller's own counts, then one line per
    /// joined Mac with its state, when it was last seen and the controller's sentence.
    ///
    /// A controller with fifty-three nodes would otherwise fill the screen, so the
    /// list is capped — `ponytail:` a fixed cap is enough for a tooltip; the day this
    /// needs to be read in full it wants a Members window, not a longer tooltip.
    var membersTooltip: String? {
        guard nodeSummary != nil || !nodes.isEmpty else { return nil }
        var lines: [String] = []
        if let summary = nodeSummary, let total = summary.total {
            var head = "\(summary.reachable ?? 0) of \(total) joined "
                + (total == 1 ? "Mac is" : "Macs are") + " reachable now."
            if let refused = summary.unauthorized, refused > 0 {
                head += " \(refused) answered and refused this controller's credential."
            }
            if let removed = summary.removed, removed > 0 {
                head += " \(removed) removed from the group."
            }
            lines.append(head)
        } else {
            lines.append("The controller did not state how many Macs have joined it.")
        }
        let ordered = nodesByAttention
        let shown = ordered.prefix(FleetLocation.membersTooltipLimit)
        for node in shown {
            var line = "\(node.name ?? node.id ?? "a Mac") — \(node.stateText)"
            if node.lastSeenAt?.doubleValue != nil {
                line += " · last seen \(node.lastSeenText)"
            }
            if let detail = node.detail, !detail.isEmpty {
                line += "\n    \(detail)"
            }
            lines.append(line)
        }
        if ordered.count > shown.count {
            lines.append("… and \(ordered.count - shown.count) more.")
        }
        return lines.joined(separator: "\n")
    }

    /// How many roster rows a tooltip lists before it summarises the rest.
    static let membersTooltipLimit = 12
}

// MARK: - Formatting and ranking

enum FleetFormat {
    /// A count that is honestly "unknown" when the gateway did not send it.
    static func count(_ value: Int?) -> String {
        guard let value else { return "—" }
        return "\(value)"
    }

    static func gb(_ value: Double?) -> String {
        guard let value else { return "—" }
        return value >= 100 ? String(format: "%.0f GB", value) : String(format: "%.1f GB", value)
    }

    /// How long something has been going, for "Doing now": `45 s`, `12 min`, `1 h 05 min`.
    static func elapsed(_ seconds: Double?) -> String? {
        guard let seconds, seconds >= 0 else { return nil }
        if seconds < 60 { return "\(Int(seconds)) s" }
        let minutes = Int(seconds / 60)
        if minutes < 60 { return "\(minutes) min" }
        return String(format: "%d h %02d min", minutes / 60, minutes % 60)
    }

    /// Seconds for a runtime, minutes and seconds once it needs them.
    static func seconds(_ value: Double?) -> String {
        guard let value else { return "—" }
        if value < 60 { return String(format: "%.1f s", value) }
        let minutes = Int(value) / 60
        return "\(minutes)m \(Int(value) % 60)s"
    }

    /// "4 Oct 18:40": when the job counters were reset.
    static func stamp(_ epoch: Double) -> String {
        let format = DateFormatter()
        format.dateFormat = "d MMM HH:mm"
        return format.string(from: Date(timeIntervalSince1970: epoch))
    }

    static func relative(_ epoch: Double?) -> String {
        GatewayAPI.relativeTime(epoch)
    }

    static func text(_ value: String?) -> String {
        guard let value, !value.trimmingCharacters(in: .whitespaces).isEmpty else { return "—" }
        return value
    }

    /// `insufficient_memory` reads as "insufficient memory".
    static func words(_ value: String?) -> String {
        guard let value, !value.isEmpty else { return "—" }
        return value.replacingOccurrences(of: "_", with: " ")
    }

    /// Issue severity, ordered so the strip can put errors first.
    static func severityRank(_ severity: String?) -> Int {
        switch severity?.lowercased() {
        case "error", "critical": return 0
        case "warning", "warn": return 1
        case "info", "notice": return 2
        default: return 3
        }
    }

    static func severityColour(_ severity: String?) -> NSColor {
        Health(severity: severity).tint
    }

    static func severitySymbol(_ severity: String?) -> String {
        switch severity?.lowercased() {
        case "error", "critical": return "exclamationmark.octagon.fill"
        case "warning", "warn": return "exclamationmark.triangle.fill"
        default: return "info.circle.fill"
        }
    }
}

extension FleetLocalModel {
    /// How strong this row's fit is, for picking a "best fit" across machines.
    ///
    /// Ordered by what actually serves work — available beats eligible beats
    /// merely runtime-ready — so the master table's "best fit" is the strongest
    /// machine's, never an arbitrary row's.
    var fitScore: Int {
        var score = 0
        if available == true { score += 8 }
        if memoryEligible == true { score += 4 }
        if runtimeReady == true { score += 2 }
        if (catalogError ?? "").isEmpty { score += 1 }
        return score
    }

    var fitLabel: String {
        if let label = fit?.label, !label.isEmpty { return label }
        if let state = fit?.state, !state.isEmpty { return FleetFormat.words(state) }
        if available == true { return "available" }
        if memoryEligible == true { return "fits" }
        return "—"
    }

    var memoryFloor: Double? {
        requiredFreeMemoryGb ?? minUnifiedMemoryGb
    }

    var cacheIsCached: Bool {
        (cacheState ?? "").lowercased() == "cached"
    }

    var issueText: String? {
        guard let error = catalogError, !error.trimmingCharacters(in: .whitespaces).isEmpty else {
            return nil
        }
        return error
    }
}
