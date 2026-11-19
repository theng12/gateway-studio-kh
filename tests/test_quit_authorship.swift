import Foundation

// Attendance is not authorship (`QuitAuthorship` in `menubar/main.swift`).
// The python test lifts the marked block out of `main.swift` and puts it on the command line.

var failures = 0
func check(_ ok: Bool, _ what: String) {
    if !ok { failures += 1; print("FAIL: \(what)") }
}

@main
struct QuitAuthorshipCheck {
    static func main() {
        // **The Updater names itself.** Its stop goes ahead with no dialog, however busy the
        // Gateway is and whoever happens to be looking at the window.
        check(QuitAuthorship.isAutomated(senderBundleID: "com.khupdater.launcher", holding: false),
              "the Launcher's Quit is not challenged")
        check(QuitAuthorship.isAutomated(senderBundleID: "com.khupdater.agent", holding: false),
              "the Launcher's helper's Quit is not challenged")
        check(QuitAuthorship.isAutomated(senderBundleID: "com.khupdater.feed", holding: false),
              "a helper added later is still recognised")

        // **Corroboration.** Only the hold-token route pauses dispatch, so a stop that follows one
        // is the Updater even when the AppleEvent carried no sender.
        check(QuitAuthorship.isAutomated(senderBundleID: nil, holding: true),
              "a stop during a hold is not challenged")

        // **A person's Quit is still challenged**, which is the half that must not be lost.
        check(!QuitAuthorship.isAutomated(senderBundleID: nil, holding: false),
              "a person's Quit may still be challenged")
        check(!QuitAuthorship.isAutomated(senderBundleID: "com.apple.finder", holding: false),
              "another app's Quit may still be challenged")
        check(!QuitAuthorship.isAutomated(senderBundleID: "kh.gateway.menubar", holding: false),
              "this app's own id is not the Updater")

        // The log says why, and only when it is not asking.
        check(QuitAuthorship.whyNotAsked(senderBundleID: "com.khupdater.launcher", holding: false) != nil,
              "an automated stop says why it is not asking")
        check(QuitAuthorship.whyNotAsked(senderBundleID: nil, holding: true) != nil,
              "a corroborated stop says why it is not asking")
        check(QuitAuthorship.whyNotAsked(senderBundleID: nil, holding: false) == nil,
              "a person's Quit is asked")

        if failures == 0 { print("PASS: quit authorship") }
        exit(failures == 0 ? 0 : 1)
    }
}
