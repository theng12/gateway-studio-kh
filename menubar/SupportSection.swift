import AppKit

/// Where "Support this app" lands: an in-app page that says what the money does, who receives it
/// and how, with the browser opened only by the button at the bottom.
///
/// The footer used to jump straight to nowpayments.io, which is a strange thing for a button inside
/// the app to do without a word of explanation. Nothing here takes payment: the app explains, and
/// hands off to the donation page the owner chose.
final class SupportSection: NSObject, AppSection {
    static let shared = SupportSection()

    let title = "Support this app"
    let symbol = "heart"
    let refreshesAutomatically = false

    lazy var view: NSView = buildPage()

    func activated() {}

    private final class FlippedView: NSView {
        override var isFlipped: Bool { true }
    }

    private func buildPage() -> NSView {
        let page = PageKit.page()
        PageKit.addFull(PageKit.heading(
            "Support this app",
            "Gateway Studio KH is made by one person, not a company, and stays free to run. If it earns a place in your day, you can put something back."
        ), to: page)

        page.addArrangedSubview(PageKit.sectionTitle("What your support pays for"))
        for line in [
            "New models and providers, kept working as the web apps they use change underneath them.",
            "Features asked for by the people using it — the sidebar, Fleet, the browser workers.",
            "The Macs, disks and developer accounts that test a release before you get it.",
        ] {
            page.addArrangedSubview(PageKit.label("• " + line, .body, wrapping: true))
        }

        page.addArrangedSubview(PageKit.sectionTitle("Who receives it"))
        page.addArrangedSubview(PageKit.label(
            "The app's developer, directly. There is no company in between and no subscription: a donation is a one-off payment you choose the size of.",
            .body, colour: .labelColor, wrapping: true))

        page.addArrangedSubview(PageKit.sectionTitle("How it works"))
        page.addArrangedSubview(PageKit.label(
            "The button below opens a NowPayments donation page in your browser. The payment happens there, on their site — this app never sees your card, bank details or wallet, and cannot. Nothing in the app is locked behind a payment.",
            .body, colour: .labelColor, wrapping: true))

        let open = PageKit.button("Open the donation page", target: self, action: #selector(openDonationPage))
        let note = PageKit.label(SupportLink.donationURL.absoluteString, .caption,
                                 colour: .secondaryLabelColor, wrapping: true)
        page.addArrangedSubview(open)
        page.addArrangedSubview(note)

        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(page)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = document
        NSLayoutConstraint.activate([
            page.topAnchor.constraint(equalTo: document.topAnchor),
            page.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            page.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            page.bottomAnchor.constraint(equalTo: document.bottomAnchor),
            document.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            document.trailingAnchor.constraint(equalTo: scroll.contentView.trailingAnchor),
            document.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
        ])
        return scroll
    }

    @objc private func openDonationPage() {
        if !SupportLink.openDonationPage() {
            NSSound.beep()
        }
    }
}
