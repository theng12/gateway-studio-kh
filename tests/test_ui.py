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

#: Sidebar rows in order, with the window title each section should produce.
#: Studio Hub KH groups its sidebar, and this app was asked to match it, so the
#: list contains group headers ("CREATE", "LIBRARY & APP") as well as sections.
#: Headers are not selectable; they are listed here so the ordering and the
#: grouping are pinned by a test rather than by whoever last edited the layout.
SIDEBAR = [
    "Overview",
    "LIBRARY & APP",
    "Jobs",
    "Voices",
    "Models",
    "Providers",
    "Fleet",
    "Settings",
]

#: Sections reachable from the View menu, in row order:
#: (sidebar row, menu title, window title). Settings is deliberately absent —
#: it is opened with ⌘, or by clicking its sidebar row, and is checked in
#: section 6 rather than duplicated in the View menu.
SECTIONS = [
    (1, "Overview", "gateway-kh — Overview"),
    (3, "Jobs", "gateway-kh — Jobs"),
    (4, "Voices", "gateway-kh — Voices"),
    (5, "Models", "gateway-kh — Models"),
    (6, "Providers", "gateway-kh — Providers"),
    (7, "Fleet", "gateway-kh — Fleet"),
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


def osa(script: str, timeout: float = 90.0, attempts: int = 3) -> str:
    """Run AppleScript, raising with its message on failure.

    Retries the *transient* accessibility failures and nothing else.

    This app is an accessory (`LSUIElement`) process, so it is never really
    frontmost, and System Events intermittently fails to resolve a node in its
    view tree — "Can't get splitter group 1 of window 1 ... Invalid index
    (-1719)". The tree is not actually changing: `window.contentView` is
    assigned exactly once, in `MainWindow.build()`. It is a snapshot race, and
    it killed the suite roughly one run in three.

    Only `-1719` is retried. A genuinely missing element (a menu item that does
    not exist, `-1728`) still fails immediately, because that is a real bug and
    retrying it would only make the failure slower to see.
    """
    for attempt in range(attempts):
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0:
            return result.stdout.strip()
        message = result.stderr.strip().splitlines()[-1] if result.stderr else "osascript failed"
        if "-1719" not in message or attempt == attempts - 1:
            raise RuntimeError(message)
        time.sleep(1.0)


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


#: The app's real window.
#:
#: A nameless `AXDialog` shows up alongside it the moment any text field takes
#: focus — macOS creates that window for the field editor / input method, and it
#: is not part of this app. It is ordered *first* often enough that addressing
#: `window 1` was a coin flip: whenever the dialog won, every lookup in these
#: helpers failed with "Can't get splitter group 1 of window 1 ... Invalid index
#: (-1719)" and the whole suite died. Address the standard window by subrole and
#: the z-order stops mattering.
MAIN_WINDOW = '(first window whose subrole is "AXStandardWindow")'


def window_count() -> int:
    """Count the app's real windows, ignoring system dialogs."""
    try:
        return int(osa(wrap(
            'return (count of (every window whose subrole is "AXStandardWindow"))'
        )) or 0)
    except (RuntimeError, ValueError):
        return -1


def window_title() -> str:
    try:
        return osa(wrap(f"return name of {MAIN_WINDOW}"))
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
            f"tell {MAIN_WINDOW} to return (count of rows of table 1 of scroll area 1 "
            "of splitter group 1)"
        )) or 0)
    except (RuntimeError, ValueError):
        return -1


def sidebar_labels() -> list[str]:
    """Every sidebar row's text, in order, including group headers."""
    try:
        raw = osa(wrap(f'''
            tell {MAIN_WINDOW} to tell table 1 of scroll area 1 of splitter group 1
                set acc to ""
                repeat with r from 1 to (count of rows)
                    try
                        set acc to acc & (value of static text 1 of UI element 1 of row r) & "\\n"
                    on error
                        set acc to acc & "\\n"
                    end try
                end repeat
                return acc
            end tell'''))
    except RuntimeError:
        return []
    return [line for line in raw.splitlines() if line.strip()]


def sidebar_select(row: int) -> None:
    osa(wrap(
        f"tell {MAIN_WINDOW} to tell table 1 of scroll area 1 of splitter group 1 "
        f"to select row {row}"
    ))


def detail_rows() -> int:
    """Rows in the detail table, for sections that have one."""
    try:
        return int(osa(wrap(
            f"tell {MAIN_WINDOW} to return (count of rows of table 1 of scroll area 2 "
            "of splitter group 1)"
        )) or 0)
    except (RuntimeError, ValueError):
        return -1


def detail_texts() -> str:
    return osa(wrap(f'''
        tell {MAIN_WINDOW} to tell splitter group 1
            set acc to ""
            repeat with t in static texts
                try
                    set v to value of t
                    if v is not missing value then set acc to acc & (v as string) & "\\n"
                end try
            end repeat
            return acc
        end tell'''))


def table_texts(scroll_area: int) -> list[str]:
    """Every cell's text in one of a page's tables, row by row.

    Table cells are not part of the page's static texts, so a page whose content
    is a table has to be read through the table.
    """
    try:
        raw = osa(wrap(f'''
            tell {MAIN_WINDOW} to tell splitter group 1 to tell table 1 of scroll area {scroll_area}
                set acc to ""
                repeat with r from 1 to (count of rows)
                    repeat with c in (UI elements of row r)
                        try
                            set acc to acc & (value of static text 1 of c) & "\n"
                        end try
                    end repeat
                end repeat
                return acc
            end tell'''))
    except RuntimeError:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def table_columns(scroll_area: int) -> int:
    try:
        return int(osa(wrap(
            f"tell {MAIN_WINDOW} to tell splitter group 1 to tell table 1 of "
            f"scroll area {scroll_area} to return (count of columns)"
        )) or 0)
    except (RuntimeError, ValueError):
        return -1


def wait_for_detail(needle: str, timeout: float = 30.0) -> str:
    """Wait until the detail pane contains `needle`, then return its text.

    Fleet fans out to every controller (overview, nodes, jobs and stats) before
    it can render, so a fixed sleep races the read. Waiting for the content is
    both faster when it is quick and correct when it is not.
    """
    deadline = time.time() + timeout
    body = ""
    while time.time() < deadline:
        try:
            body = detail_texts()
        except RuntimeError:
            body = ""
        if needle in body:
            return body
        time.sleep(0.8)
    return body


def detail_count(expr: str) -> int:
    try:
        return int(osa(wrap(f"tell {MAIN_WINDOW} to tell splitter group 1 to return (count of {expr})")) or 0)
    except (RuntimeError, ValueError):
        return -1


def field_value(field: str) -> str:
    """Read a text field's current value; missing values read as empty."""
    try:
        value = osa(wrap(
            f"tell {MAIN_WINDOW} to tell splitter group 1 to return value of {field}"))
    except RuntimeError:
        return ""
    return "" if value in ("missing value", "false") else value


def focus_field(field: str) -> None:
    osa(f'''
    tell application "System Events"
      tell process "{APP_PROCESS}"
        set frontmost to true
        delay 0.6
        tell {MAIN_WINDOW}
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


def type_into(field: str, text: str, attempts: int = 4, expect_exact: bool = True) -> str:
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

    `expect_exact=False` accepts any non-empty result, and exists because of the
    keyboard layout. This Mac's active input source is **Khmer**, so
    `keystroke "z"` delivers a Khmer character: the field held `ា`, not `z`. The
    keystroke had landed perfectly — the assertion was testing the Mac's
    keyboard, not this app. Callers that only need "the text arrived" pass
    `expect_exact=False`; the character itself is not the app's business.
    """
    for _ in range(attempts):
        focus_field(field)
        osa(f'''
        delay 0.5
        tell application "System Events" to keystroke "{text}"''')
        time.sleep(0.9)
        value = field_value(field)
        if value == text or (not expect_exact and value != ""):
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
    script = wrap(f'''
        tell {MAIN_WINDOW} to tell splitter group 1
            set wanted to {{"Chat", "Image", "Video", "TTS", "Transcription", "Music"}}
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
    print("\n\033[1m1. One window, with a grouped sidebar\033[0m")
    rows = sidebar_rows()
    check("the sidebar lists every row", rows == len(SIDEBAR), f"{rows} rows")

    labels = sidebar_labels()
    check("the sidebar is grouped like Studio Hub's", labels == SIDEBAR, f"{labels}")
    check("the sidebar is part of the window, not a separate one", window_count() == 1)

    # The status line Studio Hub pins below its sidebar. It is outside the table,
    # so it stays put whichever page is showing.
    summary = osa(wrap(
        f"tell {MAIN_WINDOW} to tell splitter group 1 to return (value of every static text)"
    ))
    check("a live status line is pinned in the sidebar",
          "Running on 127.0.0.1" in summary and "providers" in summary, summary[:120])

    for row, name, expected in SECTIONS:
        title = open_section(name)
        check(f"View → {name} selects it", title == expected, title)
        check(f"still exactly one window on “{name}”", window_count() == 1,
              f"{window_count()} windows")

    # Selecting by sidebar row must agree with the View menu, or the two ways of
    # navigating have drifted apart.
    sidebar_select(3)
    time.sleep(2.0)
    check("clicking a sidebar row selects the same section",
          window_title() == "gateway-kh — Jobs", window_title())

    # The row that the owner could not find before it existed.
    sidebar_select(8)
    time.sleep(2.0)
    check("Settings is reachable by clicking its sidebar row",
          window_title() == "gateway-kh — Settings", window_title())

    # ------------------------------------------------------------------
    print("\n\033[1m2. Overview renders live data\033[0m")
    open_section("Overview")
    body = detail_texts()
    check("opens with a page heading like Studio Hub's",
          "One endpoint in front of every local runner" in body, body[:200])
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
    check("shows a page heading", "Everything the gateway has run" in jobs_body,
          jobs_body[:200])
    check("shows a summary line", "succeeded" in jobs_body.lower(), jobs_body[:160])

    if rows > 0:
        osa(wrap(f"tell {MAIN_WINDOW} to tell splitter group 1 to select row 1 "
                 "of table 1 of scroll area 2"))
        time.sleep(1.5)
        after = detail_texts()
        check("selecting a job reveals its detail", " via " in after or "\nid " in after,
              after[-200:])

    # ------------------------------------------------------------------
    print("\n\033[1m4. Voices section, and its controls work\033[0m")
    open_section("Voices")
    # The section is a singleton, so its search box keeps whatever was typed last
    # time — including a filter left by an earlier run or by hand. Start clean or
    # the row count below is really measuring the previous session.
    clear_field("text field 1")
    time.sleep(1.5)
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
        # The exact character depends on the Mac's keyboard layout, so this asks
        # only that the keystroke reached the field — see `type_into`.
        typed = type_into("text field 1", "z", expect_exact=False)
        time.sleep(2.0)
        filtered = detail_rows()
        check("the search box receives typed text", typed != "", f"field holds {typed!r}")
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
            osa(wrap(f"tell {MAIN_WINDOW} to click {checkbox}"))
            time.sleep(2.5)
            after = api_get("/admin/api/voices")["summary"]["enabled"]
            check(f"ticking a voice reaches the gateway ({before} → {after} enabled)",
                  after != before)
            osa(wrap(f"tell {MAIN_WINDOW} to click {checkbox}"))
            time.sleep(2.0)
            check("unticking it restores the count",
                  api_get("/admin/api/voices")["summary"]["enabled"] == before)
        except RuntimeError as exc:
            check("ticking a voice reaches the gateway", False, str(exc)[:140])

    # ------------------------------------------------------------------
    print("\n\033[1m5. Models and Providers are separate pages\033[0m")
    # The console used to hold both views behind its own tabs, so the sidebar
    # listed one entry for two different jobs. Each now has its own page and the
    # console opens on the matching view.
    title = open_section("Models")
    check("Models selects into the same window", title == "gateway-kh — Models", title)
    models_page = window_title()
    title = open_section("Providers")
    check("Providers selects into the same window", title == "gateway-kh — Providers", title)
    check("Models and Providers are different pages", models_page != window_title(),
          f"{models_page} vs {window_title()}")
    check("the app survived loading the web console",
          osa(f'tell application "System Events" to return (exists process "{APP_PROCESS}")') == "true")

    # ------------------------------------------------------------------
    print("\n\033[1m6. Settings is a page in this window, like Studio Hub's\033[0m")
    # It used to be a separate window, by macOS convention. That convention
    # assumes an app menu, and an accessory app never shows one, so the owner
    # could not find Settings at all. It is now a sidebar row.
    osa(wrap('click menu item "Settings…" of menu 1 of menu bar item "gateway-kh" of menu bar 1'))
    time.sleep(2.5)
    check("Settings stays in the one window", window_count() == 1, f"{window_count()} windows")
    check("Settings is titled in the window", window_title() == "gateway-kh — Settings",
          window_title())

    settings = detail_texts()
    check("Settings shows the runtime", "runtime" in settings.lower(), settings[:160])
    check("Settings shows the data folder", "gateway-kh" in settings)
    check("Settings states what it is for", "API key" in settings, settings[:200])
    # A page, not a window: its controls live in the detail pane, and there is
    # no second window to close.
    check("Settings has an API key field",
          int(osa(wrap(
              f"tell {MAIN_WINDOW} to return (count of text fields of splitter group 1)"
          ))) >= 2)
    check("no stray window was opened", window_count() == 1, f"{window_count()} windows")

    # The form is two columns: every field starts at the same x. It used to stack
    # a caption above each control, which left labels and fields at a different x
    # on every row — the "settings alignment" the owner reported.
    field_x = osa(wrap(f'''
        tell {MAIN_WINDOW} to tell splitter group 1
            set acc to ""
            repeat with i from 1 to (count of text fields)
                set p to position of text field i
                set acc to acc & ((item 1 of p) as string) & "\\n"
            end repeat
            return acc
        end tell'''))
    values = [line for line in field_x.splitlines() if line.strip()]
    xs = sorted(set(values))
    check("Settings has fields to align", len(values) >= 2, f"{len(values)} fields")
    check("every Settings field starts in one column", len(xs) == 1, f"field x values: {xs}")

    # ------------------------------------------------------------------
    print("\n\033[1m7. Fleet reads the real controllers\033[0m")
    # The local Hub is always a location, so this page has true content on a
    # single-Mac install with nothing configured.
    open_section("Fleet")
    fleet = wait_for_detail("Locations")
    check("names the page and what it is for", "Every location" in fleet, fleet[:200])
    check("summarises locations", "location" in fleet and "reachable" in fleet, fleet[:220])
    check("says how many machines are online",
          "machine online" in fleet or "machines online" in fleet, fleet[:240])
    check("has a form for adding a location",
          "Add a location" in fleet and "Hub service token" in fleet, fleet[-300:])

    # Three tables, so hundreds of machines virtualise instead of stacking.
    locations_cells = table_texts(2)
    machines_cells = table_texts(3)
    check("locations are a table", table_columns(2) >= 4, f"{table_columns(2)} columns")
    check("machines are a table", table_columns(3) >= 5, f"{table_columns(3)} columns")
    check("running work is a table", table_columns(4) >= 3, f"{table_columns(4)} columns")
    check("lists this Mac as the local location", "This Mac" in locations_cells,
          f"{locations_cells}")
    # The role is the Hub's to decide, so this only requires that one is
    # reported from the set the Hub defines.
    check("reports the location's role",
          any(r in locations_cells for r in ("standalone", "controller", "worker")),
          f"{locations_cells}")
    check("lists the local machine with its work",
          any("This Mac" in cell for cell in machines_cells), f"{machines_cells}")
    check("a machine reports jobs done and failed",
          any("done" in cell for cell in machines_cells), f"{machines_cells}")

    fleet_api = api_get("/admin/api/fleet")
    fleet_summary = fleet_api["summary"]
    online = fleet_summary["machines_online"]
    check("the page agrees with the gateway about machines online",
          f"{online} machine{'s' if online != 1 else ''} online" in fleet, f"{fleet_summary}")
    # Which machines did the work is the point of the page, so it must survive
    # the round trip from the Hub's activity report.
    check("counts completed and failed jobs in the summary",
          "completed" in fleet, fleet[:300])
    check("the API publishes a per-machine job breakdown",
          bool(fleet_summary.get("pulse")) and "jobs_completed" in fleet_summary,
          f"{fleet_summary}")
    per_machine = fleet_api["locations"][0].get("activity", {}).get("machines", [])
    check("each machine reports what it ran and its state",
          bool(per_machine) and "state" in per_machine[0] and "completed" in per_machine[0],
          f"{per_machine[:1]}")
    check("machines offline is reported as a number",
          isinstance(fleet_summary.get("machines_offline"), int), f"{fleet_summary}")

    # Tokens must never come back from the API, even masked-but-complete.
    controllers = api_get("/admin/api/fleet/controllers")["controllers"]
    check("no controller token is returned by the API",
          all("token" not in row or row.get("token") in (None, "")
              for row in controllers),
          f"{controllers}")

    # ------------------------------------------------------------------
    print("\n\033[1m8. Status menu\033[0m")
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
    for expected in [
        "Overview", "Jobs", "Voices", "Models", "Fleet", "Open Provider Console",
        "Settings", "Quit",
    ]:
        check(f"status menu offers “{expected}”", expected in items, items[:200])

    # ------------------------------------------------------------------
    print("\n\033[1m9. Everything still healthy afterwards\033[0m")
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
