import Foundation

// The menu's Start Hub Studio KH item must open the app this Mac actually has: the renamed
// `Hub Studio KH.app` first, then the pre-rename `Studio Hub.app`, and nothing (so the item can say
// so) when neither is installed. The resolver (`HubAppStart` in `menubar/main.swift`) is compiled
// in beside this file: the python test copies it out of `main.swift` and puts it on the command line.

var failures = 0
func check(_ ok: Bool, _ what: String) {
    if !ok { failures += 1; print("FAIL: \(what)") }
}

@main
enum HubStartPathCheck {
    static func main() {
        let home = "/Users/one"
        let newHome = "\(home)/Applications/Hub Studio KH.app"
        let newGlobal = "/Applications/Hub Studio KH.app"
        let oldHome = "\(home)/Applications/Studio Hub.app"
        let oldGlobal = "/Applications/Studio Hub.app"

        func pick(_ existing: Set<String>) -> String? {
            HubAppStart.resolve(home: home, exists: { existing.contains($0) })
        }

        check(HubAppStart.candidates(home: home) == [newHome, newGlobal, oldHome, oldGlobal],
              "the search order is renamed ~/Applications, renamed /Applications, legacy ~/Applications, legacy /Applications")

        // 1. The renamed app is found wherever it lives.
        check(pick([newHome]) == newHome, "the renamed app in ~/Applications is found")
        check(pick([newGlobal]) == newGlobal, "the renamed app in /Applications is found")

        // 2. A Mac whose Hub has not been renamed yet still starts.
        check(pick([oldHome]) == oldHome, "a Hub not yet renamed still starts from ~/Applications")
        check(pick([oldGlobal]) == oldGlobal, "a Hub not yet renamed still starts from /Applications")

        // The renamed app wins whatever the legacy copies are.
        check(pick([newHome, oldGlobal]) == newHome, "the renamed app in ~/Applications beats a legacy copy in /Applications")
        check(pick([newGlobal, oldHome]) == newGlobal, "the renamed app in /Applications beats a legacy copy in ~/Applications")

        // 3. Neither: nothing is resolved, so the caller can disable the item and say why.
        check(pick([]) == nil, "with no Hub app installed, nothing is resolved")

        if failures == 0 { print("PASS: hub app start path") }
        exit(failures == 0 ? 0 : 1)
    }
}
