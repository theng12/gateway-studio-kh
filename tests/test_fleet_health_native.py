"""The Fleet page's Health, Last 7 days and Likely cause columns, and its Show filter — native.

`tests/test_fleet_history.py` pins what the Gateway records and derives; `tests/test_fleet_health_view.js`
pins the browser console. This pins the **native** half against the app's own decoders: the history and
causes decode from the JSON the Gateway really sends, "Problematic" is drawn as sent rather than
re-decided, an unmeasured Mac draws "—" and never "Stable", and the Show filter leaves the unknown out
of "problematic" as well as "offline".

It compiles the real `menubar/*.swift` sources with `tests/fleet_health_harness.swift` as `main`, so a
renamed field in `FleetModels.swift` fails here rather than on the owner's screen.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "needs macOS and Swift")
class FleetHealthNativeTests(unittest.TestCase):
    def test_the_history_verdict_cause_and_filter(self):
        evidence = os.environ.get("GATEWAY_TEST_EVIDENCE")
        work = Path(tempfile.mkdtemp(prefix="fleet-health-", dir=evidence))
        sources = work / "src"
        sources.mkdir()
        support = work / "support"
        support.mkdir()
        print(f"Retained native health fixtures: {work}", flush=True)
        for swift in (ROOT / "menubar").glob("*.swift"):
            shutil.copy(swift, sources / swift.name)
        subprocess.run([sys.executable, str(ROOT / "menubar/render/patch_sources.py"), str(sources)],
                       check=True)
        shutil.copy(ROOT / "tests/fleet_health_harness.swift", sources / "main.swift")
        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        command = ["xcrun", "swiftc", "-swift-version", "5", "-sdk", sdk,
                   "-target", f"{os.uname().machine}-apple-macos13.0",
                   "-framework", "Cocoa", "-framework", "WebKit", "-framework", "AVFoundation",
                   "-framework", "AVKit",
                   *map(str, sorted(sources.glob("*.swift"))), "-o", str(work / "check")]
        built = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        (work / "compile.log").write_text(built.stdout + built.stderr)
        self.assertEqual(built.returncode, 0, built.stderr[-4000:])
        checked = subprocess.run([str(work / "check")], cwd=ROOT, text=True, capture_output=True,
                                 timeout=120,
                                 env={**os.environ, "GATEWAY_APP_SUPPORT": str(support)})
        (work / "check.log").write_text(checked.stdout + checked.stderr)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr[-3000:])
        self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
