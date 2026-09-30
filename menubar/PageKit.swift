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
