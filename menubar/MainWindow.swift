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
    func activated()
}

// MARK: - Provider console section

/// The provider console, a local web page served by the gateway, shown in a real
/// window rather than handed to a browser.
final class ProvidersSection: NSObject, AppSection, WKNavigationDelegate {
    static let shared = ProvidersSection()

    let title = "Providers"
    let symbol = "square.grid.2x2"
    let refreshesAutomatically = false

    /// Lazy so the web view can be built by a closure on self.
    private lazy var webView: WKWebView = {
        let web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.navigationDelegate = self
        return web
    }()
    private var loaded = false

    lazy var view: NSView = webView

    func activated() {
        reload()
    }

    func reload() {
        guard let url = GatewayController.shared.consoleURL else { return }
        // Load once, then reload on later visits only if the gateway is up —
        // re-loading on every activation would discard half-finished edits in
        // the console's own forms.
        if !loaded {
            loaded = true
            load(url, into: webView)
        }
    }

    /// If the gateway is not listening yet, say so plainly instead of showing a
    /// raw connection error, and load for real once it should be up.
    private func load(_ url: URL, into web: WKWebView, attempt: Int = 0) {
        if GatewayController.shared.isRunning || attempt > 0 {
            web.load(URLRequest(url: url))
            return
        }
        web.loadHTMLString(Self.startingPage, baseURL: nil)
        GatewayController.shared.start()
        DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) { [weak self, weak web] in
            guard let self, let web else { return }
            self.load(url, into: web, attempt: attempt + 1)
        }
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

    /// Section names accepted by `--open` and the menu.
    private static let order = ["overview", "jobs", "voices", "providers"]

    func sectionIndex(named name: String) -> Int? {
        let needle = name.lowercased()
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

    // MARK: Construction

    private func build() {
        sections = [
            OverviewSection.shared,
            JobsSection.shared,
            VoicesSection.shared,
            ProvidersSection.shared,
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
        sidebar.rowHeight = 28
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

        let sidebarHost = NSView()
        sidebarHost.addSubview(sidebarScroll)
        NSLayoutConstraint.activate([
            sidebarScroll.topAnchor.constraint(equalTo: sidebarHost.topAnchor),
            sidebarScroll.leadingAnchor.constraint(equalTo: sidebarHost.leadingAnchor),
            sidebarScroll.trailingAnchor.constraint(equalTo: sidebarHost.trailingAnchor),
            sidebarScroll.bottomAnchor.constraint(equalTo: sidebarHost.bottomAnchor),
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
        NSLayoutConstraint.activate([
            split.topAnchor.constraint(equalTo: content.topAnchor),
            split.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            split.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            split.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            sidebarHost.widthAnchor.constraint(greaterThanOrEqualToConstant: 170),
            sidebarHost.widthAnchor.constraint(lessThanOrEqualToConstant: 260),
            container.widthAnchor.constraint(greaterThanOrEqualToConstant: 660),
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
            sidebar.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
            window.title = "gateway-kh — \(section.title)"
        }

        section.activated()
    }

    // MARK: Sidebar

    func numberOfRows(in tableView: NSTableView) -> Int { sections.count }

    func tableView(
        _ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int
    ) -> NSView? {
        guard row < sections.count else { return nil }
        let section = sections[row]

        let cell = NSTableCellView()
        let image = NSImageView()
        image.image = NSImage(systemSymbolName: section.symbol, accessibilityDescription: nil)
        image.contentTintColor = .controlAccentColor
        let label = NSTextField(labelWithString: section.title)
        label.font = .systemFont(ofSize: 13)

        image.translatesAutoresizingMaskIntoConstraints = false
        label.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(image)
        cell.addSubview(label)
        cell.imageView = image
        cell.textField = label

        NSLayoutConstraint.activate([
            image.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2),
            image.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            image.widthAnchor.constraint(equalToConstant: 18),
            label.leadingAnchor.constraint(equalTo: image.trailingAnchor, constant: 7),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        let row = sidebar.selectedRow
        guard row >= 0 else { return }
        select(row)
    }
}
