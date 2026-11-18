"""Guards the two whole-page promises the visual pass checked by hand.

1. The console never uses the browser's own confirm/alert/prompt. In a packaged webview they are
   silent no-ops — the click resolves to false and nothing happens — so every question and
   warning is the app's own dialog (uiConfirm / uiAlert).
2. Every inline handler on every page names a function that exists in the page. A button whose
   handler was renamed or deleted does nothing when clicked, and a green page shell hides it.

These are static checks over ``gateway/admin_ui.html``; they fail loudly when a page is edited.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "gateway" / "admin_ui.html").read_text(encoding="utf-8")

# Things a handler may call without this file defining them: the language's own builtins and the
# keywords that open a block inside an inline handler (`onchange="if (x) { … }"`).
ALLOWED = {
    "Number", "String", "Boolean", "Object", "Array", "JSON", "Math", "Date", "isFinite",
    "parseInt", "parseFloat", "encodeURIComponent", "Set", "Map", "Promise",
    "if", "for", "while", "switch", "return", "typeof", "function", "catch", "new",
}


def defined_names(source: str) -> set[str]:
    names: set[str] = set()
    for pattern in (
        r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(",
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
        r"\bglobalThis\.([A-Za-z_$][\w$]*)\s*=",
        r"\bwindow\.([A-Za-z_$][\w$]*)\s*=",
    ):
        names |= set(re.findall(pattern, source))
    return names


def inline_handlers(source: str) -> list[tuple[int, str]]:
    """(line number, handler source) for every on* attribute outside script bodies."""
    handlers = []
    for match in re.finditer(r'\son(?:click|change|input|submit|keydown|blur|focus)\s*=\s*"([^"]*)"', source):
        handlers.append((source[: match.start()].count("\n") + 1, match.group(1)))
    return handlers


class WebUiAudit(unittest.TestCase):
    def test_no_browser_dialogs_anywhere_in_the_console(self):
        # `uiConfirm(` and `uiAlert(` are this app's own dialogs and must not trip the check.
        found = [
            (HTML[: m.start()].count("\n") + 1, m.group(0))
            # No space before the paren: prose such as "Max prompt (characters)" is a label.
            for m in re.finditer(
                r"(?<![A-Za-z0-9_$.])(?:window\.)?(?:confirm|alert|prompt)\(", HTML
            )
        ]
        self.assertEqual(found, [], f"browser dialog(s) at {found}; use uiConfirm/uiAlert")

    def test_every_inline_handler_names_a_defined_function(self):
        known = defined_names(HTML) | ALLOWED
        missing: dict[str, list[int]] = {}
        for line, handler in inline_handlers(HTML):
            for name in re.findall(r"(?<![.$\w])([A-Za-z_$][\w$]*)\s*\(", handler):
                if name not in known:
                    missing.setdefault(name, []).append(line)
        self.assertEqual(missing, {}, f"inline handlers call undefined functions: {missing}")

    def test_every_sort_header_is_a_button_with_its_own_state(self):
        # A header that looks sortable must be one: an aria-sort cell carries a button whose
        # handler sorts this table (never another table's state).
        for match in re.finditer(r'<th[^>]*aria-sort="[^"]*"[^>]*>(.*?)</th>', HTML, re.S):
            self.assertIn('class="sort"', match.group(1), "an aria-sort header must hold a sort button")
        self.assertNotRegex(HTML, r"<th[^>]*\sonclick=", "no bare header may carry a click handler")


if __name__ == "__main__":
    unittest.main()
