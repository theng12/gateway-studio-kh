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
                                          executable: "/tmp/build/gateway-kh.app/Contents/MacOS/GatewayKH"),
              "a build outside /Applications never installs")
        check(!LoginDefault.shouldInstall(installed: false, optedOut: nil, executable: nil),
              "an unknown executable never installs")
        if failures == 0 { print("PASS: login default") }
        exit(failures == 0 ? 0 : 1)
    }
}
