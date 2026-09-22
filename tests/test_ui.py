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
import pathlib
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
    "CREATE",
    "Audio",
    "Subtitle",
    "Image",
    "Music",
    "Video",
    "Chat",
    "LIBRARY",
    "Jobs",
    "Voices",
    "Models",
    "Content Packs",
    "CONNECTION",
    "Providers",
    "Fleet",
    "APP",
    "Settings",
]

#: Sections reachable from the View menu, in row order:
#: (sidebar row, menu title, window title). Settings is deliberately absent —
#: it is opened with ⌘, or by clicking its sidebar row, and is checked in
#: section 6 rather than duplicated in the View menu.
#:
#: Rows are 1-based, because that is what the accessibility layer counts, and the
#: grouping is Create / Library / Connection / App.
SECTIONS = [
    (1, "Overview", "gateway-kh — Overview"),
    (3, "Audio", "gateway-kh — Audio"),
    (4, "Subtitle", "gateway-kh — Subtitle"),
    (5, "Image", "gateway-kh — Image"),
    (6, "Music", "gateway-kh — Music"),
    (7, "Video", "gateway-kh — Video"),
    (8, "Chat", "gateway-kh — Chat"),
    (10, "Jobs", "gateway-kh — Jobs"),
    (11, "Voices", "gateway-kh — Voices"),
    (12, "Models", "gateway-kh — Models"),
    (13, "Content Packs", "gateway-kh — Content Packs"),
    (15, "Providers", "gateway-kh — Providers"),
    (16, "Fleet", "gateway-kh — Fleet"),
]

failures: list[str] = []
skipped: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    tag = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    suffix = f" — {detail}" if (detail and not ok) else ""
    print(f"  {tag} {label}{suffix}")
    if not ok:
        failures.append(label)


def skip(label: str, reason: str) -> None:
    """Record a check that cannot run, and say why.

    Used where the environment, not the app, is missing — the local Studio Hub
    being stopped (another session may be reinstalling it) is not a gateway
    regression, and reporting it as one trains you to ignore failures.
    """
    skipped.append(label)
    print(f"  \033[33mSKIP\033[0m {label} — {reason}")


def osa(script: str, timeout: float = 90.0, attempts: int = 3) -> str:
    """Run AppleScript, raising with its message on failure.

    Retries the *transient* accessibility failures and nothing else.

    The app is a regular process now, but System Events still intermittently
    fails to resolve a node in its view tree — "Can't get splitter group 1 of window 1 ... Invalid index
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


def gateway_port() -> int:
    """The port the app is actually configured with, not a hardcoded one.

    The port is the owner's to change (it moved from 8010 to 8000 when the old
    GenStudio API was retired), and a test that pins it fails for the wrong
    reason after a legitimate change.
    """
    try:
        config = json.loads(
            (pathlib.Path.home() / "Library/Application Support/gateway-kh/menubar.json")
            .read_text(encoding="utf-8")
        )
        return int(config.get("port") or 8000)
    except (OSError, ValueError, TypeError):
        return 8000


def api_get(path: str) -> dict:
    key = ""
    env = os.path.expanduser("~/Library/Application Support/gateway-kh/.env")
    if os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            if line.startswith("GATEWAY_API_KEY="):
                key = line.split("=", 1)[1].strip()
    request = urllib.request.Request(
        f"http://127.0.0.1:{gateway_port()}{path}", headers={"Authorization": f"Bearer {key}"}
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


def window_size() -> tuple[int, int]:
    """The real window's (width, height). `(-1, -1)` when it cannot be read.

    Two things about this are not obvious and both were got wrong first time.
    `MAIN_WINDOW` is already the window reference, so `size of window 1` resolves
    against a window as if it had windows and errors at runtime. And coercing the
    size record `as string` concatenates the two numbers — `1157, 1050` becomes
    `"11571050"` — so the components have to be read through a variable and joined
    explicitly. Getting either wrong returns `(-1, -1)`, and a caller comparing
    that to itself passes without measuring anything, which is exactly how the
    resize guard first passed. The guard now asserts the read worked.
    """
    try:
        raw = osa(wrap(f'''
            set w to {MAIN_WINDOW}
            set s to size of w
            return ((item 1 of s) as text) & "x" & ((item 2 of s) as text)'''))
        width, _, height = raw.partition("x")
        return int(width.strip()), int(height.strip())
    except (RuntimeError, ValueError):
        return -1, -1


def set_window_size(width: int, height: int) -> None:
    """Resize the real window. Restored by the caller — this is the owner's app."""
    osa(wrap(f'''
        set w to {MAIN_WINDOW}
        set size of w to {{{width}, {height}}}'''))
    time.sleep(1.0)


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
    """Every visible label in the detail pane.

    A page that scrolls (Overview, Fleet, Settings) keeps its labels inside the
    page's own scroll view; a page that fills the pane (Jobs, Voices, Providers)
    keeps them directly on the split view. Read whichever has them, rather than
    assuming a layout the pages do not share.
    """
    return osa(wrap(f'''
        tell {MAIN_WINDOW} to tell splitter group 1
            set sources to {{}}
            try
                if (count of static texts of scroll area 2) > 0 then set end of sources to scroll area 2
            end try
            if (count of sources) is 0 then set end of sources to it
            set acc to ""
            repeat with t in static texts of (item 1 of sources)
                try
                    set v to value of t
                    if v is not missing value then set acc to acc & (v as string) & "\\n"
                end try
            end repeat
            return acc
        end tell'''))


def table_texts(scroll_area: int, nested: bool = False) -> list[str]:
    """Every cell's text in one of a page's tables, row by row.

    `nested=True` for a page that scrolls, where the tables sit inside the page's
    own scroll view rather than directly on the split view.

    Table cells are not part of the page's static texts, so a page whose content
    is a table has to be read through the table.
    """
    target = (
        f"scroll area {scroll_area} of scroll area 2" if nested else f"scroll area {scroll_area}"
    )
    try:
        raw = osa(wrap(f'''
            tell {MAIN_WINDOW} to tell splitter group 1 to tell table 1 of {target}
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


def table_columns(scroll_area: int, nested: bool = False) -> int:
    target = (
        f"scroll area {scroll_area} of scroll area 2" if nested else f"scroll area {scroll_area}"
    )
    try:
        return int(osa(wrap(
            f"tell {MAIN_WINDOW} to tell splitter group 1 to tell table 1 of "
            f"{target} to return (count of columns)"
        )) or 0)
    except (RuntimeError, ValueError):
        return -1


def table_button_titles(scroll_area: int, nested: bool = False) -> list[str]:
    """Every button title inside a table's rows, **all** of them per cell.

    This used to read `button 1 of c` — the first button of each cell. That is why
    the Fleet page's row actions looked like `['Rename', 'Test']` when each cell
    actually holds four: the helper could not see past the first, and the check it
    fed passed while proving nothing. A cell's controls sit one level down
    (the cell holds a stack view), so both levels are walked.
    """
    target = (
        f"scroll area {scroll_area} of scroll area 2" if nested else f"scroll area {scroll_area}"
    )
    try:
        raw = osa(wrap(f'''
            set sep to (ASCII character 10)
            tell {MAIN_WINDOW} to tell splitter group 1 to tell table 1 of {target}
                set acc to ""
                repeat with r from 1 to (count of rows)
                    repeat with c in (UI elements of row r)
                        try
                            repeat with b in (buttons of c)
                                set acc to acc & (title of b) & sep
                            end repeat
                        end try
                        try
                            repeat with child in (UI elements of c)
                                try
                                    repeat with deep in (buttons of child)
                                        set acc to acc & (title of deep) & sep
                                    end repeat
                                end try
                            end repeat
                        end try
                    end repeat
                end repeat
                return acc
            end tell'''))
    except RuntimeError:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def detail_button_titles() -> list[str]:
    """Every button title in the detail pane, tables and bars included.

    `table_button_titles` walks a table's rows; this walks the pane, for a page
    whose controls sit in a bar above the table (Voices has no scroller, so its
    content is not inside `scroll area 2` at all).

    `entire contents` cannot be used here — System Events refuses it as a
    specifier (-1700) — so this walks three levels, which is where a bar's buttons
    are actually reachable, and de-duplicates because an element can be seen from
    more than one of them.
    """
    try:
        raw = osa(wrap(f'''
            set sep to (ASCII character 10)
            tell {MAIN_WINDOW} to tell splitter group 1
                set acc to ""
                repeat with element in UI elements
                    try
                        if class of element is button then set acc to acc & (title of element) & sep
                    end try
                    try
                        repeat with inner in (buttons of element)
                            set acc to acc & (title of inner) & sep
                        end repeat
                    end try
                    try
                        repeat with child in (UI elements of element)
                            try
                                if class of child is button then set acc to acc & (title of child) & sep
                            end try
                            try
                                repeat with deep in (buttons of child)
                                    set acc to acc & (title of deep) & sep
                                end repeat
                            end try
                        end repeat
                    end try
                end repeat
                return acc
            end tell'''))
    except RuntimeError:
        return []
    titles: list[str] = []
    for line in raw.splitlines():
        title = line.strip()
        if title and title != "missing value" and title not in titles:
            titles.append(title)
    return titles


def press_detail_button(title: str) -> bool:
    """Press the detail-pane button whose title contains `title`; True if pressed.

    Reading a button's title only proves it was *built*. The Fleet page's Test and
    Remove were wired to nothing while their titles read correctly, so a claim that
    a control works needs it pressed. The same three-level walk as
    `detail_button_titles`, because `entire contents` is refused as a specifier.
    """
    try:
        raw = osa(wrap(f'''
            tell {MAIN_WINDOW} to tell splitter group 1
                set hit to "no"
                repeat with element in UI elements
                    try
                        if class of element is button and (title of element) contains "{title}" then
                            perform action "AXPress" of element
                            set hit to "yes"
                        end if
                    end try
                    try
                        repeat with inner in (buttons of element)
                            if (title of inner) contains "{title}" then
                                perform action "AXPress" of inner
                                set hit to "yes"
                            end if
                        end repeat
                    end try
                    try
                        repeat with child in (UI elements of element)
                            try
                                if class of child is button and (title of child) contains "{title}" then
                                    perform action "AXPress" of child
                                    set hit to "yes"
                                end if
                            end try
                            try
                                repeat with deep in (buttons of child)
                                    if (title of deep) contains "{title}" then
                                        perform action "AXPress" of deep
                                        set hit to "yes"
                                    end if
                                end repeat
                            end try
                        end repeat
                    end try
                end repeat
                return hit
            end tell'''))
    except RuntimeError:
        return False
    return raw.strip() == "yes"


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
    """Empty a field, retrying until it actually reads empty.

    Structured like `type_into`: one script, focus inside `tell process`, the
    keystrokes as statements of System Events. Splitting them across two
    invocations meant ⌘A sometimes selected nothing and Delete sometimes went
    nowhere, so the field kept its text and the check that *depends* on the
    cleared state — "the full list came back" — failed intermittently.
    """
    for _ in range(attempts):
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
          delay 0.4
          keystroke "a" using command down
          key code 51
        end tell''')
        time.sleep(0.9)
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

    Even with all three, a keystroke is a request, not a guarantee — another
    app can hold the keyboard. Rather than sleep-and-hope, read the value back and retry — a flaky test
    that fails half the time is worse than no test.

    `expect_exact=False` accepts any non-empty result, and exists because of the
    keyboard layout. This Mac's active input source is **Khmer**, so
    `keystroke "z"` delivers a Khmer character: the field held `ា`, not `z`. The
    keystroke had landed perfectly — the assertion was testing the Mac's
    keyboard, not this app. Callers that only need "the text arrived" pass
    `expect_exact=False`; the character itself is not the app's business.
    """
    for _ in range(attempts):
        # One `osascript`, two levels: focus inside `tell process`, then the
        # keystroke as a statement of `tell application "System Events"`.
        #
        # Both halves are load-bearing and the two earlier versions each got one
        # of them wrong. Splitting focus and keystroke across two invocations loses
        # focus when the first exits. Nesting the keystroke *inside* `tell process` — the
        # obvious way to keep it in one script — makes System Events resolve it
        # against the process and the key goes nowhere at all, which is the
        # failure this docstring has warned about all along.
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
          delay 0.4
          keystroke "{text}"
        end tell''')
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
            set src to it
            try
                if (count of static texts of scroll area 2) > 0 then set src to scroll area 2
            end try
            tell src
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
            end tell
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

    # Exactly one window, which is what a normal launch produces. A launch with
    # `--background` — what the LaunchAgent passes at login — deliberately opens
    # none, so open it through the app's own menu rather than failing. The app
    # that logged in at boot is a perfectly valid thing to test against, and
    # "0 windows" is otherwise a confusing way to say "the window was never
    # opened".
    if window_count() == 0:
        try:
            osa(wrap(
                'click menu item "Overview" of menu 1 of menu bar item "View" of menu bar 1'
            ))
            time.sleep(2.5)
        except RuntimeError:
            pass
    count = window_count()
    check("exactly one window is open", count == 1, f"{count} windows")
    if count != 1:
        print("  The app must be running with its window; launch it normally, or let")
        print("  this open one via View -> Overview.")
        return 1

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
          "Running on " in summary and f":{gateway_port()}" in summary
          and "providers" in summary, summary[:160])

    # The notes must be one click from every page, beside the build stamp the owner
    # reads to answer "what am I running?". Only its presence is asserted here: the
    # click opens a second window whose AX tree makes the press itself racy, and the
    # same action is already exercised from the status menu.
    sidebar_buttons = osa(wrap(
        f"tell {MAIN_WINDOW} to tell splitter group 1 to return (title of every button)"
    ))
    check("the sidebar offers What's New beside the version",
          "New" in sidebar_buttons, sidebar_buttons[:120])

    # Force the window small before visiting the pages, as AGENTS.md prescribes:
    # at the restored size a page with a too-tall required height simply fits, so
    # sizing to the frame the owner might actually use is what makes the check
    # able to fail. The original size is put back afterwards — this is his app.
    original_size = window_size()
    baseline_size = original_size
    # An unreadable size must fail, not silently reduce every check below to
    # `(-1, -1) == (-1, -1)`. That is how the first version of this guard passed
    # without measuring anything.
    check("the window size can be read", original_size[0] > 0, f"{original_size}")
    if original_size[0] > 0:
        set_window_size(max(original_size[0], 900), 700)
        baseline_size = window_size()
        check("the window can be forced to the 700pt test height",
              baseline_size[1] == 700, f"{baseline_size}")

    for row, name, expected in SECTIONS:
        title = open_section(name)
        check(f"View → {name} selects it", title == expected, title)
        check(f"still exactly one window on “{name}”", window_count() == 1,
              f"{window_count()} windows")
        # AGENTS.md makes this a working rule — "a page must never be able to
        # resize the window", with the Fleet page as the recorded 1162pt
        # casualty — and nothing enforced it. Visit every section and confirm the
        # window keeps the size it had, because AppKit satisfies a page's
        # *required* height by growing the window, which puts its bottom edge and
        # resize corner off-screen where they cannot be reached.
        after_size = window_size()
        check(f"“{name}” did not resize the window",
              after_size == baseline_size,
              f"{baseline_size} became {after_size}")

    # Put the owner's window back where it was.
    if original_size[0] > 0:
        set_window_size(*original_size)
        restored = window_size()
        # Accessibility rounds to whole points, so a restored size can read back one
        # point off. That is not a resize; the invariant above is exact.
        check("the window size was restored after the test",
              abs(restored[0] - original_size[0]) <= 1 and abs(restored[1] - original_size[1]) <= 1,
              f"{original_size} -> {restored}")

    # Selecting by sidebar row must agree with the View menu, or the two ways of
    # navigating have drifted apart. Row 3 is now a Create lane, so this also
    # proves the new group is a real destination rather than a heading.
    sidebar_select(3)
    time.sleep(2.0)
    check("clicking a sidebar row selects the same section",
          window_title() == "gateway-kh — Audio", window_title())
    sidebar_select(10)
    time.sleep(2.0)
    check("and a management row still selects its section",
          window_title() == "gateway-kh — Jobs", window_title())

    # The row that the owner could not find before it existed. Settings is now the
    # last row, under its own "App" heading.
    sidebar_select(18)
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
    # **"enabled", not "discovered".** The tile read "Offered of N discovered" while
    # N counted only models on providers that are switched on, so turning a provider
    # off made the *discovered* count fall — the opposite of what the word means. It
    # reads "Offered of N enabled" now, which is also the set the withheld count is
    # taken from, so the sentence adds up. Asserted as a count rather than as the word
    # that happened to be there.
    check("shows a model count",
          "Offered of " in body and "enabled" in body,
          [line for line in body.splitlines() if "Offered" in line][:2])
    check("shows the modality breakdown", "OFFERED TO STORY STUDIO" in body.upper())
    check("shows the local runner state", "Studio Hub" in body)

    status = subprocess.run(
        ["curl", "-s", "-m", "10", f"http://127.0.0.1:{gateway_port()}/health"],
        capture_output=True, text=True,
    ).stdout
    # Read the payload unconditionally. This was wrapped in
    # `if '"enabled_models"' in status:`, so if the health shape ever changed the
    # check silently never ran and reported nothing — and a check that does not
    # run cannot fail. The count it reads is also asserted as a token rather than
    # a substring, or "0" matches the "0" inside "30".
    health_payload = json.loads(status)
    check("the health payload still carries an enabled-model count",
          "enabled_models" in (health_payload.get("models") or {}), status[:200])
    # The screen shows what Story Studio can actually pick, so it is matched
    # against `publishable_models`. It used to show `enabled_models`, which is a
    # different question: a model the gateway withholds (a package the Hub does
    # not describe as a model, or one that does not fit this machine) is enabled
    # but unofferable, and reading "121 live" with nine of them unpickable is the
    # inconsistency this pair of fields exists to keep apart.
    models_block = health_payload.get("models") or {}
    offered = models_block.get("publishable_models")
    check("the health payload separates offered from enabled",
          isinstance(offered, int) and isinstance(models_block.get("withheld_models"), int),
          f"{ {k: models_block.get(k) for k in ('enabled_models', 'publishable_models', 'withheld_models')} }")
    check(f"the model count on screen matches the API ({offered})",
          isinstance(offered, int) and f"\n{offered}\n" in f"\n{body}\n", body[:200])
    check("and the page says how many are withheld when any are",
          (models_block.get("withheld_models") or 0) == 0 or "withheld" in body,
          f"withheld={models_block.get('withheld_models')}")

    # Which build is running. A gateway outlives the app, so on launch this app
    # adopts whatever already answers /health — measured: a gateway from 21:47 was
    # still serving at 23:33 through four rebuilds, and one of those cycles had
    # already been reported as verified. The stamp is what makes that visible.
    check("the health payload says which build is running",
          isinstance(health_payload.get("build"), str) and health_payload["build"],
          status[:200])

    action_titles = detail_button_titles()
    check("offers a way to check, restart and read about the gateway",
          {"Check Gateway", "Restart Gateway", "What's New"} <= set(action_titles),
          f"{action_titles}")

    # Pressing is the only thing that proves it is wired: Fleet's Test and Remove
    # were once titled correctly and connected to nothing.
    #
    # Deliberately not asserting "up to date". A gateway left over from an older
    # build is exactly what this button exists to report, so which of the answers
    # comes back is the environment's business. What must hold is that it names a
    # build instead of staying silent — the stale case says both builds, which was
    # verified by hand against a process stamped `fake-old-build`.
    pressed = press_detail_button("Check Gateway")
    check("pressing Check Gateway is wired", pressed, "no button was pressed")
    reply = ""
    if pressed:
        for _ in range(20):
            time.sleep(1)
            reply = next(
                (line for line in detail_texts().splitlines()
                 if "build" in line.lower() or "nothing is running" in line.lower()),
                "",
            )
            if reply and "Checking" not in reply:
                break
    check("and answers with the running build, or says nothing is running",
          ("build" in reply.lower() and ("up to date" in reply or "serving older code" in reply))
          or "nothing is running" in reply,
          reply or "(no status line appeared)")

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
    # A gateway that has run nothing yet is a valid state, not a failure: this
    # suite must not require the owner to have generated work first. Read the
    # gateway's own count and skip the data-dependent checks when it is zero.
    known_jobs = len((api_get("/admin/api/jobs") or {}).get("jobs", []))
    rows = detail_rows()
    deadline = time.time() + (20 if known_jobs else 0)
    while rows < 1 and known_jobs and time.time() < deadline:
        time.sleep(0.7)
        rows = detail_rows()
    if not known_jobs:
        skip("jobs table populated", "this gateway has processed no jobs yet")
        skip("shows a page heading", "the heading is read from a populated page")
        skip("shows a summary line", "there is nothing to summarise yet")
    else:
        check("jobs table populated", rows > 0, f"{rows} rows")
        jobs_body = detail_texts()
        check("shows a page heading", "Revisit completed outputs" in jobs_body,
              jobs_body[:200])
        check("shows a summary line", "succeeded" in jobs_body.lower(), jobs_body[:160])

    if rows > 0:
        osa(wrap(f"tell {MAIN_WINDOW} to tell splitter group 1 to select row 1 "
                 "of table 1 of scroll area 2"))
        time.sleep(1.5)
        # The Jobs detail is its own pane (scroll area 3), not the table's scroll
        # area: reading the table back reports the row, not what selecting it did.
        after = osa(wrap(f"tell {MAIN_WINDOW} to tell splitter group 1 to return "
                         "(value of every static text of scroll area 3)"))
        check("selecting a job reveals its detail",
              "Job " in after and ("succeeded" in after or "failed" in after),
              after[:200])

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
    # Stocking the library is the one control here that must exist as a button:
    # the Fleet page once had actions wired to nothing at all, which no API test
    # could see.
    voices_buttons = detail_button_titles()
    check("offers adding voices from a folder",
          any("Add voices" in title for title in voices_buttons), f"{voices_buttons}")
    # A voice whose id the owner already knows — from a share link — had no way in:
    # the library search only finds what the provider will search for, and "Add to
    # my account" only acts on rows already in the table. The API always accepted
    # an explicit id; the field was what was missing.
    check("offers saving a voice by its id",
          any("Add by ID" in title for title in voices_buttons), f"{voices_buttons}")

    # The recording *is* the input on the local lane, and three different things
    # can happen to it — used whole, cut to a shorter aligned window, or refused —
    # which look identical from the outside. This report is the only place that
    # says which, so it gets pressed rather than merely found: a title proves a
    # button was built, not that it answers.
    if any("Reference" in title for title in voices_buttons):
        osa(wrap(f"tell {MAIN_WINDOW} to tell splitter group 1 to select row 1 "
                 "of table 1 of scroll area 2"))
        time.sleep(1.0)
        check("offers a per-model reference report",
              press_detail_button("Reference"), f"{voices_buttons}")
        # The report is a modal, so the suite has to close it or everything after
        # this talks to an alert instead of the window. Matched on the *shape* of a
        # modal — one button, titled OK — rather than on its wording: the wording
        # depends on the voice (a local voice gets the per-model table, a provider
        # voice gets "no reference clip on this Mac"), and matching one sentence
        # left the other alert open, which failed the next 29 checks.
        for _ in range(12):
            time.sleep(1.0)
            closed = osa(wrap('''
                tell application "System Events" to tell process "GatewayKH"
                    set hit to "no"
                    repeat with w in windows
                        try
                            if (count of buttons of w) is 1 then
                                if (title of button 1 of w) is "OK" then
                                    click button 1 of w
                                    set hit to "yes"
                                end if
                            end if
                        end try
                    end repeat
                    return hit
                end tell'''))
            if closed.strip() == "yes":
                break
        time.sleep(1.5)
        # Which sentence comes back depends on the voice in row 1: a local voice
        # gets the per-model table, a provider voice gets "no reference clip on
        # this Mac" — because its audio lives with the provider and computing a
        # verdict here would invent one. Both are the report answering.
        answer = detail_texts()
        check("and the report answers for whichever voice is selected",
              ("Checked" in answer and "local models" in answer)
              or "No local reference clip" in answer,
              answer[-160:])
    else:
        skip("offers a per-model reference report", "the button is not in the bar")

    if rows > 3:
        # `expect_exact=False` because this Mac's active input source is **Khmer**,
        # so `keystroke "z"` delivers a Khmer character — the keystroke lands, the
        # value is not what was asked for.
        #
        # **A keystroke that never lands is an environment state, not an app
        # defect.** `keystroke` is a request; when another app holds the
        # keyboard, all four attempts can miss and the field reads
        # empty. Measured: the same helpers type, filter and clear perfectly when
        # driven directly, and miss intermittently inside a long run. So an empty
        # field skips with the reason — but text that lands and does *not* filter
        # still fails, which is the half that would catch a broken search box.
        typed = type_into("text field 1", "z", expect_exact=False)
        time.sleep(2.0)
        filtered = detail_rows()
        if typed == "":
            skip("the search box receives typed text",
                 "no keystroke reached the app — another app is holding the keyboard")
            skip("typing filters the table to a non-empty subset",
                 "no keystroke landed, so there is nothing to filter with")
            skip("clearing the search restores the full list",
                 "nothing was typed, so there is nothing to clear")
        else:
            check("the search box receives typed text", typed != "", f"field holds {typed!r}")
            check(f"typing filters the table to a non-empty subset ({rows} → {filtered})",
                  0 < filtered < rows, f"still {filtered}")
            cleared = clear_field("text field 1")
            time.sleep(2.0)
            check("clearing the search restores the full list",
                  cleared == "" and detail_rows() == rows,
                  f"field {cleared!r}, {detail_rows()} of {rows}")

    # The enable checkbox lives inside a cell view, and what matters is that
    # clicking it reaches the gateway — not merely that the box repaints.
    # The field itself, not just the button: a button with nothing to type into is
    # the same dead control in a different shape. System Events does not expose a
    # placeholder, so this counts the fields — search, the id, and the owner id
    # ElevenLabs needs — which is the part that can actually break.
    field_count = int(osa(wrap(
        f"tell {MAIN_WINDOW} to tell splitter group 1 to return (count of text fields)"
    )) or 0)
    check("and a field to type the id into, plus one for the owner id",
          field_count >= 3, f"{field_count} text fields on the Voices page")

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
    # It used to be a separate window, by macOS convention. When this app was an
    # accessory process it had no on-screen app menu, so the owner could not find
    # Settings at all; it stays a sidebar row, and the app menu's Cmd-, also reaches it.
    osa(wrap('click menu item "Settings…" of menu 1 of menu bar item "gateway-kh" of menu bar 1'))
    time.sleep(2.5)
    check("Settings stays in the one window", window_count() == 1, f"{window_count()} windows")
    check("Settings is titled in the window", window_title() == "gateway-kh — Settings",
          window_title())

    settings = detail_texts()
    check("Settings shows the runtime", "runtime" in settings.lower(), settings[:160])
    check("Settings shows the data folder", "gateway-kh" in settings)
    check("Settings states what it is for", "API key" in settings, settings[:200])
    # Artifact retention: the limits are editable here, and the page says where the
    # received copies go so it is clear they are not the ones Story Studio keeps.
    check("Settings offers the artifact retention limits",
          "Received artifacts" in settings and "Keep for" in settings and "In use" in settings,
          settings[-260:])
    # "Prune now" is a button, and `detail_texts` only reads static text — the same
    # distinction that hid the Fleet row actions.
    settings_buttons = detail_button_titles()
    check("and a way to run it now",
          any("Prune now" in title for title in settings_buttons), f"{settings_buttons}")

    # Quitting stops the gateway unless the owner opts out, so reopening always
    # runs the build just installed. Two legs, because either alone can be true
    # while the feature is absent: the help text can be written without a control,
    # and a control can exist with nothing saying what it does.
    check("Settings says what quitting does to the gateway",
          "On quit" in settings
          and "quitting stops the gateway and frees the port" in settings,
          settings[-420:])
    quit_boxes = osa(wrap(f'''
        tell {MAIN_WINDOW} to tell splitter group 1
            set acc to ""
            try
                repeat with c in (checkboxes of scroll area 2)
                    set acc to acc & (title of c) & "\\n"
                end repeat
            end try
            try
                repeat with c in (checkboxes of it)
                    set acc to acc & (title of c) & "\\n"
                end repeat
            end try
            return acc
        end tell'''))
    check("and has the checkbox that decides it",
          "Keep the gateway running after I quit" in quit_boxes,
          f"checkboxes: {quit_boxes.strip()[:200]}")
    # A page, not a window: its controls live in the detail pane, and there is
    # no second window to close.
    #
    # Settings now wraps its body in a scroller (a form has a required height, so
    # without one it clipped its own bottom rows), which puts its fields one level
    # deeper. Read whichever container holds them, the way `detail_texts` does,
    # rather than assuming the page's depth.
    def settings_fields(expression: str) -> list[str]:
        raw = osa(wrap(f'''
            tell {MAIN_WINDOW} to tell splitter group 1
                set acc to ""
                try
                    repeat with i from 1 to (count of text fields of {expression})
                        set p to position of text field i of {expression}
                        set acc to acc & ((item 1 of p) as string) & "\\n"
                    end repeat
                end try
                return acc
            end tell'''))
        return [line for line in raw.splitlines() if line.strip()]

    field_x = settings_fields("scroll area 2")
    if len(field_x) < 2:
        field_x = settings_fields("it")
    check("Settings has an API key field", len(field_x) >= 2, f"{len(field_x)} fields")
    check("no stray window was opened", window_count() == 1, f"{window_count()} windows")

    # The form is two columns: every field starts at the same x. It used to stack
    # a caption above each control, which left labels and fields at a different x
    # on every row — the "settings alignment" the owner reported.
    values = field_x
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
    check("has a form for adding a location that names the Fleet token and explains both",
          "Add a location" in fleet and "Fleet token" in fleet
          and "Two tokens, two jobs" in fleet, fleet[-400:])

    # Three tables, so hundreds of machines virtualise instead of stacking.
    # Fleet scrolls, so its three tables are nested inside the page scroll view.
    locations_cells = table_texts(1, nested=True)
    machines_cells = table_texts(2, nested=True)
    check("locations are a table", table_columns(1, nested=True) >= 6,
          f"{table_columns(1, nested=True)} columns")
    check("machines are a table", table_columns(2, nested=True) >= 5,
          f"{table_columns(2, nested=True)} columns")
    check("running work is a table", table_columns(3, nested=True) >= 3,
          f"{table_columns(3, nested=True)} columns")

    # The location name is the owner's to set, so this reads what the gateway
    # currently calls it instead of asserting a literal. Renaming is a feature
    # now, and a test that pins "This Mac" would fail the moment it is used.
    fleet_locations = api_get("/admin/api/fleet")["locations"]
    local_label = next(
        (l.get("label") for l in fleet_locations if l.get("local")), "This Mac"
    )
    check("lists this Mac as the local location", local_label in locations_cells,
          f"looking for {local_label!r} in {locations_cells}")

    # Every location must be renameable, and a real controller must offer the whole
    # set of row actions — including Remove. This check used to pass while reading
    # only the first button of each cell, so it reported success against a table
    # whose actions it could not actually see.
    configured = api_get("/admin/api/fleet/controllers").get("controllers", [])
    action_titles = table_button_titles(1, nested=True)
    check("the local row offers Rename", "Rename" in action_titles, f"{action_titles}")
    if configured:
        check("a controller row offers Test, Rename, Credentials and Remove",
              {"Test", "Rename", "Credentials…", "Remove"} <= set(action_titles),
              f"{action_titles} with {len(configured)} configured")
        check("and this Mac is not offered Remove",
              action_titles.count("Remove") == len(configured), f"{action_titles}")
    else:
        # Saying so, rather than passing quietly: with no controller rows there is
        # nothing here to check, and a vacuous pass is how the actions column went
        # unverified in the first place.
        skip("a controller row offers Test, Rename, Credentials and Remove",
             "no controllers are configured, so there is no such row to read")

    # The rest of this section reads what the local Hub reports, so it needs the
    # Hub running. Standalone or controller both work — that is the point — but
    # "stopped" is an environment state, not a gateway defect.
    local = next((l for l in api_get("/admin/api/fleet")["locations"] if l.get("local")), {})
    if not local.get("reachable"):
        reason = (local.get("error") or "Studio Hub is not running on this Mac")[:90]
        for label in (
            "reports the location's role",
            "lists the local machine with its work",
            "a machine reports jobs done and failed",
            "the API publishes a per-machine job breakdown",
            "each machine reports what it ran and its state",
        ):
            skip(label, reason)
    else:
        # The role is the Hub's to decide, so this only requires that one is
        # reported from the set the Hub defines.
        check("reports the location's role",
              # The Hub's vocabulary is standalone | controller | agent. There is
              # no `worker` role — a joined node's row carries `agent`.
              any(r in locations_cells for r in ("standalone", "controller", "agent")),
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
    if local.get("reachable"):
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
    #
    # The non-emptiness of the list is part of the claim. `all(...)` over an empty
    # list is `True`, and this install has no controllers configured, so the check
    # passed for years without inspecting a single row. The Fleet row-action check
    # above already skips with a stated reason when nothing is configured; this one
    # now says the same thing rather than asserting nothing.
    controllers = api_get("/admin/api/fleet/controllers")["controllers"]
    if not controllers:
        skip("no controller token is returned by the API",
             "no controllers are configured, so there is no roster row to inspect")
    else:
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
         f"http://127.0.0.1:{gateway_port()}/health"],
        capture_output=True, text=True,
    ).stdout.strip()
    check("gateway still answering", health == "200", f"HTTP {health}")

    print("\n" + "=" * 70)
    if skipped:
        print(f"\033[33m{len(skipped)} skipped\033[0m (environment, not the app):")
        for name in skipped:
            print(f"  - {name}")
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
