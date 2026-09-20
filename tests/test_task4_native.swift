import Foundation

final class GatewayController {
    static let shared = GatewayController()
    var port: Int { Int(ProcessInfo.processInfo.environment["NATIVE_TEST_PORT"] ?? "0") ?? 0 }
    var isRunning: Bool { true }
}

enum EnvFile {
    static func apiKey() -> String {
        ProcessInfo.processInfo.environment["NATIVE_TEST_KEY"] ?? ""
    }
}

@main
struct Task4NativeCheck {
    private enum Expected {
        case success
        case unauthorized
        case missingResource
        case emptyResponse
        case unsafeRedirect
        case http(Int)
        case refused(Int)
    }

    private static func fail(_ message: String) -> Never {
        FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
        exit(1)
    }

    private static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        if !condition() { fail(message) }
    }

    private static func download(_ path: String) -> Result<Data, GatewayAPIError> {
        var result: Result<Data, GatewayAPIError>?
        let deadline = Date().addingTimeInterval(5)
        GatewayAPI.download(path) { value in result = value }
        while result == nil && Date() < deadline {
            RunLoop.main.run(mode: .default, before: deadline)
        }
        guard let result else { fail("timed out downloading \(path)") }
        return result
    }

    private static func get(_ path: String) -> Result<[String: Any], Error> {
        var result: Result<[String: Any], Error>?
        let deadline = Date().addingTimeInterval(5)
        GatewayAPI.get(path) { value in result = value }
        while result == nil && Date() < deadline {
            RunLoop.main.run(mode: .default, before: deadline)
        }
        guard let result else { fail("timed out getting \(path)") }
        return result
    }

    private static func post(_ path: String) -> Result<[String: Any], Error> {
        var result: Result<[String: Any], Error>?
        let deadline = Date().addingTimeInterval(5)
        GatewayAPI.post(path, body: [:]) { value in result = value }
        while result == nil && Date() < deadline {
            RunLoop.main.run(mode: .default, before: deadline)
        }
        guard let result else { fail("timed out posting \(path)") }
        return result
    }

    private static func upload(_ path: String) -> Result<[String: Any], Error> {
        var result: Result<[String: Any], Error>?
        let deadline = Date().addingTimeInterval(5)
        GatewayAPI.upload(
            path,
            fileField: "file",
            filename: "fixture.bin",
            data: Data("fixture".utf8),
            fields: [:]
        ) { value in result = value }
        while result == nil && Date() < deadline {
            RunLoop.main.run(mode: .default, before: deadline)
        }
        guard let result else { fail("timed out uploading \(path)") }
        return result
    }

    private static func isUnsafeRedirect(
        _ result: Result<[String: Any], Error>
    ) -> Bool {
        guard case .failure(let error) = result,
              let gatewayError = error as? GatewayAPIError else { return false }
        if case .unsafeRedirect = gatewayError { return true }
        return false
    }

    private static func matches(_ result: Result<Data, GatewayAPIError>, _ expected: Expected) -> Bool {
        switch (result, expected) {
        case (.success, .success): return true
        case (.failure(.unauthorized), .unauthorized): return true
        case (.failure(.missingResource), .missingResource): return true
        case (.failure(.emptyResponse), .emptyResponse): return true
        case (.failure(.unsafeRedirect), .unsafeRedirect): return true
        case (.failure(.http(let actual)), .http(let wanted)): return actual == wanted
        case (.failure(.refused(let actual, _)), .refused(let wanted)): return actual == wanted
        default: return false
        }
    }

    static func main() {
        check(GatewayController.shared.port > 0, "fixture port is missing")
        let localPath = GatewayAPI.url("/fixture/auth")
        check(localPath?.scheme == "http",
              "local-relative path did not produce HTTP")
        check(localPath?.host == "127.0.0.1",
              "local-relative path changed the gateway host")
        check(localPath?.port == GatewayController.shared.port,
              "local-relative path changed the gateway port")
        for invalid in ["@example.com/file", "http://example.com/file", "//example.com/file"] {
            check(GatewayAPI.url(invalid) == nil,
                  "unsafe non-relative path was accepted: \(invalid)")
        }
        let success = download("/fixture/auth")
        check(matches(success, .success), "authenticated download did not succeed")
        if case .success(let data) = success {
            check(data == Data("native-fixture".utf8), "authenticated bytes changed")
        }
        check(matches(download("/fixture/unauthorized"), .unauthorized),
              "401 was not typed as unauthorized")
        check(matches(download("/fixture/gone"), .missingResource),
              "410 was not typed as missing resource")
        check(matches(download("/fixture/empty"), .emptyResponse),
              "empty 200 was accepted")
        check(matches(download("/fixture/server-error"), .refused(500)),
              "HTTP 500 was accepted or lost its status")
        check(matches(download("/fixture/slow"), .success), "slow download did not complete")
        check(matches(download("/fixture/redirect-same"), .success),
              "same-origin redirect was rejected")
        let crossOrigin = download("/fixture/redirect-cross")
        check(matches(crossOrigin, .unsafeRedirect),
              "cross-origin redirect was followed or misclassified: \(crossOrigin)")
        let jsonRedirect = get("/fixture/redirect-json")
        check(isUnsafeRedirect(jsonRedirect),
              "JSON helper followed a cross-origin redirect: \(jsonRedirect)")
        let postRedirect = post("/fixture/redirect-json")
        check(isUnsafeRedirect(postRedirect),
              "JSON POST helper followed a cross-origin redirect: \(postRedirect)")
        let multipartRedirect = upload("/fixture/redirect-multipart")
        check(isUnsafeRedirect(multipartRedirect),
              "multipart helper followed a cross-origin redirect: \(multipartRedirect)")
        print("PASS: authenticated native JSON, multipart and byte helpers enforce same-origin redirects")
    }
}
