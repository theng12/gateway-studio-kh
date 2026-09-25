"""A job records who asked for it.

Asked on 2026-09-25 and unanswerable: forty-one chat jobs failed inside one minute
and the records could not say whether Story Studio or a script had sent them. There
is **one API key** and every caller shares it, so the gateway cannot tell them apart
by credential — but it can record what the request said about itself.

This is a **hint, never a credential**: a caller that omits it is not refused, and a
caller that lies is not caught. The only rule that matters is the one every field
here follows — *"the caller said nothing"* must stay different from *"the caller
called itself something"*, so an absent hint is `None` and never `""`.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.api import CALLER_HEADER, caller_hint  # noqa: E402
from gateway.store import JobStore  # noqa: E402


class _Headers(dict):
    """A case-insensitive mapping, like Starlette's `Headers`.

    The code reads `X-Caller` in its proper spelling; a plain dict of lowercased
    keys made the first version of these tests fail against correct code. The stub
    was wrong, not the reader — and a stub that does not share the real object's
    semantics tests a different program.
    """

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__({str(k).lower(): v for k, v in values.items()})

    def get(self, key, default=None):  # type: ignore[override]
        return super().get(str(key).lower(), default)


class _Request:
    """Just enough of a Starlette request for the header read."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = _Headers(headers or {})


class CallerHintTest(unittest.TestCase):
    def test_a_declared_name_wins(self) -> None:
        request = _Request({CALLER_HEADER: "story-studio", "user-agent": "URLSession/1"})
        self.assertEqual(caller_hint(request), "story-studio")

    def test_the_user_agent_is_the_fallback(self) -> None:
        # The one that needs no cooperation: curl, python-requests and Story Studio
        # all send something, so a script is at least distinguishable from a UI.
        self.assertEqual(
            caller_hint(_Request({"user-agent": "python-requests/2.31.0"})),
            "python-requests/2.31.0",
        )

    def test_saying_nothing_is_none_and_not_an_empty_string(self) -> None:
        self.assertIsNone(caller_hint(_Request({})))
        self.assertIsNone(caller_hint(_Request({"user-agent": "   "})))
        self.assertIsNone(caller_hint(_Request({CALLER_HEADER: "  "})))

    def test_a_declared_name_that_is_blank_falls_through_to_the_agent(self) -> None:
        request = _Request({CALLER_HEADER: "   ", "user-agent": "curl/8.4.0"})
        self.assertEqual(caller_hint(request), "curl/8.4.0")

    def test_a_credential_shaped_name_is_not_treated_as_one(self) -> None:
        # It is recorded verbatim and never checked. Saying so here keeps a later
        # reader from "hardening" a field the design deliberately leaves open.
        request = _Request({CALLER_HEADER: "Bearer-sk-looking-thing"})
        self.assertEqual(caller_hint(request), "Bearer-sk-looking-thing")

    def test_a_very_long_name_is_bounded(self) -> None:
        long_name = "x" * 500
        self.assertEqual(len(caller_hint(_Request({CALLER_HEADER: long_name}))), 200)


class TheJobRemembersTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)

    def _submit(self, key: str, caller: str | None = None) -> dict:
        row, created = self.store.submit(
            modality="chat",
            model_id="m",
            params={},
            idempotency_key=key,
            provider_id="studio-hub-local",
            caller=caller,
        )
        self.assertTrue(created)
        return row

    def test_the_hint_survives_the_round_trip(self) -> None:
        row = self._submit("a", "story-studio")
        self.assertEqual(row.get("caller"), "story-studio")
        # And it is readable from the store afterwards, not only from the returned
        # row — the whole point is asking the question later.
        self.assertEqual(self.store.get(row["id"]).get("caller"), "story-studio")

    def test_a_caller_that_said_nothing_records_nothing(self) -> None:
        row = self._submit("b")
        self.assertIsNone(self.store.get(row["id"]).get("caller"))

    def test_a_blank_hint_is_absence_not_a_blank_name(self) -> None:
        for blank in ("", "   ", "\n"):
            row = self._submit(f"blank-{blank!r}", blank)
            self.assertIsNone(self.store.get(row["id"]).get("caller"),
                              f"{blank!r} was recorded as a name")

    def test_a_long_hint_is_truncated_rather_than_refused(self) -> None:
        # A hint must never be the reason a job is not created.
        row = self._submit("c", "y" * 500)
        self.assertEqual(len(self.store.get(row["id"]).get("caller")), 200)

    def test_an_idempotent_replay_keeps_the_first_hint(self) -> None:
        # A replay is the same job, so it must not rewrite who asked first — the
        # same reason it must not start a second generation.
        first = self._submit("d", "story-studio")
        again, created = self.store.submit(
            modality="chat", model_id="m", params={}, idempotency_key="d",
            provider_id="studio-hub-local", caller="something-else",
        )
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.store.get(first["id"]).get("caller"), "story-studio")

    def test_it_is_published_on_the_job_list(self) -> None:
        # A field nothing draws is the same defect as a control that does nothing:
        # the Jobs page is where the question gets asked.
        self._submit("e", "story-studio")
        rows = self.store.list_jobs(limit=10)
        self.assertTrue(rows)
        self.assertIn("caller", rows[0])
        self.assertEqual(rows[0]["caller"], "story-studio")


if __name__ == "__main__":
    unittest.main(verbosity=2)
