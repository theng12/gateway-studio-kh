import Foundation

/// **Launch at login is on by default, and the owner can switch it off** (owner rule: "on by
/// default unless they say otherwise"; Developer 2026-11-08). On 2026-11-08 this Mac restarted
/// and the gateway stayed down, because the LaunchAgent was only ever written by the Settings
/// checkbox and nobody had ticked it.
///
/// The plist stays the one record of whether it is *on*; `optedOut` records only the owner's
/// explicit "off", so a fresh install turns it on and an unticked box stays unticked.
enum LoginDefault {
    /// The only bundle that may write the owner's LaunchAgent; a test binary or a build in
    /// `menubar/build/` never installs one by accident.
    static let appExecutable = "/Applications/gateway-kh.app/Contents/MacOS/GatewayKH"

    static func shouldInstall(installed: Bool, optedOut: Bool?, executable: String?) -> Bool {
        !installed && optedOut != true && executable == appExecutable
    }
}
