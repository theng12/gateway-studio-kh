import AppKit

/// The Gateway's own liveness, watched from outside it.
///
/// The one fault the Gateway cannot report is its own freeze. When it wedged (all HTTP hanging, the
/// fleet/worker lock inversion), nothing told the owner for hours, because the notifier that would
/// have said so runs inside the process that was stuck. This app already asks `/health` every three
/// seconds to supervise the backend (`GatewayController.probe`), so the watchdog for that lives
/// here and talks to Telegram itself: while the Gateway is frozen, the Gateway's own sender is the
/// thing that is down.
///
/// **No second timer, no second probe.** The 3 s supervision probe is the measurement, and its
/// 2.5 s timeout is what "slow" means here — a health check that takes longer than that has already
/// failed, which covers "over 5 s" by construction rather than by a second rule.
///
/// **In memory, deliberately.** A restart of this app during an outage re-sends the message two
/// minutes later. Persisting it would be a third state file to keep honest, for a duplicate that a
/// rare event might produce.
enum GatewayLiveness {
    /// How long `/health` may go unanswered before the owner is told. Long enough that the app's own
    /// restart — which takes a few seconds — never produces a message, which is the point: a fault
    /// the app fixed itself is not news.
    static let alertAfter: TimeInterval = 120

    /// When the current outage began, and whether the owner has already been told about it.
    private static var unhealthySince: Date?
    private static var alerted = false
    /// The only place this ever touches the network, off the main thread.
    private static let sender = DispatchQueue(label: "kh.gateway.liveness")

    /// One probe's outcome, from `GatewayController.probe()`. Two date comparisons when nothing changed.
    ///
    /// Returns the message it decided to send, or `nil` when this probe had nothing to say — so a
    /// check can pin the decision without watching the network. The message is sent either way;
    /// the return value is the answer, not a second channel.
    @discardableResult
    static func observe(healthy: Bool, at now: Date = Date()) -> String? {
        if healthy {
            guard let since = unhealthySince else { return nil }
            unhealthySince = nil
            let told = alerted
            alerted = false
            guard told else { return nil }
            return send("🟢 The Gateway is answering again (it was not responding for "
                        + "\(Int(now.timeIntervalSince(since) / 60)) min).")
        }
        guard let since = unhealthySince else {
            unhealthySince = now
            return nil
        }
        guard !alerted, now.timeIntervalSince(since) >= alertAfter else { return nil }
        alerted = true
        return send("🔴 The Gateway has not answered /health for \(Int(now.timeIntervalSince(since) / 60)) min "
                    + "on \(ProcessInfo.processInfo.hostName). Open the Gateway menu to see its state.")
    }

    // MARK: Telegram

    /// The owner's own credentials, read from the file the Gateway's notifier writes.
    ///
    /// The same `telegram.json`, the same switches: this event has its own one in that list, so
    /// turning "Gateway not responding" off silences this too, and nothing else has to be learned.
    /// A missing file or an empty token is "not set up", and the alert is simply not sent.
    ///
    /// Not private so `tests/test_gateway_liveness.swift` can hold it to that file's shape — the
    /// one part of this that is a contract with another program rather than with itself.
    static func credentials() -> (token: String, chat: String)? {
        let path = AppPaths.data.appendingPathComponent("telegram.json")
        guard let data = try? Data(contentsOf: path),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              json["enabled"] as? Bool ?? true else { return nil }
        if let events = json["events"] as? [String: Any], let on = events["gateway_health"] as? Bool, !on {
            return nil
        }
        guard let token = json["bot_token"] as? String, !token.isEmpty,
              let chat = json["chat_id"] as? String, !chat.isEmpty else { return nil }
        return (token, chat)
    }

    private static func send(_ text: String) -> String {
        guard let credentials = credentials() else { return text }
        sender.async { post(credentials, text) }
        return text
    }

    /// POST one message to the Bot API. The token travels in the URL, so nothing here ever logs it.
    private static func post(_ credentials: (token: String, chat: String), _ text: String) {
        guard let url = URL(string: "https://api.telegram.org/bot\(credentials.token)/sendMessage") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        var components = URLComponents()
        components.queryItems = [
            URLQueryItem(name: "chat_id", value: credentials.chat),
            URLQueryItem(name: "text", value: text),
            URLQueryItem(name: "disable_web_page_preview", value: "true"),
        ]
        request.httpBody = components.percentEncodedQuery?.data(using: .utf8)
        URLSession.shared.dataTask(with: request) { _, _, error in
            // The code only: a URLSession error can carry the failing URL, and the token is in it.
            guard let error = error as? URLError else { return }
            NSLog("gateway liveness: could not reach Telegram (URLError %d)", error.errorCode)
        }.resume()
    }
}
