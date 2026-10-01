// The app's single window.
//
// The app started with one window per feature, which is how you end up with
// five floating windows and no application. This is the shell that should have
// been there from the beginning: a source list on the left, the selected
// section on the right, and one window to move around.
//
// Settings stays its own window, which is the macOS convention (Cmd-,) rather
// than a section.

import Cocoa
import WebKit

/// One navigable section of the main window.
protocol AppSection: AnyObject {
    var title: String { get }
    var symbol: String { get }
    var view: NSView { get }
    /// Whether the host should re-poll this section on a timer. Voice browsing
    /// must not: a refresh mid-interaction fights the checkbox being clicked.
    var refreshesAutomatically: Bool { get }
    /// Whether the host should re-read this section when the owner comes **back**
    /// to the window. Opt-in, and only for read-only pages.
    ///
    /// The two are different questions. A timer re-reads while the page is in
    /// front of you — wrong for anything you are part-way through, which is why
    /// Fleet and Voices decline it. Coming back is the moment a stale page is
    /// actively misleading: the owner fixed something on a Mac, returned to the
    /// window that was already open on Fleet, and read the fleet as it was before
    /// his fix. But it is opt-in for the same reason: `SettingsSection.refresh()`
    /// writes the stored key back into the field, so a page that re-read on return
    /// would discard a half-typed key the moment he switched to `.env` to copy it.
    var refreshesOnReturn: Bool { get }
    func activated()
}

extension AppSection {
    /// Off unless a page says otherwise: a section that has not thought about it
    /// must not start discarding what someone is typing.
    var refreshesOnReturn: Bool { false }
}

// MARK: - Provider console sections

/// The provider console, a local web page served by the gateway, shown in a real
/// window rather than handed to a browser.
///
/// The console has two views of its own — Models and Providers — and both are now
/// their own sidebar pages, so each instance opens the console on the matching
/// view via its URL hash. That is the whole difference between the two sections;
/// the page, its data and its controls are shared.
final class ConsoleSection: NSObject, AppSection, WKNavigationDelegate, WKUIDelegate {
    static let models = ConsoleSection(
        title: "Models", symbol: "square.stack.3d.up", viewName: "models"
    )
    static let providers = ConsoleSection(
        title: "Providers", symbol: "square.grid.2x2", viewName: "providers"
    )
    /// Content Packs is the same page whether it is opened here or in a browser.
    /// It used to be a native stub with an "Open in browser" button, so setting
    /// up the shelf, reviewing an archive and adding it to the shelf all happened
    /// somewhere else; the console already does all of it.
    static let packs = ConsoleSection(
        title: "Content Packs", symbol: "archivebox", viewName: "packs"
    )

    let title: String
    let symbol: String
    private let viewName: String
    let refreshesAutomatically = false

    private init(title: String, symbol: String, viewName: String) {
        self.title = title
        self.symbol = symbol
        self.viewName = viewName
    }

    private var injectedKey = ""
    private var hasBuiltView = false
    private var loadOverlay: NSView!
    private var loadStatusLabel: NSTextField!
    private var retryButton: NSButton!

    static func refreshCredentials() {
        models.refreshCredentialIfLoaded()
        providers.refreshCredentialIfLoaded()
        packs.refreshCredentialIfLoaded()
    }

    /// Lazy so the web view can be built by a closure on self.
    private lazy var webView: WKWebView = {
        // **The console is told the gateway key, because the app owns `.env` and the
        // owner should not have to paste a secret into a web page to see his own
        // data.** Once the gateway is bound beyond loopback — for Tailscale or the
        // LAN — `/admin/*` demands the key even from this Mac, since the check is
        // about the bind rather than the caller. Injected at document start, so it
        // is in place before the page's first request; a browser on another device
        // still pastes its own, and the console prefers the injected value.
        let config = WKWebViewConfiguration()
        let key = EnvFile.apiKey()
        if !key.isEmpty {
            config.userContentController.addUserScript(Self.credentialScript(key))
        }
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        // Without a UI delegate, `confirm()` and `alert()` in the page are **silent
        // no-ops**: `confirm` returns false, so a handler that asks first simply
        // returns and nothing happens. That is why "Disable all matching" looked
        // dead — and why every `alert(e.message)` in the console was invisible, so
        // the errors that would have explained it never appeared either.
        web.uiDelegate = self
        // A WKWebView reports its content's size as an intrinsic size and resists
        // compression, so the console page — which is wide — made the detail
        // column demand ~1720pt. The column is shared with every other page, so
        // that width stuck: Overview's fourth stat tile and Voices' Refresh button
        // were laid out past the window's right edge and were simply not visible.
        // The web view must take the width it is given, never ask for more.
        web.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        web.setContentHuggingPriority(.defaultLow, for: .horizontal)
        web.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        return web
    }()
    /// A Swift string as a JavaScript literal. The key is a secret, so it is encoded
    /// rather than pasted into the source of a script: a quote or a backslash in it
    /// would otherwise end the literal and break the page.
    private static func jsString(_ value: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [value])
        guard let data, let text = String(data: data, encoding: .utf8),
              text.count >= 2 else { return "\"\"" }
        return String(text.dropFirst().dropLast())
    }

    private var loaded = false
    /// Set when a load fails, so the next activation tries the network again
    /// instead of talking to WebKit's error page. The overlay makes that state
    /// visible and gives the owner a retry without rebuilding the web view.
    private var lastLoadFailed = false

    lazy var view: NSView = buildView()

    func activated() {
        reload()
    }

    private func buildView() -> NSView {
        hasBuiltView = true
        let host = NSView()
        webView.translatesAutoresizingMaskIntoConstraints = false
        host.addSubview(webView)

        let overlay = NSVisualEffectView()
        overlay.material = .underWindowBackground
        overlay.blendingMode = .withinWindow
        overlay.state = .active
        overlay.translatesAutoresizingMaskIntoConstraints = false
        host.addSubview(overlay)
        loadOverlay = overlay

        loadStatusLabel = NSTextField(wrappingLabelWithString: "")
        loadStatusLabel.font = TextRole.rowTitle.font
        loadStatusLabel.alignment = .center
        loadStatusLabel.maximumNumberOfLines = 3
        loadStatusLabel.lineBreakMode = .byWordWrapping
        loadStatusLabel.preferredMaxLayoutWidth = 420

        retryButton = PageKit.primaryButton("Retry", target: self, action: #selector(retryLoad), regular: true)
        retryButton.isHidden = true

        let message = NSStackView(views: [loadStatusLabel, retryButton])
        message.orientation = .vertical
        message.alignment = .centerX
        message.spacing = DS.Space.m
        message.translatesAutoresizingMaskIntoConstraints = false
        overlay.addSubview(message)

        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: host.topAnchor),
            webView.leadingAnchor.constraint(equalTo: host.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: host.bottomAnchor),
            overlay.topAnchor.constraint(equalTo: host.topAnchor),
            overlay.leadingAnchor.constraint(equalTo: host.leadingAnchor),
            overlay.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            overlay.bottomAnchor.constraint(equalTo: host.bottomAnchor),
            message.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            message.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
            message.leadingAnchor.constraint(greaterThanOrEqualTo: overlay.leadingAnchor, constant: DS.Space.xl),
            message.trailingAnchor.constraint(lessThanOrEqualTo: overlay.trailingAnchor, constant: -DS.Space.xl),
        ])
        overlay.isHidden = true
        return host
    }

    private static func credentialScript(_ key: String) -> WKUserScript {
        WKUserScript(
            source: "window.__GATEWAY_ADMIN_TOKEN__ = \(jsString(key));",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
    }

    /// Update both future document loads and the currently loaded page. The page
    /// reads this global for every request, so rotation does not reload forms or
    /// discard healthy edits.
    private func refreshCredentialIfLoaded() {
        guard hasBuiltView else { return }
        refreshCredential()
    }

    private func refreshCredential() {
        let key = EnvFile.apiKey()
        guard key != injectedKey else { return }
        injectedKey = key
        let controller = webView.configuration.userContentController
        controller.removeAllUserScripts()
        if !key.isEmpty {
            controller.addUserScript(Self.credentialScript(key))
        }
        guard loaded else { return }
        webView.evaluateJavaScript("window.__GATEWAY_ADMIN_TOKEN__ = \(Self.jsString(key));")
    }

    private func showLoading(_ message: String, retry: Bool) {
        guard loadOverlay != nil else { return }
        loadStatusLabel.stringValue = message
        retryButton.isHidden = !retry
        loadOverlay.isHidden = false
    }

    private func hideLoading() {
        loadOverlay?.isHidden = true
    }

    func reload() {
        guard let url = GatewayController.shared.consoleURL else { return }
        refreshCredential()
        let target = URL(string: url.absoluteString + "#" + viewName) ?? url
        // Load once, then reload on later visits only if the gateway is up —
        // re-loading on every activation would discard half-finished edits in
        // the console's own forms.
        //
        // `lastLoadFailed` is the exception, and it is load-bearing. `loaded` was
        // set *before* the load, so a first load that failed left WebKit's error
        // page up permanently: every later activation — including View → Reload
        // Console — took the JavaScript-only branch and evaluated `setView(...)`
        // against a page that had never run. The only recovery was quitting and
        // relaunching the app. A failed load has to be retried for real.
        if !loaded || lastLoadFailed {
            loaded = true
            lastLoadFailed = false
            load(target, into: webView)
        } else {
            // Already loaded: move the page to this section's view. Call setView
            // outright rather than assigning location.hash, so this does not
            // depend on the page happening to listen for `hashchange`.
            webView.evaluateJavaScript("setView('\(viewName)');")
        }
    }

    @objc private func retryLoad() {
        reload()
    }

    /// The console could not be fetched. Say so, and make the next visit retry.
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        lastLoadFailed = true
        showLoading("Could not load the provider console.\n\(error.localizedDescription)", retry: true)
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        lastLoadFailed = true
        showLoading("Could not load the provider console.\n\(error.localizedDescription)", retry: true)
    }

    /// If the gateway is not listening yet, say so plainly instead of showing a
    /// raw connection error, and load for real once it should be up.
    private func load(_ url: URL, into web: WKWebView, attempt: Int = 0) {
        if GatewayController.shared.isRunning || attempt > 0 {
            showLoading("Loading the provider console…", retry: false)
            var request = URLRequest(url: url)
            // Keep a stalled document from leaving the loading overlay forever.
            request.timeoutInterval = 60
            web.load(request)
            return
        }
        showLoading("Starting the gateway…", retry: false)
        web.loadHTMLString(Self.startingPage, baseURL: nil)
        GatewayController.shared.start()
        DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) { [weak self, weak web] in
            guard let self, let web else { return }
            self.load(url, into: web, attempt: attempt + 1)
        }
    }

    /// Pin the console to this section's view.
    ///
    /// The URL hash asks for the right view on load, but this states it outright
    /// once the page is ready: the hash is read by the page's own init, and a
    /// reload that skips it — or a page that has since navigated itself — would
    /// otherwise leave Models and Providers showing the same thing, which is the
    /// bug this split exists to fix.
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        lastLoadFailed = false
        if webView.url != nil { hideLoading() }
        webView.evaluateJavaScript("setView('\(viewName)');") { result, error in
        }
    }

    // MARK: JavaScript dialogs

    /// The page asks before acting; this is what makes that question appear.
    ///
    /// The confirm button says what the action is rather than "OK", because the
    /// only caller is a bulk enable or disable and "OK" would leave the count in
    /// the message as the only clue about which way it goes.
    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: Self.verb(in: message))
        alert.addButton(withTitle: "Cancel")
        // Cancel is the default: Return must not make a bulk change.
        alert.buttons.first?.keyEquivalent = ""
        alert.buttons.last?.keyEquivalent = "\r"
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    /// Errors from the page were being swallowed whole. Now they are shown.
    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = "The console reported a problem"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        alert.runModal()
        completionHandler()
    }

    /// **A file input needs this, or its button does nothing.** WKWebView shows no picker unless the
    /// UI delegate supplies one, so the console's "Import price research…" (a hidden `<input
    /// type=file>` the button clicks) was dead in the app while working in a browser.
    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        completionHandler(panel.runModal() == .OK ? panel.urls : nil)
    }

    /// A pricing-page link (`target="_blank"`) opens in the browser; the console itself never opens a
    /// second web view.
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url, url.scheme == "https" { NSWorkspace.shared.open(url) }
        return nil
    }

    /// `Enable all …` / `Disable all …` / `Clear …` → the verb alone.
    private static func verb(in message: String) -> String {
        for word in ["Enable", "Disable", "Clear", "Remove", "Delete"] where message.hasPrefix(word) {
            return word
        }
        return "OK"
    }

    private static let startingPage = """
    <!doctype html><meta charset="utf-8">
    <style>
      body { background:#0e1117; color:#e6eaf2;
             font:14px -apple-system, BlinkMacSystemFont, sans-serif;
             display:flex; align-items:center; justify-content:center;
             height:100vh; margin:0 }
      div { text-align:center } h2 { font-weight:600 } p { color:#8b95a8 }
    </style>
    <div>
      <h2>Starting the gateway…</h2>
      <p>This window will load the provider console automatically.</p>
    </div>
    """
}

// MARK: - Main window

final class MainWindow: NSObject, NSTableViewDataSource, NSTableViewDelegate {
    static let shared = MainWindow()

    private var window: NSWindow?
    private var sidebar: NSTableView!
    private var container: NSView!
    private var sections: [AppSection] = []
    private var currentIndex = -1
    private var timer: Timer?
    private let statusDot = StatusDot(.tertiaryLabelColor)
    private let sidebarStatus = NSTextField(labelWithString: "…")
    private let sidebarVersion = NSTextField(wrappingLabelWithString: "")
    private var statusTimer: Timer?

    /// Section names accepted by `--open` and the menu.
    private static let order = [
        "overview",
        "audio", "subtitle", "image", "music", "video", "chat",
        "jobs", "voices", "models", "providers", "fleet", "settings", "content packs",
        // **What's New is a page now, not a modal.** It sits directly above Settings under the
        // *App* heading, which is where Studio Hub keeps it — the two apps are read side by side,
        // so the same thing should be in the same place in both.
        "what's new",
        // Analytics is index 15, appended with the section it names — see `build()`.
        "analytics",
    ]

    /// The View menu, in sidebar order: (title, key equivalent).
    ///
    /// Derived from the same grouping the sidebar uses rather than typed out
    /// again, because a section that exists in the sidebar but not here is a
    /// destination with no door. Settings is deliberately absent — it is ⌘, and a
    /// row in the sidebar already, so a third route to it would be a third thing to
    /// keep in step.
    static let viewMenuSections: [(title: String, key: String)] = [
        ("Overview", "1"),
        ("Audio", ""), ("Subtitle", ""), ("Image", ""),
        ("Music", ""), ("Video", ""), ("Chat", ""),
        ("Jobs", "2"), ("Analytics", ""), ("Voices", "3"), ("Models", "4"), ("Content Packs", ""),
        ("Providers", "5"), ("Fleet", "6"),
        // Kept in step with the sidebar for the same reason every other row is: this list is
        // derived from the sidebar's order on purpose, and a section that exists in one and not
        // the other is a destination with no door.
        ("What's New", ""),
    ]

    /// A sidebar row is either a group header or a section.
    ///
    /// Studio Hub KH groups its sidebar into `Section("Create")` /
    /// `Section("Library & app")`; this mirrors that so the two apps are
    /// navigated the same way. The Create group used to be absent on purpose —
    /// *"an empty 'Create' heading would have been a lie dressed as navigation"* —
    /// because every page here was management. It is not empty now: the six lanes
    /// generate for real, so the heading names a group that exists.
    ///
    /// **"Library & app" was doing three jobs at once** — things you keep (Jobs,
    /// Voices, Models), the machines work runs on (Providers, Fleet), and the app's
    /// own settings — with Content Packs stranded after Settings. Split into
    /// Library, Connection and App so a row sits under a heading that answers the
    /// same question it does.
    private enum SidebarRow {
        case header(String)
        case section(Int)
    }

    private static let layout: [SidebarRow] = [
        .section(0),                                    // Overview
        .header("Create"),
        .section(1), .section(2), .section(3),          // Audio, Subtitle, Image
        .section(4), .section(5), .section(6),          // Music, Video, Chat
        .header("Library"),
        .section(7), .section(8), .section(9),          // Jobs, Voices, Models
        // Analytics sits with the other "what I have kept" pages, directly after Jobs: it is the
        // same record read the other way round — Jobs answers "what happened to that job", this
        // answers "what has this machine been doing".
        .section(15),                                   // Analytics
        .section(13),                                   // Content Packs
        .header("Connection"),
        .section(10), .section(11),                     // Providers, Fleet
        .header("App"),
        // Studio Hub's order inside its own `app` group: What's New, then Settings.
        .section(14),                                   // What's New
        .section(12),                                   // Settings
    ]

    private var rowForSection: [Int: Int] {
        var map: [Int: Int] = [:]
        for (row, entry) in Self.layout.enumerated() {
            if case .section(let index) = entry { map[index] = row }
        }
        return map
    }

    func sectionIndex(named name: String) -> Int? {
        let needle = name.lowercased() == "pack library" ? "content packs" : name.lowercased()
        if let direct = Self.order.firstIndex(of: needle) { return direct }
        return sections.firstIndex { $0.title.lowercased() == needle }
    }

    @discardableResult
    func show(section name: String? = nil) -> Bool {
        if window == nil { build() }
        guard let window else { return false }

        if let name, let index = sectionIndex(named: name) {
            select(index)
        } else if currentIndex < 0 {
            select(0)
        } else {
            // Already showing a section: a plain re-open must still refresh it.
            sections[currentIndex].activated()
        }

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    func reloadCurrentSection() {
        guard currentIndex >= 0, currentIndex < sections.count else { return }
        sections[currentIndex].activated()
    }

    /// Show the What's New **page**. Every door to the notes — the Overview button, the app menu,
    /// this one — lands on the sidebar row rather than on a separate window, so there is one place
    /// the notes live and no second surface to keep in step.
    @objc private func openWhatsNew() {
        show(section: "what's new")
    }

    // MARK: Construction

    private func build() {
        sections = [
            OverviewSection.shared,
            // The Create lanes, in Studio Hub's order, before the management pages —
            // the same order its sidebar uses.
            CreateSection.all[0], CreateSection.all[1], CreateSection.all[2],
            CreateSection.all[3], CreateSection.all[4], CreateSection.all[5],
            JobsSection.shared,
            VoicesSection.shared,
            ConsoleSection.models,
            ConsoleSection.providers,
            FleetSection.shared,
            SettingsSection.shared,
            ConsoleSection.packs,
            // Index 14, and the last entry: the sidebar order is the `order` list, and this has to
            // line up with it entry for entry.
            WhatsNewSection.shared,
            // Appended rather than inserted, deliberately: `order` and `layout` address sections by
            // **index**, so inserting Analytics beside Jobs would renumber every section after it
            // and a single missed entry would silently point a sidebar row at the wrong page. A new
            // section goes on the end and `layout` places it where it belongs on screen.
            AnalyticsSection.shared,
        ]

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1080, height: 760),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "gateway-kh"
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("GatewayMainWindow")
        window.center()
        // One toolbar-free window with a real sidebar; the split view supplies
        // the standard translucent sidebar material.
        window.minSize = NSSize(width: 860, height: 560)

        // ---- sidebar
        sidebar = NSTableView()
        sidebar.dataSource = self
        sidebar.delegate = self
        sidebar.headerView = nil
        sidebar.rowHeight = DS.Size.row
        sidebar.style = .sourceList
        sidebar.selectionHighlightStyle = .regular
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("section"))
        column.resizingMask = .autoresizingMask
        sidebar.addTableColumn(column)

        let sidebarScroll = NSScrollView()
        sidebarScroll.documentView = sidebar
        sidebarScroll.hasVerticalScroller = true
        sidebarScroll.drawsBackground = false
        sidebarScroll.translatesAutoresizingMaskIntoConstraints = false

        // Studio Hub KH pins a connection line to the bottom of its sidebar
        // (`safeAreaInset(edge: .bottom)`), so the state of the thing you are
        // driving is always on screen no matter which page you are on. Same
        // line here, same place. It replaced a Settings gear, because Settings
        // is now a row in the list rather than a lone button.
        sidebarStatus.font = TextRole.caption.font
        sidebarStatus.textColor = .secondaryLabelColor
        sidebarStatus.lineBreakMode = .byWordWrapping
        sidebarStatus.preferredMaxLayoutWidth = DS.Size.sidebar - 2 * DS.Space.l - DS.Space.s - 8

        sidebarVersion.font = TextRole.caption.font
        sidebarVersion.textColor = .secondaryLabelColor
        sidebarVersion.lineBreakMode = .byWordWrapping
        sidebarVersion.maximumNumberOfLines = 0
        // The fixed sidebar minus its margins, status dot and gap.
        sidebarVersion.preferredMaxLayoutWidth = DS.Size.sidebar - 2 * DS.Space.l - DS.Space.s - 8
        sidebarVersion.isSelectable = true
        sidebarVersion.stringValue = "App version \(GatewayController.shared.appVersion)\nBuild \(GatewayController.shared.appBuild)"

        // **The "What's New?" link is gone from the status block.** It existed because the notes
        // were a modal with one door; the row is now in the sidebar directly above Settings, so the
        // link was the same destination twice in the same column. The View menu and the Overview
        // button still reach it.
        let statusText = NSStackView(views: [sidebarStatus, sidebarVersion])
        statusText.orientation = .vertical
        statusText.alignment = .leading
        statusText.spacing = DS.Space.xs
        statusText.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        sidebarVersion.widthAnchor.constraint(equalTo: statusText.widthAnchor).isActive = true

        let statusRow = NSStackView(views: [statusDot, statusText])
        statusRow.orientation = .horizontal
        statusRow.alignment = .top
        statusRow.spacing = DS.Space.s
        statusRow.translatesAutoresizingMaskIntoConstraints = false

        let statusDivider = NSBox()
        statusDivider.boxType = .separator
        statusDivider.translatesAutoresizingMaskIntoConstraints = false

        let sidebarHost = NSView()
        sidebarHost.addSubview(sidebarScroll)
        sidebarHost.addSubview(statusDivider)
        sidebarHost.addSubview(statusRow)
        NSLayoutConstraint.activate([
            sidebarScroll.topAnchor.constraint(equalTo: sidebarHost.topAnchor),
            sidebarScroll.leadingAnchor.constraint(equalTo: sidebarHost.leadingAnchor),
            sidebarScroll.trailingAnchor.constraint(equalTo: sidebarHost.trailingAnchor),
            sidebarScroll.bottomAnchor.constraint(equalTo: statusDivider.topAnchor),

            statusDivider.leadingAnchor.constraint(equalTo: sidebarHost.leadingAnchor),
            statusDivider.trailingAnchor.constraint(equalTo: sidebarHost.trailingAnchor),
            statusDivider.bottomAnchor.constraint(equalTo: statusRow.topAnchor, constant: -DS.Space.m),

            statusRow.leadingAnchor.constraint(equalTo: sidebarHost.leadingAnchor, constant: DS.Space.l),
            statusRow.trailingAnchor.constraint(equalTo: sidebarHost.trailingAnchor, constant: -DS.Space.l),
            statusRow.bottomAnchor.constraint(equalTo: sidebarHost.bottomAnchor, constant: -DS.Space.m),
        ])

        // ---- detail
        container = NSView()
        container.translatesAutoresizingMaskIntoConstraints = false

        let split = NSSplitView()
        split.isVertical = true
        split.dividerStyle = .thin
        split.translatesAutoresizingMaskIntoConstraints = false
        split.addArrangedSubview(sidebarHost)
        split.addArrangedSubview(container)
        split.setHoldingPriority(.defaultHigh, forSubviewAt: 0)

        let content = NSView()
        content.addSubview(split)
        // The detail column's width is *closed* by these constraints.
        //
        // Before, the sidebar was only bounded (>= 170, <= 260) and the container
        // only had a floor (>= 660), with nothing tying the two to the split
        // view's actual width. The container was therefore free to grow to
        // satisfy whatever its content asked for, and one page did: the container
        // ended up ~1750pt wide inside a 960pt window, so the fourth stat tile
        // (x=1487) and the Voices Refresh button (x=1830) were laid out past the
        // right edge and simply invisible. Pinning the sidebar and giving the
        // container "everything else" makes the content width predictable, which
        // is what every `root.widthAnchor - 56` inside the pages assumes.
        let sidebarWidth = DS.Size.sidebar
        NSLayoutConstraint.activate([
            split.topAnchor.constraint(equalTo: content.topAnchor),
            split.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            split.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            split.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            sidebarHost.widthAnchor.constraint(equalToConstant: sidebarWidth),
            container.widthAnchor.constraint(
                equalTo: split.widthAnchor, constant: -sidebarWidth - 1
            ),
        ])
        window.contentView = content

        self.window = window

        // The host owns the refresh timer now, and only for sections that want
        // one — the old per-window timers each ran regardless of visibility.
        let tick = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            guard let self, self.window?.isVisible == true else { return }
            guard self.currentIndex >= 0, self.currentIndex < self.sections.count else { return }
            let section = self.sections[self.currentIndex]
            guard section.refreshesAutomatically else { return }
            section.activated()
        }
        RunLoop.main.add(tick, forMode: .common)
        timer = tick

        // **Coming back to the app re-reads the page you are looking at.**
        //
        // Only for sections that opt in (`refreshesOnReturn`): a read-only page
        // like Fleet is *misleading* when stale — the owner fixes a Mac, comes
        // back to the window that was already open on that page, and reads the
        // fleet as it was before the fix, which looks like the fix failed. A page
        // with a form in it must not be re-read behind the typist, which is why
        // the default is off and Settings leaves it off.
        //
        // One read per return is not polling: it is what pressing Refresh would
        // have done. Both notifications are observed because they cover different
        // returns — switching back from another app, and switching between this
        // app's own windows (the Jobs window is separate).
        for name in [NSApplication.didBecomeActiveNotification,
                     NSWindow.didBecomeKeyNotification] {
            NotificationCenter.default.addObserver(
                forName: name, object: nil, queue: .main
            ) { [weak self] note in
                guard let self, self.window?.isVisible == true else { return }
                if let became = note.object as? NSWindow, became != self.window { return }
                guard self.currentIndex >= 0, self.currentIndex < self.sections.count
                else { return }
                let section = self.sections[self.currentIndex]
                guard section.refreshesOnReturn else { return }
                section.activated()
            }
        }

        startStatusUpdates()
    }

    // MARK: Selection

    func select(_ index: Int) {
        guard let window, index >= 0, index < sections.count else { return }
        let section = sections[index]

        if index != currentIndex {
            container.subviews.forEach { $0.removeFromSuperview() }
            let child = section.view
            child.translatesAutoresizingMaskIntoConstraints = false
            container.addSubview(child)
            NSLayoutConstraint.activate([
                child.topAnchor.constraint(equalTo: container.topAnchor),
                child.leadingAnchor.constraint(equalTo: container.leadingAnchor),
                child.trailingAnchor.constraint(equalTo: container.trailingAnchor),
                child.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            ])
            currentIndex = index
            if let row = rowForSection[index] {
                sidebar.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
            }
            window.title = "gateway-kh — \(section.title)"
        }

        section.activated()
    }

    // MARK: Sidebar

    func numberOfRows(in tableView: NSTableView) -> Int { Self.layout.count }

    /// Group headers are not selectable rows; AppKit draws them as small caps.
    func tableView(_ tableView: NSTableView, isGroupRow row: Int) -> Bool {
        if case .header = Self.layout[row] { return true }
        return false
    }

    func tableView(
        _ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int
    ) -> NSView? {
        switch Self.layout[row] {
        case .header(let title):
            let label = NSTextField(labelWithString: title)
            label.font = TextRole.captionStrong.font
            label.textColor = .secondaryLabelColor
            let cell = NSTableCellView()
            label.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(label)
            cell.textField = label
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
                label.bottomAnchor.constraint(equalTo: cell.bottomAnchor, constant: -2),
            ])
            return cell

        case .section(let index):
            let section = sections[index]
            let cell = NSTableCellView()
            let image = NSImageView()
            image.image = NSImage(systemSymbolName: section.symbol, accessibilityDescription: nil)
            image.contentTintColor = .controlAccentColor
            let label = NSTextField(labelWithString: section.title)
            label.font = TextRole.body.font

            image.translatesAutoresizingMaskIntoConstraints = false
            label.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(image)
            cell.addSubview(label)
            cell.imageView = image
            cell.textField = label

            NSLayoutConstraint.activate([
                image.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2),
                image.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                image.widthAnchor.constraint(equalToConstant: DS.Size.rowIcon - DS.Space.xs),
                label.leadingAnchor.constraint(equalTo: image.trailingAnchor, constant: DS.Space.s),
                label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -DS.Space.xs),
                label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            ])
            return cell
        }
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        let row = sidebar.selectedRow
        guard row >= 0, row < Self.layout.count else { return }
        // A header cannot be selected into; put the highlight back where it was.
        guard case .section(let index) = Self.layout[row] else {
            if let current = rowForSection[currentIndex] {
                sidebar.selectRowIndexes(IndexSet(integer: current), byExtendingSelection: false)
            }
            return
        }
        select(index)
    }

    // MARK: Sidebar status line

    /// Keeps the bottom line current without depending on which section is
    /// showing, so it stays true while Providers has the web console up.
    private func startStatusUpdates() {
        updateStatusLine()
        let tick = Timer(timeInterval: 3, repeats: true) { [weak self] _ in
            self?.updateStatusLine()
            // Keep an embedded console current after an external .env rotation.
            // The helper touches only views that have already been built and
            // updates the page global in place, so it does not reload edits.
            ConsoleSection.refreshCredentials()
        }
        RunLoop.main.add(tick, forMode: .common)
        statusTimer = tick
    }

    private func updateStatusLine() {
        let running: Bool
        switch GatewayController.shared.state {
        case .running: running = true
        default: running = false
        }
        statusDot.colour = (running ? Health.ok : Health.off).tint
        sidebarStatus.stringValue = GatewayController.shared.sidebarSummary
    }
}
