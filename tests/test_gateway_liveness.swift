import Foundation

// The Gateway's own liveness check (`menubar/GatewayLiveness.swift`): when the owner is told, once
// per outage, and which `telegram.json` it will send through.
//
// `AppPaths` is a stand-in here. The real one lives in `main.swift`, which declares `@main` and so
// cannot be compiled into a second binary; the path it must resolve — the Gateway's own data
// folder, and `telegram.json` inside it — is pinned from the Python side in
// `tests/test_gateway_liveness.py`, against the notifier's `_default_paths()`.

enum AppPaths {
    static var data = URL(fileURLWithPath: "/nonexistent")
}

var failures = 0
func check(_ ok: Bool, _ what: String) {
    if !ok { failures += 1; print("FAIL: \(what)") }
}

@main
enum GatewayLivenessCheck {
    static func main() {
        let temp = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("gateway-liveness-\(getpid())", isDirectory: true)
        try? FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: temp) }
        AppPaths.data = temp

        // Nothing configured: the alert is still decided, and there is nowhere to post it.
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        check(GatewayLiveness.observe(healthy: false, at: t0) == nil,
              "the first bad probe only starts the clock")
        check(GatewayLiveness.observe(healthy: false, at: t0.addingTimeInterval(60)) == nil,
              "one minute is not two")
        check(GatewayLiveness.observe(healthy: false, at: t0.addingTimeInterval(119)) == nil,
              "119 seconds is not two minutes")
        let red = GatewayLiveness.observe(healthy: false, at: t0.addingTimeInterval(121))
        check(red?.hasPrefix("🔴") == true, "two minutes unanswered is the owner's business")
        check(red?.contains("2 min") == true, "and the message says how long")
        check(GatewayLiveness.observe(healthy: false, at: t0.addingTimeInterval(600)) == nil,
              "one message per outage, however often it is probed")
        let green = GatewayLiveness.observe(healthy: true, at: t0.addingTimeInterval(601))
        check(green?.hasPrefix("🟢") == true, "answering again is one message")
        check(green?.contains("10 min") == true, "and it says how long it was out")
        check(GatewayLiveness.observe(healthy: true, at: t0.addingTimeInterval(602)) == nil,
              "and only one")

        // An outage this app's own restart clears inside two minutes is never news.
        check(GatewayLiveness.observe(healthy: false, at: t0.addingTimeInterval(700)) == nil,
              "a new outage starts quietly")
        check(GatewayLiveness.observe(healthy: true, at: t0.addingTimeInterval(760)) == nil,
              "a 60 s blip is never announced")

        // Which file it sends through, and which switches silence it: the notifier's own.
        func write(_ json: String) {
            try? json.data(using: .utf8)!.write(to: temp.appendingPathComponent("telegram.json"))
        }
        check(GatewayLiveness.credentials() == nil, "no file: nothing to send through")
        write("{\"bot_token\": \"1:abc\", \"chat_id\": \"-100\"}")
        check(GatewayLiveness.credentials()?.token == "1:abc", "the saved token")
        check(GatewayLiveness.credentials()?.chat == "-100", "the saved chat")
        write("{\"enabled\": false, \"bot_token\": \"1:abc\", \"chat_id\": \"-100\"}")
        check(GatewayLiveness.credentials() == nil, "notifications switched off: nothing is sent")
        write("{\"bot_token\": \"1:abc\", \"chat_id\": \"-100\", \"events\": {\"gateway_health\": false}}")
        check(GatewayLiveness.credentials() == nil, "this event switched off: nothing is sent")
        write("{\"bot_token\": \"1:abc\", \"chat_id\": \"-100\", \"events\": {\"gateway_health\": true}}")
        check(GatewayLiveness.credentials() != nil, "and switched on again: it is sent")
        write("{\"bot_token\": \"\", \"chat_id\": \"-100\"}")
        check(GatewayLiveness.credentials() == nil, "no token: nothing is sent")
        write("{\"bot_token\": \"1:abc\", \"chat_id\": \"\"}")
        check(GatewayLiveness.credentials() == nil, "no chat: nothing is sent")

        print(failures == 0 ? "PASS: gateway liveness" : "FAIL: \(failures) check(s)")
        exit(failures == 0 ? 0 : 1)
    }
}
