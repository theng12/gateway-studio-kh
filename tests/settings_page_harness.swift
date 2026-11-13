// Settings, offscreen: the four Received-artifacts limits, and the one-line update statement.
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

// ---- updates: one sentence, and no key, fingerprint or trust control on the page
let sentence = "Updates are installed by Studio Launcher KH."
if texts().first(where: { $0.stringValue == sentence }) == nil {
    failures.append("the Updates section does not carry \"\(sentence)\"")
}
let hexValues = texts().filter {
    $0.stringValue.count >= 40 && $0.stringValue.allSatisfy { $0.isHexDigit }
}
if !hexValues.isEmpty {
    failures.append("the page shows \(hexValues.count) long hex value(s); no update key belongs here")
}
if texts().contains(where: {
    $0.stringValue.contains("App Update Key") || $0.stringValue.contains("Fingerprint")
        || $0.stringValue.contains("Public key")
}) {
    failures.append("the page still presents an App Update Key, fingerprint or public key")
}
if descendants(content).compactMap({ $0 as? NSButton })
    .contains(where: { $0.action == NSSelectorFromString("copyUpdateKey") }) {
    failures.append("a key Copy control is still on the page")
}

if failures.isEmpty { print("PASS: the limits load without the usage walk and updates are one sentence") }
else { failures.forEach { print("FAIL: \($0)") }; exit(1) }
exit(0)
