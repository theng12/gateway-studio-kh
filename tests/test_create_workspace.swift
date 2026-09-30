// Offscreen view-state regression: no server, credentials, generation or windows.
// Run: swiftc -swift-version 5 -framework AppKit -framework AVFoundation -framework AVKit \
//   menubar/PageKit.swift menubar/CreatePreview.swift menubar/CreateSection.swift \
//   tests/test_create_workspace.swift -o /tmp/gateway-create-check && /tmp/gateway-create-check
import AppKit
import Darwin

protocol AppSection: AnyObject {
    var title: String { get }
    var symbol: String { get }
    var view: NSView { get }
    var refreshesAutomatically: Bool { get }
    func activated()
}

/// The real `MainWindow` opens sidebar pages; the pages here only ask it to.
final class MainWindow {
    static let shared = MainWindow()
    static var opened: [String] = []
    @discardableResult func show(section: String?) -> Bool {
        Self.opened.append(section ?? "")
        return true
    }
}

enum GatewayAPI {
    /// The other lanes' catalogue rows, by modality; any lane not set gets one row.
    static var laneRows: [String: [[String: Any]]] = [:]
    static var missingJobs: Set<String> = []
    static var statusCalls = 0
    static var outputText = ""
    /// Bytes `download` answers with; nil = the isolated-fixture failure.
    static var downloadData: Data?
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
            var byModality: [String: Any] = ["tts": rows]
            for lane in ["transcription", "image", "music", "video", "chat"] {
                byModality[lane] = laneRows[lane] ?? [[
                    "id": lane, "model_id": lane, "provider_name": "Mock", "display_name": lane,
                ]]
            }
            completion(.success(["by_modality": byModality]))
        } else if path.hasPrefix("/v1/voices") {
            let model = URLComponents(string: "http://test\(path)")?.queryItems?.first(where: { $0.name == "model_id" })?.value ?? ""
            completion(.success(["voices": voiceRowsByModel[model] ?? [], "models": voiceRowsByModel[model] ?? []]))
        } else if path.hasPrefix("/admin/api/create/status/") && deferStatus {
            statusReply = completion
            return
        } else if failPoll {
            completion(.failure(NSError(domain: "Mock offline", code: 1)))
        } else if path.hasPrefix("/admin/api/create/status/") {
            statusCalls += 1
            let id = String(path.split(separator: "/").last ?? "")
            if missingJobs.contains(id) {
                completion(.failure(GatewayAPIError.refused(404, "no job '\(id)'")))
                return
            }
            completion(.success(["id": statusJobID, "state": state, "outputs": outputs, "output_text": outputText]))
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
        completion?(.success(["id": nextJobID, "state": state, "outputs": outputs, "output_text": outputText]))
    }
    static func upload(_ path: String, fileField: String, filename: String, data: Data,
                       fields: [String: String], completion: @escaping (Result<[String: Any], Error>) -> Void) {
        fatalError("Uploads are outside this check")
    }
    static func url(_ path: String) -> URL? { nil }
    static func download(_ path: String, completion: @escaping (Result<Data, GatewayAPIError>) -> Void) {
        downloadedPaths.append(path)
        // Exercise the helper without launching a real media application.
        if let downloadData { completion(.success(downloadData)) } else {
            completion(.failure(GatewayAPIError.transport("isolated download fixture")))
        }
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
        // No suite is named and no environment variable is set: outside the app bundle the store
        // keeps state in memory, so this process cannot reach the owner's real Create state. The
        // real suite is read (never written) to prove exactly that at the end.
        unsetenv("GATEWAY_NATIVE_DEFAULTS_SUITE")
        let realSuite = UserDefaults(suiteName: "kh.gateway.create")
        let realKeys = CreateLanes.all.map { "gateway.create.state." + $0.key }
        let realBefore = realKeys.map { realSuite?.data(forKey: $0) }
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
        GatewayAPI.outputs = [["asset_id": "a", "url": "/admin/api/artifacts/job-a/outputs/a", "media_type": "audio/mpeg", "bytes": 1100000, "present": true, "kept": false]]
        let raceWorkspace = CreateWorkspace(lane: CreateLanes.all[0])
        raceWorkspace.refreshModels()
        raceWorkspace.perform(NSSelectorFromString("generate"))
        GatewayAPI.deferStatus = true
        raceWorkspace.perform(NSSelectorFromString("loadSelectedJob"))
        GatewayAPI.nextJobID = "job-b"
        GatewayAPI.outputs = [["asset_id": "b", "url": "/admin/api/artifacts/job-b/outputs/b", "media_type": "audio/mpeg", "bytes": 2200000, "present": true, "kept": false]]
        raceWorkspace.perform(NSSelectorFromString("generate"))
        GatewayAPI.statusReply?(.success(["id": "job-a", "state": "succeeded", "outputs": [["asset_id": "a", "url": "/admin/api/artifacts/job-a/outputs/a", "media_type": "audio/mpeg", "bytes": 1100000, "present": true, "kept": false]]]))
        GatewayAPI.statusReply = nil
        GatewayAPI.deferStatus = false
        assert(labels(raceWorkspace).contains { $0 == "MP3 · 2.2 MB" }, "late selected-job response replaced the newer submission")
        assert(!labels(raceWorkspace).contains { $0.contains("1.1 MB") }, "stale selected-job output remained visible")

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
        // The result is fetched by the page itself (no "Preview" click), through the authenticated helper.
        assertEventually({ GatewayAPI.downloadedPaths.contains("/admin/api/artifacts/mock-job/outputs/0") }, "output was not fetched through GatewayAPI.download")
        assert(descendants(outputWorkspace).compactMap { $0 as? NSButton }.contains { $0.title == "Save…" && !$0.isHidden },
               "a present output has no Save action")
        GatewayAPI.rows = [["id": "a", "provider_name": "Mock", "display_name": "A",
                            "maximum_input_characters": 1, "maximum_input_characters_source": "provider"]]
        let audio = CreateLanes.all[0]
        let freshAudio = CreateLane(key: "audio-limited", title: audio.title, symbol: audio.symbol,
                                    modality: audio.modality, blurb: audio.blurb, fields: audio.fields)
        let limited = CreateWorkspace(lane: freshAudio)
        limited.refreshModels()
        descendants(limited).compactMap { $0 as? NSTextView }.first!.string = "too long"
        let beforeLimit = GatewayAPI.submitted
        limited.perform(NSSelectorFromString("generate"))
        assert(GatewayAPI.submitted == beforeLimit, "known character limit was not checked before submission")
        assert(labels(limited).contains { $0.contains("1 characters") }, "missing known-limit guidance")

        // A saved job id the gateway does not know is dropped, never offered for "reloading".
        // ("mock-job" is this fixture's id; it reached the real app once through a leaked suite.)
        GatewayAPI.state = "succeeded"
        GatewayAPI.nextJobID = "mock-job"
        GatewayAPI.statusJobID = "mock-job"
        GatewayAPI.outputs = []
        let ghost = CreateLane(key: "audio-ghost", title: audio.title, symbol: audio.symbol,
                               modality: audio.modality, blurb: audio.blurb, fields: audio.fields)
        let ghostMaker = CreateWorkspace(lane: ghost)
        ghostMaker.refreshModels()
        ghostMaker.perform(NSSelectorFromString("generate"))
        GatewayAPI.missingJobs = ["mock-job"]
        let ghostA = CreateWorkspace(lane: ghost)
        ghostA.refreshModels()
        ghostA.restoreSelectedJob()
        assert(!labels(ghostA).contains { $0.contains("mock-job") || $0.contains("is selected") },
               "an unknown saved job was shown")
        assert(labels(ghostA).contains(ghost.emptyTitle), "an unknown saved job left the result pane without its empty state")
        let callsBefore = GatewayAPI.statusCalls
        let ghostB = CreateWorkspace(lane: ghost)
        ghostB.restoreSelectedJob()
        assert(GatewayAPI.statusCalls == callsBefore, "the unknown job id was still saved and looked up again")
        GatewayAPI.missingJobs = []

        // Subtitle: a model renamed (its old id in `legacy_ids`) is the same model, and an empty
        // lane says so plainly instead of "no longer available".
        let subtitle = CreateLanes.all[1]
        GatewayAPI.laneRows["transcription"] = [["id": "old::whisper", "model_id": "whisper", "provider_name": "Hub", "display_name": "whisper"]]
        let subtitleWorkspace = CreateWorkspace(lane: subtitle)
        subtitleWorkspace.refreshModels()
        GatewayAPI.laneRows["transcription"] = [["id": "hub::whisper", "model_id": "whisper", "provider_name": "Hub",
                                                 "display_name": "whisper", "legacy_ids": ["old::whisper"]]]
        subtitleWorkspace.refreshModels()
        let subtitlePicker = descendants(subtitleWorkspace).compactMap { $0 as? NSPopUpButton }.first!
        assert(subtitlePicker.selectedItem?.representedObject as? String == "hub::whisper", "a renamed model was not carried over")
        assert(!labels(subtitleWorkspace).contains { $0.contains("no longer") }, "a renamed model was reported as gone")
        GatewayAPI.laneRows["transcription"] = []
        subtitleWorkspace.refreshModels()
        assert(labels(subtitleWorkspace).contains { $0.contains("No subtitle models are switched on") && $0.contains("Open Models") },
               "an empty Subtitle lane does not say so and how to fix it")
        assert(!labels(subtitleWorkspace).contains { $0.contains("no longer") }, "an empty lane read as a vanished model")
        let manage = descendants(subtitleWorkspace).compactMap { $0 as? NSButton }.first { $0.title == "Manage Subtitle Models" }
        assert(manage != nil, "no way to open Models from the page")
        manage?.performClick(nil)
        assert(MainWindow.opened.last == "models", "Manage Models did not open the Models page")
        GatewayAPI.laneRows["transcription"] = [["id": "other::x", "model_id": "x", "provider_name": "Hub", "display_name": "x"]]
        subtitleWorkspace.refreshModels()
        assert(labels(subtitleWorkspace).contains { $0.contains("hub::whisper is no longer switched on") },
               "a removed model is not named")

        // Chat is a conversation: both sides are shown, and the second send carries the first exchange.
        let chat = CreateLanes.all[5]
        let chatWorkspace = CreateWorkspace(lane: CreateLane(
            key: "chat-conversation", title: chat.title, symbol: chat.symbol, modality: chat.modality,
            blurb: chat.blurb, fields: chat.fields, previewTitle: chat.previewTitle))
        chatWorkspace.refreshModels()
        GatewayAPI.state = "succeeded"
        GatewayAPI.outputText = "Hello from the model"
        GatewayAPI.nextJobID = "chat-1"
        GatewayAPI.statusJobID = "chat-1"
        let chatBox = descendants(chatWorkspace).compactMap { $0 as? NSTextView }.first!
        chatBox.string = "first question"
        chatWorkspace.perform(NSSelectorFromString("generate"))
        assert(labels(chatWorkspace).contains("first question") && labels(chatWorkspace).contains("Hello from the model"),
               "the conversation does not show both turns")
        assert(chatBox.string.isEmpty, "the message field kept a sent message")
        GatewayAPI.nextJobID = "chat-2"
        chatBox.string = "second question"
        chatWorkspace.perform(NSSelectorFromString("generate"))
        let sentMessages = (GatewayAPI.submittedBodies.last?["params"] as? [String: Any])?["messages"] as? [[String: String]]
        assert(sentMessages?.map { $0["role"] ?? "" } == ["user", "assistant", "user"], "the follow-up did not carry the conversation")
        assert(sentMessages?.last?["content"] == "second question")

        // Nothing above reached the owner's real Create state.
        assert(realKeys.map { realSuite?.data(forKey: $0) } == realBefore, "a test wrote to the real Create suite")
        print("PASS: Create persistence, model/voice safety, capabilities, ambiguous retry, cancellation, authenticated output preview, unknown-job cleanup, subtitle model states, chat conversation, real state untouched")
    }
}
