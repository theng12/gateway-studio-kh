"""Escape must work everywhere (owner rule 2026-11-12).

AppKit answers Escape with a button titled Cancel, but an alert with only OK, no buttons, or a
Wait / Keep Open choice ignores it entirely (measured). The app installs `closeOnEscape()` on
those, the web page clears a search field in place and tidies a dialog's pending action when the
browser closes it, and the native Voices search answers cancelOperation. These are source
contracts: the next alert or field cannot silently regress the rule.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MENUBAR = ROOT / "menubar"
PAGE = (ROOT / "gateway" / "admin_ui.html").read_text()


def alert_blocks(path: pathlib.Path):
    """(line number, block text) for every NSAlert() literal, up to its presentation call."""
    lines = path.read_text().splitlines()
    index = 0
    while index < len(lines):
        match = re.search(r"\b(?:let|var)\s+(\w+)\s*=\s*NSAlert\(\)", lines[index])
        if not match:
            index += 1
            continue
        end = index
        while end < len(lines) and end < index + 80:
            if re.search(r"\.runModal\(\)|beginSheetModal|\.beginSheet", lines[end]):
                break
            if end > index and re.search(r"\b(?:let|var)\s+\w+\s*=\s*NSAlert\(\)", lines[end]):
                break
            end += 1
        yield index + 1, "\n".join(lines[index:end + 1])
        index = end + 1


class EveryAlertClosesOnEscapeTest(unittest.TestCase):
    def test_an_alert_without_a_cancel_button_installs_close_on_escape(self):
        misses = []
        for path in sorted(MENUBAR.glob("*.swift")):
            for line, block in alert_blocks(path):
                has_cancel = re.search(r'addButton\(withTitle: "Cancel"\)', block)
                if not has_cancel and "closeOnEscape()" not in block:
                    misses.append(f"{path.name}:{line}")
        self.assertEqual(misses, [], "these alerts ignore Escape: " + ", ".join(misses))

    def test_the_escape_closer_ends_modal_and_sheet_presentations(self):
        source = (MENUBAR / "PageKit.swift").read_text()
        self.assertIn("private final class EscapeCloser", source)
        self.assertIn("event.keyCode == 53", source)
        self.assertIn("parent.endSheet(window, returnCode: .cancel)", source)
        self.assertIn("NSApp.stopModal(withCode: .cancel)", source)
        # The monitor must not outlive its alert.
        self.assertIn("deinit { stop() }", source)
        self.assertIn("NSWindow.willCloseNotification", source)

    def test_a_running_generation_cancels_on_escape(self):
        """The Cancel button shown during a run answers Escape, in that page's own context."""
        source = (MENUBAR / "CreateSection.swift").read_text()
        self.assertIn('cancelButton?.keyEquivalent = visible ? "\\u{1b}" : ""', source)
        self.assertIn("showCancel(!terminal)", source)
        self.assertIn("func showCancel(_ visible: Bool)", source)

    def test_a_selected_table_row_clears_on_escape(self):
        """Jobs and Voices: Esc never silently does nothing when a row is selected."""
        design = (MENUBAR / "PageKit.swift").read_text()
        self.assertIn("final class EscapeClearingTableView: NSTableView", design)
        self.assertIn("event.keyCode == 53", design)
        self.assertIn("deselectAll(nil)", design)
        for name in ["JobsWindow.swift", "VoicesWindow.swift"]:
            self.assertIn("table = EscapeClearingTableView()", (MENUBAR / name).read_text(),
                          f"{name} keeps a table that ignores Escape with a row selected")

    def test_the_native_voices_search_clears_on_escape(self):
        source = (MENUBAR / "VoicesWindow.swift").read_text()
        self.assertIn("doCommandBy commandSelector: Selector", source)
        self.assertIn('#selector(NSResponder.cancelOperation(_:))', source)
        self.assertIn('searchField.stringValue = ""', source)
        self.assertIn("searchChanged()", source)


class WebViewPanelsAnswerThePageTest(unittest.TestCase):
    def test_every_javascript_panel_the_page_uses_is_implemented(self):
        """A panel the delegate does not implement is a dead control: the page gets null."""
        source = (MENUBAR / "MainWindow.swift").read_text()
        for panel in ["runJavaScriptAlertPanelWithMessage",
                      "runJavaScriptConfirmPanelWithMessage",
                      "runJavaScriptTextInputPanelWithPrompt",
                      "runOpenPanelWith"]:
            self.assertIn(panel, source, f"{panel} missing: that control is dead in the app")
        # The text panel keeps the page's default text and answers null on Cancel, which is Escape.
        self.assertIn("field.stringValue : nil", source)
        self.assertIn("alert.window.initialFirstResponder = field", source)


class WebPageClosesAndClearsOnEscapeTest(unittest.TestCase):
    def test_search_and_filter_fields_clear_on_escape(self):
        self.assertIn("document.addEventListener('keydown', event => {", PAGE)
        self.assertIn("event.key !== 'Escape'", PAGE)
        self.assertIn("field.type !== 'search' && field.type !== 'text'", PAGE)
        self.assertIn("field.value = ''", PAGE)
        self.assertIn("new Event('input', { bubbles: true })", PAGE)
        # Inside an open dialog the dialog's own Escape wins, never a half-cleared field.
        self.assertIn("field.closest('dialog[open]')", PAGE)

    def test_every_dialog_clears_its_pending_action_when_closed(self):
        for dialog_id, state in [
            ("fleet-dialog", "fleetDialogState = null"),
            ("shelf-retire-dialog", "shelfRetirePending = null"),
            ("shelf-remove-dialog", "shelfRemovePending = null"),
        ]:
            self.assertIn(f"['{dialog_id}', () => {{ {state}; }}]", PAGE,
                          f"{dialog_id} keeps its pending action after Escape")


if __name__ == "__main__":
    unittest.main()
