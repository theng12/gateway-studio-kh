// Switching pages must never change the window's size. Compiled by tests/test_window_size.py as the entry
// point of a throwaway copy of the app (see menubar/render/patch_sources.py), against the render fixture gateway.
import AppKit

let arguments = CommandLine.arguments
let launchWindow: String? = nil
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
GatewayController.shared.state = .running(providers: 24, models: 36)

func spin(_ seconds: Double) {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
}

let pages = ["audio", "subtitle", "image", "music", "video", "chat", "overview", "jobs", "analytics", "voices",
             "models", "providers", "fleet", "settings", "content packs", "what's new", "overview", "audio"]
var failures: [String] = []

let updateKeyHelp = "Signs Studio Hub and Story Studio updates so each Mac knows an update really came from this Gateway. Safe to share — it is not a password. A Mac shows the first characters of this fingerprint; they should match."

func descendants(_ view: NSView) -> [NSView] {
    [view] + view.subviews.flatMap(descendants)
}

func assertCompleteUpdateKeyHelp(_ window: NSWindow, _ appearance: String, _ disclosure: String) {
    guard let content = window.contentView,
          let label = descendants(content).compactMap({ $0 as? NSTextField })
              .first(where: { $0.stringValue == updateKeyHelp }) else {
        failures.append("\(appearance) \(disclosure): App Update Key help label is missing")
        return
    }
    let required = ceil(label.attributedStringValue.boundingRect(
        with: NSSize(width: label.bounds.width, height: CGFloat.greatestFiniteMagnitude),
        options: [.usesLineFragmentOrigin, .usesFontLeading]
    ).height)
    if label.bounds.height + 0.5 < required {
        failures.append("\(appearance) \(disclosure): App Update Key help is \(label.bounds.height)pt high but its complete text requires \(required)pt at \(label.bounds.width)pt wide")
    }
}

_ = MainWindow.shared.show(section: "audio")
guard let window = MainWindow.shared.window else { print("FAIL: no window"); exit(1) }
window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
let minimum = window.minSize

// The sizes the owner can reach: very large (the Audio page at ~1920 wide), mid, and the designed minimum.
// Each is set once and then every page is visited *in sequence*, which is how the bug showed: Overview
// shrank the window and every later page inherited the smaller size.
for size in [NSSize(width: 1900, height: 1100), NSSize(width: 1350, height: 800), minimum] {
    window.setContentSize(size)
    spin(0.5)
    let expected = window.frame.size
    for page in pages {
        _ = MainWindow.shared.show(section: page)
        spin(1.0)
        window.contentView?.layoutSubtreeIfNeeded()
        if window.frame.size != expected {
            failures.append("\(page): window went \(expected) -> \(window.frame.size) (started at content \(size))")
        }
    }
}

// The Settings page is first built wide above, then narrowed in the same process.
// Measure the text's required line-fragment height at its actual narrow width;
// checking only that the label stays inside the card would miss clipped final lines.
window.setContentSize(NSSize(width: 860, height: 700))
for (name, appearance) in [("light", NSAppearance.Name.aqua), ("dark", NSAppearance.Name.darkAqua)] {
    window.appearance = NSAppearance(named: appearance)
    _ = MainWindow.shared.show(section: "settings")
    spin(0.5)
    window.contentView?.layoutSubtreeIfNeeded()
    guard let content = window.contentView,
          let disclosure = descendants(content).compactMap({ $0 as? NSButton }).first(where: {
              $0.action == NSSelectorFromString("togglePublisherDetails:")
          }) else {
        failures.append("\(name): App Update Key disclosure is missing")
        continue
    }
    if disclosure.state == .on { disclosure.performClick(nil) }
    window.contentView?.layoutSubtreeIfNeeded()
    assertCompleteUpdateKeyHelp(window, name, "collapsed")
    disclosure.performClick(nil)
    window.contentView?.layoutSubtreeIfNeeded()
    assertCompleteUpdateKeyHelp(window, name, "expanded")
    disclosure.performClick(nil)
}
if failures.isEmpty { print("PASS: switching pages never resized the window") }
else { failures.forEach { print("FAIL: \($0)") }; exit(1) }
exit(0)
