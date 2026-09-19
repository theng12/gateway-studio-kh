// Shared HTTP access to the gateway's admin API.
//
// The windows could each build their own requests, but the API key handling and
// the "gateway is not running" case are easy to get subtly wrong once, let
// alone three times.

import Foundation

enum GatewayAPIError: LocalizedError {
    case notRunning
    case badURL
    case unauthorized
    case http(Int)
    case malformed

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
        case .badURL: return "Bad gateway URL."
        case .unauthorized: return "The gateway rejected the API key."
        case .http(let code): return "The gateway returned HTTP \(code)."
        case .refused(let code, let detail):
            return detail.isEmpty ? "The gateway returned HTTP \(code)." : detail
        case .malformed: return "The gateway returned an unexpected response."
        }
    }
}

enum GatewayAPI {
    static func url(_ path: String) -> URL? {
        URL(string: "http://127.0.0.1:\(GatewayController.shared.port)\(path)")
    }

    static func get(
        _ path: String,
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        request("GET", path, completion: completion)
    }

    static func post(
        _ path: String,
        body: [String: Any]? = nil,
        completion: ((Result<[String: Any], Error>) -> Void)? = nil
    ) {
        request("POST", path, body: body, completion: completion ?? { _ in })
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

    private static func request(
        _ method: String,
        _ path: String,
        body: [String: Any]? = nil,
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
        req.timeoutInterval = 15
        let key = EnvFile.apiKey()
        if !key.isEmpty {
            req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }

        URLSession.shared.dataTask(with: req) { data, response, error in
            if let error {
                DispatchQueue.main.async { completion(.failure(error)) }
                return
            }
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if code == 401 {
                DispatchQueue.main.async { completion(.failure(GatewayAPIError.unauthorized)) }
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
                DispatchQueue.main.async { completion(.failure(failure)) }
                return
            }
            // A 200 is not automatically a payload. A proxy page, a renamed
            // endpoint or a top-level array used to become `[:]` — a *successful*
            // empty result — so the Fleet page showed "0 of 0 locations reachable"
            // and the Voices page "0 of 0 voices enabled" as if it had measured
            // something. "I could not read the answer" must not look like "the
            // answer is nothing."
            guard let data, !data.isEmpty else {
                DispatchQueue.main.async { completion(.success([:])) }
                return
            }
            guard let parsed = try? JSONSerialization.jsonObject(with: data),
                  let json = parsed as? [String: Any] else {
                DispatchQueue.main.async { completion(.failure(GatewayAPIError.malformed)) }
                return
            }
            DispatchQueue.main.async { completion(.success(json)) }
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

    // MARK: Formatting helpers shared by the windows

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
