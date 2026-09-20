"""Focused native checks for the Jobs outputs library."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class JobsOutputsNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="gateway-jobs-outputs-")
        cls.binary = Path(cls.temp.name) / "jobs-outputs-check"
        result = subprocess.run(
            [
                "swiftc",
                "-swift-version",
                "5",
                "-framework",
                "AppKit",
                "menubar/PageKit.swift",
                "menubar/JobsWindow.swift",
                "tests/test_jobs_outputs.swift",
                "-o",
                str(cls.binary),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_native_output_library_fixture(self) -> None:
        result = subprocess.run(
            [str(self.binary)],
            cwd=ROOT,
            env={**os.environ, "NATIVE_TEST_DEFAULTS_SUITE": "gateway-jobs-output-test"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("PASS:", result.stdout)

    def test_output_library_uses_authenticated_local_file_flow(self) -> None:
        source = (ROOT / "menubar" / "JobsWindow.swift").read_text()
        self.assertIn("GatewayAPI.download(", source)
        self.assertIn('"/admin/api/artifacts/keep"', source)
        self.assertIn('"/admin/api/artifacts/kept"', source)
        self.assertIn("NSWorkspace.shared.open", source)
        self.assertIn(".atomic", source)
        self.assertNotIn("Data(contentsOf:", source)
        self.assertNotIn("URL(string: output", source)
        self.assertIn("selectionGeneration", source)
        for field in (
            "cancel_requested",
            "cancel_state",
            "retryable",
            "recovery_required",
            "recovery_state",
            "recovery_detail",
            "upstream_job_id",
            "late_result_state",
        ):
            self.assertIn(field, source)


if __name__ == "__main__":
    unittest.main()
