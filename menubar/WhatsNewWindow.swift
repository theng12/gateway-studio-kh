import AppKit

/// What's New — rendered as a page, not as a file.
///
/// **What this replaces, and why.** The window used to hand the whole notes file to one `NSTextView`
/// and set a system font on it: the reader saw literal `##`, `**bold**` and backticks, a build stamp
/// glued to the top of the text, and every line at one size. Studio Hub's What's New page is the
/// reference the owner asked for — a large *What's New* with the version beside it, a rule, then the
/// notes as headings, paragraphs and bullets at their own weights — and the two apps are read side
/// by side, so this is the same page furniture (`PageKit`) and the same block rules Studio Hub's
/// `MarkdownText` uses, rather than a second design.
///
/// The text still ships **inside the bundle** (`Contents/Resources/CHANGELOG.md`), so the screen
/// describes the build that is actually running rather than whatever the checkout says today.
final class WhatsNewWindow: NSObject {
    static let shared = WhatsNewWindow()

    private var window: NSWindow?

    /// **Show the notes.** Since 0.2.66 that means selecting the *What's New* page in the main
    /// window's sidebar rather than opening a window of its own: the owner asked for Studio Hub's
    /// shape, where What's New is a row directly above Settings, and a page reachable from three
    /// doors is better than a modal reachable from three doors.
    ///
    /// This class keeps its job as the **renderer** — `makePage` and `NotesMarkdown` are what the
    /// page shows, so there is one implementation of this typography rather than a section that
    /// copies it. `makePanel()` below is kept because a headless launch has no sidebar to select
    /// into, and because it is how the page can be measured without putting a window on a screen.
    func show() {
        if MainWindow.shared.show(section: "what's new") {
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        makePanel().makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// The notes as a standalone panel. Not the normal route any more — see `show()`.
    @discardableResult
    func makePanel() -> NSWindow {
        if let existing = window {
            return existing
        }

        let content = WhatsNewPage.makePage(
            notes: WhatsNewPage.notes(),
            version: WhatsNewPage.appVersion,
            build: GatewayController.shared.appBuild
        )

        let panel = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 640),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "What's New in gateway-kh"
        panel.contentView = content
        // A notes window must not be able to resize itself either; it has a scroller for exactly
        // that reason. `minSize` is small enough to read on a laptop screen without full screen.
        panel.minSize = NSSize(width: 460, height: 320)
        panel.setFrameAutosaveName("WhatsNewWindow")
        // Two things the old window got wrong by omission:
        //
        //   * macOS merged it into the Overview window as a *tab*, which is why it read as a modal
        //     bolted onto another window. Notes are their own window.
        //   * a programmatic `NSWindow` is released when closed by default, while this class keeps a
        //     reference to it — so the second open could have been a message to a dead window.
        panel.tabbingMode = .disallowed
        panel.isReleasedWhenClosed = false
        panel.center()
        window = panel
        return panel
    }
}
