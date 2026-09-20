// Offscreen view-state regression: no server, credentials, generation or windows.
// Run: swiftc -swift-version 5 -framework AppKit menubar/PageKit.swift \
//   menubar/CreateSection.swift tests/test_create_workspace.swift -o /tmp/gateway-create-check \
//   && /tmp/gateway-create-check
import AppKit

protocol AppSection: AnyObject {
    var title: String { get }
    var symbol: String { get }
    var view: NSView { get }
    var refreshesAutomatically: Bool { get }
    func activated()
}

enum GatewayAPI {
    static var rows: [[String: Any]] = [
        ["id": "a", "provider_name": "Mock", "display_name": "A"],
        ["id": "b", "provider_name": "Mock", "display_name": "B"],
    ]
    static var submitted = 0
    static var state = "accepted"
    static var failPoll = false
    static var failSubmit = false
    static var deferModels = false
    static var modelReply: ((Result<[String: Any], Error>) -> Void)?
    static func get(_ path: String, completion: @escaping (Result<[String: Any], Error>) -> Void) {
        if path == "/admin/api/create/models" {
            if deferModels { modelReply = completion; return }
            completion(.success(["by_modality": ["tts": rows]]))
        } else if failPoll {
            completion(.failure(NSError(domain: "Mock offline", code: 1)))
        } else {
            completion(.success(["id": "mock-job", "state": state]))
        }
    }
    static func post(_ path: String, body: [String: Any]? = nil,
                     completion: ((Result<[String: Any], Error>) -> Void)? = nil) {
        submitted += 1
        if failSubmit {
            completion?(.failure(NSError(domain: "Mock submission failed", code: 2)))
            return
        }
        completion?(.success(["id": "mock-job", "state": state]))
    }
    static func upload(_ path: String, fileField: String, filename: String, data: Data,
                       fields: [String: String], completion: @escaping (Result<[String: Any], Error>) -> Void) {
        fatalError("Uploads are outside this check")
    }
    static func url(_ path: String) -> URL? { nil }
    static func detail(from data: Data?) -> String { "Invalid response" }
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
    static func main() {
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
        assert(labels(workspace).contains("The generation was cancelled."))
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
        assert(labels(failedWorkspace) == errorLabels, "late refresh hid submission failure")
        print("PASS: Create selection, refresh race, pending submission, poll failure and cancellation")
    }
}
