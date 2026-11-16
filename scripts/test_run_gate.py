#!/usr/bin/env python3
"""Self-test for scripts/run_gate.py, the tracked Gateway release gate.

Pins the three rules that the untracked shell gates broke: a file that collects no tests is a
failure, the two owner-facing suites are skipped by name with a reason, and a serial file never
overlaps another file. Runs in a couple of seconds against synthetic fixtures; it never touches
the real tests. `python scripts/test_run_gate.py`
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import run_gate


UNITTEST_PASS = '''
import unittest

class Passes(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertTrue(True)
'''

ZERO_TESTS = '''
import unittest

def helper():
    return 1
'''

FAILING = '''
import unittest

class Fails(unittest.TestCase):
    def test_boom(self):
        self.fail("deliberate")
'''

SCRIPT = '''
print("All 3 end-to-end checks passed.")
'''

PROBE = '''
import os, time, unittest, pathlib

class Probe(unittest.TestCase):
    def test_sleep(self):
        path = os.environ["GATE_PROBE"]
        assert os.environ["KHU_HOLD_TOKEN_FILE"] == str(pathlib.Path(os.environ["GATEWAY_APP_SUPPORT"]) / "launcher-hold-token")
        assert not pathlib.Path(os.environ["KHU_HOLD_TOKEN_FILE"]).exists()
        with open(path, "a") as handle:
            handle.write(f"start {__file__} {time.time()}\\n")
        time.sleep(0.4)
        with open(path, "a") as handle:
            handle.write(f"end {__file__} {time.time()}\\n")
'''


def build(root: Path, files: dict[str, str]) -> None:
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "_isolation.py").write_text('"""fixture"""\n')
    for name, source in files.items():
        (tests / name).write_text(source)


class RunGateSelfTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="run-gate-selftest-")
        self.root = Path(self._tmp.name) / "checkout"
        self.evidence = Path(self._tmp.name) / "evidence"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def gate(self, files: list[str], workers: int = 1, serial: dict[str, str] | None = None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            summary = run_gate.run_gate(
                self.root, [self.root / "tests" / name for name in files],
                workers, self.evidence, serial=serial or {},
            )
        return summary, out.getvalue()

    def test_zero_tests_is_a_failure(self) -> None:
        build(self.root, {"test_zero.py": ZERO_TESTS})
        summary, output = self.gate(["test_zero.py"])
        self.assertEqual(summary["failed"], 1, output)
        self.assertIn("collected no tests", output)
        self.assertFalse(json.loads((self.evidence / "summary.json").read_text())["results"][0]["passed"])

    def test_counts_and_summary(self) -> None:
        build(self.root, {"test_pass.py": UNITTEST_PASS, "test_fail.py": FAILING, "test_script.py": SCRIPT})
        self.addCleanup(setattr, run_gate, "SCRIPT_STYLE", run_gate.SCRIPT_STYLE)
        run_gate.SCRIPT_STYLE = run_gate.SCRIPT_STYLE | {"test_script.py"}
        summary, output = self.gate(["test_pass.py", "test_fail.py", "test_script.py"])
        self.assertEqual((summary["files_run"], summary["passed"], summary["failed"]), (3, 2, 1), output)
        self.assertEqual(summary["unittest_tests"], 3)  # 2 from test_pass, 1 from test_fail
        self.assertEqual(summary["e2e_checks"], 3)      # the script's own line is counted, not assumed
        self.assertIn("FAIL test_fail", output)

    def test_skip_lines_carry_their_reason(self) -> None:
        build(self.root, {"test_pass.py": UNITTEST_PASS, "test_ui.py": "", "test_fleet_standalone.py": ""})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_gate.main(["--root", str(self.root), "--evidence", str(self.evidence), "--workers", "1"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("SKIP test_ui", out.getvalue())
        self.assertIn("SKIP test_fleet_standalone", out.getvalue())
        summary = json.loads((self.evidence / "summary.json").read_text())
        self.assertEqual((summary["files_run"], summary["skipped"]), (1, 2))

    def test_evidence_is_never_reused(self) -> None:
        build(self.root, {"test_pass.py": UNITTEST_PASS})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            run_gate.main(["--root", str(self.root), "--evidence", str(self.evidence)])
            with self.assertRaises(SystemExit) as refused:
                run_gate.main(["--root", str(self.root), "--evidence", str(self.evidence)])
        self.assertEqual(refused.exception.code, 2)

    def test_serial_file_never_overlaps_another(self) -> None:
        names = ["test_serial_probe.py", "test_parallel_a.py", "test_parallel_b.py"]
        build(self.root, {name: PROBE for name in names})
        probe = Path(self._tmp.name) / "probe.log"
        os.environ["GATE_PROBE"] = str(probe)
        self.addCleanup(os.environ.pop, "GATE_PROBE", None)
        summary, output = self.gate(names, workers=3, serial={"test_serial_probe.py": "self-test"})
        self.assertEqual(summary["failed"], 0, output)

        spans, lines = {}, probe.read_text().splitlines()
        for index, line in enumerate(lines):
            edge, path, at = line.split(" ", 2)
            key = Path(path).stem
            spans.setdefault(key, []).append((edge, float(at)))
        serial_span = sorted(at for edge, at in spans["test_serial_probe"])
        self.assertEqual(len(serial_span), 2, lines)
        parallel = []
        for name in ("test_parallel_a", "test_parallel_b"):
            span = sorted(at for edge, at in spans[name])
            self.assertEqual(len(span), 2, lines)
            parallel.append(span)
            overlap = span[0] < serial_span[1] and serial_span[0] < span[1]
            self.assertFalse(overlap, f"{name} overlapped the serial file: {lines}")
        # And the two parallel files really did overlap, so the probe proves something.
        self.assertTrue(parallel[0][0] < parallel[1][1] and parallel[1][0] < parallel[0][1], lines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
