import AppKit

/// What's New — a **page in the sidebar**, the way Studio Hub has it.
///
/// **What this replaces.** What's New was a separate `NSWindow` opened from a small link at the
/// bottom of the sidebar and from the Overview page. The owner asked for the Studio Hub shape
/// instead: a row in the sidebar directly **above Settings**, under the same *App* heading, so the
/// two apps are navigated the same way and the notes are somewhere you can simply click rather than
/// a modal bolted onto the window.
///
/// The page itself is not new — `WhatsNewPage.makePage` renders the header, the rule and the
/// notes with Studio Hub's own block rules, and this section shows exactly that view. One renderer,
/// two doors, so a change to the notes' typography cannot reach one and miss the other.
///
/// **Nothing here polls.** The notes ship inside the bundle (`Contents/Resources/whats-new.md`), so
/// they describe the build that is running and cannot change while this process is alive — a
/// rebuilt app is a new process with a new bundle. `refreshesAutomatically` is off for the same
/// reason the type has no `refresh()`: a timer here would re-read a file that is, by construction,
/// the same file.
final class WhatsNewSection: NSObject, AppSection {
    static let shared = WhatsNewSection()

    let title = "What's New"
    /// The same symbol Studio Hub's What's New row uses.
    let symbol = "sparkles"
    let refreshesAutomatically = false

    /// Built once. `makePage` already returns a complete scrolling page — its own `NSScrollView`,
    /// sized to the window it is given — so this is a whole section rather than a fragment that
    /// needs a scroller wrapped around it.
    lazy var view: NSView = WhatsNewPage.makePage(
        notes: WhatsNewPage.notes(),
        version: WhatsNewPage.appVersion,
        build: GatewayController.shared.appBuild
    )

    func activated() {}
}
