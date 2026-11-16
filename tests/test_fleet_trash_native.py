"""The Fleet Trash view's three fields, native (Gateway 0.2.264).

The owner asked for an all-Macs Trash view: one row per Mac with the trash it holds, its free disk and
its free memory as separate columns, and one step to empty a Mac's Trash Folder. This pins the half a
window cannot: the three figures are read from the Mac's own trash answer (Studio Hub 0.6.243) and the
fleet row, each into its own column; a field a Mac does not report reads "not reported", never 0; a
boolean is not a number; and memory is never drawn as disk.

It compiles the real `menubar/*.swift` sources with `tests/fleet_trash_harness.swift` as `main`, so a
renamed field in the decoders fails here rather than on the owner's screen.

What is *not* here, and needs no test because it already exists: the delete route's authority. Owner
app-window secret only, count and bytes re-check with 409 `TRASH_CHANGED`, and an API key or a browser
session refused - `tests/test_remote_trash.py` pins that, through the same route this view uses with
`items: "all"`.
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
class FleetTrashNativeTests(unittest.TestCase):
    def test_the_three_fields_and_not_reported(self):
        evidence = os.environ.get("GATEWAY_TEST_EVIDENCE")
        work = Path(tempfile.mkdtemp(prefix="fleet-trash-", dir=evidence))
        sources = work / "src"
        sources.mkdir()
        support = work / "support"
        support.mkdir()
        print(f"Retained native trash fixtures: {work}", flush=True)
        for swift in (ROOT / "menubar").glob("*.swift"):
            shutil.copy(swift, sources / swift.name)
        subprocess.run([sys.executable, str(ROOT / "menubar/render/patch_sources.py"), str(sources)],
                       check=True)
        shutil.copy(ROOT / "tests/fleet_trash_harness.swift", sources / "main.swift")
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
