import Foundation

// Launch at login is on by default (`menubar/LoginDefault.swift`), switchable off, and only the
// installed app bundle ever writes the LaunchAgent.

var failures = 0
func check(_ ok: Bool, _ what: String) {
    if !ok { failures += 1; print("FAIL: \(what)") }
}

@main
enum LoginDefaultCheck {
    static func main() {
        let app = LoginDefault.appExecutable
        check(LoginDefault.shouldInstall(installed: false, optedOut: nil, executable: app),
              "first launch with no choice recorded turns launch at login on")
        check(LoginDefault.shouldInstall(installed: false, optedOut: false, executable: app),
              "a recorded 'on' with the plist missing puts it back")
        check(!LoginDefault.shouldInstall(installed: false, optedOut: true, executable: app),
              "an explicit 'off' stays off")
        check(!LoginDefault.shouldInstall(installed: true, optedOut: nil, executable: app),
              "already installed: nothing to do")
        check(!LoginDefault.shouldInstall(installed: false, optedOut: nil,
                                          executable: "/tmp/build/gateway-kh.app/Contents/MacOS/Gateway Studio KH"),
              "a build outside /Applications never installs")
        check(!LoginDefault.shouldInstall(installed: false, optedOut: nil, executable: nil),
              "an unknown executable never installs")
        // Re-registration once per new build (1301, 2026-11-16: spawn failed, exit 78, 10 060 runs).
        check(LoginDefault.shouldReregister(enabled: true, registeredBuild: "a1b2c3d-202611150900",
                                            currentBuild: "e4f5a6b-202611162200", executable: app,
                                            runningAsAgent: false),
              "a new build redoes the registration")
        check(LoginDefault.shouldReregister(enabled: true, registeredBuild: nil,
                                            currentBuild: "e4f5a6b-202611162200", executable: app,
                                            runningAsAgent: false),
              "an enabled registration from an older build, which recorded none, is redone")
        check(!LoginDefault.shouldReregister(enabled: true, registeredBuild: "e4f5a6b-202611162200",
                                             currentBuild: "e4f5a6b-202611162200", executable: app,
                                             runningAsAgent: false),
              "the same build is left alone: once per build, not every launch")
        check(!LoginDefault.shouldReregister(enabled: true, registeredBuild: nil, currentBuild: "x",
                                             executable: app, runningAsAgent: true),
              "the copy launchd started never unregisters itself")
        check(!LoginDefault.shouldReregister(enabled: false, registeredBuild: nil, currentBuild: "x",
                                             executable: app, runningAsAgent: false),
              "nothing registered (or switched off): nothing to redo")
        check(!LoginDefault.shouldReregister(enabled: true, registeredBuild: nil, currentBuild: "x",
                                             executable: "/tmp/build/gateway-kh.app/Contents/MacOS/Gateway Studio KH",
                                             runningAsAgent: false),
              "a build outside /Applications never re-registers")
        check(!LoginDefault.shouldReregister(enabled: true, registeredBuild: nil, currentBuild: nil,
                                             executable: app, runningAsAgent: false),
              "a bundle with no build stamp is not guessed at")
        if failures == 0 { print("PASS: login default") }
        exit(failures == 0 ? 0 : 1)
    }
}
