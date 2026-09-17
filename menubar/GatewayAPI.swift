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

    var errorDescription: String? {
        switch self {
        case .notRunning: return "The gateway is not running."
        case .badURL: return "Bad gateway URL."
        case .unauthorized: return "The gateway rejected the API key."
        case .http(let code): return "The gateway returned HTTP \(code)."
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
        let key = EnvFile.apiKey(GatewayController.shared.gatewayDir)
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
            if code == 401 || code == 403 {
                DispatchQueue.main.async { completion(.failure(GatewayAPIError.unauthorized)) }
                return
            }
            guard code == 200, let data, !data.isEmpty else {
                DispatchQueue.main.async { completion(.failure(GatewayAPIError.http(code))) }
                return
            }
            // An empty body is legal for some POSTs; treat it as an empty object
            // rather than a failure.
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            DispatchQueue.main.async { completion(.success(json)) }
        }.resume()
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
