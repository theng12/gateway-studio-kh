"""Exercise the actual Fleet install report offscreen with retained synthetic state."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "needs macOS and Swift")
class FleetModelSpreadNativeTests(unittest.TestCase):
    def test_report_disclosure_preserves_progress_failures_records_and_geometry(self):
        evidence = os.environ.get("GATEWAY_TEST_EVIDENCE")
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="fleet-report-", dir=evidence))
        sources = work / "src"
        sources.mkdir()
        support = work / "support"
        support.mkdir()
        print(f"Retained native report fixtures: {work}", flush=True)
        for swift in (ROOT / "menubar").glob("*.swift"):
            shutil.copy(swift, sources / swift.name)
        subprocess.run([sys.executable, str(ROOT / "menubar/render/patch_sources.py"), str(sources)], check=True)
        shutil.copy(ROOT / "tests/fleet_model_spread_harness.swift", sources / "main.swift")
        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        command = ["xcrun", "swiftc", "-swift-version", "5", "-sdk", sdk,
                   "-target", f"{os.uname().machine}-apple-macos13.0",
                   "-framework", "Cocoa", "-framework", "WebKit", "-framework", "AVFoundation", "-framework", "AVKit",
                   *map(str, sorted(sources.glob("*.swift"))), "-o", str(work / "check")]
        built = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        (work / "compile.log").write_text(built.stdout + built.stderr)
        self.assertEqual(built.returncode, 0, built.stderr[-4000:])
        checked = subprocess.run([str(work / "check")], cwd=ROOT, text=True, capture_output=True, timeout=30,
                                 env={**os.environ, "GATEWAY_APP_SUPPORT": str(support)})
        (work / "check.log").write_text(checked.stdout + checked.stderr)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr[-3000:])
        self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
