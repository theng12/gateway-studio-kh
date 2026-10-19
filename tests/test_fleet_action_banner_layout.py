"""Render Fleet action notices offscreen, without the owner's app or gateway.

The artifacts are retained in dist/ so a failed layout can be inspected rather
than removed automatically. Set FLEET_BANNER_ARTIFACTS to choose their location.
"""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_fleet_layout import FIXTURE

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import AppKit

enum GatewayAPI {
    static func relativeTime(_ epoch: Double?) -> String { epoch == nil ? "—" : "just now" }
}

final class BannerHost: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSColor.windowBackgroundColor.setFill()
        dirtyRect.fill()
    }
}

@main struct BannerLayoutCheck {
    static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }

    static func settle(_ view: NSView) {
        for _ in 0..<5 {
            descendants(view).forEach { $0.needsLayout = true }
            view.layoutSubtreeIfNeeded()
        }
    }

    static func capture(_ view: NSView, to url: URL) {
        let rendered = NSImage(data: view.dataWithPDF(inside: view.bounds))!
        let image = NSImage(size: view.bounds.size)
        image.lockFocus()
        view.effectiveAppearance.performAsCurrentDrawingAppearance {
            NSColor.windowBackgroundColor.setFill()
            NSRect(origin: .zero, size: image.size).fill()
            // AppKit's PDF snapshot preserves text but omits CALayer fills.
            // Paint the production layers first, avoiding cacheDisplay's
            // offscreen alpha artifacts without changing any production tint.
            for child in descendants(view) {
                guard let layer = child.layer else { continue }
                var rect = child.convert(child.bounds, to: view)
                if view.isFlipped { rect.origin.y = image.size.height - rect.maxY }
                let path = NSBezierPath(roundedRect: rect, xRadius: layer.cornerRadius, yRadius: layer.cornerRadius)
                if let background = layer.backgroundColor, let color = NSColor(cgColor: background) {
                    color.setFill(); path.fill()
                }
                if layer.borderWidth > 0, let border = layer.borderColor, let color = NSColor(cgColor: border) {
                    color.setStroke(); path.lineWidth = layer.borderWidth; path.stroke()
                }
            }
            rendered.draw(in: NSRect(origin: .zero, size: image.size), from: .zero, operation: .sourceOver, fraction: 1)
        }
        image.unlockFocus()
        let opaque = NSBitmapImageRep(data: image.tiffRepresentation!)!
        try! opaque.representation(using: .png, properties: [:])!.write(to: url)
    }

    static func main() {
        setbuf(stdout, nil)
        let app = NSApplication.shared
        app.appearance = NSAppearance(named: .darkAqua)
        let directory = URL(fileURLWithPath: CommandLine.arguments[1])
        let baseline = ProcessInfo.processInfo.environment["FLEET_BANNER_BASELINE"] == "1"
        let realDetail = "Reachability and models were re-read. Discovered models arrive disabled — enable the ones you want on the Models page."
        let longDetail = (1...12).map { "Controller \($0) refused this release. Install the trusted publisher key, then try again." }.joined(separator: "\n")
        for appearance in [NSAppearance.Name.darkAqua, .aqua] {
        app.appearance = NSAppearance(named: appearance)
        for width: CGFloat in [320, 640, 1643] {
            let size = NSSize(width: width, height: 640)
            let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            window.appearance = NSAppearance(named: appearance)
            let initialWindowSize = window.frame.size
            let host = BannerHost(frame: NSRect(origin: .zero, size: size))
            window.contentView = host
            let banner = FleetBanner(frame: .zero)
            let stack = NSStackView(views: [banner, NSTextField(labelWithString: "Fleet content below")])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = DS.Space.m
            stack.translatesAutoresizingMaskIntoConstraints = false
            host.addSubview(stack)
            banner.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            NSLayoutConstraint.activate([
                stack.topAnchor.constraint(equalTo: host.topAnchor),
                stack.leadingAnchor.constraint(equalTo: host.leadingAnchor),
                stack.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            ])
            settle(host)
            assert(banner.isHidden, "a new banner should be hidden")
            var longHeight: CGFloat = 0
            for (name, kind, title, detail) in [
                ("success", FleetBanner.Kind.success, "Refreshed 8 locations", realDetail),
                ("warning", .warning, "Some locations did not answer", realDetail),
                ("error", .error, "Could not publish Studio Hub", longDetail),
                ("title-only", .warning, "Saved", ""),
                ("long-title", .error, String(repeating: "A location with a long name ", count: 12), "Check its credentials."),
                ("short-update", .success, "Saved", "Ready."),
            ] {
                banner.show(kind, title: title, detail: detail)
                settle(host)
                assert(!banner.isHidden)
                let labels = descendants(banner).compactMap { $0 as? NSTextField }.filter { !$0.isHidden }
                let frames = labels.map { $0.alignmentRect(forFrame: $0.convert($0.bounds, to: banner)) }
                let bottom = frames.map(\.minY).min()!
                let top = banner.bounds.height - frames.map(\.maxY).max()!
                let textLeft = frames.map(\.minX).min()!
                let label = labels.last!
                let idealHeight = label.cell!.cellSize(forBounds: NSRect(x: 0, y: 0, width: label.bounds.width, height: 1000)).height
                print("\(name) width=\(width) banner=\(banner.frame) text=\(frames) top=\(top) bottom=\(bottom) wanted=\(idealHeight)")
                if !baseline {
                    assert(bottom >= DS.Space.m - 0.5, "\(name): bottom padding is only \(bottom)pt")
                    assert(top >= DS.Space.m - 0.5, "\(name): top padding is only \(top)pt")
                    assert(textLeft >= DS.Space.l + 18 + DS.Space.s - 0.5)
                    assert(frames.allSatisfy { $0.maxX <= banner.bounds.maxX - DS.Space.l + 0.5 }, "text overflows the card")
                    assert(label.bounds.height >= idealHeight - 0.5, "wrapping text is clipped")
                    assert(banner.bounds.height <= 140, "six-line detail cap was lost")
                    assert(window.contentView!.bounds.size == size, "banner resized its window")
                    assert(window.frame.size == initialWindowSize, "banner grew its window")
                    assert(banner.frame.width == width, "banner overflowed the page width")
                    if name == "short-update" { assert(banner.bounds.height < longHeight, "old long height survives a short update") }
                }
                if name == "error" { longHeight = banner.bounds.height }
                host.display()
                if name == "success" || name == "warning" || name == "error" {
                    capture(banner, to: directory.appendingPathComponent("\(appearance.rawValue)-\(name)-\(Int(width)).png"))
                }
            }
            banner.show(.success, title: "Saved", detail: "Ready.")
            let successTimer = Mirror(reflecting: banner).children.first { $0.label == "hideTimer" }!
            let timer = Mirror(reflecting: successTimer.value).children.first!.value as! Timer
            assert(timer.isValid)
            banner.show(.warning, title: "Still needs attention", detail: "Check the location.")
            assert(!timer.isValid, "a later warning did not cancel the success timer")
            assert(!banner.isHidden, "warnings must stay visible")
            banner.show(.success, title: "Saved", detail: "Ready.")
            let finalTimer = Mirror(reflecting: Mirror(reflecting: banner).children.first { $0.label == "hideTimer" }!.value).children.first!.value as! Timer
            finalTimer.fire()
            assert(banner.isHidden, "success timer did not clear the banner")
            banner.show(.idle, title: "", detail: "")
            assert(banner.isHidden)
            banner.show(.error, title: "", detail: "")
            assert(banner.isHidden)
            window.close()
        }
        // The neighboring attention card and grouped warning list already use
        // explicit insets. Render those real views in the same batch for review.
        let payload = try! JSONSerialization.jsonObject(with: Data(contentsOf: directory.appendingPathComponent("fleet.json"))) as! [String: Any]
        let snapshot = FleetSnapshot.from(payload)!
        let groups = FleetGroupsTab(frame: .zero)
        groups.update(snapshot: snapshot)
        let warnings = FleetWarningList()
        warnings.set(warnings: [FleetWarning(id: "offline", kind: "offline", health: .warn,
            text: "2 nodes did not answer.", fix: "Check the Macs and their connection, then refresh the Fleet page.")])
        for (name, content) in [("groups-attention", groups as NSView), ("grouped-warning", warnings as NSView)] {
            let size = NSSize(width: 900, height: 620)
            let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
            window.appearance = NSAppearance(named: appearance)
            let host = BannerHost(frame: NSRect(origin: .zero, size: size))
            window.contentView = host
            content.translatesAutoresizingMaskIntoConstraints = false
            host.addSubview(content)
            NSLayoutConstraint.activate([
                content.topAnchor.constraint(equalTo: host.topAnchor, constant: DS.Space.l),
                content.leadingAnchor.constraint(equalTo: host.leadingAnchor, constant: DS.Space.l),
                content.trailingAnchor.constraint(equalTo: host.trailingAnchor, constant: -DS.Space.l),
            ])
            if name == "groups-attention" { content.bottomAnchor.constraint(equalTo: host.bottomAnchor, constant: -DS.Space.l).isActive = true }
            settle(host)
            if let clamp = warnings.subviews.first as? FleetClampedScroll, name == "grouped-warning" {
                let frame = clamp.convert(clamp.bounds, to: warnings)
                assert(frame.minY >= DS.Space.s - 0.5 && warnings.bounds.height - frame.maxY >= DS.Space.s - 0.5,
                       "grouped warning lost its existing vertical insets")
            }
            host.display()
            capture(content, to: directory.appendingPathComponent("\(appearance.rawValue)-\(name).png"))
            window.close()
        }
        }
        print("PASS: Fleet action banners preserve wrapping, balanced insets, bounded height and lifecycle")
    }
}
'''


class FleetActionBannerLayout(unittest.TestCase):
    def test_action_notices_keep_padding_after_wrapping(self):
        base = Path(os.environ.get("FLEET_BANNER_ARTIFACTS", ROOT / "dist"))
        base.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="fleet-action-banner-", dir=base))
        source = directory / "banner.swift"
        binary = directory / "banner"
        (directory / "fleet.json").write_text(json.dumps(FIXTURE))
        # Compile the production class, not a copied implementation. FleetSection
        # itself depends on the app controller and is deliberately excluded.
        banner = (ROOT / "menubar/FleetSection.swift").read_text().split("final class FleetBanner: NSView {", 1)[1]
        source.write_text("import AppKit\nfinal class FleetBanner: NSView {" + banner + HARNESS)
        compiled = subprocess.run(
            ["swiftc", "-swift-version", "5", "-framework", "AppKit",
             "menubar/PageKit.swift", "menubar/FleetModels.swift", "menubar/FleetStartupModel.swift",
             "menubar/FleetLastStop.swift", "menubar/FleetWorkstation.swift", "menubar/FleetMacTrashModel.swift",
             "menubar/FleetTabs.swift",
             "menubar/FleetGroups.swift", "menubar/SecretField.swift", "menubar/FleetBanner.swift",
             str(source), "-o", str(binary)],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        checked = subprocess.run([str(binary), str(directory)], capture_output=True, text=True, timeout=30)
        (directory / "measurements.log").write_text(checked.stdout + checked.stderr)
        print(f"Retained Fleet banner artifacts: {directory}\n{checked.stdout}")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
