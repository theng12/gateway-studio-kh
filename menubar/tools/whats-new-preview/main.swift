import AppKit

/// Renders the What's New page offscreen to a PNG, so a change to it can be *looked at* without
/// opening a window on the operator's screen.
///
/// **Why this exists.** The rule in this repo for a visual change is "prefer a screenshot over
/// another test run", and the app's own UI suite is reserved for the owner's explicit request
/// because it front-windows the app and presses controls. This harness compiles the *real*
/// `WhatsNewWindow.makePage`, lays it out at a given size, and writes the pixels — no window, no
/// activation, no Accessibility permission, and nothing on screen.
///
/// Build and run from the repository root:
///
/// ```sh
/// swiftc -target arm64-apple-macos11.0 -o /tmp/whats-new-preview \
///     menubar/tools/whats-new-preview/main.swift menubar/WhatsNewWindow.swift \
///     menubar/WhatsNewPage.swift menubar/PageKit.swift
///
/// (The file is named `main.swift` because a multi-file `swiftc` build allows top-level code only
/// in the file with that name.)
/// /tmp/whats-new-preview menubar/whats-new.md /tmp/whats-new-720.png 720 640
/// /tmp/whats-new-preview menubar/whats-new.md /tmp/whats-new-narrow.png 470 640
/// ```
///
/// The narrow size is the one worth looking at: it is where a notes page either wraps or pushes a
/// horizontal scroller into the header.

/// The window's `show()` reads the running build stamp from the app's controller. The page itself
/// takes the stamp as a parameter, so the preview supplies this stand-in and nothing else.
final class GatewayController {
    static let shared = GatewayController()
    var appBuild = "preview-000000000000"
}

let arguments = CommandLine.arguments
let notesPath = arguments.count > 1 ? arguments[1] : "menubar/whats-new.md"
let outputPath = arguments.count > 2 ? arguments[2] : "/tmp/whats-new-preview.png"
let width = arguments.count > 3 ? CGFloat(Double(arguments[3]) ?? 720) : 720
let height = arguments.count > 4 ? CGFloat(Double(arguments[4]) ?? 640) : 640

_ = NSApplication.shared
NSApp.setActivationPolicy(.prohibited)

/// `--window` exercises the shipping path instead of the page builder: `WhatsNewWindow.shared.show()`
/// creates the real window, and calling it twice proves the second open reuses it rather than
/// messaging a window that AppKit has already released. Nothing is ordered front (the app is `.prohibited`).
///
/// **Behind a flag, because it needs the whole app.** `WhatsNewWindow.shared` reaches `MainWindow`,
/// which reaches every section — so a build of this file plus the renderer alone cannot compile it,
/// and that is exactly the build the offscreen half needs. Leaving it unconditional meant the
/// documented command did not compile at all, so the one harness for *looking* at a visual change
/// was unusable. Add `-D PREVIEW_WINDOW` and the app's own sources for this half:
///
/// ```sh
/// swiftc -target arm64-apple-macos11.0 -D PREVIEW_WINDOW -o /tmp/whats-new-preview-window \
///     menubar/tools/whats-new-preview/main.swift menubar/*.swift
/// /tmp/whats-new-preview-window --window
/// ```
#if PREVIEW_WINDOW
if arguments.contains("--window") {
    let first = WhatsNewWindow.shared
    first.show()
    first.show()
    let windows = NSApp.windows.filter { $0.title.contains("What's New") }
    guard let window = windows.first else {
        FileHandle.standardError.write("show() created no What's New window\n".data(using: .utf8)!)
        exit(5)
    }
    print("window: title=\"\(window.title)\" windows=\(windows.count) visible=\(window.isVisible)"
          + " tabbing=\(window.tabbingMode == .disallowed ? "disallowed" : "allowed")"
          + " frame=\(Int(window.frame.width))x\(Int(window.frame.height))")
    exit(0)
}
#endif

guard let notes = try? String(contentsOfFile: notesPath, encoding: .utf8) else {
    FileHandle.standardError.write("cannot read \(notesPath)\n".data(using: .utf8)!)
    exit(2)
}

let appearanceName = arguments.count > 5 ? arguments[5] : "dark"
// How far down the notes to render. The visible top is what a reader sees first; a later offset is
// where the bullets and rules live, and a page that only ever gets looked at from the top is a page
// whose lists nobody has seen.
let offset = arguments.count > 6 ? CGFloat(Double(arguments[6]) ?? 0) : 0
let appearance = NSAppearance(named: appearanceName == "light" ? .aqua : .darkAqua)
NSApp.appearance = appearance

// `appVersion` and `makePage` moved to `WhatsNewPage` when the renderer was split out of the
// window in 0.2.66, and this tool was left naming the old home — so the one harness for LOOKING
// at a visual change stopped compiling, and the documented command below stopped working with
// it. The tool has to name what the app names.
let page = WhatsNewPage.makePage(notes: notes, version: WhatsNewPage.appVersion, build: "preview-000000000000")
let frame = NSRect(x: 0, y: 0, width: width, height: height)
page.frame = frame

// A window is what gives the page a real layout pass and a real appearance; it is never ordered
// front, so nothing appears on anyone's screen.
let window = NSWindow(contentRect: frame, styleMask: [.titled], backing: .buffered, defer: false)
window.appearance = appearance
window.contentView = page
window.layoutIfNeeded()
page.layoutSubtreeIfNeeded()
page.layoutSubtreeIfNeeded()

// A layout report, because a blank PNG and a squeezed page look the same in a file listing: this
// says what the constraints resolved to, and `document == (width - 56)` is the wrapping contract.
func report(_ view: NSView) {
    let scroll = view.subviews.compactMap { $0 as? NSScrollView }.first
    let document = scroll?.documentView
    let column = document?.subviews.first
    let stack = column as? NSStackView
    print("page \(Int(view.bounds.width))x\(Int(view.bounds.height))"
          + " | document \(Int(document?.bounds.width ?? 0))x\(Int(document?.bounds.height ?? 0))"
          + " | blocks \(stack?.arrangedSubviews.count ?? 0)")
    if let first = stack?.arrangedSubviews.first {
        print("  first block \(type(of: first)) \(Int(first.frame.width))x\(Int(first.frame.height))"
              + " at y=\(Int(first.frame.origin.y))")
    }
}
report(page)

// The page's own background, so the pixels are the window's and not a transparent hole: text in a
// dark appearance is white, and white on transparent reads as an empty image.
let background = NSView(frame: frame)
background.wantsLayer = true
background.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
background.addSubview(page)
page.frame = background.bounds
window.contentView = background
window.layoutIfNeeded()
background.layoutSubtreeIfNeeded()

guard let rep = background.bitmapImageRepForCachingDisplay(in: background.bounds) else {
    FileHandle.standardError.write("no bitmap for \(Int(width))x\(Int(height))\n".data(using: .utf8)!)
    exit(3)
}
if offset > 0, let scroll = page.subviews.compactMap({ $0 as? NSScrollView }).first {
    // Scroll first, then lay out: `cacheDisplay` copies what the clip view is showing, and a scroll
    // that happens after the layout pass is a scroll the render never sees.
    scroll.contentView.scroll(to: NSPoint(x: 0, y: offset))
    scroll.reflectScrolledClipView(scroll.contentView)
    background.layoutSubtreeIfNeeded()
}
background.cacheDisplay(in: background.bounds, to: rep)
guard let png = rep.representation(using: .png, properties: [:]) else { exit(4) }
try png.write(to: URL(fileURLWithPath: outputPath))
print("wrote \(outputPath) at \(Int(width))x\(Int(height))")
