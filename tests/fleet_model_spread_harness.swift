import AppKit

let arguments = CommandLine.arguments
let launchWindow: String? = nil
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fatalError(message) }
}

func descendants(_ view: NSView) -> [NSView] {
    [view] + view.subviews.flatMap(descendants)
}

func plan(_ state: String, failed: Bool = false) -> FleetModelSpread {
    let macs = (0..<60).map { index in
        FleetSpreadMac(locationId: "fixture", machine: "mac-\(index)", label: "Mac \(index)",
            state: failed && index == 0 ? "failed" : (state == "running" && index < 3 ? "copying" : "done"),
            text: failed && index == 0 ? "Failed: The model copy could not be verified. Retry from the network source."
                : (state == "running" && index < 3 ? "Copying 42% from Mac 9 (LAN)" : "Installed from the internet"),
            note: index == 59 ? "Long final machine note remains available in details." : nil)
    }
    return FleetModelSpread(id: "spread-fixture", packageId: "fixture-4bit",
        label: "Fish Audio S2 Pro 5B MLX 4-bit", state: state,
        done: failed ? 59 : (state == "running" ? 57 : 60), total: 60,
        networks: [FleetSpreadNetwork(network: "A", title: "Network A", done: 20, total: 20, macs: Array(macs[0..<20])),
                   FleetSpreadNetwork(network: "B", title: "Network B", done: 20, total: 20, macs: Array(macs[20..<40])),
                   FleetSpreadNetwork(network: "C", title: "Network C", done: 20, total: 20, macs: Array(macs[40..<60]))])
}

let view = FleetModelSpreadView()
view.translatesAutoresizingMaskIntoConstraints = false
view.widthAnchor.constraint(equalToConstant: 584).isActive = true
view.update([plan("finished")])
view.layoutSubtreeIfNeeded()
require(view.fittingSize.height < 180, "a completed 60-Mac report must start compact instead of pushing the model tables below it")
require(!descendants(view).compactMap { $0 as? NSTextField }.contains { $0.stringValue.hasPrefix("Mac 59 —") },
        "completed machine details must be collapsed initially")

func disclosure(_ root: NSView) -> NSButton {
    guard let button = descendants(root).compactMap({ $0 as? NSButton }).first(where: {
        $0.title == "Show details" || $0.title == "Hide details"
    }) else { fatalError("the report needs a keyboard-accessible details disclosure") }
    require(button.accessibilityRole() == .disclosureTriangle, "details must expose its disclosure role")
    return button
}

disclosure(view).performClick(nil)
view.layoutSubtreeIfNeeded()
let scroll = descendants(view).compactMap { $0 as? NSScrollView }.first!
require(scroll.fittingSize.height <= 221, "the details scroller must stay within 220 points")
require(descendants(view).compactMap { $0 as? NSTextField }.contains {
    $0.stringValue.contains("Mac 59") && $0.stringValue.contains("Long final machine note")
}, "all saved machine results and notes must remain inspectable inside bounded details")
view.update([plan("finished")])
require(disclosure(view).title == "Hide details", "polling must retain this plan's deliberate expansion")
disclosure(view).performClick(nil)
require(disclosure(view).title == "Show details", "details must close without deleting the summary")

var stopped: String?
view.onStop = { stopped = $0.id }
view.update([plan("running")])
guard let stop = descendants(view).compactMap({ $0 as? NSButton }).first(where: { $0.title == "Stop" })
    else { fatalError("Stop must remain outside collapsible details for active installs") }
stop.performClick(nil)
require(stopped == "spread-fixture", "Stop must keep its original plan-owned callback")
require(descendants(view).compactMap { $0 as? NSTextField }.contains { $0.stringValue.contains("3 active") },
        "active machine progress must remain visible outside collapsed details")
disclosure(view).performClick(nil)
view.update([plan("finished")])
require(disclosure(view).title == "Show details", "a successful completed run must collapse after active polling")
require(!descendants(view).compactMap { $0 as? NSButton }.contains { $0.title == "Stop" },
        "finished reports must not offer an active Stop action")

view.update([plan("finished", failed: true)])
let failures = descendants(view).compactMap { $0 as? NSTextField }
require(failures.contains { $0.stringValue.contains("1 failed") }, "failure count must remain visible in the compact summary")
require(failures.contains { $0.stringValue.contains("could not be verified") && $0.maximumNumberOfLines == 2 },
        "the first failure must remain visible with a bounded two-line explanation")
view.update([plan("finished", failed: true)])
require(descendants(view).compactMap { $0 as? NSTextField }.contains { $0.stringValue.contains("1 failed") },
        "polling must not hide the failed terminal state")

let tab = FleetModelsTab(frame: .zero)
tab.update(models: [], workers: [], filter: FleetFilter(), installs: [plan("finished")])
let window = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: 860, height: 700),
                      styleMask: [.borderless, .resizable], backing: .buffered, defer: false)
let host = NSView()
tab.translatesAutoresizingMaskIntoConstraints = false
host.addSubview(tab)
NSLayoutConstraint.activate([
    tab.leadingAnchor.constraint(equalTo: host.leadingAnchor, constant: DS.Size.sidebar),
    tab.trailingAnchor.constraint(equalTo: host.trailingAnchor),
    tab.topAnchor.constraint(equalTo: host.topAnchor),
    tab.bottomAnchor.constraint(equalTo: host.bottomAnchor),
])
window.contentView = host
window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
window.orderFront(nil)
window.contentView?.layoutSubtreeIfNeeded()
let before = window.frame.size
disclosure(tab).performClick(nil)
window.contentView?.layoutSubtreeIfNeeded()
require(window.frame.size == before, "opening bounded details must not resize the window")
func detailScroll(_ root: NSView) -> NSScrollView {
    guard let scroll = descendants(root).compactMap({ $0 as? NSScrollView }).first(where: {
        $0.frame.height > 200 && $0.frame.height <= 221
    }) else { fatalError("open report needs its bounded details scroll view") }
    return scroll
}
let deepScroll = detailScroll(tab)
require(abs((deepScroll.documentView?.frame.width ?? 0) - deepScroll.contentView.bounds.width) < 1,
        "details document width must track its clip view without horizontal overflow")
deepScroll.contentView.scroll(to: NSPoint(x: 0, y: 500))
deepScroll.reflectScrolledClipView(deepScroll.contentView)
let readingPosition = deepScroll.contentView.bounds.origin.y
require(readingPosition > 100, "the scroll retention proof must start deep in the machine report")
tab.update(models: [], workers: [], filter: FleetFilter(), installs: [plan("finished")])
window.contentView?.layoutSubtreeIfNeeded()
require(abs(detailScroll(tab).contentView.bounds.origin.y - readingPosition) < 1,
        "polling must preserve the machine report's reading position")
tab.update(models: [], workers: [], filter: FleetFilter(), installs: [])
tab.update(models: [], workers: [], filter: FleetFilter(), installs: [plan("finished")])
window.contentView?.layoutSubtreeIfNeeded()
require(disclosure(tab).title == "Show details", "a report no longer shown must drop its UI expansion state")
disclosure(tab).performClick(nil)
window.contentView?.layoutSubtreeIfNeeded()
require(detailScroll(tab).contentView.bounds.origin.y == 0,
        "a report no longer shown must drop its retained scroll position")
disclosure(tab).performClick(nil)
window.contentView?.layoutSubtreeIfNeeded()
require(window.frame.size == before, "closing details must not resize the window")
var long = plan("running")
long.label = "Fish Audio S2 Pro 5B MLX 4-bit — extended publisher model label with a long installation description that must wrap within the minimum Fleet pane and keep Stop reachable"
tab.update(models: [], workers: [], filter: FleetFilter(), installs: [long])
window.contentView?.layoutSubtreeIfNeeded()
require(window.frame.size == before, "a long model label must not force the minimum window wider")
let longStop = descendants(tab).compactMap { $0 as? NSButton }.first { $0.title == "Stop" }!
let stopRect = tab.convert(longStop.bounds, from: longStop)
require(stopRect.minX >= 0 && stopRect.maxX <= tab.bounds.width,
        "Stop must remain reachable within the narrow Fleet pane for a long model title")
window.orderOut(nil)
print("PASS: compact completed, active, failed and bounded detail report behavior; 60 records retained; window geometry preserved")
