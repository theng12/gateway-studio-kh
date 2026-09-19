import Cocoa

/// Shows the bundled "what's new" notes.
///
/// A plain window with a scrolling text view rather than an `NSAlert`: the notes
/// are long, and a modal alert that has to be dismissed to read the bottom is
/// worse than no notes at all. Read-only and selectable, so a line can be copied.
///
/// The text ships **inside the bundle** (`Contents/Resources/whats-new.md`), so it
/// describes the build actually running rather than whatever the repo happens to
/// say today — the same rule the rest of this app follows.
final class WhatsNewWindow: NSObject {
    static let shared = WhatsNewWindow()

    private var window: NSWindow?

    func show() {
        if let existing = window {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let text = Self.notes()
        let view = NSTextView()
        view.isEditable = false
        view.isSelectable = true
        view.drawsBackground = false
        view.textContainerInset = NSSize(width: 18, height: 16)
        view.font = .systemFont(ofSize: 12.5)
        view.string = text
        // Links in the notes should open in the browser, not replace the notes.
        view.isAutomaticLinkDetectionEnabled = true

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.documentView = view
        scroll.translatesAutoresizingMaskIntoConstraints = false

        let content = NSView()
        content.addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: content.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])

        let panel = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 620),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "gateway-kh — What's New"
        panel.contentView = content
        // A notes window must not be able to resize itself either; it has a
        // scroller for exactly that reason.
        panel.minSize = NSSize(width: 460, height: 320)
        panel.setFrameAutosaveName("WhatsNewWindow")
        panel.center()
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        window = panel
    }

    /// The bundled notes, with the build stamp on top so the reader knows which
    /// build these notes belong to.
    private static func notes() -> String {
        let build = GatewayController.shared.appBuild
        let header = "Build \(build)\n\n"
        guard let url = Bundle.main.url(forResource: "whats-new", withExtension: "md"),
              let text = try? String(contentsOf: url, encoding: .utf8) else {
            return header + "No release notes are bundled with this build."
        }
        return header + text
    }
}
