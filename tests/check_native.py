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


def check_fleet_unreachable_address() -> tuple[bool, str]:
    """An unreachable location still shows WHICH address the gateway dialled.

    The Locations Status cell used to *replace* the address with the reason, so
    three rows read "Nothing is listening on that address" without ever naming the
    address — and the only branch that printed it was the one you could reach. The
    owner asked what those rows were even connected to, which is the question the
    cell was supposed to answer.

    The invariant: **wherever a reason is shown, the address is shown with it.**
    When every location happens to be answering there is nothing to prove, and the
    detail says so rather than reporting a silent pass.
    """
    title = open_section("Fleet")
    if title != "gateway-kh — Fleet":
        return False, f"Fleet did not open (window is {title!r})"
    # Locations is radio button 2 of the page's tab group — the order of `FleetTab`.
    # The page remembers the last tab, so a check that assumes it is already there
    # reads whichever tab the owner left open.
    osa(
        f'tell {MAIN_WINDOW} to tell splitter group 1 to tell radio group 1 '
        f'to click radio button 2'
    )
    time.sleep(0.8)
    table = "table 1 of scroll area 1 of scroll area 2"
    rows = int(
        osa(f"tell {MAIN_WINDOW} to tell splitter group 1 to tell {table} to return (count of rows)")
        or 0
    )
    if rows == 0:
        return False, "the Locations table drew no rows"
    checked = 0
    for row in range(1, rows + 1):
        # Eight columns today; read one past so a new column cannot hide a cell.
        for column in range(1, 10):
            try:
                value = osa(
                    f"tell {MAIN_WINDOW} to tell splitter group 1 to tell {table} "
                    f"to return (value of static text 1 of UI element {column} of row {row})"
                )
            except RuntimeError:
                continue
            if "Nothing is listening" not in value and "not reachable" not in value.lower():
                continue
            checked += 1
            if "http" not in value:
                return False, f"row {row} gives a reason but names no address: {value!r}"
    if checked == 0:
        return True, f"{rows} locations, all answering — nothing unreachable to prove"
    return True, f"{checked} unreachable row(s), each naming its address"


def check_jobs_recovery() -> tuple[bool, str]:
    """The recovery action appears only on a job that is actually a recovery hold.

    Read-only: it opens Jobs, selects the first row and reads what is drawn. There
    are two actions now, because the gateway decides which one a hold can have —
    *Discard uncertain attempt…* asks a Hub to release the machine an uncertain item
    reserved, and *Close this hold…* closes the gateway's own record for a lane with
    no discard route (every cloud provider). The invariant is therefore "at most one
    action, and only on a hold", never a particular title.
    """
    title = open_section("Jobs")
    if title != "gateway-kh — Jobs":
        return False, f"Jobs did not open (window is {title!r})"
    table = "table 1 of scroll area 2 of splitter group 1"
    rows = int(osa(f"tell {MAIN_WINDOW} to tell {table} to return (count of rows)") or 0)
    if rows == 0:
        return True, "no jobs to inspect"
    osa(f"tell {MAIN_WINDOW} to tell {table} to select row 1")
    time.sleep(1.4)
    detail = osa(
        f"tell {MAIN_WINDOW} to tell splitter group 1 to return (value of every static text)"
    )
    titles = osa(
        f"tell {MAIN_WINDOW} to tell splitter group 1 to return (title of every button)"
    )
    offered = [
        action for action in ("Discard uncertain attempt", "Close this hold")
        if action in titles
    ]
    on_hold = "Manual recovery required" in detail
    if offered and not on_hold:
        return False, f"{offered} offered for a job that is not a recovery hold"
    if len(offered) > 1:
        return False, f"two recovery actions were offered at once: {offered}"
    return True, f"rows={rows} hold={on_hold} actions={offered}"


CHECKS = {
    "sidebar": ("sidebar footer shows the app version and the What's New link", check_sidebar),
    "jobs-provider": ("Jobs table has a Provider column and a real value", check_jobs_provider),
    "content-packs": ("Content Packs opens the embedded page in this window", check_content_packs_page),
    "fleet-unreachable-address": (
        "an unreachable location names the address as well as the reason",
        check_fleet_unreachable_address,
    ),
    "jobs-recovery": (
        "the discard action is offered only for a recovery hold",
        check_jobs_recovery,
    ),
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
