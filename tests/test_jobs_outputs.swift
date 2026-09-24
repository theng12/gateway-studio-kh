import AppKit

protocol AppSection: AnyObject {
    var title: String { get }
    var symbol: String { get }
    var view: NSView { get }
    var refreshesAutomatically: Bool { get }
    func activated()
}

final class GatewayController {
    static let shared = GatewayController()
    let port = 43123
}

enum GatewayAPIError: LocalizedError {
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .transport(let detail): return detail
        }
    }
}

enum GatewayAPI {
    static var jobs: [[String: Any]] = []
    static var getPaths: [String] = []
    static var postPaths: [String] = []
    static var postBodies: [[String: Any]] = []
    static var downloads: [(String, (Result<Data, GatewayAPIError>) -> Void)] = []
    static var deferJobs = false
    static var deferredJobsCompletion: ((Result<[String: Any], Error>) -> Void)?
    static var deferCancel = false
    static var deferredCancelCompletion: ((Result<[String: Any], Error>) -> Void)?

    static func get(
        _ path: String,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        getPaths.append(path)
        if path == "/admin/api/artifacts/kept" {
            completion(.success([
                "artifacts": [["job_id": "job-output", "asset_id": "asset-missing"]]
            ]))
        } else if path.hasPrefix("/admin/api/create/status/") {
            let jobID = String(path.dropFirst("/admin/api/create/status/".count))
            let job = jobs.first { $0["id"] as? String == jobID } ?? [:]
            completion(.success(job))
        } else {
            if deferJobs {
                deferredJobsCompletion = completion
                return
            }
            completion(.success(["jobs": jobs, "stats": ["total": jobs.count]]))
        }
    }

    static func post(
        _ path: String,
        body: [String: Any]? = nil,
        completion: ((Result<[String: Any], Error>) -> Void)? = nil
    ) {
        postPaths.append(path)
        postBodies.append(body ?? [:])
        if path.hasSuffix("/cancel") {
            if deferCancel {
                deferredCancelCompletion = completion
                return
            }
            completion?(.success([
                "id": "job-running",
                "state": "running",
                "cancel_requested": true,
                "cancel_state": "requested",
            ]))
        } else {
            completion?(.success(["pinned": body?["keep"] as? Bool ?? false]))
        }
    }

    static func download(
        _ path: String,
        completion: @escaping (Result<Data, GatewayAPIError>) -> Void
    ) {
        downloads.append((path, completion))
    }

    static func relativeTime(_ timestamp: Double?) -> String { "now" }
    static func duration(_ seconds: Double?) -> String { "1s" }
}

@main
struct JobsOutputsCheck {
    static let jobQueued: [String: Any] = [
        "id": "job-queued",
        "state": "queued",
        "modality": "image",
        "model_name": "image-model",
        "provider_name": "Mock",
        "created_at": 1.0,
        "outputs": [],
        "cancel_requested": false,
        "cancel_state": "queued",
        "retryable": false,
        "recovery_required": false,
        "recovery_state": NSNull(),
        "recovery_detail": NSNull(),
        "upstream_job_id": NSNull(),
        "late_result_state": NSNull(),
    ]

    /// An uncertain Hub attempt: the state a recovery hold is in, with the
    /// upstream identifiers the discard call has to name.
    static let jobUncertain: [String: Any] = [
        "id": "job-uncertain",
        "state": "failed",
        "modality": "music",
        "model_name": "music-model",
        "provider_name": "Studio Hub",
        "created_at": 1.0,
        "outputs": [],
        "error_code": "UNKNOWN_OUTCOME",
        "error_detail": "the Hub settled the item without knowing the worker's outcome",
        "cancel_requested": false,
        "cancel_state": NSNull(),
        "retryable": false,
        "recovery_required": true,
        "recovery_state": "unknown_outcome",
        "recovery_detail": "Studio Hub settled batch hub-batch-9 item 0.",
        "recovery_action": "discard_upstream",
        "recovery_context": ["hub_batch_id": "hub-batch-9", "hub_item_index": 0],
        "discard_remote": NSNull(),
        "upstream_job_id": "hub-batch-9",
        "late_result_state": NSNull(),
    ]

    /// A hold recorded before the gateway kept that context: nothing to name, so
    /// offering a discard would fail on press. The gateway publishes no action for
    /// it, which is what the window must obey.
    static let jobHoldWithoutContext: [String: Any] = [
        "id": "job-hold-old",
        "state": "failed",
        "modality": "image",
        "model_name": "image-model",
        "provider_name": "Studio Hub",
        "created_at": 1.0,
        "outputs": [],
        "error_code": "UNKNOWN_OUTCOME",
        "error_detail": "an older hold",
        "cancel_requested": false,
        "cancel_state": NSNull(),
        "retryable": false,
        "recovery_required": true,
        "recovery_state": "unknown_outcome",
        "recovery_detail": "no upstream identifiers were recorded",
        "recovery_action": NSNull(),
        "recovery_context": NSNull(),
        "discard_remote": NSNull(),
        "upstream_job_id": NSNull(),
        "late_result_state": NSNull(),
    ]

    /// A **cloud** hold: the provider accepted the request and the gateway
    /// restarted before it settled. No cloud lane has a discard route, so the
    /// gateway publishes `close_hold` and the operator closes the record locally —
    /// the action that did not exist at all, which left this job stuck for good.
    static let jobCloudHold: [String: Any] = [
        "id": "job-cloud-hold",
        "state": "failed",
        "modality": "chat",
        "model_name": "deepseek-v4-flash",
        "provider_name": "OpenRouter",
        "created_at": 1.0,
        "outputs": [],
        "error_code": "UNKNOWN_OUTCOME",
        "error_detail": "The gateway restarted while this job was in flight.",
        "cancel_requested": false,
        "cancel_state": NSNull(),
        "retryable": false,
        "recovery_required": true,
        "recovery_state": "unknown_outcome",
        "recovery_detail": "Upstream job id: gen-abc123.",
        "recovery_action": "close_hold",
        "recovery_context": NSNull(),
        "discard_remote": NSNull(),
        "upstream_job_id": "gen-abc123",
        "late_result_state": NSNull(),
    ]

    static let jobRunning: [String: Any] = [
        "id": "job-running",
        "state": "running",
        "modality": "chat",
        "model_name": "chat-model",
        "provider_name": "Mock",
        "created_at": 1.0,
        "outputs": [],
        "output_text": "A chat output that remains revisitable.",
        "cancel_requested": true,
        "cancel_state": "requested",
        "retryable": true,
        "recovery_required": true,
        "recovery_state": "manual_review",
        "recovery_detail": "The upstream request may still settle late.",
        "upstream_job_id": "upstream-1",
        "late_result_state": "unknown",
    ]

    static let jobOutputs: [[String: Any]] = [
        [
            "asset_id": "asset-present",
            "url": "/admin/api/artifacts/job-output/outputs/asset-present",
            "media_type": "audio/mpeg",
            "bytes": 12,
            "present": true,
            "kept": false,
        ],
        [
            "asset_id": "asset-missing",
            "url": "/admin/api/artifacts/job-output/outputs/asset-missing",
            "media_type": "image/png",
            "bytes": 33,
            "present": false,
            "kept": true,
        ],
    ]

    static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }

    static func labels(_ view: NSView) -> [String] {
        descendants(view).compactMap { ($0 as? NSTextField)?.stringValue }
    }

    static func buttons(_ view: NSView) -> [NSButton] {
        descendants(view).compactMap { $0 as? NSButton }
    }

    static func main() {
        _ = NSApplication.shared

        let outputJob: [String: Any] = [
            "id": "job-output",
            "state": "succeeded",
            "modality": "chat",
            "model_name": "chat-model",
            "provider_name": "Mock",
            "created_at": 1.0,
            "outputs": jobOutputs,
            "output_text": "A chat output that remains revisitable.",
            "cancel_requested": false,
            "cancel_state": "none",
            "retryable": false,
            "recovery_required": false,
            "recovery_state": NSNull(),
            "recovery_detail": NSNull(),
            "upstream_job_id": NSNull(),
            "late_result_state": NSNull(),
        ]
        GatewayAPI.jobs = [
            outputJob, jobRunning, jobQueued, jobUncertain, jobHoldWithoutContext, jobCloudHold,
        ]

        let section = JobsSection()
        _ = section.view
        section.activated()

        let table = descendants(section.view).compactMap { $0 as? NSTableView }.first!
        assert(table.numberOfRows == 6, "jobs did not load")
        table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))

        let outputLabels = labels(section.view)
        assert(outputLabels.contains("A chat output that remains revisitable."), "chat output missing")
        assert(GatewayAPI.getPaths.contains("/admin/api/create/status/job-output"), "status detail endpoint missing")
        assert(outputLabels.contains { $0.contains("Missing file") }, "missing-file state missing")
        assert(buttons(section.view).contains { $0.title == "Preview" }, "preview action missing")
        assert(buttons(section.view).contains { $0.title == "Save…" }, "save action missing")
        assert(buttons(section.view).contains { $0.title == "Open" }, "open action missing")
        assert(buttons(section.view).contains { $0.title == "Pin" }, "pin action missing")

        let preview = buttons(section.view).first { $0.title == "Preview" }!
        preview.performClick(nil)
        assert(GatewayAPI.downloads.count == 1, "preview did not use download helper")
        let staleCompletion = GatewayAPI.downloads[0].1

        table.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        let runningLabels = labels(section.view)
        assert(runningLabels.contains { $0.localizedCaseInsensitiveContains("cancellation requested") }, "running cancellation state missing")
        assert(runningLabels.contains { $0.contains("The upstream request may still settle late.") }, "recovery detail missing: \(runningLabels)")
        assert(runningLabels.contains { $0.contains("manual") }, "recovery state missing")

        staleCompletion(.success(Data("stale-output".utf8)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(labels(section.view).contains { $0.contains("The upstream request may still settle late.") }, "stale output replaced selected job: \(labels(section.view))")

        // ── an uncertain Hub hold: offered, confirmed, then discarded ──
        table.selectRowIndexes(IndexSet(integer: 3), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(labels(section.view).contains { $0.contains("hub-batch-9") },
               "the hold should name the attempt it is waiting on")
        assert(buttons(section.view).contains { $0.title.hasPrefix("Discard") },
               "a discardable hold must offer the discard action: \(buttons(section.view).map { $0.title })")

        let postsBefore = GatewayAPI.postPaths.count
        section.confirmDiscard = { false }
        buttons(section.view).first { $0.title.hasPrefix("Discard") }!.performClick(nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(GatewayAPI.postPaths.count == postsBefore,
               "declining the confirmation must discard nothing: \(GatewayAPI.postPaths)")

        section.confirmDiscard = { true }
        buttons(section.view).first { $0.title.hasPrefix("Discard") }!.performClick(nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(GatewayAPI.postPaths.contains("/admin/api/jobs/job-uncertain/discard-uncertain"),
               "the discard must reach the operator endpoint: \(GatewayAPI.postPaths)")

        // A hold that names no upstream attempt offers nothing: the call would
        // have nothing to abandon and would fail on press.
        table.selectRowIndexes(IndexSet(integer: 4), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(!buttons(section.view).contains { $0.title.hasPrefix("Discard") },
               "a hold with no upstream attempt must not offer a discard: \(buttons(section.view).map { $0.title })")
        assert(!buttons(section.view).contains { $0.title.hasPrefix("Close") },
               "and nothing else, because the gateway published no action for it: \(buttons(section.view).map { $0.title })")

        // ── a cloud hold: nothing upstream to ask, so the record is closed here ──
        table.selectRowIndexes(IndexSet(integer: 5), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(labels(section.view).contains { $0.contains("gen-abc123") },
               "the cloud hold should name the upstream request it is waiting on")
        assert(buttons(section.view).contains { $0.title.hasPrefix("Close") },
               "a cloud hold must offer the local close the gateway published: \(buttons(section.view).map { $0.title })")
        assert(!buttons(section.view).contains { $0.title.hasPrefix("Discard") },
               "and never a discard, which no cloud lane can perform")

        let cloudPostsBefore = GatewayAPI.postPaths.count
        section.confirmDiscard = { false }
        buttons(section.view).first { $0.title.hasPrefix("Close") }!.performClick(nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(GatewayAPI.postPaths.count == cloudPostsBefore,
               "declining the confirmation must close nothing: \(GatewayAPI.postPaths)")

        section.confirmDiscard = { true }
        buttons(section.view).first { $0.title.hasPrefix("Close") }!.performClick(nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(GatewayAPI.postPaths.contains("/admin/api/jobs/job-cloud-hold/discard-uncertain"),
               "closing the hold must reach the same operator endpoint: \(GatewayAPI.postPaths)")

        table.selectRowIndexes(IndexSet(integer: 2), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(labels(section.view).contains { $0.localizedCaseInsensitiveContains("queued") }, "queued cancellation state missing")
        let cancel = buttons(section.view).first { $0.title.contains("Cancel") }!
        cancel.performClick(nil)
        assert(GatewayAPI.postPaths.contains("/admin/api/create/status/job-queued/cancel"), "cancel endpoint missing")

        // Refresh with a reordered response. The same job, not the same row, must remain selected.
        table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        GatewayAPI.jobs = [jobQueued, outputJob, jobRunning]
        section.activated()
        assert(table.selectedRow == 1, "refresh did not preserve selected job")

        // Pin the output through its job/asset identity rather than its URL.
        table.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        let pin = buttons(section.view).first { $0.title == "Pin" }!
        pin.performClick(nil)
        assert(GatewayAPI.postPaths.contains("/admin/api/artifacts/keep"), "pin endpoint missing")
        let body = GatewayAPI.postBodies.last!
        assert(body["job_id"] as? String == "job-output", "pin job identity missing")
        assert(body["asset_id"] as? String == "asset-present", "pin asset identity missing")
        assert(body["keep"] as? Bool == true, "pin state missing")

        // A delayed output must still complete after a same-job refresh. The
        // refresh redraws the detail pane but does not represent a new selection.
        GatewayAPI.jobs = [outputJob]
        GatewayAPI.downloads.removeAll()
        let delayedSection = JobsSection()
        _ = delayedSection.view
        delayedSection.activated()
        let delayedTable = descendants(delayedSection.view).compactMap { $0 as? NSTableView }.first!
        delayedTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        buttons(delayedSection.view).first { $0.title == "Preview" }?.performClick(nil)
        assert(GatewayAPI.downloads.count == 1, "delayed preview did not start")
        delayedSection.activated()
        GatewayAPI.downloads[0].1(.success(Data("delayed-output".utf8)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        assert(
            labels(delayedSection.view).contains {
                $0.contains("Opened a local copy") || $0.contains("Could not open the local file")
            },
            "same-job refresh invalidated delayed download: \(labels(delayedSection.view))"
        )

        // A user selection made while a jobs read is pending wins over the
        // selection that existed when that read began.
        GatewayAPI.jobs = [outputJob, jobRunning]
        let selectionSection = JobsSection()
        _ = selectionSection.view
        selectionSection.activated()
        let selectionTable = descendants(selectionSection.view).compactMap { $0 as? NSTableView }.first!
        selectionTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        GatewayAPI.deferJobs = true
        selectionSection.activated()
        selectionTable.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        GatewayAPI.deferJobs = false
        GatewayAPI.deferredJobsCompletion?(.success([
            "jobs": [outputJob, jobRunning],
            "stats": ["total": 2],
        ]))
        GatewayAPI.deferredJobsCompletion = nil
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(selectionTable.selectedRow == 1, "user selection was overwritten by pending refresh")
        assert(
            labels(selectionSection.view).contains("Job job-running · cancellation requested"),
            "pending refresh restored stale selection: \(labels(selectionSection.view))"
        )

        // A late cancel response must not write status into a different job's
        // detail pane.
        GatewayAPI.jobs = [jobQueued, jobRunning]
        let cancelSection = JobsSection()
        _ = cancelSection.view
        cancelSection.activated()
        let cancelTable = descendants(cancelSection.view).compactMap { $0 as? NSTableView }.first!
        cancelTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        GatewayAPI.deferCancel = true
        buttons(cancelSection.view).first { $0.title.contains("Cancel") }?.performClick(nil)
        cancelTable.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
        GatewayAPI.deferCancel = false
        GatewayAPI.deferredCancelCompletion?(.success([
            "id": "job-queued",
            "state": "cancelled",
            "cancel_requested": true,
            "cancel_state": "confirmed",
        ]))
        GatewayAPI.deferredCancelCompletion = nil
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        assert(
            !labels(cancelSection.view).contains { $0.contains("Refreshing lifecycle state") },
            "late cancel response overwrote another job's status"
        )

        print("PASS: Jobs outputs library native fixture")
    }
}
