import AppKit

/// Shared page furniture, so every page in the window reads the same way.
///
/// These mirror the sibling **Studio Hub KH** app's SwiftUI `heading`,
/// `sectionTitle` and `empty` helpers. The two apps are used side by side, and
/// the owner asked for the same design language here: a large title with a
/// one-line explanation of what the page is for, `title3`-weight section
/// titles, and empty states that say what to *do* rather than only what is
/// missing.
/// A document view for a scrolling page.
///
/// `NSScrollView` scrolls a non-flipped document to its **bottom** left, so a
/// tall page opens showing its last line. Flipping the coordinate system makes
/// it start at the top, which is where a reader expects to begin.
final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

enum PageKit {
    /// Artifact endpoints have no filename extension; use the stated media type.
    static func artifactExtension(_ mediaType: String) -> String {
        switch mediaType.lowercased().split(separator: ";").first?.trimmingCharacters(in: .whitespaces) {
        case "audio/mpeg": return "mp3"
        case "audio/wav", "audio/x-wav": return "wav"
        case "audio/ogg": return "ogg"
        case "image/png": return "png"
        case "image/jpeg": return "jpg"
        case "image/webp": return "webp"
        case "video/mp4": return "mp4"
        case "text/plain": return "txt"
        case "text/vtt": return "vtt"
        case "application/x-subrip": return "srt"
        case "application/json": return "json"
        default: return "bin"
        }
    }

    /// Studio Hub pads every page body by 28. The pages here previously used
    /// 18/20 and 14/16, which made switching sections look like switching apps.
    static let pagePadding: CGFloat = 28
    /// The gap between major blocks on a page.
    static let blockSpacing: CGFloat = 22
    /// Descriptions and empty-state text wrap at this width. A width is needed
    /// because a wrapping `NSTextField` inside an `NSStackView` has no other way
    /// to learn where to break.
    static let textWidth: CGFloat = 620

    static func label(
        _ text: String,
        size: CGFloat,
        weight: NSFont.Weight = .regular,
        colour: NSColor = .labelColor,
        wrapping: Bool = false
    ) -> NSTextField {
        let field = wrapping
            ? NSTextField(wrappingLabelWithString: text)
            : NSTextField(labelWithString: text)
        field.font = .systemFont(ofSize: size, weight: weight)
        field.textColor = colour
        // A label is never why a page needs more width, and never the thing that
        // should grow when there is spare height.
        field.setContentHuggingPriority(.required, for: .vertical)
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        if wrapping {
            field.preferredMaxLayoutWidth = textWidth
            field.isSelectable = true
        }
        return field
    }

    /// Page title plus its one-line purpose, the way every Studio Hub page opens.
    static func heading(_ title: String, _ description: String) -> NSView {
        let stack = NSStackView(views: [
            label(title, size: 26, weight: .semibold),
            label(description, size: 13, colour: .secondaryLabelColor, wrapping: true),
        ])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        return stack
    }

    /// A section title within a page.
    static func sectionTitle(_ text: String) -> NSTextField {
        label(text, size: 15, weight: .semibold)
    }

    /// What a page shows when it has nothing to show yet.
    static func empty(_ title: String, _ detail: String, symbol: String) -> NSView {
        let icon = NSImageView()
        icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 28, weight: .light)
        icon.contentTintColor = .secondaryLabelColor

        let stack = NSStackView(views: [
            icon,
            label(title, size: 13, weight: .semibold),
            label(detail, size: 12, colour: .secondaryLabelColor, wrapping: true),
        ])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 32, left: 32, bottom: 32, right: 32)
        return stack
    }

    /// A page body: vertical stack with Studio Hub's padding and spacing.
    ///
    /// `alignment: .leading` matches Studio Hub's `VStack(alignment: .leading)`;
    /// full-width rows opt in by pinning their own width to the stack.
    static func page() -> NSStackView {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = blockSpacing
        root.edgeInsets = NSEdgeInsets(
            top: pagePadding, left: pagePadding, bottom: pagePadding, right: pagePadding
        )
        // The detail column is exactly as wide as the window gives it. Without
        // this the page argues for its content's ideal width and wins, pushing
        // the whole split view past the window's right edge.
        root.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        root.translatesAutoresizingMaskIntoConstraints = false
        return root
    }

    /// The width of the label column in a form, so every control starts at the
    /// same x no matter how long its label is.
    static let formLabelWidth: CGFloat = 150

    /// A compact top-label form row. Top labels keep long captions and controls
    /// usable when the native window is narrowed; the page scrolls instead of
    /// asking AppKit to grow the window past its minimum.
    static func formRow(_ caption: String, _ control: NSView) -> NSView {
        formRow(caption, [control])
    }

    static func formRow(_ caption: String, _ controls: [NSView]) -> NSView {
        let label = self.label(caption, size: 12, colour: .secondaryLabelColor)
        label.alignment = .left

        let column = NSStackView(views: controls)
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 6

        let row = NSStackView(views: [label, column])
        row.orientation = .vertical
        row.alignment = .leading
        row.spacing = 5
        row.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return row
    }

    /// Secondary explanatory text, Studio Hub's `.callout` +
    /// `.foregroundStyle(.secondary)`.
    static func help(_ text: String) -> NSTextField {
        label(text, size: 11.5, colour: .secondaryLabelColor, wrapping: true)
    }

    /// A confirmation for something that cannot be taken back. Cancel is the
    /// default, so a stray Return does nothing.
    static func confirm(_ message: String, _ detail: String, ok: String) -> Bool {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = detail
        alert.alertStyle = .warning
        alert.addButton(withTitle: ok)
        alert.addButton(withTitle: "Cancel")
        alert.buttons.first?.keyEquivalent = ""
        alert.buttons.last?.keyEquivalent = "\r"
        return alert.runModal() == .alertFirstButtonReturn
    }

    /// A full-width row inside a `page()`.
    static func row(_ views: [NSView], spacing: CGFloat = 10) -> NSStackView {
        let row = NSStackView(views: views)
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = spacing
        return row
    }
}

/// A rounded panel that follows light/dark mode. A `CALayer` colour is fixed at the moment it is
/// set, so a card that assigned `controlBackgroundColor.cgColor` once stayed light after the
/// system went dark; `updateLayer` re-resolves it whenever the appearance changes.
final class CardView: NSView {
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }
    required init?(coder: NSCoder) { fatalError("not used") }
    override var wantsUpdateLayer: Bool { true }
    override func updateLayer() {
        layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
        layer?.borderColor = NSColor.separatorColor.cgColor
        layer?.borderWidth = 1
        layer?.cornerRadius = 8
    }
}

/// Something in a preview that must stop when the preview is replaced (audio, video).
protocol PreviewStoppable: AnyObject { func stop() }

/// **The one frame every Create page uses**, after Studio Hub's generator pages: a large title, then
/// the form on the left (310-440 pt, scrolling on its own) and the result on the right, taking every
/// remaining point of width and height. Six pages, one layout, so they cannot drift apart one fix
/// at a time. A page fills `form` (via `addForm`), puts buttons in `previewActions`, and swaps what
/// the result card shows with `setPreview`.
final class CreatePageLayout: NSView {
    static let formMinWidth: CGFloat = 310
    static let formIdealWidth: CGFloat = 390
    static let formMaxWidth: CGFloat = 440
    /// Below this the preview is too narrow to be a preview; the form gives way first.
    static let previewMinWidth: CGFloat = 260
    static let gap: CGFloat = 24

    let form = NSStackView()
    let formScroll = NSScrollView()
    let preview = NSStackView()
    let previewActions = NSStackView()
    let previewCard = CardView()
    let caption = PageKit.label("", size: 12, colour: .secondaryLabelColor, wrapping: true)
    private(set) var content: NSView?

    init(title: String, blurb: String, previewTitle: String) {
        super.init(frame: .zero)
        let heading = PageKit.heading(title, blurb)

        form.orientation = .vertical
        form.alignment = .leading
        form.spacing = 18
        form.translatesAutoresizingMaskIntoConstraints = false
        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(form)
        formScroll.documentView = document
        formScroll.hasVerticalScroller = true
        formScroll.autohidesScrollers = true
        formScroll.drawsBackground = false
        formScroll.borderType = .noBorder

        previewActions.orientation = .horizontal
        previewActions.spacing = 8
        let title = PageKit.sectionTitle(previewTitle)
        // The title keeps its words; the buttons are what give way when the window is at its narrowest.
        title.setContentCompressionResistancePriority(.required, for: .horizontal)
        let header = PageKit.row([title, NSView(), previewActions])
        caption.preferredMaxLayoutWidth = 0
        caption.isHidden = true
        preview.orientation = .vertical
        preview.alignment = .leading
        preview.spacing = 12
        for view in [header, previewCard, caption] { preview.addArrangedSubview(view) }
        header.widthAnchor.constraint(equalTo: preview.widthAnchor).isActive = true
        previewCard.widthAnchor.constraint(equalTo: preview.widthAnchor).isActive = true
        caption.widthAnchor.constraint(equalTo: preview.widthAnchor).isActive = true
        previewCard.heightAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true
        // The card takes whatever height the header and caption leave.
        previewCard.setContentHuggingPriority(NSLayoutConstraint.Priority(1), for: .vertical)
        previewCard.setContentCompressionResistancePriority(NSLayoutConstraint.Priority(1), for: .vertical)

        for view in [heading, formScroll, preview] {
            view.translatesAutoresizingMaskIntoConstraints = false
            addSubview(view)
        }
        let pad = PageKit.pagePadding
        let ideal = formScroll.widthAnchor.constraint(equalToConstant: Self.formIdealWidth)
        // Below 500, or the window treats the ideal as a minimum and refuses to get narrower.
        ideal.priority = NSLayoutConstraint.Priority(400)
        NSLayoutConstraint.activate([
            heading.topAnchor.constraint(equalTo: topAnchor, constant: pad),
            heading.leadingAnchor.constraint(equalTo: leadingAnchor, constant: pad),
            heading.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -pad),

            formScroll.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: PageKit.blockSpacing),
            formScroll.leadingAnchor.constraint(equalTo: leadingAnchor, constant: pad),
            formScroll.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -pad),
            formScroll.widthAnchor.constraint(greaterThanOrEqualToConstant: Self.formMinWidth),
            formScroll.widthAnchor.constraint(lessThanOrEqualToConstant: Self.formMaxWidth),
            ideal,

            preview.topAnchor.constraint(equalTo: formScroll.topAnchor),
            preview.leadingAnchor.constraint(equalTo: formScroll.trailingAnchor, constant: Self.gap),
            preview.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -pad),
            preview.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -pad),
            preview.widthAnchor.constraint(greaterThanOrEqualToConstant: Self.previewMinWidth),

            form.topAnchor.constraint(equalTo: document.topAnchor),
            form.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            // Room for the overlay scroller, so it never sits on a control.
            form.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -16),
            form.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -20),
            document.widthAnchor.constraint(equalTo: formScroll.contentView.widthAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// A full-width row of the form.
    func addForm(_ view: NSView) {
        form.addArrangedSubview(view)
        view.widthAnchor.constraint(equalTo: form.widthAnchor).isActive = true
    }

    /// Replace what the result card shows. The view fills the card, and must be able to shrink
    /// below its natural size (an image, a player).
    func setPreview(_ view: NSView) {
        func stopAll(_ view: NSView) {
            (view as? PreviewStoppable)?.stop()
            view.subviews.forEach(stopAll)
        }
        for old in previewCard.subviews {
            stopAll(old)
            old.removeFromSuperview()
        }
        content = view
        view.translatesAutoresizingMaskIntoConstraints = false
        previewCard.addSubview(view)
        let low = NSLayoutConstraint.Priority(1)
        view.setContentCompressionResistancePriority(low, for: .horizontal)
        view.setContentCompressionResistancePriority(low, for: .vertical)
        let inset: CGFloat = 16
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: previewCard.topAnchor, constant: inset),
            view.leadingAnchor.constraint(equalTo: previewCard.leadingAnchor, constant: inset),
            view.trailingAnchor.constraint(equalTo: previewCard.trailingAnchor, constant: -inset),
            view.bottomAnchor.constraint(equalTo: previewCard.bottomAnchor, constant: -inset),
        ])
    }

    func setCaption(_ text: String) {
        caption.stringValue = text
        caption.isHidden = text.isEmpty
    }
}
