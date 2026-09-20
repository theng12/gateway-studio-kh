"""Run the bounded native Create workspace check in a clean process."""

import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class Task5NativeTests(unittest.TestCase):
    def test_create_workspace_without_external_defaults_suite(self):
        with tempfile.TemporaryDirectory(prefix="gateway-task5-") as temp:
            binary = pathlib.Path(temp) / "create-workspace-check"
            command = [
                "swiftc",
                "-swift-version",
                "5",
                "-framework",
                "AppKit",
                str(ROOT / "menubar/PageKit.swift"),
                str(ROOT / "menubar/CreateSection.swift"),
                str(ROOT / "tests/test_create_workspace.swift"),
                "-o",
                str(binary),
            ]
            compile_result = subprocess.run(
                command, cwd=ROOT, text=True, capture_output=True, check=False
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                compile_result.stdout + compile_result.stderr,
            )

            environment = os.environ.copy()
            environment.pop("GATEWAY_NATIVE_DEFAULTS_SUITE", None)
            run_result = subprocess.run(
                [str(binary)],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(
                run_result.returncode,
                0,
                run_result.stdout + run_result.stderr,
            )
            self.assertIn("PASS:", run_result.stdout)


if __name__ == "__main__":
    unittest.main()
