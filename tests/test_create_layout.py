"""Offscreen geometry checks for the six Create pages: one shared layout, form beside a
result pane that fills the window, controls big enough to use, and a real preview per lane."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import AVKit

@main struct LayoutCheck {
    static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }
    static func labels(_ view: NSView) -> [String] {
        descendants(view).compactMap { ($0 as? NSTextField)?.stringValue }
    }
    static func spin(_ seconds: Double, until done: () -> Bool = { false }) {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end && !done() { RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02)) }
    }
    static func window(_ view: NSView, _ width: CGFloat, _ height: CGFloat) -> NSWindow {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = view
        return window
    }

    // Small real media, so the players have something to open.
    static func wav() -> Data {
        var d = Data()
        func u32(_ v: UInt32) { var v = v.littleEndian; d.append(Data(bytes: &v, count: 4)) }
        func u16(_ v: UInt16) { var v = v.littleEndian; d.append(Data(bytes: &v, count: 2)) }
        let samples = (0..<4410).map { Int16(8000 * sin(Double($0) / 8)) }
        d.append("RIFF".data(using: .ascii)!); u32(UInt32(36 + samples.count * 2)); d.append("WAVEfmt ".data(using: .ascii)!)
        u32(16); u16(1); u16(1); u32(22050); u32(44100); u16(2); u16(16)
        d.append("data".data(using: .ascii)!); u32(UInt32(samples.count * 2))
        samples.withUnsafeBufferPointer { d.append(Data(buffer: $0)) }
        return d
    }
    static func png() -> Data {
        let image = NSImage(size: NSSize(width: 64, height: 48))
        image.lockFocus(); NSColor.systemPink.setFill(); NSRect(x: 0, y: 0, width: 64, height: 48).fill(); image.unlockFocus()
        return NSBitmapImageRep(data: image.tiffRepresentation!)!.representation(using: .png, properties: [:])!
    }
    static let srt = "1\n00:00:00,000 --> 00:00:04,200\nFirst words\n\n2\n00:00:04,200 --> 00:00:09,000\nSecond words\n"

    static func main() {
        _ = NSApplication.shared
        assert(PageKit.artifactExtension("image/png") == "png")
        assert(PageKit.artifactExtension("audio/mpeg") == "mp3")
        assert(PageKit.artifactExtension("application/x-subrip; charset=utf-8") == "srt")
        assert(PageKit.artifactExtension("application/unknown") == "bin")
        assert(TranscriptText.clock("00:01:05,200") == "1:05")

        // ---- geometry: every lane, wide, medium and the app's real minimum detail width
        for lane in CreateLanes.all {
            for (width, height) in [(CGFloat(1600), CGFloat(1000)), (1100, 800), (660, 560)] {
                let tag = "\(lane.key) at \(Int(width))x\(Int(height)): "
                let workspace = CreateWorkspace(lane: lane)
                let host = window(workspace, width, height)
                workspace.refreshModels()
                workspace.layoutSubtreeIfNeeded()
                let all = descendants(workspace)
                let pages = all.compactMap { $0 as? CreatePageLayout }
                assert(pages.count == 1, tag + "not built on the shared CreatePageLayout")
                let page = pages[0]
                let form = page.formScroll.frame, preview = page.preview.frame
                assert(form.maxX <= preview.minX, tag + "form and preview overlap")
                assert(abs(form.maxY - preview.maxY) < 1, tag + "form and preview do not share a top")
                assert(form.width >= 310 && form.width <= 440, tag + "form column \(form.width) outside 310...440")
                assert(abs(preview.maxX - (host.contentView!.bounds.width - 28)) < 1.5, tag + "preview does not reach the right margin")
                assert(preview.width >= 240, tag + "preview too narrow: \(preview.width)")
                assert(form.height >= height - 200 && preview.height >= height - 200, tag + "columns do not fill the height")
                if width >= 1100 { assert(preview.width > form.width * 1.5, tag + "preview does not take the extra width") }
                assert(page.previewCard.frame.height >= 220, tag + "result card collapsed")
                assert(page.previewCard.frame.width == preview.width)
                assert(host.frame.width == width, tag + "the window had to grow to fit the page")
                // Something is in the result card before there is a result.
                assert(page.content != nil && labels(page).contains(lane.emptyTitle), tag + "no empty state in the result pane")

                // Controls big enough to use.
                let generate = all.compactMap { $0 as? NSButton }.first { $0.title == "Generate" }!
                assert(generate.frame.width >= 100 && generate.frame.height >= 24, tag + "Generate is small")
                let popup = all.compactMap { $0 as? NSPopUpButton }.first { $0.itemTitles.contains { $0.contains("—") } }!
                assert(popup.superview != nil && popup.frame.width >= 240, tag + "model picker is narrow: \(popup.frame.width)")
                for field in lane.fields where field.kind == .script {
                    let editor = all.compactMap { $0 as? NSTextView }.first!.enclosingScrollView!
                    assert(editor.frame.width >= 300 && editor.frame.height >= 150, tag + "script editor is small")
                }
                assert(page.form.frame.width <= page.formScroll.frame.width, tag + "the form is wider than its column")

                // Order of the form: model picker, then the lane's inputs, Generate last.
                func top(_ view: NSView) -> CGFloat { view.convert(view.bounds, to: nil).maxY }
                func bottom(_ view: NSView) -> CGFloat { view.convert(view.bounds, to: nil).minY }
                let formViews = descendants(page.formScroll).filter { !$0.isHiddenOrHasHiddenAncestor }
                let firstInput: NSView = lane.isUpload
                    ? formViews.compactMap { $0 as? NSButton }.first { $0.title == "Choose File…" }!
                    : formViews.compactMap { $0 as? NSTextView }.first!.enclosingScrollView!
                assert(top(popup) > top(firstInput), tag + "the model picker is not above the first input")
                assert(bottom(popup) > top(firstInput), tag + "the model picker overlaps the first input")
                assert(top(generate) < bottom(firstInput) + 1, tag + "Generate is above an input")
                for view in formViews where view !== generate && (view is NSControl || view is NSTextView)
                    && (view as? NSTextField)?.isEditable != false {  // labels (the status line) may follow Generate
                    let extent = (view as? NSTextView)?.enclosingScrollView ?? view
                    assert(bottom(generate) <= bottom(extent) + 1, tag + "Generate is not the last control: \(type(of: view))")
                }
            }
        }

        // ---- previews: the real page restores the job a same-key "producer" page made
        GatewayAPI.state = "succeeded"
        let expectations: [(String, String, [[String: Any]], Data?)] = [
            ("audio", "audio/wav", [[:]], wav()),
            ("music", "audio/wav", [[:], [:]], wav()),
            ("image", "image/png", [[:], [:], [:]], png()),
            ("video", "video/mp4", [[:]], Data(count: 64)),
            ("subtitle", "application/x-subrip", [[:]], srt.data(using: .utf8)!),
            ("chat", "", [], nil),
        ]
        for (key, media, slots, data) in expectations {
            let lane = CreateLanes.all.first { $0.key == key }!
            let tag = "\(key) preview: "
            GatewayAPI.statusJobID = "job-\(key)"
            GatewayAPI.nextJobID = "job-\(key)"
            GatewayAPI.downloadData = data
            GatewayAPI.outputText = "Reply for \(key)"
            GatewayAPI.outputs = slots.enumerated().map { index, _ in [
                "asset_id": "\(index)", "url": "/admin/api/artifacts/job-\(key)/outputs/\(index)",
                "media_type": media, "bytes": 10, "present": true, "kept": false] }
            let producer = CreateWorkspace(lane: CreateLane(
                key: key, title: lane.title, symbol: lane.symbol, modality: lane.modality, blurb: "", fields: []))
            producer.refreshModels()
            producer.perform(NSSelectorFromString("generate"))

            let workspace = CreateWorkspace(lane: lane)
            let host = window(workspace, 1600, 1000)
            workspace.refreshModels()
            workspace.restoreSelectedJob()
            let page = descendants(workspace).compactMap { $0 as? CreatePageLayout }.first!
            switch key {
            case "audio", "music":
                spin(3) { descendants(page.previewCard).contains { ($0 as? AudioPlayerView)?.canPlay == true } }
                assert(descendants(page.previewCard).contains { ($0 as? AudioPlayerView)?.canPlay == true }, tag + "no inline audio player")
                assert(descendants(page.previewCard).contains { $0 is WaveformView }, tag + "no waveform")
                if key == "music" {
                    assert(descendants(page.previewCard).contains { $0 is NSSegmentedControl }, tag + "two results but no way to pick")
                }
            case "image":
                spin(3) { descendants(page.previewCard).compactMap { $0 as? ImageGalleryView }.first?.shownImage != nil }
                let gallery = descendants(page.previewCard).compactMap { $0 as? ImageGalleryView }.first
                assert(gallery?.shownImage != nil, tag + "no large image")
                assert(gallery?.count == 3, tag + "thumbnails for every result")
                assert(descendants(page.previewCard).compactMap { $0 as? NSButton }.count == 3, tag + "thumbnail strip missing")
            case "video":
                spin(3) { descendants(page.previewCard).contains { $0 is VideoPreviewView } }
                assert(descendants(page.previewCard).contains { $0 is AVPlayerView }, tag + "no video player")
            case "subtitle":
                spin(3) { descendants(page.previewCard).contains { ($0 as? NSTextView)?.string.contains("Second words") == true } }
                let text = descendants(page.previewCard).compactMap { $0 as? NSTextView }.first?.string ?? ""
                assert(text.contains("0:04") && text.contains("Second words"), tag + "transcript lost its timestamps: \(text)")
            default:
                assert(descendants(page.previewCard).contains { $0 is ConversationView }, tag + "no conversation view")
                assert(labels(page).contains("Reply for chat"), tag + "the reply is not shown")
            }
            workspace.layoutSubtreeIfNeeded()
            let content = page.content!
            assert(content.frame.width >= page.previewCard.frame.width - 40, tag + "preview does not fill the card width")
            assert(content.frame.height >= page.previewCard.frame.height - 40, tag + "preview does not fill the card height")
            assert(host.frame.width == 1600)
        }
        print("PASS: six Create pages share one layout: form beside a result pane that fills the window, sized controls, and a real preview per lane")
    }
}
'''


class CreateLayout(unittest.TestCase):
    def test_form_beside_preview_and_real_previews(self):
        fixture = (ROOT / "tests/test_create_workspace.swift").read_text().split("@main")[0]
        with tempfile.TemporaryDirectory(prefix="gateway-create-layout-") as directory:
            source = Path(directory) / "layout.swift"
            binary = Path(directory) / "layout"
            source.write_text(fixture + HARNESS)
            compiled = subprocess.run(["swiftc", "-swift-version", "5", "-framework", "AppKit",
                "-framework", "AVFoundation", "-framework", "AVKit",
                "menubar/PageKit.swift", "menubar/CreatePreview.swift", "menubar/CreateSection.swift",
                str(source), "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            checked = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
