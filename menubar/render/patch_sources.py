"""Open the few private seams the render harness needs, in a throwaway copy of the app sources.

    patch_sources.py <work-dir>

Nothing here is in the shipped app. Each replacement asserts the line it expects, so a refactor that moves
one fails loudly here instead of producing a render that silently shows the wrong thing.
"""
import pathlib
import sys

work = pathlib.Path(sys.argv[1])


def patch(name: str, old: str, new: str) -> None:
    path = work / name
    text = path.read_text()
    assert old in text, f"{name}: `{old}` moved; update patch_sources.py"
    path.write_text(text.replace(old, new, 1))


# The app's own entry point (CLI flags, single-instance guard, NSApplication.run) cannot sit beside the harness's.
main = work / "main.swift"
text = main.read_text()
marker = "// MARK: - Entry point"
assert marker in text, "main.swift: the entry-point comment moved; update patch_sources.py"
(work / "AppCore.swift").write_text(text[:text.index(marker)])
# render-page.sh puts the harness entry point in main.swift, replacing this copy.

patch("AppCore.swift",
      "private(set) var state: GatewayState = .stopped", "var state: GatewayState = .stopped")
patch("MainWindow.swift", "    private var window: NSWindow?", "    var window: NSWindow?")
patch("MainWindow.swift", "    private var sections: [AppSection] = []", "    var sections: [AppSection] = []")
patch("MainWindow.swift", "        window.makeKeyAndOrderFront(nil)\n        NSApp.activate(ignoringOtherApps: true)\n",
      "")
patch("MainWindow.swift", "    private lazy var webView: WKWebView = {", "    lazy var webView: WKWebView = {")
patch("MainWindow.swift", "    private var container: NSView!", "    var container: NSView!")
