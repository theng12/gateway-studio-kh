import Foundation

// Launch at login is on by default (`menubar/LoginDefault.swift`), switchable off, and only the
// installed app bundle ever writes the LaunchAgent.
// The job check (`LaunchAgentJobCheck` in `menubar/main.swift`) is compiled in beside this file:
// the python test copies it out of `main.swift` and puts it on the command line.

var failures = 0
func check(_ ok: Bool, _ what: String) {
    if !ok { failures += 1; print("FAIL: \(what)") }
}

/// What `launchctl print gui/501/kh.gateway.menubar` printed on 1301, 2026-11-17: a row left
/// behind by the old SMAppService registration. It wears the label, so `launchctl print` exits 0,
/// but it is not the plist's job.
let submittedRow = """
kh.gateway.menubar = {
\tactive count = 0
\tpath = (submitted by smd.93)
\ttype = Submitted
\tmanaged_by = com.apple.xpc.ServiceManagement
\tjob state = spawn failed
\truns = 2
\tlast exit reason = OS_REASON_CODESIGNING
}
"""

/// What it prints once the plist owns the label.
let plistRow = """
kh.gateway.menubar = {
\tactive count = 0
\tpath = /Users/one/Library/LaunchAgents/kh.gateway.menubar.plist
\ttype = Loaded
\tprogram = /Applications/gateway-kh.app/Contents/MacOS/Gateway Studio KH
}
"""

let plistPath = "/Users/one/Library/LaunchAgents/kh.gateway.menubar.plist"

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

        // `isLoaded` means the plist's own job, so a submitted row is not "loaded".
        check(!LaunchAgentJobCheck.isPlistJob(printOutput: submittedRow, plistPath: plistPath),
              "a row submitted by SMAppService is not the plist's job, so isLoaded is false for it")
        check(LaunchAgentJobCheck.isPlistJob(printOutput: plistRow, plistPath: plistPath),
              "the plist's own row counts as loaded")
        check(!LaunchAgentJobCheck.isPlistJob(printOutput: plistRow,
                                              plistPath: "/Users/one/Library/LaunchAgents/kh.gateway.other.plist"),
              "another plist's path is not our job")

        if failures == 0 { print("PASS: login default") }
        exit(failures == 0 ? 0 : 1)
    }
}
