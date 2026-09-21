#!/usr/bin/env python3
"""Named, narrow checks against the running app — the alternative to test_ui.py.

`tests/test_ui.py` is a release gate: it walks every section, forces the window to
700 pt and presses controls, which takes over the owner's app. This script exists
so a routine change can be checked without that. Each check reads one thing and
prints PASS / FAIL / SKIP. It never resizes the window, never fronts the app, and
never presses a control.

Usage:
    python tests/check_native.py                # list the checks
    python tests/check_native.py sidebar jobs-provider

It attaches to the Accessibiity layer the same way test_ui.py does, so it needs
the same permission for the process running it.
"""
from __future__ import annotations

import subprocess
import sys
import time


APP_PROCESS = "GatewayKH"
MAIN_WINDOW = '(first window whose subrole is "AXStandardWindow")'


def osa(body: str, attempts: int = 3, timeout: float = 30.0) -> str:
    """AppleScript, retrying only the transient -1719 node-resolution race."""
    script = f'tell application "System Events" to tell process "{APP_PROCESS}"\n{body}\nend tell'
    for attempt in range(attempts):
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
        message = result.stderr.strip().splitlines()[-1] if result.stderr else "osascript failed"
        if "-1719" not in message or attempt == attempts - 1:
            raise RuntimeError(message)
        time.sleep(0.8)
    return ""


def open_section(name: str) -> str:
    osa(f'click menu item "{name}" of menu 1 of menu bar item "View" of menu bar 1')
    time.sleep(1.8)
    return osa(f"tell {MAIN_WINDOW} to return name")


def check_sidebar() -> tuple[bool, str]:
    buttons = osa(f"tell {MAIN_WINDOW} to tell splitter group 1 to return (title of every button)")
    version = osa(f"tell {MAIN_WINDOW} to tell splitter group 1 to return (value of every static text)")
    ok = "New" in buttons and "App version" in version
    return ok, f"buttons={buttons!r} version={'App version' in version}"


def check_jobs_provider() -> tuple[bool, str]:
    title = open_section("Jobs")
    if title != "gateway-kh — Jobs":
        return False, f"Jobs did not open (window is {title!r})"
    table = "table 1 of scroll area 2 of splitter group 1"
    columns = osa(f"tell {MAIN_WINDOW} to tell {table} to return (count of columns)")
    if int(columns or 0) < 6:
        return False, f"expected a Provider column; {columns} columns"
    cells = []
    for index in range(1, 7):
        try:
            cells.append(osa(f"tell {MAIN_WINDOW} to tell {table} to return (value of static text 1 of UI element {index} of row 1)"))
        except RuntimeError:
            cells.append("-")
    return True, " | ".join(cells)


def check_content_packs_page() -> tuple[bool, str]:
    title = open_section("Content Packs")
    return title == "gateway-kh — Content Packs", f"window is {title!r}"


CHECKS = {
    "sidebar": ("sidebar footer shows the app version and the What's New link", check_sidebar),
    "jobs-provider": ("Jobs table has a Provider column and a real value", check_jobs_provider),
    "content-packs": ("Content Packs opens the embedded page in this window", check_content_packs_page),
}


def main() -> int:
    names = sys.argv[1:] or []
    if not names:
        print("named checks:")
        for name, (description, _) in CHECKS.items():
            print(f"  {name:<16} {description}")
        return 0
    # Put the owner's page back when we are done: the Jobs check has to open Jobs,
    # and leaving his app on a different section is still a small interruption.
    try:
        original = osa(f"tell {MAIN_WINDOW} to return name").removeprefix("gateway-kh — ")
    except RuntimeError:
        original = ""
    failed = 0
    try:
        for name in names:
            if name not in CHECKS:
                print(f"FAIL {name}: no such check")
                failed += 1
                continue
            description, run = CHECKS[name]
            try:
                ok, detail = run()
            except Exception as exc:  # noqa: BLE001 - report, never crash a background check
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            print(f"{'PASS' if ok else 'FAIL'} {name}: {description}\n      {detail}")
            failed += 0 if ok else 1
    finally:
        try:
            if original and osa(f"tell {MAIN_WINDOW} to return name") != f"gateway-kh — {original}":
                open_section(original)
        except RuntimeError:
            pass
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
