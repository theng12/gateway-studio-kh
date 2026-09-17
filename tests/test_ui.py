#!/usr/bin/env python3
"""UI tests that actually click.

The e2e suite proves the gateway's contract. This proves the *app*: it drives
the real macOS accessibility layer to navigate the sidebar through the actual
menus, read what each section renders, type into fields, tick checkboxes and
press buttons — then verifies the gateway changed, not merely that a widget
repainted.

That distinction matters. A window can build and lay out perfectly while a
button does nothing, and only clicking finds that. So does a layout that
"works" but is unreadable.

Requires the app to be running, and Accessibility permission for whatever
terminal this is run from.

Run:  .venv/bin/python tests/test_ui.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

APP_PROCESS = "GatewayKH"

#: Sidebar sections in order, with the window title each should produce.
SECTIONS = [
    ("Overview", "gateway-kh — Overview"),
    ("Jobs", "gateway-kh — Jobs"),
    ("Voices", "gateway-kh — Voices"),
    ("Providers", "gateway-kh — Providers"),
]

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    tag = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    suffix = f" — {detail}" if (detail and not ok) else ""
    print(f"  {tag} {label}{suffix}")
    if not ok:
        failures.append(label)


def osa(script: str, timeout: float = 90.0) -> str:
    """Run AppleScript, raising with its message on failure."""
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        message = result.stderr.strip().splitlines()[-1] if result.stderr else "osascript failed"
        raise RuntimeError(message)
    return result.stdout.strip()


def wrap(body: str) -> str:
    return f'tell application "System Events" to tell process "{APP_PROCESS}"\n{body}\nend tell'


def api_get(path: str) -> dict:
    key = ""
    env = os.path.expanduser("~/Library/Application Support/gateway-kh/.env")
    if os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            if line.startswith("GATEWAY_API_KEY="):
                key = line.split("=", 1)[1].strip()
    request = urllib.request.Request(
        f"http://127.0.0.1:8010{path}", headers={"Authorization": f"Bearer {key}"}
    )
    return json.load(urllib.request.urlopen(request, timeout=30))


def window_count() -> int:
    try:
        return int(osa(wrap("return count of windows")) or 0)
    except (RuntimeError, ValueError):
        return -1


def window_title() -> str:
    try:
        return osa(wrap("return name of window 1"))
    except RuntimeError:
        return ""


def open_section(name: str) -> str:
    """Select a section from the real View menu and return the window title."""
    osa(wrap(f'click menu item "{name}" of menu 1 of menu bar item "View" of menu bar 1'))
    time.sleep(2.5)
    return window_title()


def sidebar_rows() -> int:
    try:
        return int(osa(wrap(
            "tell window 1 to return (count of rows of table 1 of scroll area 1 "
            "of splitter group 1)"
        )) or 0)
    except (RuntimeError, ValueError):
        return -1


def detail_rows() -> int:
    """Rows in the detail table, for sections that have one."""
    try:
        return int(osa(wrap(
            "tell window 1 to return (count of rows of table 1 of scroll area 2 "
            "of splitter group 1)"
        )) or 0)
    except (RuntimeError, ValueError):
        return -1


def detail_texts() -> str:
    return osa(wrap('''
        tell window 1 to tell splitter group 1
            set acc to ""
            repeat with t in static texts
                try
                    set v to value of t
                    if v is not missing value then set acc to acc & (v as string) & "\\n"
                end try
            end repeat
            return acc
        end tell'''))


def detail_count(expr: str) -> int:
    try:
        return int(osa(wrap(f"tell window 1 to tell splitter group 1 to return (count of {expr})")) or 0)
    except (RuntimeError, ValueError):
        return -1


def field_value(field: str) -> str:
    """Read a text field's current value; missing values read as empty."""
    try:
        value = osa(wrap(
            f"tell window 1 to tell splitter group 1 to return value of {field}"))
    except RuntimeError:
        return ""
    return "" if value in ("missing value", "false") else value


def focus_field(field: str) -> None:
    osa(f'''
    tell application "System Events"
      tell process "{APP_PROCESS}"
        set frontmost to true
        delay 0.6
        tell window 1
          perform action "AXRaise"
          tell splitter group 1 to set focused of {field} to true
        end tell
      end tell
    end tell''')


def clear_field(field: str, attempts: int = 4) -> str:
    """Empty a field, retrying until it actually reads empty."""
    for _ in range(attempts):
        focus_field(field)
        osa('''
        delay 0.4
        tell application "System Events" to keystroke "a" using command down
        tell application "System Events" to key code 51''')
        time.sleep(0.8)
        value = field_value(field)
        if value == "":
            return value
    return field_value(field)


def type_into(field: str, text: str, attempts: int = 4) -> str:
    """Focus a field and type into it, retrying until the value sticks.

    Three things matter here, and each cost a debugging round:

      * Frontmost does not survive between separate `osascript` invocations —
        the terminal takes focus back each time the process exits — so
        activating, focusing and typing have to happen in one script.
      * `keystroke` must be its own statement against System Events, *not*
        nested inside `tell process`: there the command resolves against the
        process and the key goes nowhere.
      * The window must be raised first, or a web view behind the field
        swallows the keys silently.

    Even with all three, this app is an accessory (`LSUIElement`) process: it
    is never *really* frontmost, so a keystroke is a request, not a guarantee.
    Rather than sleep-and-hope, read the value back and retry — a flaky test
    that fails half the time is worse than no test.
    """
    for _ in range(attempts):
        focus_field(field)
        osa(f'''
        delay 0.5
        tell application "System Events" to keystroke "{text}"''')
        time.sleep(0.9)
        value = field_value(field)
        if value == text:
            return value
        # A partial or duplicated landing would poison the next attempt.
        if value:
            clear_field(field)
    return field_value(field)


def read_modality_rows(settle: int = 4) -> list[tuple[int, int]]:
    """Read the Overview modality bars as (top, height), sorted by position.

    Waits for two consecutive identical reads so a mid-rebuild layout is never
    mistaken for a broken one.
    """
    script = wrap('''
        tell window 1 to tell splitter group 1
            set wanted to {"Chat", "Image", "Video", "TTS", "Transcription", "Music"}
            set acc to ""
            repeat with t in static texts
                try
                    set v to (value of t) as string
                    if wanted contains v then
                        set p to position of t
                        set s to size of t
                        set acc to acc & v & ":" & (item 2 of p) & ":" & (item 2 of s) & "\\n"
                    end if
                end try
            end repeat
            return acc
        end tell''')
    previous: list[tuple[int, int]] = []
    for _ in range(settle):
        rows: list[tuple[int, int]] = []
        for line in osa(script).splitlines():
            parts = line.split(":")
            if len(parts) == 3:
                rows.append((int(parts[1]), int(parts[2])))
        # A duplicate name means the view is mid-rebuild; not settled yet.
        rows = sorted(set(rows))
        if rows and rows == previous:
            return rows
        previous = rows
        time.sleep(1.2)
    return previous


def main() -> int:
    print("\n\033[1m0. Preconditions\033[0m")
    try:
        running = osa(f'tell application "System Events" to return (exists process "{APP_PROCESS}")')
    except RuntimeError as exc:
        print(f"  could not reach the accessibility layer: {exc}")
        return 1
    check("the app is running", running == "true", running)
    if running != "true":
        return 1

    # The app should have opened exactly one window on launch.
    count = window_count()
    check("exactly one window is open", count == 1, f"{count} windows")

    # ------------------------------------------------------------------
    print("\n\033[1m1. One window with a sidebar\033[0m")
    rows = sidebar_rows()
    check("the sidebar lists every section", rows == len(SECTIONS), f"{rows} rows")
    check("the sidebar is part of the window, not a separate one", window_count() == 1)

    for name, expected in SECTIONS:
        title = open_section(name)
        check(f"View → {name} selects it", title == expected, title)
        check(f"still exactly one window on “{name}”", window_count() == 1,
              f"{window_count()} windows")

    # ------------------------------------------------------------------
    print("\n\033[1m2. Overview renders live data\033[0m")
    open_section("Overview")
    body = detail_texts()
    check("reports the gateway as running", "Gateway running" in body, body[:120])
    check("shows the endpoint and uptime", "127.0.0.1:" in body and "up " in body)
    check("shows provider counts", "/18" in body)
    check("shows a model count", "discovered" in body)
    check("shows the modality breakdown", "OFFERED TO STORY STUDIO" in body.upper())
    check("shows the local runner state", "Studio Hub" in body)

    status = subprocess.run(
        ["curl", "-s", "-m", "10", "http://127.0.0.1:8010/health"],
        capture_output=True, text=True,
    ).stdout
    if '"enabled_models"' in status:
        enabled = json.loads(status)["models"]["enabled_models"]
        check(f"the model count on screen matches the API ({enabled})",
              f"\n{enabled}\n" in f"\n{body}\n", body[:200])

    # The first version of this view built fine and was unreadable: the bar rows
    # had no height, so the stack view crushed them together. "It opened" would
    # never have caught that — measuring the rows does.
    #
    # Two things make this check honest rather than lucky. Overview re-renders
    # every 5 s, so a single read can land mid-rebuild and see half-positioned
    # rows; read until two consecutive reads agree. And accessibility returns
    # elements in its own order, not visual order, so sort by position before
    # asking whether the spacing is even — otherwise the check is really testing
    # System Events' element ordering.
    chart = read_modality_rows()
    check("every modality row is rendered", len(chart) >= 4, f"{len(chart)} rows: {chart}")
    if len(chart) >= 2:
        overlapping = [
            (chart[i], chart[i + 1])
            for i in range(len(chart) - 1)
            if chart[i + 1][0] - chart[i][0] < chart[i][1]
        ]
        check("modality rows do not overlap", not overlapping, f"rows={chart}")
        gaps = [chart[i + 1][0] - chart[i][0] for i in range(len(chart) - 1)]
        check("modality rows are evenly spaced", len(set(gaps)) == 1, f"gaps={gaps}")

    # ------------------------------------------------------------------
    print("\n\033[1m3. Jobs section\033[0m")
    open_section("Jobs")
    rows = detail_rows()
    deadline = time.time() + 20
    while rows < 1 and time.time() < deadline:
        time.sleep(0.7)
        rows = detail_rows()
    check("jobs table populated", rows > 0, f"{rows} rows")
    jobs_body = detail_texts()
    check("shows a summary line", "succeeded" in jobs_body.lower(), jobs_body[:160])

    if rows > 0:
        osa(wrap("tell window 1 to tell splitter group 1 to select row 1 "
                 "of table 1 of scroll area 2"))
        time.sleep(1.5)
        after = detail_texts()
        check("selecting a job reveals its detail", " via " in after or "\nid " in after,
              after[-200:])

    # ------------------------------------------------------------------
    print("\n\033[1m4. Voices section, and its controls work\033[0m")
    open_section("Voices")
    rows = detail_rows()
    deadline = time.time() + 25
    while rows < 1 and time.time() < deadline:
        time.sleep(0.7)
        rows = detail_rows()
    check("voice table populated", rows > 0, f"{rows} rows")
    voices_body = detail_texts()
    check("shows a voice summary", "enabled" in voices_body.lower(), voices_body[:160])
    check("has a provider filter", detail_count("pop up buttons") == 1)
    check("has a library toggle", detail_count("checkboxes") >= 1)

    if rows > 3:
        typed = type_into("text field 1", "z")
        time.sleep(2.0)
        filtered = detail_rows()
        check("the search box receives typed text", typed == "z", f"field holds {typed!r}")
        check(f"typing filters the table ({rows} → {filtered})", 0 <= filtered < rows,
              f"still {filtered}")
        cleared = clear_field("text field 1")
        time.sleep(2.0)
        check("clearing the search restores the full list",
              cleared == "" and detail_rows() == rows,
              f"field {cleared!r}, {detail_rows()} of {rows}")

    # The enable checkbox lives inside a cell view, and what matters is that
    # clicking it reaches the gateway — not merely that the box repaints.
    checkbox = ("checkbox 1 of UI element 1 of row 1 of table 1 of scroll area 2 "
                "of splitter group 1")
    if rows > 0:
        try:
            before = api_get("/admin/api/voices")["summary"]["enabled"]
            osa(wrap(f"tell window 1 to click {checkbox}"))
            time.sleep(2.5)
            after = api_get("/admin/api/voices")["summary"]["enabled"]
            check(f"ticking a voice reaches the gateway ({before} → {after} enabled)",
                  after != before)
            osa(wrap(f"tell window 1 to click {checkbox}"))
            time.sleep(2.0)
            check("unticking it restores the count",
                  api_get("/admin/api/voices")["summary"]["enabled"] == before)
        except RuntimeError as exc:
            check("ticking a voice reaches the gateway", False, str(exc)[:140])

    # ------------------------------------------------------------------
    print("\n\033[1m5. Providers section\033[0m")
    title = open_section("Providers")
    check("Providers selects into the same window", title == "gateway-kh — Providers", title)
    check("the app survived loading the web console",
          osa(f'tell application "System Events" to return (exists process "{APP_PROCESS}")') == "true")

    # ------------------------------------------------------------------
    print("\n\033[1m6. Settings is its own window (macOS convention)\033[0m")
    osa(wrap('click menu item "Settings…" of menu 1 of menu bar item "gateway-kh" of menu bar 1'))
    time.sleep(2.5)
    check("Settings opens as a separate window", window_count() == 2, f"{window_count()} windows")
    settings = osa(wrap('''
        tell window "gateway-kh Settings"
            set acc to ""
            repeat with t in static texts
                try
                    set v to value of t
                    if v is not missing value then set acc to acc & (v as string) & "\\n"
                end try
            end repeat
            return acc
        end tell'''))
    check("Settings shows the runtime", "runtime" in settings.lower(), settings[:160])
    check("Settings shows the data folder", "gateway-kh" in settings)
    check("Settings has an API key field",
          int(osa(wrap('tell window "gateway-kh Settings" to return (count of text fields)'))) >= 2)
    # The close button is not button 1 — button 1 is the first content button
    # ("Copy"). Find it by subrole, the way a user finds the red traffic light.
    osa(wrap('''
        tell window "gateway-kh Settings"
            repeat with b in buttons
                if (subrole of b) is "AXCloseButton" then
                    click b
                    exit repeat
                end if
            end repeat
        end tell'''))
    time.sleep(1.5)
    check("Settings closes and leaves one window", window_count() == 1,
          f"{window_count()} windows")

    # ------------------------------------------------------------------
    print("\n\033[1m7. Status menu\033[0m")
    items = osa(wrap('''
        click menu bar item 1 of menu bar 2
        delay 1
        set acc to ""
        repeat with i in (menu items of menu 1 of menu bar item 1 of menu bar 2)
            try
                set acc to acc & (name of i) & "\\n"
            end try
        end repeat
        key code 53
        return acc'''))
    for expected in ["Overview", "Jobs", "Voices", "Quit"]:
        check(f"status menu offers “{expected}”", expected in items, items[:200])

    # ------------------------------------------------------------------
    print("\n\033[1m8. Everything still healthy afterwards\033[0m")
    check("one window left", window_count() == 1, f"{window_count()} windows")
    health = subprocess.run(
        ["curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}",
         "http://127.0.0.1:8010/health"],
        capture_output=True, text=True,
    ).stdout.strip()
    check("gateway still answering", health == "200", f"HTTP {health}")

    print("\n" + "=" * 70)
    if failures:
        print(f"\033[31m{len(failures)} of {checks} UI checks FAILED\033[0m")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"\033[32mAll {checks} UI checks passed.\033[0m")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
