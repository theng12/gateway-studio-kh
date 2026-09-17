#!/usr/bin/env python3
"""UI tests that actually click.

The e2e suite proves the gateway's contract. This proves the *app*: it drives
the real macOS accessibility layer to open windows through the actual menus,
read what they render, type into fields, tick checkboxes, and press buttons.

That distinction matters. A window can build and lay out perfectly while a
button does nothing, and only clicking finds that.

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
WINDOW_OVERVIEW = "gateway-kh — Overview"
WINDOW_JOBS = "gateway-kh — Jobs"
WINDOW_VOICES = "gateway-kh — Voices"
WINDOW_CONSOLE = "gateway-kh — Provider Console"
WINDOW_SETTINGS = "gateway-kh Settings"

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
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr else "osascript failed")
    return result.stdout.strip()


def wrap(body: str) -> str:
    return f'tell application "System Events" to tell process "{APP_PROCESS}"\n{body}\nend tell'


def focus_app() -> None:
    """Bring the app forward before sending keystrokes.

    This is an accessory (`LSUIElement`) app, so it is never "frontmost" in the
    usual sense. System Events delivers `keystroke` to whatever *is* frontmost,
    so typing silently goes elsewhere and a focused field stays empty — which
    looks exactly like broken search. A real user clicking the field is fine;
    only synthetic input needs this.
    """
    osa(wrap("set frontmost to true"))
    time.sleep(0.6)


def type_into(title: str, field: str, text: str) -> None:
    """Focus a field and type into it, in a single script.

    Three things matter here, and each cost a debugging round:

      * Frontmost does not survive between separate `osascript` invocations —
        the terminal takes focus back each time the process exits — so
        activating, focusing and typing have to happen in one script.
      * `keystroke` must be its own statement against System Events, *not*
        nested inside `tell process`: there the command resolves against the
        process and the key goes nowhere.
      * The target window must be *raised*. Several windows are open by this
        point and whichever is key receives the keys — a web view behind the
        field swallows them silently.
    """
    osa(f'''
    tell application "System Events"
      tell process "{APP_PROCESS}"
        set frontmost to true
        delay 0.6
        tell window "{title}"
          perform action "AXRaise"
          set focused of {field} to true
        end tell
      end tell
    end tell
    delay 0.5
    tell application "System Events" to keystroke "{text}"''')


def clear_field(title: str, field: str) -> None:
    osa(f'''
    tell application "System Events"
      tell process "{APP_PROCESS}"
        set frontmost to true
        delay 0.5
        tell window "{title}"
          perform action "AXRaise"
          set focused of {field} to true
        end tell
      end tell
    end tell
    delay 0.4
    tell application "System Events" to keystroke "a" using command down
    tell application "System Events" to key code 51''')


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


def menu_click(item: str, menu: str = "View") -> None:
    osa(wrap(f'click menu item "{item}" of menu 1 of menu bar item "{menu}" of menu bar 1'))


def window_titles() -> list[str]:
    raw = osa(wrap("return name of windows"))
    return [t.strip() for t in raw.split(",") if t.strip()] if raw else []


def close_all_windows() -> None:
    for title in window_titles():
        try:
            osa(wrap(f'tell window "{title}" to click button 1'))
        except RuntimeError:
            pass
        time.sleep(0.4)


def texts(title: str) -> str:
    return osa(wrap(f'''
        tell window "{title}"
            set acc to ""
            repeat with t in static texts
                try
                    set v to value of t
                    if v is not missing value then set acc to acc & (v as string) & "\\n"
                end try
            end repeat
            return acc
        end tell'''))


def count_of(title: str, expr: str) -> int:
    try:
        raw = osa(wrap(f'tell window "{title}" to return (count of {expr})'))
        return int(raw or 0)
    except (RuntimeError, ValueError):
        return -1


def table_rows(title: str) -> int:
    return count_of(title, "rows of table 1 of scroll area 1")


def wait_for_window(title: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(title == t for t in window_titles()):
            return True
        time.sleep(0.5)
    return False


def wait_for_rows(title: str, minimum: int = 1, timeout: float = 25.0) -> int:
    """Tables fill asynchronously; poll rather than sleeping a fixed time."""
    deadline = time.time() + timeout
    rows = 0
    while time.time() < deadline:
        rows = table_rows(title)
        if rows >= minimum:
            return rows
        time.sleep(0.7)
    return rows


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

    close_all_windows()

    # ------------------------------------------------------------------
    print("\n\033[1m1. Windows open through the real menu\033[0m")
    for item, title in [
        ("Overview", WINDOW_OVERVIEW),
        ("Jobs", WINDOW_JOBS),
        ("Voices", WINDOW_VOICES),
        ("Open Provider Console", WINDOW_CONSOLE),
    ]:
        menu_click(item)
        opened = wait_for_window(title)
        check(f"View → {item} opens “{title}”", opened,
              f"windows now: {window_titles()}")
        time.sleep(1)

    # ------------------------------------------------------------------
    print("\n\033[1m2. Overview renders live data\033[0m")
    body = texts(WINDOW_OVERVIEW)
    check("reports the gateway as running", "Gateway running" in body, body[:120])
    check("shows the endpoint and uptime", "127.0.0.1:" in body and "up " in body)
    check("shows provider counts", "/18" in body)
    check("shows a model count", "discovered" in body)
    check("shows the modality breakdown", "OFFERED TO STORY STUDIO" in body.upper())
    check("shows the local runner state", "Studio Hub" in body)

    # The counts on screen must match what the API reports.
    status = subprocess.run(
        ["curl", "-s", "-m", "10", "http://127.0.0.1:8010/health"],
        capture_output=True, text=True,
    ).stdout
    if '"enabled_models"' in status:
        import json as _json

        enabled = _json.loads(status)["models"]["enabled_models"]
        check(f"the model count on screen matches the API ({enabled})",
              f"\n{enabled}\n" in f"\n{body}\n", body[:200])

    check("Overview has its action buttons",
          count_of(WINDOW_OVERVIEW, "buttons") >= 4,
          str(count_of(WINDOW_OVERVIEW, "buttons")))

    # ------------------------------------------------------------------
    print("\n\033[1m3. Jobs window lists real work\033[0m")
    rows = wait_for_rows(WINDOW_JOBS, 1)
    check("jobs table populated", rows > 0, f"{rows} rows")
    jobs_body = texts(WINDOW_JOBS)
    check("shows a summary line", "succeeded" in jobs_body.lower(), jobs_body[:160])

    if rows > 0:
        # Selecting a row must populate the detail pane — that is the whole
        # point of the window. Assert on the detail's own content rather than on
        # "the text changed", which is true whether or not it worked.
        osa(wrap(f'tell window "{WINDOW_JOBS}" to select row 1 of table 1 of scroll area 1'))
        time.sleep(1.5)
        after = texts(WINDOW_JOBS)
        check("selecting a job reveals its detail",
              " via " in after or "\nid " in after, after[-200:])

    # ------------------------------------------------------------------
    print("\n\033[1m4. Voices window, and its controls work\033[0m")
    rows = wait_for_rows(WINDOW_VOICES, 1)
    check("voice table populated", rows > 0, f"{rows} rows")

    voices_body = texts(WINDOW_VOICES)
    check("shows a voice summary", "enabled" in voices_body.lower(), voices_body[:160])
    check("has a provider filter", count_of(WINDOW_VOICES, "pop up buttons") == 1)
    check("has a library toggle", count_of(WINDOW_VOICES, "checkboxes") >= 1)

    # Typing in the search box must actually filter the table.
    if rows > 3:
        type_into(WINDOW_VOICES, "text field 1", "z")
        time.sleep(2.5)
        typed = osa(wrap(f'tell window "{WINDOW_VOICES}" to return value of text field 1'))
        filtered = table_rows(WINDOW_VOICES)
        check("the search box receives typed text", typed == "z", f"field holds {typed!r}")
        check(f"typing filters the table ({rows} → {filtered})", 0 <= filtered < rows,
              f"still {filtered}")

        # Clear it again so later assertions see the full list.
        clear_field(WINDOW_VOICES, "text field 1")
        time.sleep(2.5)
        check("clearing the search restores the full list",
              table_rows(WINDOW_VOICES) == rows,
              f"{table_rows(WINDOW_VOICES)} of {rows}")

    # The enable checkbox lives inside a cell view, not directly on the row, and
    # what matters is that clicking it reaches the gateway — not merely that the
    # box repaints.
    checkbox = ("checkbox 1 of UI element 1 of row 1 of table 1 of scroll area 1 "
                f'of window "{WINDOW_VOICES}"')
    if rows > 0:
        try:
            before = api_get("/admin/api/voices")["summary"]["enabled"]
            osa(wrap(f"click {checkbox}"))
            time.sleep(2.5)
            after = api_get("/admin/api/voices")["summary"]["enabled"]
            check(f"ticking a voice reaches the gateway ({before} → {after} enabled)",
                  after != before)
            # Put it back so the owner's configuration is unchanged.
            osa(wrap(f"click {checkbox}"))
            time.sleep(2.0)
            restored = api_get("/admin/api/voices")["summary"]["enabled"]
            check(f"unticking it restores the count ({after} → {restored})",
                  restored == before)
        except RuntimeError as exc:
            check("ticking a voice reaches the gateway", False, str(exc)[:140])

    # The preview button must at least be wired and not crash the app.
    try:
        osa(wrap(f'click button 1 of UI element 2 of row 1 of table 1 of scroll area 1 '
                 f'of window "{WINDOW_VOICES}"'))
        time.sleep(4)
        check("the preview button is clickable and the app survives",
              osa(f'tell application "System Events" to return (exists process "{APP_PROCESS}")') == "true")
        osa(wrap(f'click button 1 of window "{WINDOW_VOICES}"'))  # Stop preview
        time.sleep(1)
    except RuntimeError as exc:
        check("the preview button is clickable and the app survives", False, str(exc)[:140])

    # ------------------------------------------------------------------
    print("\n\033[1m5. Settings\033[0m")
    menu_click("Settings…", menu="gateway-kh")
    opened = wait_for_window(WINDOW_SETTINGS)
    check("Settings opens from the app menu", opened, f"windows: {window_titles()}")
    if opened:
        time.sleep(1)
        settings_body = texts(WINDOW_SETTINGS)
        check("Settings shows the runtime", "runtime" in settings_body.lower(), settings_body[:160])
        check("Settings shows the data folder", "gateway-kh" in settings_body)
        check("Settings has an API key field", count_of(WINDOW_SETTINGS, "text fields") >= 2,
              str(count_of(WINDOW_SETTINGS, "text fields")))
        check("Settings has checkboxes for autostart",
              count_of(WINDOW_SETTINGS, "checkboxes") >= 2,
              str(count_of(WINDOW_SETTINGS, "checkboxes")))

    # ------------------------------------------------------------------
    print("\n\033[1m6. Console window loaded\033[0m")
    check("console window exists", any(WINDOW_CONSOLE == t for t in window_titles()),
          f"windows: {window_titles()}")
    # The web view's contents are not exposed to accessibility, so the check is
    # that the window exists and the process is still alive — a failed load
    # would have crashed it or left the window empty and titled.
    check("the app survived opening the web console",
          osa(f'tell application "System Events" to return (exists process "{APP_PROCESS}")') == "true")

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
    print("\n\033[1m8. The app is still healthy after all that\033[0m")
    check("process still running",
          osa(f'tell application "System Events" to return (exists process "{APP_PROCESS}")') == "true")
    health = subprocess.run(
        ["curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}",
         "http://127.0.0.1:8010/health"],
        capture_output=True, text=True,
    ).stdout.strip()
    check("gateway still answering", health == "200", f"HTTP {health}")

    close_all_windows()

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
