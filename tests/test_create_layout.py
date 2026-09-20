"""Offscreen geometry checks using the same isolated native API fixture."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CreateLayout(unittest.TestCase):
    def test_form_and_outputs_do_not_overlap_at_minimum_width(self):
        fixture = (ROOT / "tests/test_create_workspace.swift").read_text().split("@main")[0]
        harness = r'''
@main struct LayoutCheck {
    static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }
    static func main() {
        let suite = "gateway-layout-test-" + UUID().uuidString
        setenv("GATEWAY_NATIVE_DEFAULTS_SUITE", suite, 1)
        defer { UserDefaults(suiteName: suite)?.removePersistentDomain(forName: suite) }
        _ = NSApplication.shared
        assert(PageKit.artifactExtension("image/png") == "png")
        assert(PageKit.artifactExtension("audio/mpeg") == "mp3")
        assert(PageKit.artifactExtension("application/x-subrip; charset=utf-8") == "srt")
        assert(PageKit.artifactExtension("application/unknown") == "bin")
        for lane in CreateLanes.all {
            let workspace = CreateWorkspace(lane: lane)
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 659, height: 534),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            window.contentView = workspace
            workspace.refreshModels()
            workspace.layoutSubtreeIfNeeded()
            let generate = descendants(workspace).compactMap { $0 as? NSButton }
                .first { $0.title == "Generate" }!
            let split = descendants(workspace).compactMap { $0 as? NSStackView }.first {
                $0.arrangedSubviews.count == 2 &&
                descendants($0.arrangedSubviews[0]).contains { $0 === generate } &&
                descendants($0.arrangedSubviews[1]).compactMap { $0 as? NSTextField }
                    .contains { $0.stringValue == "Nothing generated yet" }
            }!
            let form = split.arrangedSubviews[0]
            let output = split.arrangedSubviews[1]
            assert(form.frame.height > 80, "collapsed form: " + lane.key)
            assert(output.frame.height > 80, "collapsed output: " + lane.key)
            assert(!form.frame.intersects(output.frame), "overlapping form/output: " + lane.key)
            assert(form.frame.width <= 659 && output.frame.width <= 659,
                   "content forced wider than minimum: " + lane.key)
        }
        print("PASS: six lanes fit minimum width without form/output overlap")
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="gateway-create-layout-") as directory:
            source = Path(directory) / "layout.swift"
            binary = Path(directory) / "layout"
            source.write_text(fixture + harness)
            compiled = subprocess.run(["swiftc", "-swift-version", "5", "-framework", "AppKit",
                "menubar/PageKit.swift", "menubar/CreateSection.swift", str(source), "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            checked = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
