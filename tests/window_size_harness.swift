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

let updateKeyHelp = "Signs Studio Hub and Story Studio updates so each Mac knows an update really came from this Gateway. Safe to share — it is not a password."
let updateKeyCaption = "Paste this into each Mac (Studio Hub → Connections → App Update Key)"

func descendants(_ view: NSView) -> [NSView] {
    [view] + view.subviews.flatMap(descendants)
}

/// The App Update Key page carries one long value that has to be readable whole at the narrowest
/// window. A label stays inside its card while still clipping its last line, so the check is the
/// text's own required line-fragment height at the width it actually got.
func assertComplete(_ window: NSWindow, _ appearance: String, _ what: String, _ text: String) {
    guard let content = window.contentView,
          let label = descendants(content).compactMap({ $0 as? NSTextField })
              .first(where: { $0.stringValue == text }) else {
        failures.append("\(appearance): \(what) is missing")
        return
    }
    let required = ceil(label.attributedStringValue.boundingRect(
        with: NSSize(width: label.bounds.width, height: CGFloat.greatestFiniteMagnitude),
        options: [.usesLineFragmentOrigin, .usesFontLeading]
    ).height)
    if label.bounds.height + 0.5 < required {
        failures.append("\(appearance): \(what) is \(label.bounds.height)pt high but its complete text requires \(required)pt at \(label.bounds.width)pt wide")
    }
}

/// The App Update Key the owner pastes must be the whole key: 64 hex characters, not a truncated
/// line. The page used to show three numbers, one of them ellipsised, and the fingerprint went into
/// Studio Hub's field by mistake. This is the regression check for that.
func assertWholeUpdateKey(_ window: NSWindow, _ appearance: String) -> String? {
    guard let content = window.contentView else { return nil }
    let fields = descendants(content).compactMap { $0 as? NSTextField }
    let hex = fields.first(where: {
        $0.stringValue.count == 64 && $0.stringValue.allSatisfy { $0.isHexDigit }
    })
    guard let hex else {
        failures.append("\(appearance): the App Update Key hex value is missing")
        return nil
    }
    if hex.stringValue.contains("…") || hex.lineBreakMode == .byTruncatingMiddle
        || hex.lineBreakMode == .byTruncatingTail {
        failures.append("\(appearance): the App Update Key is truncated on screen")
    }
    if !hex.isSelectable {
        failures.append("\(appearance): the App Update Key is not selectable")
    }
    if fields.contains(where: { $0.stringValue == "Public key (base64)" || $0.stringValue == "Fingerprint" }) {
        failures.append("\(appearance): a second App Update Key value is still on the page")
    }
    let copy = descendants(content).compactMap { $0 as? NSButton }.first(where: {
        $0.action == NSSelectorFromString("copyUpdateKey")
    })
    if copy == nil { failures.append("\(appearance): the App Update Key Copy button is missing") }
    assertComplete(window, appearance, "the App Update Key help text", updateKeyHelp)
    assertComplete(window, appearance, "the App Update Key caption", updateKeyCaption)
    assertComplete(window, appearance, "the App Update Key value", hex.stringValue)
    return hex.stringValue
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
var shownKeys: [String] = []
for (name, appearance) in [("light", NSAppearance.Name.aqua), ("dark", NSAppearance.Name.darkAqua)] {
    window.appearance = NSAppearance(named: appearance)
    _ = MainWindow.shared.show(section: "settings")
    spin(0.5)
    window.contentView?.layoutSubtreeIfNeeded()
    if let key = assertWholeUpdateKey(window, name) { shownKeys.append(key) }
}
if shownKeys.count == 2, shownKeys[0] != shownKeys[1] {
    failures.append("the App Update Key differs between light and dark")
}
if failures.isEmpty { print("PASS: switching pages never resized the window") }
else { failures.forEach { print("FAIL: \($0)") }; exit(1) }
exit(0)
