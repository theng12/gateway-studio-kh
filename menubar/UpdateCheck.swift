import AppKit
import CryptoKit
import Foundation

enum UpdateState: Equatable {
    case idle
    case checking
    case upToDate(version: String)
    case available(version: String)
    case unknown(String)
}

final class UpdateChecker {
    static let shared = UpdateChecker()
    private(set) var state: UpdateState = .idle

    private init() {}

    // This app never installs anything (owner rule 2026-11-13): it reads the signed Launcher feed and
    // hands every install to Studio Launcher KH, the only installer. It may check the feed itself.
    func check(completion: @escaping (UpdateState) -> Void) {
        DispatchQueue.main.async {
            self.state = .checking
            let currentVersion = GatewayController.shared.appVersion
            DispatchQueue.global(qos: .utility).async {
                self.fetch(currentVersion: currentVersion) { result in
                    DispatchQueue.main.async {
                        self.state = result
                        completion(result)
                    }
                }
            }
        }
    }

    private func fetch(currentVersion: String, completion: @escaping (UpdateState) -> Void) {
        struct Config: Decodable { let feedURL: String; let publicKey: String }
        struct Envelope: Decodable { let feed: String; let signature: String }
        struct Feed: Decodable { let apps: [String: App] }
        struct App: Decodable { let bundleID: String?; let version: String? }

        let configURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/KHUpdater/base/config.json")
        guard let data = try? Data(contentsOf: configURL),
              let config = try? JSONDecoder().decode(Config.self, from: data),
              let url = URL(string: config.feedURL),
              let scheme = url.scheme?.lowercased(), ["http", "https"].contains(scheme),
              url.host?.isEmpty == false,
              let keyData = Data(base64Encoded: config.publicKey),
              let key = try? Curve25519.Signing.PublicKey(rawRepresentation: keyData) else {
            completion(.unknown("Studio Launcher KH is not set up on this Mac."))
            return
        }

        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10)
        request.httpMethod = "GET"
        let session = URLSession(configuration: .ephemeral)
        session.dataTask(with: request) { data, response, error in
            defer { session.finishTasksAndInvalidate() }
            guard error == nil, let response = response as? HTTPURLResponse,
                  (200...299).contains(response.statusCode), let data = data else {
                completion(.unknown("The update feed could not be reached."))
                return
            }
            guard let envelope = try? JSONDecoder().decode(Envelope.self, from: data),
                  let payload = Data(base64Encoded: envelope.feed),
                  let signature = Data(base64Encoded: envelope.signature),
                  key.isValidSignature(signature, for: payload) else {
                completion(.unknown("The update feed's signature did not verify, so no update answer is shown."))
                return
            }
            guard let feed = try? JSONDecoder().decode(Feed.self, from: payload),
                  let app = feed.apps.values.first(where: { $0.bundleID == "kh.gateway.menubar" }),
                  let version = app.version else {
                completion(.unknown("Gateway Studio KH is missing from the update feed."))
                return
            }
            guard let available = Self.versionParts(version), let current = Self.versionParts(currentVersion) else {
                completion(.unknown("The update version or installed version could not be read."))
                return
            }
            let count = max(available.count, current.count)
            for index in 0..<count {
                let candidate = index < available.count ? available[index] : 0
                let installed = index < current.count ? current[index] : 0
                if candidate != installed {
                    completion(candidate > installed ? .available(version: version) : .upToDate(version: version))
                    return
                }
            }
            completion(.upToDate(version: version))
        }.resume()
    }

    private static func versionParts(_ version: String) -> [Int]? {
        let parts = version.split(separator: ".", omittingEmptySubsequences: false)
        guard (1...6).contains(parts.count) else { return nil }
        var numbers: [Int] = []
        for part in parts {
            guard (1...9).contains(part.utf8.count),
                  part.utf8.allSatisfy({ (48...57).contains($0) }),
                  let number = Int(part) else { return nil }
            numbers.append(number)
        }
        return numbers
    }

    /// Open Studio Launcher KH — the only installer. The app itself never installs, mounts or unpacks.
    func openLauncher() -> Bool {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.khupdater.launcher") else {
            return false
        }
        return NSWorkspace.shared.open(url)
    }
}
