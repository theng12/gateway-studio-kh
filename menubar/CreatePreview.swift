import AppKit
import AVFoundation
import AVKit

// What the Create pages show in their result card, one view per kind of output. They are plain
// views with no knowledge of the gateway: the page downloads the bytes and hands them over.

/// The loudness envelope of an audio file, with a playhead. Click or drag to seek.
final class WaveformView: NSView {
    /// 0...1 per bucket. Empty until the file has been read.
    var peaks: [Float] = [] { didSet { needsDisplay = true } }
    var progress: CGFloat = 0 { didSet { needsDisplay = true } }
    var onSeek: ((CGFloat) -> Void)?

    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: 140) }

    /// Peak per bucket, read in chunks so a long narration never sits in memory whole.
    static func peaks(of url: URL, buckets: Int = 240, done: @escaping ([Float]) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            var result = [Float](repeating: 0, count: buckets)
            if let file = try? AVAudioFile(forReading: url), file.length > 0,
               let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 32768) {
                let total = Double(file.length)
                var position = 0.0
                while file.framePosition < file.length {
                    guard (try? file.read(into: buffer)) != nil, buffer.frameLength > 0,
                          let samples = buffer.floatChannelData?[0] else { break }
                    for frame in 0..<Int(buffer.frameLength) {
                        let bucket = min(buckets - 1, Int((position + Double(frame)) / total * Double(buckets)))
                        result[bucket] = max(result[bucket], abs(samples[frame]))
                    }
                    position += Double(buffer.frameLength)
                }
            }
            let top = max(result.max() ?? 0, 0.0001)
            DispatchQueue.main.async { done(result.map { $0 / top }) }
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        let bars = max(1, Int(bounds.width / 5))
        let source = peaks.isEmpty ? [Float](repeating: 0.05, count: bars) : peaks
        for bar in 0..<bars {
            let lo = bar * source.count / bars
            let hi = max(lo + 1, (bar + 1) * source.count / bars)
            let level = CGFloat(source[lo..<min(hi, source.count)].max() ?? 0)
            let height = max(3, level * bounds.height)
            let played = CGFloat(bar) / CGFloat(bars) < progress
            (played ? NSColor.controlAccentColor : NSColor.tertiaryLabelColor).setFill()
            NSBezierPath(roundedRect: NSRect(x: CGFloat(bar) * bounds.width / CGFloat(bars) + 0.5,
                                             y: (bounds.height - height) / 2,
                                             width: max(2, bounds.width / CGFloat(bars) - 2), height: height),
                         xRadius: 1.5, yRadius: 1.5).fill()
        }
    }

    private func seek(_ event: NSEvent) {
        let x = convert(event.locationInWindow, from: nil).x
        onSeek?(min(1, max(0, x / max(bounds.width, 1))))
    }
    override func mouseDown(with event: NSEvent) { seek(event) }
    override func mouseDragged(with event: NSEvent) { seek(event) }
}

/// An inline audio player: waveform, play/pause, elapsed / total.
final class AudioPlayerView: NSView, PreviewStoppable {
    private var player: AVAudioPlayer?
    private let wave = WaveformView()
    private let playButton = NSButton()
    private let time = PageKit.label("0:00 / 0:00", .body, colour: .secondaryLabelColor)
    private var timer: Timer?
    /// False when the file could not be opened; the page says so and offers Open / Save instead.
    var canPlay: Bool { player != nil }

    init(url: URL) {
        super.init(frame: .zero)
        player = try? AVAudioPlayer(contentsOf: url)
        player?.prepareToPlay()

        playButton.bezelStyle = .circular
        playButton.controlSize = .large
        playButton.target = self
        playButton.action = #selector(toggle)
        playButton.widthAnchor.constraint(equalToConstant: 44).isActive = true
        playButton.heightAnchor.constraint(equalToConstant: 44).isActive = true
        playButton.isEnabled = player != nil
        updateButton()
        wave.onSeek = { [weak self] fraction in
            guard let player = self?.player else { return }
            player.currentTime = Double(fraction) * player.duration
            self?.tick()
        }
        time.stringValue = "0:00 / \(Self.clock(player?.duration ?? 0))"
        let row = PageKit.row([playButton, time], spacing: DS.Space.m)
        let stack = NSStackView(views: [wave, row])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.l
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        wave.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            stack.topAnchor.constraint(greaterThanOrEqualTo: topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: bottomAnchor),
        ])
        WaveformView.peaks(of: url) { [weak self] in self?.wave.peaks = $0 }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    static func clock(_ seconds: TimeInterval) -> String {
        let whole = Int(seconds.isFinite ? seconds : 0)
        return String(format: "%d:%02d", whole / 60, whole % 60)
    }

    private func updateButton() {
        let name = player?.isPlaying == true ? "pause.fill" : "play.fill"
        playButton.image = NSImage(systemSymbolName: name, accessibilityDescription: name == "play.fill" ? "Play" : "Pause")
    }

    @objc private func toggle() {
        guard let player else { return }
        if player.isPlaying {
            player.pause()
            timer?.invalidate()
        } else {
            if player.currentTime >= player.duration { player.currentTime = 0 }
            player.play()
            timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in self?.tick() }
        }
        updateButton()
    }

    private func tick() {
        guard let player else { return }
        wave.progress = player.duration > 0 ? CGFloat(player.currentTime / player.duration) : 0
        time.stringValue = "\(Self.clock(player.currentTime)) / \(Self.clock(player.duration))"
        if !player.isPlaying && timer != nil {
            timer?.invalidate()
            timer = nil
            updateButton()
        }
    }

    func stop() {
        player?.stop()
        timer?.invalidate()
        timer = nil
    }

    override func viewDidMoveToWindow() {
        if window == nil { stop() }
    }

    deinit { timer?.invalidate() }
}

/// An inline video player with the system transport.
final class VideoPreviewView: NSView, PreviewStoppable {
    let playerView = AVPlayerView()

    init(url: URL) {
        super.init(frame: .zero)
        playerView.player = AVPlayer(url: url)
        playerView.controlsStyle = .inline
        playerView.translatesAutoresizingMaskIntoConstraints = false
        addSubview(playerView)
        NSLayoutConstraint.activate([
            playerView.topAnchor.constraint(equalTo: topAnchor),
            playerView.leadingAnchor.constraint(equalTo: leadingAnchor),
            playerView.trailingAnchor.constraint(equalTo: trailingAnchor),
            playerView.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func stop() { playerView.player?.pause() }
    override func viewDidMoveToWindow() { if window == nil { stop() } }
}

/// One large image, and a strip of thumbnails when the model returned several.
final class ImageGalleryView: NSView {
    private let main = NSImageView()
    private let loading = PageKit.label("Loading image…", .body, colour: .secondaryLabelColor)
    private var images: [NSImage?]
    private var thumbs: [NSButton] = []
    private(set) var selected = 0
    var onSelect: ((Int) -> Void)?
    var count: Int { images.count }
    var shownImage: NSImage? { main.image }

    init(count: Int) {
        images = Array(repeating: nil, count: count)
        super.init(frame: .zero)
        main.imageScaling = .scaleProportionallyUpOrDown
        main.imageAlignment = .alignCenter
        main.setContentCompressionResistancePriority(NSLayoutConstraint.Priority(1), for: .horizontal)
        main.setContentCompressionResistancePriority(NSLayoutConstraint.Priority(1), for: .vertical)
        main.setContentHuggingPriority(NSLayoutConstraint.Priority(1), for: .vertical)
        main.translatesAutoresizingMaskIntoConstraints = false
        loading.translatesAutoresizingMaskIntoConstraints = false
        addSubview(main)
        addSubview(loading)
        var constraints = [
            main.topAnchor.constraint(equalTo: topAnchor),
            main.leadingAnchor.constraint(equalTo: leadingAnchor),
            main.trailingAnchor.constraint(equalTo: trailingAnchor),
            loading.centerXAnchor.constraint(equalTo: main.centerXAnchor),
            loading.centerYAnchor.constraint(equalTo: main.centerYAnchor),
        ]
        if count > 1 {
            let strip = NSStackView()
            strip.orientation = .horizontal
            strip.spacing = DS.Space.s
            strip.translatesAutoresizingMaskIntoConstraints = false
            for index in 0..<count {
                let thumb = NSButton()
                thumb.isBordered = false
                thumb.imageScaling = .scaleProportionallyUpOrDown
                thumb.wantsLayer = true
                thumb.layer?.cornerRadius = DS.Radius.control
                thumb.tag = index
                thumb.target = self
                thumb.action = #selector(pick(_:))
                thumb.setAccessibilityLabel("Result \(index + 1)")
                thumb.widthAnchor.constraint(equalToConstant: 84).isActive = true
                thumb.heightAnchor.constraint(equalToConstant: 64).isActive = true
                thumbs.append(thumb)
                strip.addArrangedSubview(thumb)
            }
            addSubview(strip)
            constraints += [
                strip.leadingAnchor.constraint(equalTo: leadingAnchor),
                strip.bottomAnchor.constraint(equalTo: bottomAnchor),
                strip.topAnchor.constraint(equalTo: main.bottomAnchor, constant: DS.Space.m),
            ]
        } else {
            constraints.append(main.bottomAnchor.constraint(equalTo: bottomAnchor))
        }
        NSLayoutConstraint.activate(constraints)
        highlight()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func setImage(_ image: NSImage?, at index: Int) {
        guard index >= 0, index < images.count else { return }
        images[index] = image
        if index < thumbs.count { thumbs[index].image = image }
        if index == selected { show() }
    }

    func select(_ index: Int) {
        guard index >= 0, index < images.count else { return }
        selected = index
        show()
        highlight()
        onSelect?(index)
    }

    @objc private func pick(_ sender: NSButton) { select(sender.tag) }

    private func show() {
        main.image = images[selected]
        loading.isHidden = images[selected] != nil
    }

    private func highlight() {
        for (index, thumb) in thumbs.enumerated() {
            thumb.layer?.borderWidth = index == selected ? 2 : 0
            thumb.layer?.borderColor = NSColor.controlAccentColor.cgColor
        }
    }
}

/// A transcript: SubRip becomes a timestamp gutter and the words, everything else is shown as is.
enum TranscriptText {
    static func attributed(_ raw: String) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let style = NSMutableParagraphStyle()
        style.tabStops = [NSTextTab(textAlignment: .left, location: 64)]
        style.defaultTabInterval = 64
        style.headIndent = 64
        style.paragraphSpacing = 12
        style.lineSpacing = 3
        let body: [NSAttributedString.Key: Any] = [
            .font: TextRole.body.font, .foregroundColor: NSColor.labelColor, .paragraphStyle: style]
        let stamp: [NSAttributedString.Key: Any] = [
            .font: TextRole.secondary.font,
            .foregroundColor: NSColor.secondaryLabelColor, .paragraphStyle: style]
        let text = raw.replacingOccurrences(of: "\r\n", with: "\n")
        for block in text.components(separatedBy: "\n\n") {
            var lines = block.split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
            if lines.isEmpty { continue }
            var start = ""
            if let arrow = lines.firstIndex(where: { $0.contains("-->") }), arrow <= 1 {
                start = clock(String(lines[arrow].components(separatedBy: "-->")[0]))
                lines.removeSubrange(0...arrow)
            }
            if start.isEmpty {
                out.append(NSAttributedString(string: lines.joined(separator: " ") + "\n", attributes: body))
            } else {
                out.append(NSAttributedString(string: start + "\t", attributes: stamp))
                out.append(NSAttributedString(string: lines.joined(separator: " ") + "\n", attributes: body))
            }
        }
        return out
    }

    /// `00:01:05,200` → `1:05`.
    static func clock(_ stamp: String) -> String {
        let parts = stamp.trimmingCharacters(in: .whitespaces).split(whereSeparator: { $0 == ":" || $0 == "," || $0 == "." })
        guard parts.count >= 3, let h = Int(parts[0]), let m = Int(parts[1]), let s = Int(parts[2]) else { return "" }
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }

    static func view(_ raw: String) -> NSScrollView {
        let text = NSTextView()
        text.isEditable = false
        text.isSelectable = true
        text.drawsBackground = false
        text.textContainerInset = NSSize(width: 4, height: 4)
        text.isVerticallyResizable = true
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = true
        text.textStorage?.setAttributedString(attributed(raw))
        let scroll = NSScrollView()
        scroll.documentView = text
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        return scroll
    }
}

/// One turn of a chat.
struct ChatTurn {
    var role: String
    var text: String
    var failed = false
}

/// The conversation: a card per turn, You and Model, newest at the bottom.
final class ConversationView: NSScrollView {
    let stack = NSStackView()

    init(turns: [ChatTurn], footer: NSView? = nil) {
        super.init(frame: .zero)
        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.m
        stack.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(stack)
        documentView = document
        hasVerticalScroller = true
        drawsBackground = false
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: document.topAnchor),
            stack.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: document.bottomAnchor),
            document.widthAnchor.constraint(equalTo: contentView.widthAnchor),
        ])
        for turn in turns { add(bubble(turn)) }
        if let footer { add(footer) }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func add(_ view: NSView) {
        stack.addArrangedSubview(view)
        view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    private func bubble(_ turn: ChatTurn) -> NSView {
        let mine = turn.role == "user"
        let who = PageKit.label(mine ? "You" : "Model", .rowTitle, colour: .secondaryLabelColor)
        let words = PageKit.label(turn.text, .body, colour: turn.failed ? Health.bad.text : .labelColor, wrapping: true)
        words.preferredMaxLayoutWidth = 0
        let inner = NSStackView(views: [who, words])
        inner.orientation = .vertical
        inner.alignment = .leading
        inner.spacing = DS.Space.xs
        inner.translatesAutoresizingMaskIntoConstraints = false
        words.widthAnchor.constraint(equalTo: inner.widthAnchor).isActive = true
        let box = BubbleView(highlighted: mine)
        box.addSubview(inner)
        NSLayoutConstraint.activate([
            inner.topAnchor.constraint(equalTo: box.topAnchor, constant: DS.Space.s),
            inner.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: DS.Space.m),
            inner.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -12),
            inner.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -10),
        ])
        return box
    }

    func scrollToEnd() {
        layoutSubtreeIfNeeded()
        guard let document = documentView else { return }
        contentView.scroll(to: NSPoint(x: 0, y: max(0, document.frame.height - contentView.bounds.height)))
        reflectScrolledClipView(contentView)
    }
}

private final class BubbleView: NSView {
    let highlighted: Bool
    init(highlighted: Bool) {
        self.highlighted = highlighted
        super.init(frame: .zero)
        wantsLayer = true
    }
    required init?(coder: NSCoder) { fatalError("not used") }
    override var wantsUpdateLayer: Bool { true }
    override func updateLayer() {
        layer?.cornerRadius = DS.Radius.card
        layer?.backgroundColor = (highlighted
            ? NSColor.controlAccentColor.withAlphaComponent(0.14)
            : NSColor.labelColor.withAlphaComponent(0.06)).cgColor
    }
}
