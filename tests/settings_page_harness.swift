// Settings, offscreen: the four Received-artifacts limits, and the one App Update Key value.
//
// Compiled by `tests/test_settings_retention_fields.py` as the entry point of a throwaway copy of
// the app (see `menubar/render/patch_sources.py`), against a stub gateway whose limits read answers
// at once and whose usage walk never answers at all. That is the shape of the defect being pinned:
// the limits used to ride on the usage walk in one response, and a read the app abandons at its
// ceiling blanked all four fields.
import AppKit

let arguments = CommandLine.arguments
let launchWindow: String? = nil
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
GatewayController.shared.state = .running(providers: 4, models: 9)

/// The key the stub gateway publishes. The same constant is in the Python test beside this file.
let stubKey = "e6b9eece5ec6b3f4a1d0c9b8a7f6e5d4c3b2a1908f7e6d5c4b3a291807f6e5d4"

var failures: [String] = []

func spin(_ seconds: Double) {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
}

func descendants(_ view: NSView) -> [NSView] {
    [view] + view.subviews.flatMap(descendants)
}

_ = MainWindow.shared.show(section: "settings")
guard let window = MainWindow.shared.window, let content = window.contentView else {
    print("FAIL: no window"); exit(1)
}
window.setFrameOrigin(NSPoint(x: -20000, y: -20000))

let texts = { descendants(content).compactMap { $0 as? NSTextField } }

/// The editable fields of one form row, left to right. The caption is that row's own label, so this
/// reads the two fields beside the limit it names rather than any number anywhere on the page.
func rowFields(_ caption: String) -> [String] {
    guard let label = texts().first(where: { $0.stringValue == caption }),
          let row = label.superview else { return [] }
    return descendants(row).compactMap { $0 as? NSTextField }
        .filter { $0.isEditable }
        .sorted { $0.frame.minX < $1.frame.minX }
        .map { $0.stringValue }
}

// **The limits must arrive without the usage walk.** Poll rather than sleep a fixed time, so a slow
// machine does not fail a working page — but the ceiling is deliberately small, because the whole
// point is that these four numbers wait for nothing slow.
var limits: [[String]] = []
let deadline = Date().addingTimeInterval(10)
while Date() < deadline {
    spin(0.25)
    limits = [rowFields("Keep for"), rowFields("Trash Folder")]
    if limits[0].count == 2, limits[1].count == 2,
       !limits[0].contains(""), !limits[1].contains("") { break }
}
if limits != [["3", "8"], ["7", "20"]] {
    failures.append("the limits read \(limits), not [[3, 8], [7, 20]] — the stub answers the limits "
        + "at once, so these numbers must not be waiting on the usage walk")
}

// The stub's usage walk never answers, so the "in use" line is still empty. If it were not, the
// assertion above would be passing for the wrong reason.
if let inUse = texts().first(where: { $0.stringValue == "In use" }),
   let row = inUse.superview {
    let line = descendants(row).compactMap { $0 as? NSTextField }.first(where: { $0 !== inUse })
    if let line, !line.stringValue.isEmpty {
        failures.append("the usage line answered after all (\(line.stringValue)); this check needs it to stay silent")
    }
}

// ---- the App Update Key: one value, whole, and it is the hex key
let keyLabels = texts().filter { $0.stringValue.count == 64 && $0.stringValue.allSatisfy { $0.isHexDigit } }
guard let key = keyLabels.first else {
    failures.append("the App Update Key is not on the page as a 64-character hex value")
    print(failures.map { "FAIL: \($0)" }.joined(separator: "\n")); exit(1)
}
if key.stringValue != stubKey {
    failures.append("the App Update Key reads \(key.stringValue), not the key the gateway published")
}
if !key.isSelectable {
    failures.append("the App Update Key is not selectable, so it cannot be copied by hand")
}
if key.lineBreakMode == .byTruncatingMiddle || key.lineBreakMode == .byTruncatingTail
    || key.stringValue.contains("…") {
    failures.append("the App Update Key is truncated on screen")
}
// The second long value is what put the wrong string into a Mac in the first place. One is allowed.
let longValues = texts().filter {
    $0.stringValue.count >= 40 && $0.stringValue.allSatisfy { $0.isHexDigit }
}
if longValues.count != 1 {
    failures.append("the page shows \(longValues.count) long key values; it must show one")
}
if texts().contains(where: { $0.stringValue == "Fingerprint" || $0.stringValue == "Public key (base64)"
    || $0.stringValue == "Technical details" || $0.stringValue == "Public key (hex)" }) {
    failures.append("a second App Update Key value is still labelled on the page")
}
let caption = "Paste this into each Mac (Studio Hub → Connections → App Update Key)"
if !texts().contains(where: { $0.stringValue == caption }) {
    failures.append("the App Update Key is not labelled with where it is pasted")
}
let expectedHint = "To check a Mac: its App Update Key should start with \(stubKey.prefix(8))."
if !texts().contains(where: { $0.stringValue == expectedHint }) {
    failures.append("the compare line is not \"\(expectedHint)\"")
}
if descendants(content).compactMap({ $0 as? NSButton })
    .first(where: { $0.action == NSSelectorFromString("copyUpdateKey") }) == nil {
    failures.append("the App Update Key Copy button is missing")
}

if failures.isEmpty { print("PASS: the limits load without the usage walk and one key is shown") }
else { failures.forEach { print("FAIL: \($0)") }; exit(1) }
exit(0)
