import AppKit

/// **The What's New page itself** — the header, the rule, and the notes rendered as type.
///
/// Split out of `WhatsNewWindow` deliberately. There are now **two doors** to the same page (the
/// sidebar row the owner asked for, and a panel for a headless launch), and neither of them should
/// own the typography: a renderer that lives inside a window cannot be compiled or measured without
/// the window, which is exactly what `tests/test_whats_new_page.py` has to do to prove the markdown
/// becomes headings and bullets rather than literal `##` and `**`.
///
/// The text ships **inside the bundle** (`Contents/Resources/CHANGELOG.md`), so the screen
/// describes the build that is actually running rather than whatever the checkout says today.
enum WhatsNewPage {
/// The version this build is, from the bundle rather than a constant that can drift — the same
/// source Studio Hub's What's New reads.
static var appVersion: String {
    (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String)
        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) } ?? "unknown"
}

/// The bundled notes, or a plain explanation when the bundle is missing them.
static func notes() -> String {
    guard let url = Bundle.main.url(forResource: "CHANGELOG", withExtension: "md"),
          let text = try? String(contentsOf: url, encoding: .utf8),
          !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        return """
        # What's new in Gateway Studio KH

        These notes were not found inside the app, so this window cannot describe the build you
        are running. The release history is in CHANGELOG.md.
        """
    }
    return text
}

// MARK: The page

/// The whole page: header, rule, notes. Exposed so a preview harness can render exactly what the
/// window shows, at whatever size, without opening the window on someone's screen.
static func makePage(notes: String, version: String, build: String) -> NSView {
    let document = NotesDocumentView()
    document.translatesAutoresizingMaskIntoConstraints = false

    // The shared page scaffold, like every other page: title, one-line subtitle (the version and this
    // build's stamp), then the notes. The page used to draw its own header bar and rule.
    let column = PageKit.page()
    column.spacing = DS.Space.s
    let header = PageKit.heading("What's New", "Version \(version) · build \(build)")
    column.addArrangedSubview(header)
    column.setCustomSpacing(DS.Space.xl, after: header)
    let wide = column.widthAnchor

    func pin(_ view: NSView) {
        view.widthAnchor.constraint(equalTo: wide, constant: -(PageKit.pagePadding * 2)).isActive = true
    }

    for block in NotesMarkdown.blocks(from: notes) {
        switch block {
        case .heading(let level, let text):
            // The notes open with `#` (their own title, which the page heading already says); `##` is a
            // release and `###` a feature inside it.
            if level <= 1 { continue }
            let label = PageKit.label("", level == 2 ? .section : .rowTitle, wrapping: true)
            label.attributedStringValue = NotesMarkdown.inline(text, base: label.font!)
            if level == 2, column.arrangedSubviews.count > 1 {
                column.setCustomSpacing(DS.Space.xl, after: column.arrangedSubviews.last!)
            }
            column.addArrangedSubview(label)
            pin(label)
            column.setCustomSpacing(DS.Space.tight, after: label)
        case .paragraph(let text):
            let label = PageKit.label("", .body, wrapping: true)
            label.attributedStringValue = NotesMarkdown.inline(text, base: label.font!)
            column.addArrangedSubview(label)
            pin(label)
        case .bullet(let text):
            let label = PageKit.label("", .body, wrapping: true)
            label.attributedStringValue = NotesMarkdown.bullet(text, base: label.font!)
            column.addArrangedSubview(label)
            pin(label)
            column.setCustomSpacing(DS.Space.tight, after: label)
        case .rule:
            let rule = separator()
            column.addArrangedSubview(rule)
            pin(rule)
            column.setCustomSpacing(DS.Space.m, after: rule)
        }
    }

    document.addSubview(column)
    NSLayoutConstraint.activate([
        column.topAnchor.constraint(equalTo: document.topAnchor),
        column.leadingAnchor.constraint(equalTo: document.leadingAnchor),
        column.trailingAnchor.constraint(equalTo: document.trailingAnchor),
        column.bottomAnchor.constraint(equalTo: document.bottomAnchor),
    ])

    let scroll = NSScrollView()
    scroll.hasVerticalScroller = true
    scroll.drawsBackground = false
    scroll.autohidesScrollers = true
    scroll.documentView = document
    scroll.translatesAutoresizingMaskIntoConstraints = false

    let content = NSView()
    content.addSubview(scroll)
    NSLayoutConstraint.activate([
        scroll.topAnchor.constraint(equalTo: content.topAnchor),
        scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor),
        scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor),
        scroll.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        // The document is exactly as wide as the window gives it, so the notes wrap instead of
        // arguing for their own ideal width and pushing a horizontal scroller into the page.
        document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
    ])
    return content
}

private static func separator() -> NSBox {
    let box = NSBox()
    box.boxType = .separator
    box.translatesAutoresizingMaskIntoConstraints = false
    return box
}
}

/// A scrolling page's document view that keeps its wrapping labels wrapped to the visible width.
///
/// A wrapping `NSTextField` inside an `NSStackView` has no other way to learn where to break, and a
/// notes page is nothing but wrapping labels: `preferredMaxLayoutWidth` is set from the window's own
/// width on every layout pass, and only when it actually changed, because writing it unconditionally
/// is how a layout loop starts.
final class NotesDocumentView: NSView {
    /// `NSScrollView` scrolls a non-flipped document to its **bottom** left, so a tall page would
    /// open showing its last line. `PageKit.FlippedView` would do this, but it is `final` and this
    /// view needs its own `layout()`; one line here beats loosening a shared type for one caller.
    override var isFlipped: Bool { true }

    override func layout() {
        let available = bounds.width - (PageKit.pagePadding * 2)
        for label in wrappingLabels(in: self) where available > 0 {
            if abs(label.preferredMaxLayoutWidth - available) > 0.5 {
                label.preferredMaxLayoutWidth = available
                label.invalidateIntrinsicContentSize()
            }
        }
        super.layout()
    }

    private func wrappingLabels(in view: NSView) -> [NSTextField] {
        var found: [NSTextField] = []
        for subview in view.subviews {
            if let field = subview as? NSTextField, field.isEditable == false, field.lineBreakMode == .byWordWrapping {
                found.append(field)
            }
            found.append(contentsOf: wrappingLabels(in: subview))
        }
        return found
    }
}

/// A small Markdown renderer, enough for the notes files and nothing more.
///
/// The same block rules and the same inline parser as Studio Hub's `MarkdownText`, so a note reads
/// identically in both apps. A third-party Markdown view would be a dependency for one window, and
/// the system's Markdown support covers inline spans only — not the headings and bullets these notes
/// are made of.
enum NotesMarkdown {
    enum Block {
        case heading(level: Int, text: String)
        case bullet(String)
        case paragraph(String)
        case rule
    }

    static func blocks(from markdown: String) -> [Block] {
        var blocks: [Block] = []
        var paragraph: [String] = []

        func flush() {
            guard !paragraph.isEmpty else { return }
            blocks.append(.paragraph(paragraph.joined(separator: " ")))
            paragraph = []
        }

        for raw in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty {
                flush()
                continue
            }
            if line == "---" {
                flush()
                blocks.append(.rule)
                continue
            }
            if line.hasPrefix("#") {
                flush()
                let level = line.prefix(while: { $0 == "#" }).count
                let text = line.drop(while: { $0 == "#" }).trimmingCharacters(in: .whitespaces)
                blocks.append(.heading(level: level, text: text))
                continue
            }
            if line.hasPrefix("- ") {
                flush()
                blocks.append(.bullet(String(line.dropFirst(2))))
                continue
            }
            paragraph.append(line)
        }
        flush()
        return blocks
    }

    /// One bullet, with a hanging indent so a wrapped line starts under the text and not under the
    /// dot — which is the whole difference between a list and a paragraph that begins with a dot.
    static func bullet(_ text: String, base: NSFont) -> NSAttributedString {
        let style = NSMutableParagraphStyle()
        style.firstLineHeadIndent = 0
        style.headIndent = DS.Space.l
        style.tabStops = [NSTextTab(textAlignment: .left, location: DS.Space.l)]
        style.lineSpacing = 2
        let dot = NSMutableAttributedString(
            string: "\u{2022}\t",
            attributes: [.font: base, .foregroundColor: NSColor.secondaryLabelColor]
        )
        dot.append(inline(text, base: base))
        dot.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: dot.length))
        return dot
    }

    /// Bold, italic and code spans — all the notes use beyond headings and bullets.
    ///
    /// **Hand-rolled on purpose, and the reason is the deployment target.** Studio Hub parses these
    /// with Foundation's `AttributedString(markdown:)`; that type needs macOS 12, and this app runs
    /// on 11.0 (`MACOS_MIN` in `build.sh`, `LSMinimumSystemVersion` in the bundle), so the same call
    /// is not available here. Raising the floor for one window would trade a supported Mac for a
    /// renderer, so this parses the three span kinds the notes actually contain — audited against
    /// `CHANGELOG.md`: no tables, no block quotes, no numbered or nested lists, no links, no code
    /// fences, no strikethrough, and no double-backtick spans.
    ///
    /// Recursive, so `**bold with `code` inside**` keeps both. An unclosed marker is plain text, not
    /// a half-styled line: `span(in:marker:)` returns nil and the character is copied through.
    static func inline(_ text: String, base: NSFont, colour: NSColor = .labelColor) -> NSAttributedString {
        let out = NSMutableAttributedString()
        var plain = ""
        var index = text.startIndex

        func flush() {
            guard !plain.isEmpty else { return }
            out.append(NSAttributedString(string: plain,
                                          attributes: [.font: base, .foregroundColor: colour]))
            plain = ""
        }

        while index < text.endIndex {
            let rest = text[index...]
            if let (inner, after) = span(in: rest, marker: "**") {
                flush()
                out.append(inline(inner, base: trait(.boldFontMask, of: base), colour: colour))
                index = after
            } else if let (inner, after) = span(in: rest, marker: "`") {
                flush()
                out.append(NSAttributedString(string: inner, attributes: [
                    .font: TextRole.mono.font,
                    .foregroundColor: colour,
                    // A code span is a different *kind* of text, so it gets a different background
                    // rather than a louder colour; the notes use it for identifiers and flags.
                    .backgroundColor: NSColor.quaternaryLabelColor.withAlphaComponent(0.28),
                ]))
                index = after
            } else if let (inner, after) = span(in: rest, marker: "*") {
                flush()
                out.append(inline(inner, base: trait(.italicFontMask, of: base), colour: colour))
                index = after
            } else {
                plain.append(text[index])
                index = text.index(after: index)
            }
        }
        flush()

        let style = NSMutableParagraphStyle()
        style.lineSpacing = 2
        out.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: out.length))
        return out
    }

    /// The `marker…marker` span at the start of `text`, with what follows it — or nil when the
    /// marker does not open here or is never closed.
    private static func span(in text: Substring, marker: String) -> (String, String.Index)? {
        guard text.hasPrefix(marker) else { return nil }
        let body = text.dropFirst(marker.count)
        guard let closing = body.range(of: marker) else { return nil }
        return (String(body[body.startIndex..<closing.lowerBound]), closing.upperBound)
    }

    private static func trait(_ trait: NSFontTraitMask, of font: NSFont) -> NSFont {
        NSFontManager.shared.convert(font, toHaveTrait: trait)
    }
}
