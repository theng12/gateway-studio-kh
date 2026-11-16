// Renders pages of the real Gateway app, offscreen, to PNG. A development tool, not a test.
//
//     menubar/render/render-page.sh [pages] [out-dir] [WxH ...]
//
// The app's own sources are compiled as they are (minus its entry point), the page is shown in the app's
// own MainWindow — sidebar and all — in an NSWindow parked far off every screen, and the window's
// content view is written out as a bitmap. Web console pages (Models, Providers, Content Packs) are
// WKWebViews, which `cacheDisplay` cannot draw, so their `takeSnapshot` is composited over the shell.
//
// It talks only to the fixture gateway the driver starts (a random local port, a temp profile).
import AppKit
import WebKit

// `arguments` and `launchWindow` are globals the app's own entry point declares; AppDelegate reads them.
let arguments = CommandLine.arguments
let launchWindow: String? = nil
let pageNames = (arguments.count > 1 ? arguments[1] : "overview").split(separator: ",").map(String.init)
let output = arguments.count > 2 ? arguments[2] : "/tmp/gateway-render"
var sizes: [(CGFloat, CGFloat)] = []
for size in arguments.dropFirst(3) {
    let parts = size.lowercased().split(separator: "x")
    if parts.count == 2, let w = Double(parts[0]), let h = Double(parts[1]) { sizes.append((CGFloat(w), CGFloat(h))) }
}
if sizes.isEmpty { sizes = [(1100, 800), (1600, 1000)] }
// One appearance per process. Several views here paint a layer colour once, from `NSColor.cgColor`, at the
// moment they are built (see the audit's dark/light list), so the appearance has to be set *before* the
// window exists and a second appearance needs a second process; render-page.sh does that for `both`.
let darkMode = ProcessInfo.processInfo.environment["GATEWAY_RENDER_APPEARANCE"] == "dark"
let suffixForAppearance = darkMode ? "-dark" : ""

let application = NSApplication.shared
application.setActivationPolicy(.accessory)
application.appearance = NSAppearance(named: darkMode ? .darkAqua : .aqua)
try? FileManager.default.createDirectory(atPath: output, withIntermediateDirectories: true)
GatewayController.shared.state = .running(providers: 24, models: 36)

func spin(_ seconds: Double) {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
}

/// `takeSnapshot` returns white for a view in a window that is not on any screen, so the page is asked for a PDF
/// of itself instead: the same layout, drawn from the render tree rather than from the screen.
func snapshot(_ web: WKWebView) -> NSImage? {
    var image: NSImage?
    var done = false
    let configuration = WKPDFConfiguration()
    configuration.rect = web.bounds
    web.createPDF(configuration: configuration) { result in
        if case .success(let data) = result { image = NSImage(data: data) }
        done = true
    }
    let deadline = Date().addingTimeInterval(8)
    while !done && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
    return image
}

func write(_ window: NSWindow, web: WKWebView?, to path: String) {
    guard let view = window.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
    // `cacheDisplay` paints the window's own background white, whatever the appearance, so a dark render would be
    // white-on-white. Give the content view a layer filled with the real window colour, resolved in the appearance.
    view.wantsLayer = true
    window.effectiveAppearance.performAsCurrentDrawingAppearance {
        view.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
    }
    // A selected sidebar row's highlight is an NSVisualEffectView (`.selection`) the window server composites;
    // `cacheDisplay` draws it as a black slab over the row's label. Paint the inactive-window grey in its place.
    func flattenSelection(_ v: NSView) {
        if let effect = v as? NSVisualEffectView, effect.material == .selection, !effect.isHidden, let row = effect.superview {
            effect.isHidden = true
            let box = NSBox(frame: effect.frame)
            box.boxType = .custom
            box.borderWidth = 0
            box.cornerRadius = 5
            window.effectiveAppearance.performAsCurrentDrawingAppearance { box.fillColor = .unemphasizedSelectedContentBackgroundColor }
            row.addSubview(box, positioned: .below, relativeTo: nil)
        }
        v.subviews.forEach(flattenSelection)
    }
    flattenSelection(view)
    view.cacheDisplay(in: view.bounds, to: rep)
    let shot = web.flatMap { snapshot($0) }
    let target = web.map { view.convert($0.bounds, from: $0) } ?? .zero
    let composed = NSImage(size: view.bounds.size, flipped: false) { rect in
        rep.draw(in: rect)
        shot?.draw(in: target)
        return true
    }
    guard let tiff = composed.tiffRepresentation, let merged = NSBitmapImageRep(data: tiff),
          let png = merged.representation(using: .png, properties: [:]) else { return }
    try? png.write(to: URL(fileURLWithPath: path))
    print("wrote \(path)")
}

/// `fleet-rename`: the Fleet page's Rename sheet (an NSAlert, so not part of any page) for a sample node, and
/// the titles of that node's "…" menu, so the sheet can be looked at without opening the app.
func renderRenameSheet() {
    let json = #"{"location_id":"ctl1","machine":"terranash-0108-m1-8-256-hub","label":"terranash-0108-m1-8-256","reachable":true}"#
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    guard let machine = try? decoder.decode(FleetMachine.self, from: Data(json.utf8)) else { print("no sample machine"); return }
    if let button = FleetStartup.menuButton(for: machine, window: { nil }, done: {}) as? FleetMenuButton {
        print("menu: \(button.itemTitles)")
    }
    let (alert, field) = FleetStartup.renameAlert(for: machine)
    field.stringValue = "terranash-1409-m1-8-256"
    alert.layout()
    alert.window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
    alert.window.orderFront(nil)
    spin(0.8)
    write(alert.window, web: nil, to: "\(output)/fleet-rename\(suffixForAppearance).png")
    alert.window.orderOut(nil)
}

/// `fleet-workstation`: the Lite Mode sheet for a Mac the shop is using, in the owner's defaults (works
/// through use, `fleet-workstation.png`) and with Only-when-idle and Step-aside on (`fleet-workstation-idle.png`);
/// and the "…" menu titles for that Mac and for one whose Studio Hub is too old to report the mode.
func renderWorkstationSheet() {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    func sample(idleOnly: Bool, stepAside: Bool) -> String {
        #"{"location_id":"ctrl-shops","machine":"shop-2","label":"Shop 2","reachable":true,"node_role":"agent","workstation":{"enabled":true,"idle_only":\#(idleOnly),"idle_minutes":3,"step_aside":\#(stepAside),"resume_after_minutes":2,"start_free_gb":null,"quiet_hours":["22:00-07:00"],"hide_menu_bar_icon":false,"state":"standing_by","reason":"someone is using this Mac","yields_today":0,"jobs_today":2}}"#
    }
    let old = #"{"location_id":"ctrl-shops","machine":"shop-6","label":"Shop 6","reachable":true,"node_role":"agent"}"#
    guard let machine = try? decoder.decode(FleetMachine.self, from: Data(sample(idleOnly: false, stepAside: false).utf8)),
          let tooOld = try? decoder.decode(FleetMachine.self, from: Data(old.utf8)) else { print("no sample machine"); return }
    for sample in [machine, tooOld] {
        if let button = FleetStartup.menuButton(for: sample, window: { nil }, done: {}) as? FleetMenuButton {
            print("menu \(sample.label ?? ""): \(button.itemTitles)")
        }
    }
    for (suffix, idleOnly, stepAside) in [("", false, false), ("-idle", true, true)] {
        guard let shown = try? decoder.decode(FleetMachine.self, from: Data(sample(idleOnly: idleOnly, stepAside: stepAside).utf8)),
              let current = shown.workstation else { continue }
        let (alert, _) = FleetWorkstationForm.alert(for: shown, current)
        alert.layout()
        alert.window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        alert.window.orderFront(nil)
        spin(0.8)
        write(alert.window, web: nil, to: "\(output)/fleet-workstation\(suffix)\(suffixForAppearance).png")
        alert.window.orderOut(nil)
    }
    // `fleet-workstation-pending.png`: an offline Mac with a change waiting; the sheet opens on the waiting values.
    let seen = Date().timeIntervalSince1970 - 7200
    let pending = #"{"location_id":"ctrl-shops","machine":"shop-3","label":"Shop 3","reachable":false,"state":"offline","node_role":"agent","workstation":{"enabled":true,"idle_only":true,"idle_minutes":3,"step_aside":true,"resume_after_minutes":2,"start_free_gb":null,"quiet_hours":["22:00-07:00"],"hide_menu_bar_icon":false,"state":"working","stale":true},"lite_mode_pending":{"settings":{"enabled":true,"idle_only":true,"idle_minutes":10,"step_aside":true,"resume_after_minutes":5,"start_free_gb":3.5,"quiet_hours":["21:00-08:00"],"hide_menu_bar_icon":true},"at":\#(seen + 3600),"offline_since":\#(seen)}}"#
    if let shown = try? decoder.decode(FleetMachine.self, from: Data(pending.utf8)), let current = shown.liteModeSheetStart {
        if let button = FleetStartup.menuButton(for: shown, window: { nil }, done: {}) as? FleetMenuButton {
            print("menu \(shown.label ?? ""): \(button.itemTitles)")
        }
        let (alert, _) = FleetWorkstationForm.alert(for: shown, current)
        alert.layout()
        alert.window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        alert.window.orderFront(nil)
        spin(0.8)
        write(alert.window, web: nil, to: "\(output)/fleet-workstation-pending\(suffixForAppearance).png")
        alert.window.orderOut(nil)
    }
}

/// `fleet-add` (with GATEWAY_RENDER_FIXTURE=add-location): Fleet → Locations as the owner adds a controller —
/// a slow location keeping what it last reported (`fleet-add-slow`), the add in flight (`fleet-add-adding`) and
/// the new row on the page before its first read lands (`fleet-add-just-added`). Drawn tall, so the Add form and
/// the table are in one image.
func renderAddLocation() {
    // The app's Fleet row is the web page now, so this renders the **kept** native page in a
    // window of its own. That is the point of keeping it: the old page (and its Add form) can
    // still be looked at and restored without being wired into the app.
    let (width, height) = sizes.first ?? (1100, 1500)
    let native = FleetSection.shared
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                          styleMask: [.borderless, .resizable], backing: .buffered, defer: false)
    window.contentView = native.view
    native.select(.locations)
    native.activated()
    window.setContentSize(NSSize(width: width, height: height))
    window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
    window.makeKeyAndOrderFront(nil)
    func shot(_ name: String) {
        window.contentView?.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        write(window, web: nil, to: "\(output)/fleet-add-\(name)\(suffixForAppearance).png")
    }
    spin(2.0)
    shot("slow")
    native.renderAdd(label: "Shops", url: "http://100.120.246.9:47873", token: "fixture-fleet-token")
    spin(1.0)
    shot("adding")
    spin(5.5)
    shot("just-added")
    GatewayAPI.delete("/admin/api/fleet/controllers/4bb919aee2e4")  // the fixture forgets it for the next run
    spin(1.0)
}

/// `fleet-install`: the "Install a model…" picker for a 16 GB node (`fleet-install.png`) and its "…" menu titles,
/// plus the same menu for an 8 GB node and an older gateway; then rows mid-download and finished, so
/// `fleet:machines` rendered in the same run shows them on the Machines tab.
func renderInstallSheet() {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let fish = #"{"package_id":"fish-audio-s2-pro-4bit","model":"majentik/fishaudio-s2-pro-MLX-4bit","label":"Fish S2 Pro 4-bit","size_gb":3.5}"#
    let qwen = #"{"package_id":"qwen3-tts-12hz-0.6b-base-4bit","model":"mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit","label":"Qwen3 TTS 0.6B","size_gb":3.4}"#
    let samples = [
        #"{"location_id":"ctrl-blue","machine":"blue-1","label":"Blue Room 1","reachable":true,"node_role":"agent","installable_models":[\#(fish),\#(qwen)]}"#,
        #"{"location_id":"ctrl-blue","machine":"blue-2","label":"Blue Room 2","reachable":true,"node_role":"agent","installable_models":[]}"#,
        #"{"location_id":"ctrl-blue","machine":"blue-9","label":"Older gateway","reachable":true,"node_role":"agent"}"#,
    ].compactMap { try? decoder.decode(FleetMachine.self, from: Data($0.utf8)) }
    for sample in samples {
        if let button = FleetStartup.menuButton(for: sample, window: { nil }, done: {}) as? FleetMenuButton {
            print("menu \(sample.label ?? ""): \(button.itemTitles)")
        }
    }
    guard let first = samples.first else { print("no sample machine"); return }
    let (alert, _) = FleetModelInstall.pickerAlert(for: first, first.installableModels ?? [])
    alert.layout()
    alert.window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
    alert.window.orderFront(nil)
    spin(0.8)
    write(alert.window, web: nil, to: "\(output)/fleet-install\(suffixForAppearance).png")
    alert.window.orderOut(nil)
    for (machine, run) in [("blue-1", #"{"state":"running","total_bytes":3500000000,"bytes_done":1470000000}"#),
                           ("blue-ctl", #"{"state":"done"}"#),
                           ("ctrl-studio2/s2-1", #"{"state":"error","error_tail":"download failed: HTTP 503"}"#)] {
        let run = (try? JSONSerialization.jsonObject(with: Data(run.utf8))) as? [String: Any] ?? [:]
        let name = machine == "blue-ctl" ? "Qwen3 TTS 0.6B" : "Fish S2 Pro 4-bit"
        let (text, health, _) = FleetModelInstall.sentence(name, run)
        FleetModelInstall.status[machine.contains("/") ? machine : "ctrl-blue/\(machine)"] = (text, health)
        print("row \(machine): \(text)")
    }
}

/// `fleet-network`: the "Network…" sheets (LAN model sharing) for a location (`fleet-network-location.png`) and for a
/// shop iMac that follows its group's network (`fleet-network-mac.png`), and that Mac's "…" menu titles.
func renderNetworkSheets() {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let mac = #"{"location_id":"4bb919aee2e4","machine":"imac-4","label":"Shop iMac 4","reachable":true,"node_role":"agent","network":"C","network_override":null,"installable_models":[]}"#
    let place = #"{"id":"de7250e146a7","label":"terranash-1100","network":"A"}"#
    guard let machine = try? decoder.decode(FleetMachine.self, from: Data(mac.utf8)),
          let location = try? decoder.decode(FleetLocation.self, from: Data(place.utf8)) else { print("no sample"); return }
    if let button = FleetStartup.menuButton(for: machine, window: { nil }, done: {}) as? FleetMenuButton {
        print("menu \(machine.label ?? ""): \(button.itemTitles)")
    }
    FleetNetwork.names = ["A", "B", "C", "E"]
    let typing = FleetNetwork.machineAlert(machine)
    typing.popup.selectItem(at: typing.popup.numberOfItems - 1)
    typing.changed()
    typing.field.stringValue = "d "
    typing.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification))
    print("new network typed 'd ' saves \(typing.value ?? "nothing")")
    print("mac items: \(FleetNetwork.machineAlert(machine).popup.itemTitles)")
    for (name, picker) in [("location", FleetNetwork.locationAlert(location)), ("mac", FleetNetwork.machineAlert(machine)),
                           ("mac-new", typing)] {
        let alert = picker.alert
        alert.layout()
        alert.window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        alert.window.orderFront(nil)
        spin(0.8)
        write(alert.window, web: nil, to: "\(output)/fleet-network-\(name)\(suffixForAppearance).png")
        alert.window.orderOut(nil)
    }
}

for page in pageNames where page == "fleet-install" { renderInstallSheet() }
for page in pageNames where page == "fleet-network" { renderNetworkSheets() }
for page in pageNames where page == "fleet-rename" { renderRenameSheet() }
for page in pageNames where page == "fleet-workstation" { renderWorkstationSheet() }
for page in pageNames where page == "fleet-add" { renderAddLocation() }
for page in pageNames where !["fleet-rename", "fleet-workstation", "fleet-add", "fleet-install", "fleet-network"].contains(page) {
    // `fleet:locations` is the Fleet page on one of its inner tabs.
    let parts = page.split(separator: ":").map(String.init)
    let name = parts[0].replacingOccurrences(of: "-", with: " ")
    let file = page.replacingOccurrences(of: ":", with: "-").replacingOccurrences(of: " ", with: "-")
    do {
        let suffix = suffixForAppearance
        for size in sizes {
            _ = MainWindow.shared.show(section: name == "whatsnew" ? "what's new" : name)
            guard let window = MainWindow.shared.window else { print("no window for \(page)"); continue }
            // A titled window is clamped to the screen's height; GATEWAY_RENDER_TALL=1 draws a taller page
            // (to read a long page in one image) by dropping the title bar, which the bitmap never included.
            if ProcessInfo.processInfo.environment["GATEWAY_RENDER_TALL"] == "1" {
                window.styleMask = [.borderless, .resizable]
                window.setFrame(NSRect(x: -20000, y: -20000, width: size.0, height: size.1), display: true)
            }
            window.setContentSize(NSSize(width: size.0, height: size.1))
            let requestedWindowSize = window.frame.size
            window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
            window.makeKeyAndOrderFront(nil)
            let isConsole = ["models", "providers", "content packs", "fleet"].contains(name)
            spin(isConsole ? 6.0 : 1.8)
            // **Fleet is the web page now**, so its inner tab is chosen in the page, exactly as a
            // browser chooses it — after the page has loaded. `groups-problems` is Groups with the
            // Problematic filter applied. A build that still has the kept native page in its
            // sidebar falls back to selecting the native tab.
            if name == "fleet", parts.count > 1 {
                let tab = parts[1] == "models-open" ? "models"
                    : parts[1] == "groups-problems" ? "groups" : parts[1]
                if let web = ConsoleSection.fleet.webView as WKWebView? {
                    var failure: Error?
                    var done = false
                    web.evaluateJavaScript(parts[1] == "groups-problems"
                        ? "selectFleetTab('groups'); fleetSetFilter('problematic');"
                        : "selectFleetTab('\(tab)');") { _, error in failure = error; done = true }
                    let deadline = Date().addingTimeInterval(8)
                    while !done && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
                    if let failure { print("fleet tab \(tab) did not apply: \(failure)") }
                    spin(1.2)
                } else if let number = ["groups": 0, "locations": 1, "machines": 2, "models": 3, "jobs": 4, "trash": 5][tab],
                          let kind = FleetTab(rawValue: number) {
                    FleetSection.shared.select(kind)
                }
            }
            if name == "fleet", parts.count > 1, ["models", "models-open"].contains(parts[1]) {
                func buttons(_ view: NSView) -> [NSButton] {
                    (view as? NSButton).map { [$0] } ?? view.subviews.flatMap(buttons)
                }
                let reports = window.contentView.map(buttons)?.filter {
                    $0.accessibilityRole() == .disclosureTriangle
                        && ($0.title == "Show details" || $0.title == "Hide details")
                } ?? []
                if ProcessInfo.processInfo.environment["GATEWAY_RENDER_REPORT"] != nil && reports.isEmpty {
                    fatalError("the fixture install report was not rendered")
                }
                let open = parts[1] == "models-open"
                for button in reports where button.title == (open ? "Show details" : "Hide details") {
                    button.performClick(nil)
                }
                spin(0.2)
            }
            window.contentView?.layoutSubtreeIfNeeded()
            if name == "fleet", parts.count > 1, ["models", "models-open"].contains(parts[1]) {
                precondition(window.frame.size == requestedWindowSize, "model report changed the requested window size")
            }
            window.displayIfNeeded()
            // `jobs:recovery` is Jobs with a Studio Hub hold ("Needs recovery") selected.
            if name == "jobs", parts.count > 1, parts[1] == "recovery" {
                JobsSection.shared.activated()
                spin(1.0)
                if let row = JobsSection.shared.rows.firstIndex(where: { $0["recovery_action"] as? String == "discard_upstream" }) {
                    JobsSection.shared.table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
                    spin(1.0)
                } else { print("jobs:recovery: no discard_upstream row in the fixture") }
            }
            // `analytics:model` is Analytics on one tab; `providers:pools` / `providers:pools-edit` the Pools tab, the
            // latter with the editor and picker open on the pool the owner has.
            if name == "analytics", parts.count > 1, let tab = ["type": 0, "model": 1, "machine": 2][parts[1]] {
                AnalyticsSection.shared.renderSelect(tab)
                spin(0.5)
            }
            if name == "providers", parts.count > 1, let web = ConsoleSection.providers.webView as WKWebView? {
                var js = "selectProviderTab('pools');"
                if parts[1] == "pools-edit" {
                    js += "setTimeout(() => { editPool('gemini-free-all'); setTimeout(() => { const q = document.getElementById('pool-q'); q.value = 'flash'; poolSearch('flash'); }, 1200); }, 800);"
                }
                web.evaluateJavaScript(js, completionHandler: nil)
                spin(parts[1] == "pools-edit" ? 4.0 : 2.0)
            }
            let web: WKWebView? = {
                switch name {
                case "models": return ConsoleSection.models.webView
                case "providers": return ConsoleSection.providers.webView
                case "content packs": return ConsoleSection.packs.webView
                case "fleet": return ConsoleSection.fleet.webView
                default: return nil
                }
            }()
            if let web, ProcessInfo.processInfo.environment["GATEWAY_RENDER_DEBUG"] == "1" {
                var js = "?"
                var done = false
                web.evaluateJavaScript("document.readyState + ' ' + document.body.innerText.length + ' ' + location.href") { r, e in js = "\(r ?? "nil") \(e.map { "\($0)" } ?? "")"; done = true }
                while !done { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
                print("web: loading=\(web.isLoading) url=\(web.url?.absoluteString ?? "nil") frame=\(web.frame) window=\(web.window != nil) js=\(js)")
            }
            if ProcessInfo.processInfo.environment["GATEWAY_RENDER_DEBUG"] == "width", let page = MainWindow.shared.container?.subviews.first {
                // Which view makes the page ask for the width it asks for: follow the widest child down.
                func drill(_ view: NSView, _ depth: Int) {
                    let text = (view as? NSTextField).map { " \"\($0.stringValue.prefix(40))\"" } ?? ""
                    print(String(repeating: "  ", count: depth) + "\(type(of: view)) fits \(Int(view.fittingSize.width))\(text)")
                    guard depth < 16 else { return }
                    let kids = (view as? NSStackView)?.arrangedSubviews ?? view.subviews
                    if let widest = kids.max(by: { $0.fittingSize.width < $1.fittingSize.width }), widest.fittingSize.width > 200 { drill(widest, depth + 1) }
                }
                print("== \(page) content width \(Int(window.contentView?.bounds.width ?? 0))")
                drill(page, 0)
            }
            write(window, web: web, to: "\(output)/\(file)-\(Int(size.0))x\(Int(size.1))\(suffix).png")
            window.orderOut(nil)
        }
    }
}
exit(0)
