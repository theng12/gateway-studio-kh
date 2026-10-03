import AppKit

// One state -> colour mapping (Health(state:)), pinned: amber in flight, red failed, green done, grey owner-chosen.
var failures = 0
func check(_ state: String, _ expected: Health) {
    let got = Health(state: state)
    if got != expected { failures += 1; print("FAIL: \(state) is \(got), expected \(expected)") }
}

@main
enum StateColourCheck {
    static func main() {
        for word in ["running", "queued", "accepted", "dispatching", "waiting", "waiting: busy", "updating", "Updating…",
                     "sending", "sent", "installing", "in flight", "working", "stalled", "stop requested",
                     "cancellation requested"] { check(word, .warn) }
        for word in ["failed", "error", "uncertain", "3 failed", "2 uncertain", "needs recovery", "unknown_outcome",
                     "recovery_required"] { check(word, .bad) }
        for word in ["succeeded", "done", "updated", "latest", "finished"] { check(word, .ok) }
        for word in ["cancelled", "canceled", "paused", "stopped", "skipped", "", "something new"] { check(word, .off) }
        check("RUNNING", .warn)
        if Health.warn.tint != NSColor.systemOrange || Health.bad.tint != NSColor.systemRed
            || Health.ok.tint != NSColor.systemGreen || Health.off.tint != NSColor.systemGray {
            failures += 1; print("FAIL: amber/red/green/grey tints changed")
        }
        if failures > 0 { exit(1) }
        print("PASS: state colours")
    }
}
