import Cocoa

/// The ordinary browser owns file selection and downloads. No duplicate import
/// protocol or WebKit-only download bridge is needed in the native shell.
final class PackLibrarySection: NSObject, AppSection {
    static let shared = PackLibrarySection()
    let title = "Content Packs"
    let symbol = "archivebox"
    let refreshesAutomatically = false
    private let status = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)

    lazy var view: NSView = {
        let page = PageKit.page()
        page.addArrangedSubview(PageKit.heading("Content Packs", "Set up the private shelf, author new packs, and review preserved archives."))
        page.addArrangedSubview(PageKit.help("The browser separates authoring drafts, shelved releases, and preserved archives. Add to the private shelf is always explicit; importing an archive does not establish ownership or install anything."))
        page.addArrangedSubview(PageKit.help("The first delivery slice supports Series Formula, Topic Pack, Visual Style, and Thumbnail Recipe packages. Topic Pack drafts use the local Story Studio authoring tools and registry reservation."))
        let open = PageKit.primaryButton("Open Content Packs in Browser", target: self, action: #selector(openLibrary), regular: true)
        page.addArrangedSubview(open)
        page.addArrangedSubview(status)
        return page
    }()

    func activated() {}

    @objc private func openLibrary() {
        guard GatewayController.shared.isRunning,
              let url = GatewayAPI.url("/admin#packs") else {
            status.stringValue = "Start Gateway before opening Content Packs."
            return
        }
        if !NSWorkspace.shared.open(url) {
            status.stringValue = "Could not open your browser. Content Packs is at \(url.absoluteString)."
        } else {
            status.stringValue = "Opened Content Packs. If asked, use the Gateway key from Settings—not a provider key."
        }
    }
}
