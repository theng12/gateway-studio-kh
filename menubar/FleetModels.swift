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

    private enum CodingKeys: String, CodingKey {
        case generatedAt, summary, locations, machines, running, issues, models
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
    var runningJobs: Int?
    var probeMachinesHidden: Int?
    var localModelsTotal: Int?
    var modelsAvailable: Int?
    var workersTotal: Int?
    var workersReady: Int?
    var issues: Int?
    /// The Hub's own per-state machine counts. Kept because the pulse line is
    /// read verbatim from the gateway rather than re-derived here.
    var pulse: [String: Int]?
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

    private enum CodingKeys: String, CodingKey {
        case id, label, url, local, reachable, error, role, version, providerId, latencyMs
        case machines, workers, localModels, modelHealth, machineSummary, runningJobs
        case modelsDiscovered, modelsEnabled, modelsPublishable
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
        providerId = try? c.decode(String.self, forKey: .providerId)
        latencyMs = try? c.decode(Int.self, forKey: .latencyMs)
        machines = lenientArray(c, forKey: .machines)
        workers = lenientArray(c, forKey: .workers)
        localModels = lenientArray(c, forKey: .localModels)
        modelHealth = lenientArray(c, forKey: .modelHealth)
        machineSummary = try? c.decode(FleetMachineSummary.self, forKey: .machineSummary)
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

// MARK: - Machine

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
    var averageSeconds: Double?
    var medianRuntimeS: Double?
    var chip: String?
    var memoryGb: Double?
    var diskFreeGb: Double?
    var health: String?
    /// **Job counts per modality**, not a list of models the machine offers. The
    /// table labels the column "Work done by kind" so the two are never confused.
    var modalities: [String: Int]?
    var limitation: String?
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

    /// Seconds for a runtime, minutes and seconds once it needs them.
    static func seconds(_ value: Double?) -> String {
        guard let value else { return "—" }
        if value < 60 { return String(format: "%.1f s", value) }
        let minutes = Int(value) / 60
        return "\(minutes)m \(Int(value) % 60)s"
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
        switch severity?.lowercased() {
        case "error", "critical": return .systemRed
        case "warning", "warn": return .systemOrange
        case "info", "notice": return .systemBlue
        default: return .secondaryLabelColor
        }
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
