// Shared HTTP access to the gateway's admin API.
//
// The windows could each build their own requests, but the API key handling and
// the "gateway is not running" case are easy to get subtly wrong once, let
// alone three times.

import Foundation

enum GatewayAPIError: LocalizedError {
    case notRunning
    /// `/health` answers but a read ran out of time: slow, not down (0.2.157).
    case busy
    case badURL
    case unauthorized
    case http(Int)
    case malformed
    case emptyResponse
    case invalidResponse
    case transport(String)
    case unsafeRedirect
    case missingResource

    /// A refused request, carrying what the gateway actually said.
    ///
    /// Every non-200 used to collapse into "The gateway returned HTTP 409" or,
    /// worse, "The gateway rejected the API key" — and 403 is not an API-key
    /// problem at all. The gateway answers 403 with the real reason ("These
    /// voices have not been allowed for cloning and distribution: …"), so a
    /// permission decision the owner had made was reported to him as a broken
    /// key. The server's own sentence is the only useful thing on the wire.
    case refused(Int, String)

    var errorDescription: String? {
        switch self {
        case .notRunning: return "The gateway is not running."
        case .busy: return "The gateway is busy, retrying…"
        case .badURL: return "Bad gateway URL."
        case .unauthorized: return "The gateway rejected the API key."
        case .http(let code): return "The gateway returned HTTP \(code)."
        case .emptyResponse: return "The gateway returned an empty response."
        case .invalidResponse: return "The gateway returned an invalid response."
        case .transport(let detail):
            return detail.isEmpty ? "Could not reach the gateway." : "Could not reach the gateway: \(detail)"
        case .unsafeRedirect: return "The gateway tried to redirect the request to another origin."
        case .missingResource: return "The requested file is no longer available."
        case .refused(let code, let detail):
            return detail.isEmpty ? "The gateway returned HTTP \(code)." : detail
        case .malformed: return "The gateway returned an unexpected response."
        }
    }
}

/// The owner's own app window, told apart from anything else that can reach the gateway (owner decision 4g).
///
/// Loopback is trusted and the API key also signs a browser in, so an agent holding the key looks like the
/// owner to every ordinary admin route. A per-launch secret, made here and kept only in this process, is
/// handed to the gateway this app spawns on a stdin pipe (`GatewayController.spawn`), never argv, the
/// environment or a file. The native client sends it back on the few routes that need the owner himself;
/// the web console is never given it. A gateway this app adopted instead of spawning never received it.
enum OwnerConsole {
    static let header = "X-Gateway-Owner-Console"
    static let secret: String = (0..<32).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
}

enum GatewayAPI {
    static func url(_ path: String) -> URL? {
        guard path.hasPrefix("/"), !path.hasPrefix("//") else { return nil }
        let port = GatewayController.shared.port
        guard let target = URL(string: "http://127.0.0.1:\(port)\(path)"),
              let components = URLComponents(
                url: target,
                resolvingAgainstBaseURL: false
              ),
              components.scheme == "http",
              components.host == "127.0.0.1",
              components.port == port,
              components.user == nil,
              components.password == nil else {
            return nil
        }
        return target
    }

    static func get(
        _ path: String,
        timeout: TimeInterval = 15,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        // `timeout` exists for the one read that is allowed to wait: the Fleet
        // page's Refresh, which asks the gateway for a real fan-out across every
        // location instead of the cached snapshot a plain read is served. 15 s is
        // the right ceiling for a cache read and too short for a sweep, and the
        // caller that forces says so.
        request("GET", path, timeout: timeout, completion: completion)
    }

    /// `ownerConsole` proves the request comes from this app's own window (`OwnerConsole`): only for the
    /// actions the gateway allows nowhere else, such as a permanent Trash Folder delete.
    static func post(
        _ path: String,
        body: [String: Any]? = nil,
        timeout: TimeInterval = 15,
        ownerConsole: Bool = false,
        completion: ((Result<[String: Any], Error>) -> Void)? = nil
    ) {
        request("POST", path, body: body, timeout: timeout, ownerConsole: ownerConsole,
                completion: completion ?? { _ in })
    }

    static func patch(
        _ path: String,
        body: [String: Any]? = nil,
        completion: ((Result<[String: Any], Error>) -> Void)? = nil
    ) {
        request("PATCH", path, body: body, completion: completion ?? { _ in })
    }

    static func put(
        _ path: String,
        body: [String: Any]? = nil,
        completion: ((Result<[String: Any], Error>) -> Void)? = nil
    ) {
        request("PUT", path, body: body, completion: completion ?? { _ in })
    }

    static func delete(
        _ path: String,
        completion: ((Result<[String: Any], Error>) -> Void)? = nil
    ) {
        request("DELETE", path, completion: completion ?? { _ in })
    }

    /// Download authenticated bytes without letting a redirect send the key to
    /// another origin. Every completion is delivered on the main queue.
    static func download(
        _ path: String,
        completion: @escaping (Result<Data, GatewayAPIError>) -> Void
    ) {
        guard GatewayController.shared.isRunning else {
            DispatchQueue.main.async { completion(.failure(.notRunning)) }
            return
        }
        guard let target = url(path) else {
            DispatchQueue.main.async { completion(.failure(.badURL)) }
            return
        }

        let key = EnvFile.apiKey()
        guard let (session, redirectDelegate) = authenticatedSession(for: target, key: key) else {
            DispatchQueue.main.async { completion(.failure(.badURL)) }
            return
        }
        var request = URLRequest(url: target)
        request.httpMethod = "GET"
        request.timeoutInterval = 60
        if !key.isEmpty {
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }

        session.dataTask(with: request) { data, response, error in
            let finish: (Result<Data, GatewayAPIError>) -> Void = { result in
                DispatchQueue.main.async {
                    completion(result)
                    session.invalidateAndCancel()
                }
            }

            if redirectDelegate.rejectedCrossOrigin {
                finish(.failure(.unsafeRedirect))
                return
            }
            if let error {
                finish(.failure(
                    .transport(error.localizedDescription)
                ))
                return
            }
            guard let response = response as? HTTPURLResponse else {
                finish(.failure(.invalidResponse))
                return
            }
            guard (200...299).contains(response.statusCode) else {
                if response.statusCode == 401 {
                    finish(.failure(.unauthorized))
                } else if response.statusCode == 404 || response.statusCode == 410 {
                    finish(.failure(.missingResource))
                } else {
                    finish(.failure(.refused(response.statusCode, detail(from: data))))
                }
                return
            }
            guard let data, !data.isEmpty else {
                finish(.failure(.emptyResponse))
                return
            }
            finish(.success(data))
        }.resume()
    }

    /// A multipart POST, for the one route that carries a file.
    ///
    /// It exists rather than a generic "send bytes" because transcription is the
    /// only lane here whose payload is not JSON, and the refusal handling has to
    /// stay identical to `request`'s: the gateway answers a too-large file with a
    /// 413 and a sentence naming the limit, and that sentence is the whole value of
    /// the answer.
    static func upload(
        _ path: String,
        fileField: String,
        filename: String,
        data: Data,
        fields: [String: String],
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        guard GatewayController.shared.isRunning else {
            DispatchQueue.main.async { completion(.failure(GatewayAPIError.notRunning)) }
            return
        }
        guard let target = url(path) else {
            DispatchQueue.main.async { completion(.failure(GatewayAPIError.badURL)) }
            return
        }
        let boundary = "gateway-kh-\(UUID().uuidString)"
        var body = Data()
        for (name, value) in fields {
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".data(using: .utf8)!)
            body.append("\(value)\r\n".data(using: .utf8)!)
        }
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"\(fileField)\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: application/octet-stream\r\n\r\n".data(using: .utf8)!)
        body.append(data)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)

        var req = URLRequest(url: target)
        req.httpMethod = "POST"
        // Longer than the JSON timeout: this carries the bytes and the gateway
        // stages them before it answers.
        req.timeoutInterval = 120
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let key = EnvFile.apiKey()
        if !key.isEmpty {
            req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        req.httpBody = body

        guard let (session, redirectDelegate) = authenticatedSession(for: target, key: key) else {
            DispatchQueue.main.async { completion(.failure(GatewayAPIError.badURL)) }
            return
        }
        let finish: (Result<[String: Any], Error>) -> Void = { result in
            DispatchQueue.main.async {
                completion(result)
                session.invalidateAndCancel()
            }
        }
        session.dataTask(with: req) { data, response, error in
            if redirectDelegate.rejectedCrossOrigin {
                finish(.failure(GatewayAPIError.unsafeRedirect))
                return
            }
            if let error {
                finish(.failure(error))
                return
            }
            guard let response = response as? HTTPURLResponse else {
                finish(.failure(GatewayAPIError.invalidResponse))
                return
            }
            let code = response.statusCode
            if code == 401 {
                finish(.failure(GatewayAPIError.unauthorized))
                return
            }
            guard code == 200 || code == 202 else {
                let detail = GatewayAPI.detail(from: data)
                finish(.failure(GatewayAPIError.refused(code, detail)))
                return
            }
            guard let data, !data.isEmpty,
                  let parsed = try? JSONSerialization.jsonObject(with: data),
                  let json = parsed as? [String: Any] else {
                finish(.failure(GatewayAPIError.malformed))
                return
            }
            finish(.success(json))
        }.resume()
    }

    private static func request(
        _ method: String,
        _ path: String,
        body: [String: Any]? = nil,
        timeout: TimeInterval = 15,
        ownerConsole: Bool = false,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        guard GatewayController.shared.isRunning else {
            DispatchQueue.main.async { completion(.failure(GatewayAPIError.notRunning)) }
            return
        }
        guard let target = url(path) else {
            DispatchQueue.main.async { completion(.failure(GatewayAPIError.badURL)) }
            return
        }

        var req = URLRequest(url: target)
        req.httpMethod = method
        // The caller's ceiling: a release publish carries a whole artifact and
        // waits for the gateway to push it to every controller, so it asks for
        // far more than the 15 s a plain admin read needs.
        req.timeoutInterval = timeout
        let key = EnvFile.apiKey()
        guard let (session, redirectDelegate) = authenticatedSession(for: target, key: key) else {
            DispatchQueue.main.async { completion(.failure(GatewayAPIError.badURL)) }
            return
        }
        if !key.isEmpty {
            req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        if ownerConsole {
            req.setValue(OwnerConsole.secret, forHTTPHeaderField: OwnerConsole.header)
        }

        let finish: (Result<[String: Any], Error>) -> Void = { result in
            DispatchQueue.main.async {
                completion(result)
                session.invalidateAndCancel()
            }
        }
        session.dataTask(with: req) { data, response, error in
            if redirectDelegate.rejectedCrossOrigin {
                finish(.failure(GatewayAPIError.unsafeRedirect))
                return
            }
            if let error {
                // `isRunning` passed above, so `/health` answers: a timed-out read is a busy
                // gateway; the visible page re-reads on the window's 5 s timer. Writes keep the plain error.
                let busy = method == "GET" && (error as? URLError)?.code == .timedOut
                finish(.failure(busy ? GatewayAPIError.busy : error))
                return
            }
            guard let response = response as? HTTPURLResponse else {
                finish(.failure(GatewayAPIError.invalidResponse))
                return
            }
            let code = response.statusCode
            if code == 401 {
                finish(.failure(GatewayAPIError.unauthorized))
                return
            }
            guard code == 200 else {
                // Read the reason before deciding what to say. `admin.py` answers
                // every refusal as `{"detail": "..."}`, deliberately, and that
                // sentence is the difference between "your key is wrong" and
                // "you have not allowed these voices to be shared".
                let detail = GatewayAPI.detail(from: data)
                let failure: GatewayAPIError = code == 403
                    ? .refused(code, detail.isEmpty ? "The gateway refused this action." : detail)
                    : .refused(code, detail)
                finish(.failure(failure))
                return
            }
            // A 200 is not automatically a payload. A proxy page, a renamed
            // endpoint or a top-level array used to become `[:]` — a *successful*
            // empty result — so the Fleet page showed "0 of 0 locations reachable"
            // and the Voices page "0 of 0 voices enabled" as if it had measured
            // something. "I could not read the answer" must not look like "the
            // answer is nothing."
            guard let data, !data.isEmpty else {
                finish(.failure(GatewayAPIError.emptyResponse))
                return
            }
            guard let parsed = try? JSONSerialization.jsonObject(with: data),
                  let json = parsed as? [String: Any] else {
                finish(.failure(GatewayAPIError.malformed))
                return
            }
            finish(.success(json))
        }.resume()
    }

    /// The gateway's own explanation for a refusal, if it sent one.
    static func detail(from data: Data?) -> String {
        guard let data, !data.isEmpty,
              let parsed = try? JSONSerialization.jsonObject(with: data),
              let object = parsed as? [String: Any] else { return "" }
        if let text = object["detail"] as? String { return text }
        // FastAPI's own validation shape: `{"detail": [{"msg": ...}]}`.
        if let rows = object["detail"] as? [[String: Any]] {
            let messages = rows.compactMap { $0["msg"] as? String }
            if !messages.isEmpty { return messages.joined(separator: " ") }
        }
        if let text = object["message"] as? String { return text }
        return ""
    }

    private static func authenticatedSession(
        for target: URL,
        key: String
    ) -> (URLSession, AuthenticatedRedirectDelegate)? {
        guard let origin = RequestOrigin(url: target) else { return nil }
        let delegate = AuthenticatedRedirectDelegate(origin: origin, key: key)
        let session = URLSession(
            configuration: .ephemeral,
            delegate: delegate,
            delegateQueue: nil
        )
        return (session, delegate)
    }

    private struct RequestOrigin: Equatable {
        let scheme: String
        let host: String
        let port: Int?

        init?(url: URL) {
            guard let scheme = url.scheme?.lowercased(),
                  let host = url.host?.lowercased() else { return nil }
            self.scheme = scheme
            self.host = host
            self.port = url.port ?? (scheme == "http" ? 80 : (scheme == "https" ? 443 : nil))
        }
    }

    private final class AuthenticatedRedirectDelegate: NSObject, URLSessionTaskDelegate {
        let origin: RequestOrigin
        let key: String
        private(set) var rejectedCrossOrigin = false

        init(origin: RequestOrigin, key: String) {
            self.origin = origin
            self.key = key
        }

        func urlSession(
            _ session: URLSession,
            task: URLSessionTask,
            willPerformHTTPRedirection response: HTTPURLResponse,
            newRequest request: URLRequest,
            completionHandler: @escaping (URLRequest?) -> Void
        ) {
            guard let redirectedURL = request.url,
                  let redirectedOrigin = RequestOrigin(url: redirectedURL),
                  redirectedOrigin == origin else {
                rejectedCrossOrigin = true
                completionHandler(nil)
                return
            }
            var authorized = request
            if !key.isEmpty {
                authorized.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
            }
            completionHandler(authorized)
        }
    }

    // MARK: Formatting helpers shared by the windows

    private static let clockFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()

    /// The wall-clock time of an epoch, for "as of" lines and job details where "5m ago" is not enough.
    static func clock(_ epoch: Double) -> String {
        clockFormatter.string(from: Date(timeIntervalSince1970: epoch))
    }

    static func relativeTime(_ epoch: Double?) -> String {
        guard let epoch, epoch > 0 else { return "—" }
        let seconds = Date().timeIntervalSince1970 - epoch
        if seconds < 60 { return "\(Int(max(seconds, 0)))s ago" }
        if seconds < 3600 { return "\(Int(seconds / 60))m ago" }
        if seconds < 86_400 { return "\(Int(seconds / 3600))h ago" }
        return "\(Int(seconds / 86_400))d ago"
    }

    static func duration(_ seconds: Double?) -> String {
        guard let seconds else { return "—" }
        if seconds < 60 { return String(format: "%.1fs", seconds) }
        let minutes = Int(seconds) / 60
        return "\(minutes)m \(Int(seconds) % 60)s"
    }

    static func uptime(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        let hours = seconds / 3600
        if hours < 24 { return "\(hours)h \(minutes(seconds))m" }
        return "\(hours / 24)d \(hours % 24)h"
    }

    private static func minutes(_ seconds: Int) -> Int { (seconds % 3600) / 60 }
}
