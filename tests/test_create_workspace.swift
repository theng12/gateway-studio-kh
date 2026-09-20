// Offscreen view-state regression: no server, credentials, generation or windows.
// Run: swiftc -swift-version 5 -framework AppKit menubar/PageKit.swift \
//   menubar/CreateSection.swift tests/test_create_workspace.swift -o /tmp/gateway-create-check \
//   && /tmp/gateway-create-check
import AppKit
import Darwin

protocol AppSection: AnyObject {
    var title: String { get }
    var symbol: String { get }
    var view: NSView { get }
    var refreshesAutomatically: Bool { get }
    func activated()
}

enum GatewayAPI {
    static var rows: [[String: Any]] = [
        [
            "id": "a", "model_id": "a", "provider_name": "Mock", "display_name": "A",
            "maximum_input_characters": 300, "maximum_input_characters_source": "provider",
            "supports_voice_cloning": true,
        ],
        [
            "id": "b", "model_id": "b", "provider_name": "Mock", "display_name": "B",
            "maximum_input_characters": NSNull(), "maximum_input_characters_source": NSNull(),
            "supports_voice_cloning": true,
        ],
    ]
    static var voiceRowsByModel: [String: [[String: Any]]] = [
        "a": [["id": "voice-a", "display_name": "Voice A", "provider": "Mock"]],
        "b": [["id": "voice-b", "display_name": "Voice B", "provider": "Mock"]],
    ]
    static var submitted = 0
    static var state = "accepted"
    static var failPoll = false
    static var failSubmit = false
    static var ambiguousSubmit = false
    static var ambiguousSubmitError: Error?
    static var deferModels = false
    static var modelReply: ((Result<[String: Any], Error>) -> Void)?
    static var deferStatus = false
    static var statusReply: ((Result<[String: Any], Error>) -> Void)?
    static var nextJobID = "mock-job"
    static var statusJobID = "mock-job"
    static var submittedBodies: [[String: Any]] = []
    static var submittedKeys: [String] = []
    static var downloadedPaths: [String] = []
    static var outputs: [[String: Any]] = []
    static func get(_ path: String, completion: @escaping (Result<[String: Any], Error>) -> Void) {
        if path == "/admin/api/create/models" {
            if deferModels { modelReply = completion; return }
            completion(.success(["by_modality": ["tts": rows]]))
        } else if path.hasPrefix("/v1/voices") {
            let model = URLComponents(string: "http://test\(path)")?.queryItems?.first(where: { $0.name == "model_id" })?.value ?? ""
            completion(.success(["voices": voiceRowsByModel[model] ?? [], "models": voiceRowsByModel[model] ?? []]))
        } else if path.hasPrefix("/admin/api/create/status/") && deferStatus {
            statusReply = completion
            return
        } else if failPoll {
            completion(.failure(NSError(domain: "Mock offline", code: 1)))
        } else {
            completion(.success(["id": statusJobID, "state": state, "outputs": outputs]))
        }
    }
    static func post(_ path: String, body: [String: Any]? = nil,
                     completion: ((Result<[String: Any], Error>) -> Void)? = nil) {
        if path.hasSuffix("/cancel") {
            completion?(.success([
                "id": "mock-job", "state": state,
                "cancel_requested": true, "cancel_state": "requested",
            ]))
            return
        }
        submitted += 1
        if let body { submittedBodies.append(body) }
        if let key = body?["idempotency_key"] as? String { submittedKeys.append(key) }
        if let ambiguousSubmitError {
            completion?(.failure(ambiguousSubmitError))
            return
        }
        if ambiguousSubmit {
            completion?(.failure(GatewayAPIError.transport("lost response")))
            return
        }
        if failSubmit {
            completion?(.failure(NSError(domain: "Mock submission failed", code: 2)))
            return
        }
        completion?(.success(["id": nextJobID, "state": state, "outputs": outputs]))
    }
    static func upload(_ path: String, fileField: String, filename: String, data: Data,
                       fields: [String: String], completion: @escaping (Result<[String: Any], Error>) -> Void) {
        fatalError("Uploads are outside this check")
    }
    static func url(_ path: String) -> URL? { nil }
    static func download(_ path: String, completion: @escaping (Result<Data, GatewayAPIError>) -> Void) {
        downloadedPaths.append(path)
        // Exercise the helper without launching a real media application.
        completion(.failure(GatewayAPIError.transport("isolated download fixture")))
    }
    static func detail(from data: Data?) -> String { "Invalid response" }
}

enum GatewayAPIError: LocalizedError {
    case notRunning, badURL, unauthorized, malformed, emptyResponse
    case invalidResponse, transport(String), unsafeRedirect, missingResource
    case refused(Int, String)
    var errorDescription: String? {
        switch self {
        case .transport(let detail): return detail
        default: return "mock gateway error"
        }
    }
}

@main
struct CreateWorkspaceCheck {
    static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }
    static func labels(_ workspace: NSView) -> [String] {
        descendants(workspace).compactMap { ($0 as? NSTextField)?.stringValue }
    }
    static func tick() {
        RunLoop.current.run(until: Date().addingTimeInterval(1.7))
    }
    static func assertEventually(_ condition: @escaping () -> Bool, _ message: String) {
        let deadline = Date().addingTimeInterval(1)
        while !condition() && Date() < deadline {
            RunLoop.main.run(mode: .default, before: deadline)
        }
        assert(condition(), message)
    }
    static func main() {
        let suiteName = "gateway-task5-test-\(UUID().uuidString)"
        setenv("GATEWAY_NATIVE_DEFAULTS_SUITE", suiteName, 1)
        let defaults = UserDefaults(suiteName: suiteName)!
        defaults.removePersistentDomain(forName: suiteName)
        defer { defaults.removePersistentDomain(forName: suiteName) }
        _ = NSApplication.shared
        let workspace = CreateWorkspace(lane: CreateLanes.all[0])
        workspace.refreshModels()
        let views = descendants(workspace)
        let picker = views.compactMap { $0 as? NSPopUpButton }.first!
        let generate = views.compactMap { $0 as? NSButton }.first { $0.title == "Generate" }!
        assert(picker.titleOfSelectedItem == "Mock — A")

        picker.selectItem(at: 1)
        workspace.refreshModels()
        assert(picker.titleOfSelectedItem == "Mock — B", "refresh changed model")
        assert(!labels(workspace).contains("loading models…"), "refresh left loading status")

        GatewayAPI.deferModels = true
        workspace.refreshModels()
        picker.selectItem(at: 0)
        GatewayAPI.modelReply?(.success(["by_modality": ["tts": GatewayAPI.rows]]))
        GatewayAPI.deferModels = false
        assert(picker.selectedItem?.representedObject as? String == "a", "late refresh undid selection")

        GatewayAPI.rows = [["id": "b", "provider_name": "Mock", "display_name": "B"]]
        workspace.refreshModels()
        assert(picker.selectedItem?.representedObject == nil, "missing selection silently replaced")
        assert(!generate.isEnabled, "Generate enabled without a selected model")

        picker.selectItem(withTitle: "Mock — B")
        workspace.perform(NSSelectorFromString("modelChanged"))
        assert(generate.isEnabled)
        workspace.perform(NSSelectorFromString("generate"))
        assert(!generate.isEnabled)
        workspace.refreshModels()
        assert(!generate.isEnabled, "refresh re-enabled pending generation")
        workspace.perform(NSSelectorFromString("generate"))
        assert(GatewayAPI.submitted == 1, "pending job submitted twice")

        GatewayAPI.failPoll = true
        tick()
        assert(labels(workspace).contains { $0.contains("Cannot check this job") }, "poll failure hidden")
        assert(!generate.isEnabled)
        GatewayAPI.failPoll = false
        GatewayAPI.state = "cancelled"
        tick()
        assert(generate.isEnabled)
        assert(labels(workspace).contains { $0.hasPrefix("The generation was cancelled.") })
        assert(!descendants(workspace).contains { $0 is NSProgressIndicator }, "cancelled job still spinning")

        let failedWorkspace = CreateWorkspace(lane: CreateLanes.all[0])
        failedWorkspace.refreshModels()
        GatewayAPI.deferModels = true
        failedWorkspace.refreshModels()
        GatewayAPI.failSubmit = true
        failedWorkspace.perform(NSSelectorFromString("generate"))
        let errorLabels = labels(failedWorkspace)
        assert(errorLabels.contains { $0.contains("Mock submission failed") })
        GatewayAPI.modelReply?(.success(["by_modality": ["tts": GatewayAPI.rows]]))
        GatewayAPI.deferModels = false
        GatewayAPI.failSubmit = false
        assert(labels(failedWorkspace) == errorLabels, "late refresh hid submission failure")

        // Drafts and selected jobs restore from an isolated suite without ever
        // submitting as a side effect of construction or navigation.
        let submittedBeforeRestore = GatewayAPI.submitted
        let script = descendants(workspace).compactMap { $0 as? NSTextView }.first!
        script.string = "persist this script"
        NotificationCenter.default.post(name: NSText.didChangeNotification, object: script)
        let restored = CreateWorkspace(lane: CreateLanes.all[0])
        restored.refreshModels()
        assertEventually({ labels(restored).contains("Your selected model is no longer available. Select a model to continue.") == false }, "restored form did not load")
        assert(GatewayAPI.submitted == submittedBeforeRestore, "restoring a lane submitted work")
        assert(descendants(restored).compactMap { $0 as? NSTextView }.first?.string == "persist this script", "nested script editor did not persist")

        // The voice value survives a model refresh even when the compatible list
        // changes; a model change must never silently choose another speaker.
        let voiceControls = descendants(workspace).compactMap { $0 as? NSComboBox }
        assert(!voiceControls.isEmpty, "voice control is not searchable")
        if let voice = voiceControls.first {
            voice.stringValue = "voice-a"
            NotificationCenter.default.post(name: NSText.didChangeNotification, object: voice)
        }
        GatewayAPI.rows = [
            ["id": "b", "model_id": "b", "provider_name": "Mock", "display_name": "B",
             "maximum_input_characters": NSNull(), "maximum_input_characters_source": NSNull(),
             "supports_voice_cloning": true],
            ["id": "a", "model_id": "a", "provider_name": "Mock", "display_name": "A",
             "maximum_input_characters": 300, "maximum_input_characters_source": "provider",
             "supports_voice_cloning": true],
        ]
        workspace.refreshModels()
        assert(voiceControls.first?.stringValue == "voice-a", "model refresh substituted a voice")

        picker.selectItem(withTitle: "Mock — A")
        workspace.perform(NSSelectorFromString("modelChanged"))
        assert(labels(workspace).contains { $0.contains("provider") }, "capability source label missing")
        picker.selectItem(withTitle: "Mock — B")
        workspace.perform(NSSelectorFromString("modelChanged"))
        assert(labels(workspace).contains { $0.localizedCaseInsensitiveContains("unknown") }, "unknown capability was not shown as unknown")

        // A transport loss keeps the exact attempt and disables editing until an
        // explicit same-key retry.
        let retryWorkspace = CreateWorkspace(lane: CreateLanes.all[0])
        retryWorkspace.refreshModels()
        GatewayAPI.ambiguousSubmit = true
        retryWorkspace.perform(NSSelectorFromString("generate"))
        assert(labels(retryWorkspace).contains { $0.localizedCaseInsensitiveContains("unknown") }, "lost response was presented as a normal failure")
        assert(!descendants(retryWorkspace).compactMap { $0 as? NSButton }.contains { $0.title == "Generate" && $0.isEnabled }, "ambiguous attempt remained editable")
        let firstBody = GatewayAPI.submittedBodies.last!
        let firstKey = GatewayAPI.submittedKeys.last!
        GatewayAPI.ambiguousSubmit = false
        let retry = descendants(retryWorkspace).compactMap { $0 as? NSButton }.first { $0.title.contains("Retry") }
        assert(retry != nil, "same-attempt retry action missing")
        retry?.performClick(nil)
        assertEventually({ GatewayAPI.submitted >= 3 }, "same-attempt retry did not submit")
        let canonical: ([String: Any]) -> Data = { body in
            try! JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
        }
        assert(canonical(GatewayAPI.submittedBodies.last!) == canonical(firstBody), "retry changed the exact payload")
        assert(GatewayAPI.submittedKeys.last == firstKey, "retry changed the idempotency key")

        GatewayAPI.ambiguousSubmitError = URLError(.networkConnectionLost)
        let urlErrorWorkspace = CreateWorkspace(lane: CreateLanes.all[0])
        urlErrorWorkspace.refreshModels()
        urlErrorWorkspace.perform(NSSelectorFromString("generate"))
        assert(labels(urlErrorWorkspace).contains { $0.localizedCaseInsensitiveContains("unknown") }, "URLError was treated as a definite submit failure")
        assert(descendants(urlErrorWorkspace).compactMap { $0 as? NSButton }.contains { $0.title.contains("Retry") && !$0.isHidden }, "URLError lost the retryable attempt")
        GatewayAPI.ambiguousSubmitError = nil
        let separateDraft = descendants(urlErrorWorkspace).compactMap { $0 as? NSButton }.first { $0.title == "Start separate draft" }
        separateDraft?.performClick(nil)

        GatewayAPI.state = "succeeded"
        GatewayAPI.nextJobID = "job-a"
        GatewayAPI.statusJobID = "job-a"
        GatewayAPI.outputs = [["asset_id": "a", "url": "/admin/api/artifacts/job-a/outputs/a", "media_type": "audio/mpeg", "bytes": 1, "present": true, "kept": false]]
        let raceWorkspace = CreateWorkspace(lane: CreateLanes.all[0])
        raceWorkspace.refreshModels()
        raceWorkspace.perform(NSSelectorFromString("generate"))
        GatewayAPI.deferStatus = true
        raceWorkspace.perform(NSSelectorFromString("loadSelectedJob"))
        GatewayAPI.nextJobID = "job-b"
        GatewayAPI.outputs = [["asset_id": "b", "url": "/admin/api/artifacts/job-b/outputs/b", "media_type": "audio/mpeg", "bytes": 2, "present": true, "kept": false]]
        raceWorkspace.perform(NSSelectorFromString("generate"))
        GatewayAPI.statusReply?(.success(["id": "job-a", "state": "succeeded", "outputs": [["asset_id": "a", "url": "/admin/api/artifacts/job-a/outputs/a", "media_type": "audio/mpeg", "bytes": 1, "present": true, "kept": false]]]))
        GatewayAPI.statusReply = nil
        GatewayAPI.deferStatus = false
        assert(labels(raceWorkspace).contains { $0.contains("Asset b") }, "late selected-job response replaced the newer submission")
        assert(!labels(raceWorkspace).contains { $0.contains("Asset a") }, "stale selected-job output remained visible")

        // Preview goes through the authenticated byte helper, not a raw URL or a
        // synchronous Data(contentsOf:) call.
        GatewayAPI.state = "succeeded"
        GatewayAPI.nextJobID = "mock-job"
        GatewayAPI.statusJobID = "mock-job"
        GatewayAPI.outputs = [[
            "asset_id": "0", "url": "/admin/api/artifacts/mock-job/outputs/0",
            "media_type": "audio/mpeg", "bytes": 12, "present": true, "kept": false,
        ]]
        let outputWorkspace = CreateWorkspace(lane: CreateLanes.all[0])
        outputWorkspace.refreshModels()
        outputWorkspace.perform(NSSelectorFromString("generate"))
        let preview = descendants(outputWorkspace).compactMap { $0 as? NSButton }.first { $0.title == "Preview" }
        assert(preview != nil, "output preview action missing")
        preview?.performClick(nil)
        assertEventually({ GatewayAPI.downloadedPaths.contains("/admin/api/artifacts/mock-job/outputs/0") }, "preview did not use GatewayAPI.download")
        print("PASS: Create persistence, model/voice safety, capabilities, ambiguous retry, cancellation and authenticated output preview")
    }
}
