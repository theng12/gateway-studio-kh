#!/usr/bin/env python3
"""The Gateway release gate: every `tests/test_*.py` in its own process, bounded in parallel.

This file replaces the per-release `run-gate.sh` copies under `_orchestra/`, which were never
tracked and drifted. It exists because of two measured defects:

* 0.2.203's gate ran `python -m unittest tests.<name>` for every file, including the script-style
  suites (`test_e2e.py`, `test_ui.py`, `test_fleet_standalone.py`: a `main()`, no TestCase). Those
  reported `Ran 0 tests` and exited 0, so three suites that ran nothing counted as passes.
  **Here "Ran 0 tests" is a FAILURE**, never a pass.
* One file per process is mandatory, not tidiness: `_NetworkDisabled` leaks across files when
  `unittest discover` imports them all into one process.

Style is content-decided, not guessed at run time: a file in `SCRIPT_STYLE` runs as a script, and
every other file runs under `unittest` and must collect at least one test. A new script-style file
therefore fails loudly (`collected no tests`) instead of passing silently — add it to the list.

Parallelism is the owner's decision, 2026-10-26 ("yes. in parallel"). A bounded pool runs files
concurrently; the files in `SERIAL` run alone, because they own the screen or assert wall-clock
ceilings and a busy Mac has already produced a false red (2026-10-24).

Scope: the Python suites. The console/JS suites (`node --test tests/<file>.js`) and the native
single-element checks (`tests/check_native.py`) keep their own commands; they are not part of this
gate.

    python scripts/run_gate.py --evidence /tmp/gate-1          # default worker count
    python scripts/run_gate.py --evidence /tmp/gate-2 --workers 1   # serial, for comparison
    python scripts/run_gate.py --evidence /tmp/gate-3 --only e2e    # one file, while debugging
    python scripts/test_run_gate.py                            # the runner's own self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


#: Never run here. Both are named, not globbed, so a SKIP line always carries its reason.
SKIP = {
    "test_ui.py": "drives the installed app and takes over the owner's windows",
    "test_fleet_standalone.py": "needs GATEWAY_ALLOW_LIVE_GENERATION=1 and the owner's real Hub to serve as a location",
}

#: Script-style suites: a `main()` and no TestCase. Running them under unittest collects nothing.
SCRIPT_STYLE = {
    "test_e2e.py",
    "test_music_output_format_advertised.py",  # runpy entry point for the audited music artifact
    "test_ui.py",
    "test_fleet_standalone.py",
}

#: Runs alone, never beside another file. Window-touching/native offscreen suites (swiftc + AppKit
#: windows), and suites that assert a wall-clock ceiling a loaded Mac can break.
SERIAL = {
    # native/offscreen: compile menubar sources and realise AppKit windows
    "test_create_layout.py": "native offscreen AppKit layout",
    "test_fleet_action_banner_layout.py": "native offscreen AppKit layout",
    "test_fleet_banner.py": "native offscreen AppKit layout",
    "test_fleet_layout.py": "native offscreen AppKit layout",
    "test_jobs_outputs.py": "native offscreen AppKit layout",
    "test_secret_field.py": "native offscreen AppKit layout",
    "test_settings_retention_fields.py": "native offscreen AppKit layout",
    "test_task5_native.py": "native offscreen AppKit layout",
    "test_whats_new_page.py": "native offscreen AppKit layout",
    "test_window_size.py": "resizes the real app window and waits on a fixture server",
    # real wall-clock ceilings
    "test_catalog_fast.py": "asserts a catalogue walk under 1.0 s",
    "test_jobs_list_fast.py": "asserts a list probe under 2.5 s",
    "test_lossless_restart.py": "asserts a drain window between 0.5 s and 2.0 s",
}

#: A hung suite must not hang the gate. The longest real suite here (test_window_size) is ~110 s.
TIMEOUT_SECONDS = 900

#: How long a finite process group may take to disappear after its leader exits.
SETTLE_SECONDS = 5.0

RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
E2E = re.compile(r"All (\d+) end-to-end checks passed")


@dataclass
class Result:
    name: str
    style: str
    passed: bool
    exit_code: int | None
    seconds: float
    tests: int | None = None
    checks: int | None = None
    reason: str = ""
    log: Path | None = None


def command(path: Path) -> list[str]:
    """One file, one process. `-B` keeps __pycache__ out of the worktree."""
    if path.name in SCRIPT_STYLE:
        return [sys.executable, "-B", str(path)]
    return [sys.executable, "-B", "-m", "unittest", f"tests.{path.stem}"]


def child_env(root: Path, profile: Path) -> dict[str, str]:
    """A fresh profile for every file, even one that forgets to isolate itself.

    The 14 suites that never call `sandbox_env` would otherwise bind to the owner's real
    Application Support on first `import gateway.*`. Only the two roots are set: every other
    gateway path derives from them (`config.DATA_DIR`), so a suite that repoints just
    `GATEWAY_APP_SUPPORT` at its own temp dir still moves the rest with it — pinning the derived
    paths here shadows the suite's own choice (test_e2e.py discovered no voices that way).
    Paths only: no API key, so no file can decide another's auth.
    """
    env = dict(os.environ)
    # The Launcher file and browser worker endpoint are ambient credentials, not source.
    env.pop("KHU_HOLD_TOKEN_FILE", None)
    env.pop("GATEWAY_ENDPOINT_FILE", None)
    for derived in ("GATEWAY_ARTIFACTS", "GATEWAY_DB", "GATEWAY_CREDENTIALS", "GATEWAY_MODEL_CACHE",
                    "GATEWAY_ENV_FILE", "GATEWAY_FLEET", "GATEWAY_VOICES", "GATEWAY_VOICES_OUT"):
        env.pop(derived, None)
    env.update(
        GATEWAY_APP_SUPPORT=str(profile),
        GATEWAY_DATA=str(profile / "data"),
        STUDIOHUB_READY_FILE=str(profile / "no-hub.json"),
        GATEWAY_NO_MIGRATE="1",
        # Never the owner's real iCloud Drive, drives or backups: listing them can wait for ever
        # on a macOS permission answer when the gate runs outside an app (launchd).
        GATEWAY_BACKUP_ICLOUD=str(profile / "backup-icloud"),
        GATEWAY_BACKUP_LOCAL=str(profile / "backup-local"),
        GATEWAY_BACKUP_VOLUMES=str(profile / "backup-volumes"),
        GATEWAY_BACKUP_TRASH=str(profile / "backup-trash"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    env["KHU_HOLD_TOKEN_FILE"] = str(profile / "launcher-hold-token")
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(root), str(root / "tests"), env.get("PYTHONPATH")])
    )
    return env


def group_present(pid: int) -> bool:
    """True while anything at all is left in this file's own process group."""
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_group(pid: int, grace: float = 5.0) -> None:
    """TERM the whole process group, then KILL it. Fixture servers are grandchildren."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while group_present(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if group_present(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_one(root: Path, path: Path, evidence: Path) -> Result:
    name = path.stem
    style = "script" if path.name in SCRIPT_STYLE else "unittest"
    profile = Path(tempfile.mkdtemp(prefix=f"gateway-gate-{name}-"))
    log_path = evidence / f"{name}.log"
    started = time.monotonic()
    timed_out = leftover = False
    exit_code: int | None = None
    process: subprocess.Popen | None = None
    try:
        with log_path.open("w") as log:
            process = subprocess.Popen(
                command(path), cwd=root, env=child_env(root, profile),
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_group(process.pid)
                exit_code = process.wait()
        text = log_path.read_text(encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    seconds = round(time.monotonic() - started, 1)

    # The leader exiting does not mean it left nothing behind (a fixture server, a compiler child).
    # A suite that already sent its server SIGTERM gets a moment to finish before we call it stray.
    if process is not None:
        deadline = time.monotonic() + SETTLE_SECONDS
        while group_present(process.pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if group_present(process.pid):
            leftover = True
            stop_group(process.pid)

    ran = RAN.search(text)
    checks = E2E.search(text)
    result = Result(
        name=name, style=style, passed=True, exit_code=exit_code, seconds=seconds,
        tests=int(ran.group(1)) if ran else None,
        checks=int(checks.group(1)) if checks else None,
        log=log_path,
    )
    if timed_out:
        result.passed, result.reason = False, f"timed out after {TIMEOUT_SECONDS}s"
    elif exit_code != 0:
        result.passed, result.reason = False, f"exit {exit_code}"
    elif leftover:
        result.passed, result.reason = False, "left processes running after exit"
    elif style == "unittest" and not result.tests:
        result.passed = False
        result.reason = ("collected no tests — a script-style file belongs in SCRIPT_STYLE, "
                         "and a unittest file must define at least one TestCase")
    return result


class Gate:
    """Hand one file at a time to the pool; a serial file waits for every other file to finish."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._active = 0
        self._exclusive = False

    def enter(self, exclusive: bool) -> None:
        with self._cv:
            while self._exclusive or (exclusive and self._active):
                self._cv.wait()
            self._exclusive = exclusive
            self._active += 1

    def leave(self, exclusive: bool) -> None:
        with self._cv:
            self._active -= 1
            if exclusive:
                self._exclusive = False
            self._cv.notify_all()


def run_gate(root: Path, files: list[Path], workers: int, evidence: Path,
             serial: dict[str, str] | None = None) -> dict:
    serial = SERIAL if serial is None else serial
    evidence.mkdir(parents=True, exist_ok=True)
    gate = Gate()
    lock = threading.Lock()
    pending = iter(files)
    results: list[Result] = []
    print(f"GATE root {root} | {len(files)} files | workers {workers}", flush=True)
    for name, why in serial.items():
        if name in {path.name for path in files}:
            print(f"SERIAL {name}: {why}", flush=True)

    def worker() -> None:
        while True:
            with lock:
                path = next(pending, None)
            if path is None:
                return
            exclusive = path.name in serial
            gate.enter(exclusive)
            try:
                result = run_one(root, path, evidence)
            except Exception as exc:  # a runner crash must not shrink the file count silently
                result = Result(name=path.stem, style="error", passed=False, exit_code=None,
                                seconds=0.0, reason=f"runner error: {exc}")
            finally:
                gate.leave(exclusive)
            with lock:
                results.append(result)
            status = "PASS" if result.passed else "FAIL"
            detail = f"{result.tests} tests" if result.tests is not None else result.style
            if result.checks is not None:
                detail += f", {result.checks} e2e checks"
            if not result.passed:
                detail += f", {result.reason}"
            print(f"{status} {result.name} ({detail}, {result.seconds}s)", flush=True)

    started = time.monotonic()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = round(time.monotonic() - started, 1)

    results.sort(key=lambda r: r.name)
    failed = [r for r in results if not r.passed]
    skipped = {name: why for name, why in SKIP.items() if (root / "tests" / name).exists()}
    tests = sum(r.tests or 0 for r in results)
    counted = [r for r in results if r.tests is not None]
    checks = sum(r.checks or 0 for r in results if r.checks is not None)
    summary = {
        "root": str(root),
        "workers": workers,
        "wall_seconds": wall,
        "files_run": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "skipped": len(skipped),
        "unittest_tests": tests,
        "files_reporting_tests": len(counted),
        "e2e_checks": checks,
        "results": [r.__dict__ | {"log": str(r.log)} for r in results],
        "skipped_files": skipped,
    }
    for name, why in skipped.items():
        print(f"SKIP {name[:-3]} ({why})", flush=True)
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"GATE: {len(results)} files run, {summary['passed']} passed, {len(failed)} failed, "
        f"{len(skipped)} skipped | {tests} tests in {len(counted)} files | "
        f"e2e {checks} checks | {wall}s wall"
    )
    for result in failed:
        print(f"\n--- {result.name}: {result.reason} ({result.log})")
        if not result.log or not result.log.is_file():
            continue
        tail = result.log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-12:]
        for line in tail:
            print(f"    {line}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=repo_root(), help="Gateway checkout to gate")
    parser.add_argument("--evidence", type=Path, help="directory for per-file logs and summary.json (never reused)")
    parser.add_argument("--workers", type=int, help="concurrent files (env GATEWAY_GATE_WORKERS; 1 = serial)")
    parser.add_argument("--only", action="append", default=[], metavar="SUBSTRING",
                        help="run only files whose name contains SUBSTRING (repeatable; debugging only)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not (root / "tests" / "_isolation.py").is_file():
        parser.error(f"{root} is not a Gateway checkout (tests/_isolation.py is missing)")
    files = sorted((root / "tests").glob("test_*.py"))
    files = [path for path in files if path.name not in SKIP]
    if args.only:
        files = [path for path in files if any(part in path.name for part in args.only)]
    if not files:
        parser.error("no test files selected")

    workers = args.workers or int(os.environ.get("GATEWAY_GATE_WORKERS") or 0) \
        or min(3, max(1, (os.cpu_count() or 2) // 2))
    if workers < 1:
        parser.error("--workers must be at least 1")

    if args.evidence:
        evidence = args.evidence.resolve()
        if evidence.exists() and any(evidence.iterdir()):
            parser.error(f"refusing to reuse a non-empty evidence directory: {evidence}")
        evidence.mkdir(parents=True, exist_ok=True)
    else:
        evidence = Path(tempfile.mkdtemp(prefix="gateway-gate-evidence-"))
    print(f"evidence {evidence}", flush=True)

    summary = run_gate(root, files, workers, evidence)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
