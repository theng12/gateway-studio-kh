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

for page in pageNames {
    // `fleet:locations` is the Fleet page on one of its inner tabs.
    let parts = page.split(separator: ":").map(String.init)
    let name = parts[0].replacingOccurrences(of: "-", with: " ")
    let file = page.replacingOccurrences(of: ":", with: "-").replacingOccurrences(of: " ", with: "-")
    do {
        let suffix = suffixForAppearance
        for size in sizes {
            _ = MainWindow.shared.show(section: name == "whatsnew" ? "what's new" : name)
            guard let window = MainWindow.shared.window else { print("no window for \(page)"); continue }
            if parts.count > 1, let tab = ["groups": 0, "groups-problems": 0, "locations": 1, "machines": 2, "models": 3, "jobs": 4][parts[1]],
               let kind = FleetTab(rawValue: tab) {
                FleetSection.shared.select(kind)
                FleetSection.shared.showProblemsOnly(parts[1] == "groups-problems")
            }
            // A titled window is clamped to the screen's height; GATEWAY_RENDER_TALL=1 draws a taller page
            // (to read a long page in one image) by dropping the title bar, which the bitmap never included.
            if ProcessInfo.processInfo.environment["GATEWAY_RENDER_TALL"] == "1" {
                window.styleMask = [.borderless, .resizable]
                window.setFrame(NSRect(x: -20000, y: -20000, width: size.0, height: size.1), display: true)
            }
            window.setContentSize(NSSize(width: size.0, height: size.1))
            window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
            window.makeKeyAndOrderFront(nil)  // a non-key window draws the selected sidebar row as a black slab
            let isConsole = ["models", "providers", "content packs"].contains(name)
            spin(isConsole ? 6.0 : 1.8)
            window.contentView?.layoutSubtreeIfNeeded()
            window.displayIfNeeded()
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
