import Cocoa

/// The ordinary browser owns file selection and downloads. No duplicate import
/// protocol or WebKit-only download bridge is needed in the native shell.
final class PackLibrarySection: NSObject, AppSection {
    static let shared = PackLibrarySection()
    let title = "Pack Library"
    let symbol = "archivebox"
    let refreshesAutomatically = false
    private let status = PageKit.label("", size: 12, colour: .secondaryLabelColor, wrapping: true)

    lazy var view: NSView = {
        let page = PageKit.page()
        page.addArrangedSubview(PageKit.heading("Pack Library", "Preserve archives and author private Topic Pack candidates."))
        page.addArrangedSubview(PageKit.help("Archive safety and SHA-256 checks do not establish ownership. Story Studio still needs trusted receipts or publisher signatures and compatible content before installation. Your transfer and authoring sources remain separate."))
        page.addArrangedSubview(PageKit.help("Topic Pack drafts use your local Story Studio authoring tools and an existing registry reservation. Save, preview and export in the browser; exporting a candidate does not publish or install it."))
        let open = NSButton(title: "Open Pack Library in Browser", target: self, action: #selector(openLibrary))
        open.bezelStyle = .rounded
        page.addArrangedSubview(open)
        page.addArrangedSubview(status)
        return page
    }()

    func activated() {}

    @objc private func openLibrary() {
        guard GatewayController.shared.isRunning,
              let url = GatewayAPI.url("/admin#packs") else {
            status.stringValue = "Start Gateway before opening the library."
            return
        }
        if !NSWorkspace.shared.open(url) {
            status.stringValue = "Could not open your browser. The library is at \(url.absoluteString)."
        } else {
            status.stringValue = "Opened the local library. If asked, use the Gateway key from Settings—not a provider key."
        }
    }
}
