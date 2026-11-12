import AppKit

// MARK: - Design tokens

/// **The one source of type, spacing, radii and status colour for every native page.** The sibling Studio
/// Hub app follows the same scale (`Design.swift` there), so the two read as one product. A page asks for a
/// role (`TextRole.body`), a step (`DS.Space.l`), a radius (`DS.Radius.card`) and a state (`Health.warn`);
/// it never types a point size or a colour. Approved spec: `_orchestra/audit-20261001/design/README.md`.
enum DS {
    /// 4 pt grid; `tight` (6) is the one allowed half-step, for title-to-subtitle and label-to-control.
    enum Space {
        static let xs: CGFloat = 4
        static let tight: CGFloat = 6
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        static let xl: CGFloat = 24
        static let page: CGFloat = 28
    }
    enum Radius {
        /// Cards, panels, banners, previews.
        static let card: CGFloat = 8
        /// Buttons, fields, thumbnails, code blocks, table containers.
        static let control: CGFloat = 6
        static let sheet: CGFloat = 12
    }
    enum Size {
        /// The sidebar, in both apps.
        static let sidebar: CGFloat = 220
        static let row: CGFloat = 28
        static let smallControl: CGFloat = 22
        static let regularControl: CGFloat = 28
        static let badge: CGFloat = 20
        static let meter: CGFloat = 4
        static let rowIcon: CGFloat = 22
    }
}

/// The type scale: eight roles, no half points, no bold. See README 2.1.
enum TextRole {
    case pageTitle, section, rowTitle, body, secondary, caption, captionStrong, figure, mono

    var font: NSFont {
        switch self {
        case .pageTitle: return .systemFont(ofSize: 26, weight: .semibold)
        case .section: return .systemFont(ofSize: 15, weight: .semibold)
        case .rowTitle: return .systemFont(ofSize: 13, weight: .semibold)
        case .body: return .systemFont(ofSize: 13)
        case .secondary: return .systemFont(ofSize: 12)
        case .caption: return .systemFont(ofSize: 11)
        case .captionStrong: return .systemFont(ofSize: 11, weight: .semibold)
        case .figure: return .monospacedDigitSystemFont(ofSize: 22, weight: .semibold)
        case .mono: return .monospacedSystemFont(ofSize: 12, weight: .regular)
        }
    }
}

/// The one status vocabulary. Green is fine, amber means look soon, red means act now, grey is idle or
/// unknown, blue is a fact (never an alarm). Colour is only ever used for state, never for decoration.
enum Health: Int, Comparable {
    case ok, off, info, warn, bad
    static func < (a: Health, b: Health) -> Bool { a.rawValue < b.rawValue }

    var tint: NSColor {
        switch self {
        case .ok: return .systemGreen
        case .warn: return .systemOrange
        case .bad: return .systemRed
        case .off: return .systemGray
        case .info: return .systemBlue
        }
    }

    /// Text in a status colour. Plain `systemOrange` on a white card is about 2.3:1; blending 40 % toward the
    /// label colour darkens it in light mode and lightens it in dark mode, so one token reads in both.
    var text: NSColor {
        let base = tint
        let fraction: CGFloat = self == .off ? 0.2 : 0.4
        return NSColor(name: nil) { appearance in
            let dark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return base.blended(withFraction: fraction, of: dark ? .white : .black) ?? base
        }
    }

    var symbol: String {
        switch self {
        case .ok: return "checkmark.circle.fill"
        case .warn: return "exclamationmark.triangle.fill"
        case .bad: return "xmark.octagon.fill"
        case .off: return "moon.zzz.fill"
        case .info: return "info.circle.fill"
        }
    }

    /// The gateway's issue severities (`error`, `warning`, `info`).
    init(severity: String?) {
        switch severity?.lowercased() {
        case "error", "critical", "bad", "red", "stop": self = .bad
        case "warning", "warn", "amber": self = .warn
        case "info", "notice": self = .info
        case "ok", "green": self = .ok
        default: self = .off
        }
    }

    /// **The one state -> colour mapping** for a job, batch, rollout step or node. Every state pill goes through
    /// here, so a word never has two colours: running and everything that is still in flight is amber, a failure
    /// (or an outcome the Gateway cannot know) is red, a finished good result is green, and a state the owner
    /// chose (cancelled, paused) is neutral grey. An unrecognised word is grey, never a guessed colour.
    init(state: String?) {
        let word = (state ?? "").lowercased().replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ").trimmingCharacters(in: .whitespaces)
        if ["failed", "error", "errored", "uncertain", "unknown outcome", "needs recovery", "recovery required"].contains(word)
            || word.hasSuffix(" failed") || word.hasSuffix(" uncertain") {
            self = .bad
        } else if ["succeeded", "success", "done", "finished", "complete", "completed", "updated", "latest"].contains(word) {
            self = .ok
        } else if ["running", "queued", "accepted", "dispatching", "waiting", "updating", "updating…", "starting", "sending",
                   "sent", "installing", "in flight", "in progress", "working", "pending", "retrying", "stalled",
                   "stop requested", "cancellation requested", "cancelling"].contains(word)
                    || word.hasPrefix("waiting") {
            self = .warn
        } else {
            // cancelled, canceled, paused, stopped, skipped, idle, unknown, anything else.
            self = .off
        }
    }
}

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

    /// Every page pads its body by 28 on all four sides (web console included).
    static let pagePadding: CGFloat = DS.Space.page
    /// The gap between major blocks on a page.
    static let blockSpacing: CGFloat = DS.Space.xl
    /// Descriptions and empty-state text wrap at this width. A width is needed
    /// because a wrapping `NSTextField` inside an `NSStackView` has no other way
    /// to learn where to break.
    static let textWidth: CGFloat = 620

    static func label(
        _ text: String,
        _ role: TextRole,
        colour: NSColor = .labelColor,
        wrapping: Bool = false
    ) -> NSTextField {
        let field = wrapping
            ? NSTextField(wrappingLabelWithString: text)
            : NSTextField(labelWithString: text)
        field.font = role.font
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

    /// Page title plus its one-line purpose; the title always matches the sidebar row. `actions` sit at the
    /// trailing edge of the title block (small buttons), so a page's own verbs live in one predictable place.
    static func heading(_ title: String, _ description: String, actions: [NSView] = []) -> NSView {
        let text = NSStackView(views: [
            label(title, .pageTitle),
            label(description, .body, colour: .secondaryLabelColor, wrapping: true),
        ])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = DS.Space.tight
        guard !actions.isEmpty else { return text }
        // Beside buttons the description wraps at the width the row leaves it, not at the text-column width.
        if let description = text.arrangedSubviews.last as? NSTextField { description.preferredMaxLayoutWidth = 0 }
        let strip = NSStackView(views: actions)
        strip.orientation = .horizontal
        strip.spacing = DS.Space.s
        let row = NSStackView(views: [text, NSView(), strip])
        row.orientation = .horizontal
        row.alignment = .top
        row.spacing = DS.Space.m
        return row
    }

    /// A section title within a page.
    static func sectionTitle(_ text: String) -> NSTextField {
        label(text, .section)
    }

    /// A row of filters or view controls, 12 below the heading (spec 2.7b "toolbar slot").
    static func toolbar(_ views: [NSView]) -> NSStackView {
        let row = NSStackView(views: views)
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = DS.Space.m
        return row
    }

    /// What a page shows when it has nothing to show yet.
    static func empty(_ title: String, _ detail: String, symbol: String) -> NSView {
        let icon = NSImageView()
        icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 28, weight: .light)
        icon.contentTintColor = .secondaryLabelColor

        let blurb = label(detail, .secondary, colour: .secondaryLabelColor, wrapping: true)
        blurb.preferredMaxLayoutWidth = 440
        blurb.alignment = .center
        let stack = NSStackView(views: [icon, label(title, .rowTitle), blurb])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = DS.Space.s
        stack.edgeInsets = NSEdgeInsets(top: DS.Space.xl, left: DS.Space.xl, bottom: DS.Space.xl, right: DS.Space.xl)
        return stack
    }

    /// First load: a small spinner and a caption, never a blank page.
    static func loading(_ what: String = "Loading…") -> NSView {
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isIndeterminate = true
        spinner.startAnimation(nil)
        return row([spinner, label(what, .secondary, colour: .secondaryLabelColor)], spacing: DS.Space.s)
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

    /// Add a view to a `page()` at the full width inside the margins.
    static func addFull(_ view: NSView, to page: NSStackView) {
        page.addArrangedSubview(view)
        view.widthAnchor.constraint(equalTo: page.widthAnchor, constant: -pagePadding * 2).isActive = true
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
        let label = self.label(caption, .secondary, colour: .secondaryLabelColor)
        label.alignment = .left

        let column = NSStackView(views: controls)
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = DS.Space.tight

        let row = NSStackView(views: [label, column])
        row.orientation = .vertical
        row.alignment = .leading
        row.spacing = DS.Space.xs
        row.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return row
    }

    /// Secondary explanatory text, Studio Hub's `.callout` +
    /// `.foregroundStyle(.secondary)`.
    static func help(_ text: String) -> NSTextField {
        label(text, .secondary, colour: .secondaryLabelColor, wrapping: true)
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
    static func row(_ views: [NSView], spacing: CGFloat = DS.Space.s) -> NSStackView {
        let row = NSStackView(views: views)
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = spacing
        return row
    }

    // MARK: Buttons

    /// A bordered secondary button, `.small` for row and toolbar actions and `.regular` for forms and
    /// dialogs. **Title Case** ("Open Data Folder"); an ellipsis only when a dialog follows.
    @discardableResult
    static func button(_ title: String, target: AnyObject? = nil, action: Selector? = nil,
                       regular: Bool = false) -> NSButton {
        let button = NSButton(title: title, target: target, action: action)
        button.bezelStyle = .rounded
        button.controlSize = regular ? .regular : .small
        return button
    }

    /// The one primary action of a page region: accent-filled.
    static func primaryButton(_ title: String, target: AnyObject? = nil, action: Selector? = nil,
                              regular: Bool = false) -> NSButton {
        let button = PrimaryButton(title: title, target: target, action: action)
        button.controlSize = regular ? .regular : .small
        return button
    }
}

extension PageKit {
    /// A control that is `width` wide when the pane has room and gives way down to `min` when it has not. The
    /// preference sits below `NSLayoutPriorityDragThatCanResizeWindow` (510), so it can never be the reason a
    /// window at its minimum size grows ("a page must never be able to resize the window").
    static func softWidth(_ view: NSView, _ width: CGFloat, min: CGFloat) {
        let preferred = view.widthAnchor.constraint(equalToConstant: width)
        preferred.priority = NSLayoutConstraint.Priority(400)
        preferred.isActive = true
        view.widthAnchor.constraint(greaterThanOrEqualToConstant: min).isActive = true
    }
}

/// A card around a table (radius 8, separator border) whose layout pass re-fits the table's flexible columns.
final class TableCard: CardView {
    var onLayout: (() -> Void)?
    override func layout() { super.layout(); onLayout?() }

    /// Put `scroll` inside the card, 1 pt in so the border shows and the corners clip.
    func hold(table scroll: NSScrollView) {
        scroll.borderType = .noBorder
        scroll.drawsBackground = false
        scroll.wantsLayer = true
        scroll.layer?.cornerRadius = DS.Radius.card - 1
        scroll.layer?.masksToBounds = true
        scroll.translatesAutoresizingMaskIntoConstraints = false
        addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: topAnchor, constant: 1),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 1),
            scroll.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -1),
            scroll.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -1),
        ])
    }
}

/// A table row that draws its own bottom separator, so the lines stop with the data (a table's grid lines
/// keep going down through the empty space below the last row).
final class SeparatorRowView: NSTableRowView {
    override func drawBackground(in dirtyRect: NSRect) {
        super.drawBackground(in: dirtyRect)
        NSColor.separatorColor.setFill()
        NSRect(x: 0, y: isFlipped ? bounds.height - 1 : 0, width: bounds.width, height: 1).fill()
    }
}

extension PageKit {
    /// Fit a table's columns to its pane. `flex` maps a column id to its weight and its base width: when the pane is
    /// wider than the bases and the fixed columns together, the spare width is shared out by weight; when it is
    /// narrower, the flexible columns shrink toward their `minWidth` first. `hiding` maps a column id to the pane
    /// width below which that column is dropped (a secondary column, so the primary ones keep readable widths).
    /// A pane too narrow for even the minimums keeps them and scrolls: the last resort, not the layout.
    static func fitColumns(of table: NSTableView, in scroll: NSScrollView,
                           flex: [String: (weight: CGFloat, base: CGFloat)], hiding: [String: CGFloat] = [:]) {
        let available = scroll.contentSize.width
        guard available > 0 else { return }
        for column in table.tableColumns {
            if let threshold = hiding[column.identifier.rawValue] { column.isHidden = available < threshold }
        }
        let shown = table.tableColumns.filter { !$0.isHidden }
        let flexible = shown.filter { flex[$0.identifier.rawValue] != nil }
        guard !flexible.isEmpty else { return }
        let spacing = table.intercellSpacing.width * CGFloat(shown.count)
        let fixed = shown.filter { flex[$0.identifier.rawValue] == nil }.reduce(0) { $0 + $1.width }
        let bases = flexible.reduce(0) { $0 + (flex[$1.identifier.rawValue]?.base ?? 0) }
        let extra = available - fixed - spacing - bases
        let weights = flexible.reduce(0) { $0 + (flex[$1.identifier.rawValue]?.weight ?? 0) }
        let shrinkable = flexible.reduce(0) { $0 + max(0, (flex[$1.identifier.rawValue]?.base ?? 0) - $1.minWidth) }
        for column in flexible {
            guard let spec = flex[column.identifier.rawValue] else { continue }
            var width = spec.base
            if extra >= 0 {
                width += floor(extra * spec.weight / weights)
            } else if shrinkable > 0 {
                width -= (spec.base - column.minWidth) * min(1, -extra / shrinkable)
            }
            width = max(column.minWidth, floor(width))
            if abs(column.width - width) > 0.5 { column.width = width }
        }
    }
}

/// An accent-filled button (Studio Hub's `.borderedProminent`).
///
/// Drawn here rather than through `bezelColor`/`keyEquivalent`: the system only fills a default button while
/// its app is active, so an offscreen render (and a window behind another app) would show it grey. Colour is
/// resolved in `draw`, so it follows the accent colour and appearance.
final class PrimaryButton: NSButton {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        isBordered = false
        setButtonType(.momentaryPushIn)
        focusRingType = .exterior
    }

    convenience init(title: String, target: AnyObject?, action: Selector?) {
        self.init(frame: .zero)
        self.title = title
        self.target = target
        self.action = action
    }

    private var handler: (() -> Void)?

    /// A primary button whose action is a closure.
    convenience init(title: String, regular: Bool = true, handler: @escaping () -> Void) {
        self.init(title: title, target: nil, action: nil)
        controlSize = regular ? .regular : .small
        self.handler = handler
        target = self
        action = #selector(fire)
    }

    @objc private func fire() { handler?() }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var title: String {
        didSet { invalidateIntrinsicContentSize(); needsDisplay = true }
    }
    override var isEnabled: Bool { didSet { needsDisplay = true } }
    override var controlSize: NSControl.ControlSize {
        didSet { invalidateIntrinsicContentSize(); needsDisplay = true }
    }

    private var role: TextRole { controlSize == .small ? .secondary : .body }
    private var height: CGFloat {
        switch controlSize {
        case .small, .mini: return DS.Size.smallControl
        case .large: return DS.Size.regularControl + DS.Space.xs
        default: return DS.Size.regularControl
        }
    }

    override var intrinsicContentSize: NSSize {
        let text = (title as NSString).size(withAttributes: [.font: role.font])
        return NSSize(width: ceil(text.width) + 2 * DS.Space.m, height: height)
    }

    override func draw(_ dirtyRect: NSRect) {
        let fill = NSColor.controlAccentColor
        let shade: NSColor
        if !isEnabled { shade = NSColor.labelColor.withAlphaComponent(0.12) }
        else if isHighlighted { shade = fill.blended(withFraction: 0.25, of: .black) ?? fill }
        else { shade = fill }
        shade.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: DS.Radius.control, yRadius: DS.Radius.control).fill()
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        let colour: NSColor = isEnabled ? .white : .disabledControlTextColor
        let text = NSAttributedString(string: title, attributes: [
            .font: NSFont.systemFont(ofSize: role.font.pointSize, weight: .medium),
            .foregroundColor: colour, .paragraphStyle: paragraph,
        ])
        let size = text.size()
        text.draw(in: NSRect(x: 0, y: (bounds.height - size.height) / 2, width: bounds.width, height: size.height))
    }
}

/// A rounded panel that follows light/dark mode. A `CALayer` colour is fixed at the moment it is
/// set, so a card that assigned `controlBackgroundColor.cgColor` once stayed light after the
/// system went dark; `updateLayer` re-resolves it whenever the appearance changes.
///
/// `tint` turns it into a status banner (10 % fill, 35 % border) for the worst state it
/// reports; without one it is the plain card: control background, 1 pt separator border, radius 8.
class CardView: NSView {
    var tint: NSColor? { didSet { needsDisplay = true } }

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }
    required init?(coder: NSCoder) { fatalError("not used") }
    override var wantsUpdateLayer: Bool { true }
    override func updateLayer() {
        if let tint {
            layer?.backgroundColor = tint.withAlphaComponent(0.10).cgColor
            layer?.borderColor = tint.withAlphaComponent(0.35).cgColor
        } else {
            layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
            layer?.borderColor = NSColor.separatorColor.cgColor
        }
        layer?.borderWidth = 1
        layer?.cornerRadius = DS.Radius.card
    }

    /// Put `content` inside the card with `inset` on every side.
    func hold(_ content: NSView, inset: NSEdgeInsets = NSEdgeInsets(
        top: DS.Space.m, left: DS.Space.l, bottom: DS.Space.m, right: DS.Space.l)) {
        content.translatesAutoresizingMaskIntoConstraints = false
        addSubview(content)
        NSLayoutConstraint.activate([
            content.topAnchor.constraint(equalTo: topAnchor, constant: inset.top),
            content.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -inset.bottom),
            content.leadingAnchor.constraint(equalTo: leadingAnchor, constant: inset.left),
            content.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -inset.right),
        ])
    }
}

/// A status badge: tinted capsule, 11 pt semibold, optional symbol. The badge text always names the state,
/// so colour is never the only signal.
final class Badge: NSView {
    private let health: Health
    private var natural = NSSize(width: 60, height: DS.Size.badge)

    init(_ text: String, _ health: Health, symbol: Bool = false) {
        self.health = health
        super.init(frame: .zero)
        wantsLayer = true
        let label = NSTextField(labelWithString: text)
        label.font = TextRole.captionStrong.font
        label.textColor = health.text
        label.lineBreakMode = .byTruncatingTail
        var views: [NSView] = []
        if symbol, let image = NSImage(systemSymbolName: health.symbol, accessibilityDescription: nil) {
            let icon = NSImageView(image: image)
            icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 10, weight: .semibold)
            icon.contentTintColor = health.text
            views.append(icon)
        }
        views.append(label)
        let stack = NSStackView(views: views)
        stack.spacing = DS.Space.xs
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: DS.Space.s),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -DS.Space.s),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
        natural = NSSize(width: stack.fittingSize.width + 2 * DS.Space.s, height: DS.Size.badge)
        setContentHuggingPriority(.required, for: .horizontal)
        setContentHuggingPriority(.required, for: .vertical)
        setContentCompressionResistancePriority(.required, for: .horizontal)
    }
    required init?(coder: NSCoder) { fatalError("not used") }
    override var intrinsicContentSize: NSSize { natural }
    override var wantsUpdateLayer: Bool { true }
    override func updateLayer() {
        layer?.backgroundColor = health.tint.withAlphaComponent(0.14).cgColor
        layer?.borderColor = health.tint.withAlphaComponent(0.35).cgColor
        layer?.borderWidth = 1
        layer?.cornerRadius = DS.Size.badge / 2
    }
}

/// A thin usage bar. Drawn in `draw`, which always runs in the right appearance.
final class Meter: NSView {
    var fraction: CGFloat { didSet { needsDisplay = true } }
    var health: Health { didSet { needsDisplay = true } }
    /// Overrides the status colour (a plain proportion bar that is not a state).
    var fill: NSColor? { didSet { needsDisplay = true } }
    init(fraction: Double, health: Health) {
        self.fraction = CGFloat(max(0, min(1, fraction)))
        self.health = health
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        heightAnchor.constraint(equalToConstant: DS.Size.meter).isActive = true
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirtyRect: NSRect) {
        let radius = bounds.height / 2
        NSColor.quaternaryLabelColor.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: radius, yRadius: radius).fill()
        (fill ?? health.tint).setFill()
        NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: bounds.width * max(0, min(1, fraction)), height: bounds.height),
                     xRadius: radius, yRadius: radius).fill()
    }
}

/// A small filled circle in a status colour. Drawn, not layer-backed, so it follows the appearance.
final class StatusDot: NSView {
    var colour: NSColor { didSet { needsDisplay = true } }
    init(_ colour: NSColor, diameter: CGFloat = 8) {
        self.colour = colour
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        widthAnchor.constraint(equalToConstant: diameter).isActive = true
        heightAnchor.constraint(equalToConstant: diameter).isActive = true
    }
    required init?(coder: NSCoder) { fatalError("not used") }
    override func draw(_ dirtyRect: NSRect) {
        colour.setFill()
        NSBezierPath(ovalIn: bounds).fill()
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
    static let previewMinWidth: CGFloat = 240
    static let gap: CGFloat = DS.Space.xl

    let form = NSStackView()
    let formScroll = NSScrollView()
    let preview = NSStackView()
    let previewActions = NSStackView()
    let previewCard = CardView()
    let caption = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    private(set) var content: NSView?

    init(title: String, blurb: String, previewTitle: String) {
        super.init(frame: .zero)
        let heading = PageKit.heading(title, blurb)

        form.orientation = .vertical
        form.alignment = .leading
        form.spacing = DS.Space.l
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
        previewActions.spacing = DS.Space.s
        let title = PageKit.sectionTitle(previewTitle)
        // The title keeps its words; the buttons are what give way when the window is at its narrowest.
        title.setContentCompressionResistancePriority(.required, for: .horizontal)
        let header = PageKit.row([title, NSView(), previewActions])
        caption.preferredMaxLayoutWidth = 0
        caption.isHidden = true
        preview.orientation = .vertical
        preview.alignment = .leading
        preview.spacing = DS.Space.m
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
            form.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -DS.Space.l),
            form.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -DS.Space.xl),
            document.widthAnchor.constraint(equalTo: formScroll.contentView.widthAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// A full-width row of the form.
    func addForm(_ view: NSView) {
        Self.yieldWidth(view)
        form.addArrangedSubview(view)
        view.widthAnchor.constraint(equalTo: form.widthAnchor).isActive = true
    }

    /// A popup, combo box, field or label in the form never asks for more width than its column has: its width
    /// preference gives way (priority 250), so the form column can stay at 310 and the window at its 860 pt minimum.
    private static func yieldWidth(_ view: NSView) {
        if view is NSPopUpButton || view is NSComboBox || view is NSTextField || view is NSSegmentedControl {
            view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        }
        view.subviews.forEach(yieldWidth)
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
        let inset = DS.Space.l
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

// MARK: - Escape clears a table's selection

/// A table whose Escape clears the selected row (owner rule 2026-11-12): in a page whose table has
/// a selection, Escape does something defined and harmless instead of nothing. The destructive
/// actions stay explicit buttons.
final class EscapeClearingTableView: NSTableView {
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53, !selectedRowIndexes.isEmpty {
            deselectAll(nil)
            return
        }
        super.keyDown(with: event)
    }
}

// MARK: - Escape closes every dialog

/// Owner rule (2026-11-12): Escape closes dialogs, sheets, popovers and menus everywhere.
///
/// AppKit maps Escape to a button titled **Cancel** by itself — measured: a two-button alert with
/// Cancel closes, and `alertSecondButtonReturn` comes back. But an alert with only **OK**, or with
/// no buttons at all, or one offering Wait / Restart Anyway, ignores Escape completely (measured:
/// the window stayed up). `closeOnEscape()` watches for Escape while its alert is the key window
/// and ends it as cancelled, so none of those dialogs traps the keyboard.
private final class EscapeCloser {
    private var monitor: Any?
    private var token: NSObjectProtocol?

    init(window: NSWindow) {
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak window] event in
            guard let window, event.keyCode == 53, window.isVisible,
                  window.isKeyWindow || NSApp.modalWindow === window else { return event }
            if let parent = window.sheetParent {
                parent.endSheet(window, returnCode: .cancel)
            } else {
                NSApp.stopModal(withCode: .cancel)
                window.orderOut(nil)
            }
            return nil
        }
        token = NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification, object: window, queue: .main
        ) { [weak self] _ in self?.stop() }
    }

    func stop() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        if let token { NotificationCenter.default.removeObserver(token) }
        token = nil
    }

    deinit { stop() }
}

private nonisolated(unsafe) var escapeCloserKey: UInt8 = 0

extension NSAlert {
    /// See `EscapeCloser`. Only needed where no Cancel button answers Escape; calling it twice is safe.
    @discardableResult
    func closeOnEscape() -> NSAlert {
        if objc_getAssociatedObject(self, &escapeCloserKey) == nil {
            objc_setAssociatedObject(
                self, &escapeCloserKey, EscapeCloser(window: window),
                .OBJC_ASSOCIATION_RETAIN_NONATOMIC
            )
        }
        return self
    }
}
