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
    static let appExecutable = "/Applications/gateway-kh.app/Contents/MacOS/Gateway Studio KH"

    static func shouldInstall(installed: Bool, optedOut: Bool?, executable: String?) -> Bool {
        !installed && optedOut != true && executable == appExecutable
    }

    /// **Register again once per new build** (2026-11-16: 1301's agent sat at "spawn failed",
    /// exit 78, 10 060 runs). macOS keeps the registered bundle's launch constraint with the
    /// record, and an update replaces the code, so a record from an older build can stop matching
    /// the app on disk; `service.status` still reads `.enabled`, so the app never redid it.
    ///
    /// Not from the copy launchd itself started (`runningAsAgent`): it evidently spawns, and
    /// unregistering would stop this very process. That copy only records the build.
    static func shouldReregister(enabled: Bool, registeredBuild: String?, currentBuild: String?,
                                 executable: String?, runningAsAgent: Bool) -> Bool {
        enabled && !runningAsAgent && executable == appExecutable
            && currentBuild != nil && registeredBuild != currentBuild
    }
}
