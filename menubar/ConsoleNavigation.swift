// Which pages the console's web view may follow.
//
// **The Gateway key is injected into that web view as a user script, so where the main frame is
// allowed to go is a security boundary, not a convenience.** A link to another origin followed in
// this frame would hand that page the key; every other section's native view had no such problem
// because it was not a browser. The rule is kept in this small file, apart from `MainWindow.swift`,
// so it is testable without building the whole app.

import Foundation

enum ConsoleNavigation {
    /// The starting page (`loadHTMLString` with no base URL) and anything the page itself draws.
    /// These carry no foreign origin and never leave the view.
    private static let ownSchemes: Set<String> = ["about", "data", "blob"]

    static func isSameOrigin(_ url: URL, as console: URL) -> Bool {
        url.scheme?.lowercased() == console.scheme?.lowercased()
            && url.host?.lowercased() == console.host?.lowercased()
            && url.port == console.port
    }

    /// Whether the main frame may load `url`. `true` for the console's own origin and the page's
    /// own schemes; `false` for everything else, including another port or host.
    static func allowsMainFrame(_ url: URL?, console: URL?) -> Bool {
        guard let url else { return true }
        let scheme = (url.scheme ?? "").lowercased()
        if ownSchemes.contains(scheme) { return true }
        guard let console else { return false }
        return isSameOrigin(url, as: console)
    }

    /// An http(s) link the owner clicked, to be handed to the browser — where there is no key.
    /// Any other scheme (mailto, file, a custom app) is cancelled without opening anything.
    static func opensInBrowser(_ url: URL?) -> Bool {
        guard let scheme = url?.scheme?.lowercased() else { return false }
        return scheme == "http" || scheme == "https"
    }
}
