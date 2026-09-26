"""What a model has been measured doing — and the rule that it is never confused
with what a producer stated.

Asked 2026-09-26: *"we need to keep these for that when I want to qualify a new LLM,
we can apply the same parameter check too."* The numbers existed once, read by hand,
and were lost.

The tests below are mostly about **what is not counted**. A queued job is not a
sample, a lane that states no usage is not a lane that used zero tokens, and a
missing `generation_seconds` is not a rate of zero — each of those turns a record of
real work into a plausible-looking invention, which is the one failure both projects
refuse.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-qualification-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"), "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"), "GATEWAY_VOICES_OUT": str(base / "voices.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "fixture-key",
})

from gateway import qualification  # noqa: E402
from gateway.store import JobStore  # noqa: E402


class EnvelopeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self._n = 0

    def _ran(
        self, *, provider="cloud-a", model="m1", modality="chat",
        prompt=None, completion=None, seconds=None, extra=None, result=True,
    ) -> dict:
        """One job that reached a terminal state, optionally with a payload."""
        self._n += 1
        row, _ = self.store.submit(
            modality=modality, model_id=model, params={},
            idempotency_key=f"k{self._n}", provider_id=provider,
        )
        if result:
            payload: dict = {}
            if prompt is not None or completion is not None:
                usage = {}
                if prompt is not None:
                    usage["prompt_tokens"] = prompt
                if completion is not None:
                    usage["completion_tokens"] = completion
                payload["usage"] = usage
            if seconds is not None:
                payload["generation_seconds"] = seconds
            payload.update(extra or {})
            self.store.succeed(row["id"], result={"text": "x", "payload": payload})
        return self.store.get(row["id"])

    def _row(self, provider="cloud-a", model="m1") -> dict:
        found = qualification.envelope(self.store)
        for row in found:
            if row["provider_id"] == provider and row["model_id"] == model:
                return row
        self.fail(f"no envelope for {provider}/{model}: {found}")

    # -- what is counted --------------------------------------------------
    def test_the_largest_completion_that_actually_ran_is_the_headline(self) -> None:
        # Not the last, not the mean. "Can this lane carry 8,000 tokens" is answered
        # by the biggest it has carried, and nothing else answers it.
        self._ran(completion=100, seconds=10)
        self._ran(completion=900, seconds=10)
        self._ran(completion=400, seconds=10)
        row = self._row()
        self.assertEqual(row["max_completion_tokens"], 900)
        self.assertEqual(row["samples"], 3)

    def test_the_prompt_side_is_measured_too(self) -> None:
        self._ran(prompt=1200, completion=10, seconds=1)
        self._ran(prompt=80, completion=10, seconds=1)
        self.assertEqual(self._row()["max_prompt_tokens"], 1200)

    def test_a_job_that_has_not_run_is_not_a_sample(self) -> None:
        # A queue is not evidence. Counting a pending job would let a model read as
        # qualified on the strength of work nobody has done yet.
        self._ran(completion=500, seconds=10)
        self.store.submit(modality="chat", model_id="m1", params={},
                          idempotency_key="pending", provider_id="cloud-a")
        row = self._row()
        self.assertEqual(row["samples"], 1)
        self.assertEqual(row["max_completion_tokens"], 500)

    def test_a_lane_that_states_no_usage_is_skipped_not_zeroed(self) -> None:
        # The whole record is `usage`. A lane that reports none is *unmeasured*;
        # folding it in as a zero-token sample would drag every rate and maximum
        # toward "this lane does nothing".
        self._ran(completion=700, seconds=7)
        self._ran(result=False)
        row = self._row()
        self.assertEqual(row["samples"], 1)
        self.assertEqual(row["with_usage"], 1)

    def test_a_result_that_is_not_json_is_skipped_rather_than_crashing(self) -> None:
        row = self._ran(completion=10, seconds=1)
        self.store._conn.execute(
            "UPDATE jobs SET result_json = ? WHERE id = ?", ("{not json", row["id"]))
        self.store._conn.commit()
        self.assertEqual(qualification.envelope(self.store), [])

    # -- the rate ---------------------------------------------------------
    def test_the_rate_is_the_median_so_one_cold_run_does_not_move_it(self) -> None:
        # A cold turn loads the model first and reports a rate an order of magnitude
        # low. A mean would carry it into the number a ceiling gets sized against.
        for _ in range(5):
            self._ran(completion=100, seconds=10)     # 10 tok/s
        self._ran(completion=100, seconds=1000)       # 0.1 tok/s, one bad turn
        self.assertEqual(self._row()["median_tokens_per_second"], 10.0)

    def test_no_generation_time_is_an_absent_rate_and_never_a_zero(self) -> None:
        self._ran(completion=500)
        self.assertIsNone(self._row()["median_tokens_per_second"])

    def test_a_zero_second_generation_does_not_divide_by_zero(self) -> None:
        self._ran(completion=500, seconds=0)
        self.assertIsNone(self._row()["median_tokens_per_second"])

    def test_a_boolean_is_not_a_token_count(self) -> None:
        # `True` is an `int` in Python. Read as a number it would enter the record
        # as a one-token completion, which is a fabricated sample. The prompt on the
        # same payload is a real measurement and is still published — a bogus field
        # discards itself, not the whole job.
        self._ran(extra={"usage": {"prompt_tokens": 30, "completion_tokens": True}})
        row = self._row()
        self.assertIsNone(row["max_completion_tokens"])
        self.assertEqual(row["max_prompt_tokens"], 30)
        self.assertEqual(row["with_usage"], 0)

    # -- whose number it is -----------------------------------------------
    def test_every_row_says_the_number_is_ours(self) -> None:
        # The rule the whole feature rests on. A measured number of ours must never
        # be readable as a figure the producer declared.
        self._ran(completion=500, seconds=10)
        self.assertEqual(self._row()["source"], "gateway")

    def test_a_model_never_run_is_absent_rather_than_zero(self) -> None:
        # "No measurement" and "measured at nothing" are different answers, and only
        # one of them is a reason to go and run something.
        self._ran(completion=500, seconds=10)
        self.assertEqual(qualification.envelope(self.store, model_id="never-run"), [])
        self.assertIsNone(qualification.qualification_for(self.store, "cloud-a", "never"))

    # -- machines ---------------------------------------------------------
    def test_a_named_machine_is_split_out(self) -> None:
        self._ran(completion=100, seconds=10, extra={"hub_machine": "mac-a"})
        self._ran(completion=900, seconds=10, extra={"hub_machine": "mac-b"})
        row = self._row()
        self.assertEqual(sorted(row["machines"]), ["mac-a", "mac-b"])
        self.assertEqual(row["machines"]["mac-b"]["max_completion_tokens"], 900)
        self.assertEqual(row["machines"]["mac-a"]["samples"], 1)

    def test_the_chat_lane_names_no_machine_and_none_is_invented(self) -> None:
        # Measured live: the chat payload carries `finish_reason`, `model`, `usage`
        # and `generation_seconds`, and nothing that identifies a Mac. The record
        # says so by staying empty rather than by guessing "local".
        self._ran(completion=500, seconds=10)
        self.assertEqual(self._row()["machines"], {})

    # -- reading ------------------------------------------------------------------
    def test_provider_and_model_narrow_the_read(self) -> None:
        self._ran(provider="cloud-a", model="m1", completion=10, seconds=1)
        self._ran(provider="cloud-b", model="m1", completion=20, seconds=1)
        self._ran(provider="cloud-a", model="m2", completion=30, seconds=1)
        self.assertEqual(len(qualification.envelope(self.store)), 3)
        self.assertEqual(len(qualification.envelope(self.store, provider_id="cloud-a")), 2)
        one = qualification.envelope(self.store, provider_id="cloud-a", model_id="m2")
        self.assertEqual([r["max_completion_tokens"] for r in one], [30])

    def test_one_model_on_two_lanes_is_two_records(self) -> None:
        # Two vendors serving the same weights are two different products: the same
        # rule the catalogue namespaces by.
        self._ran(provider="cloud-a", model="same", completion=10, seconds=1)
        self._ran(provider="cloud-b", model="same", completion=9000, seconds=1)
        rows = {r["provider_id"]: r for r in qualification.envelope(self.store)}
        self.assertEqual(rows["cloud-a"]["max_completion_tokens"], 10)
        self.assertEqual(rows["cloud-b"]["max_completion_tokens"], 9000)

    def test_the_window_bounds_how_far_back_the_read_looks(self) -> None:
        for _ in range(5):
            self._ran(completion=100, seconds=10)
        self.assertEqual(qualification.envelope(self.store, limit=2)[0]["samples"], 2)

    def test_modalities_are_reported_beside_the_model(self) -> None:
        self._ran(modality="chat", completion=10, seconds=1)
        self._ran(modality="chat", completion=10, seconds=1)
        self.assertEqual(self._row()["modalities"], ["chat"])


class _StubCatalog:
    """Just the read the route makes."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def all_rows(self) -> list[dict]:
        return self._rows


class TheRoutePublishesBothNumbersTest(unittest.TestCase):
    """The stated ceiling and the measured maximum, side by side and never merged."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from gateway import admin, fleet
        from gateway.catalog import encode_model_id

        self.admin, self.fleet = admin, fleet
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self._saved = (admin.store, admin.catalog)
        admin.store = self.store
        self.addCleanup(self._restore)
        self.encode = encode_model_id

    def _restore(self) -> None:
        self.admin.store, self.admin.catalog = self._saved

    def _ran(self, provider: str, model: str, completion: int, seconds: float) -> None:
        row, _ = self.store.submit(modality="chat", model_id=model, params={},
                                   idempotency_key=f"{provider}{model}{completion}",
                                   provider_id=provider)
        self.store.succeed(row["id"], result={"text": "x", "payload": {
            "usage": {"completion_tokens": completion}, "generation_seconds": seconds,
        }})

    def test_a_local_job_joins_the_collapsed_catalogue_namespace(self) -> None:
        # The lane the ceiling question is actually about. A job routed to
        # `studio-hub-9701220bf30f` is catalogue row `studio-hub::~Qwen3.5-9B-4bit`;
        # joining on the raw provider id left every local row reading as unknown.
        self._ran("studio-hub-9701220bf30f", "Qwen3.5-9B-4bit", 4096, 236.0)
        self.admin.catalog = _StubCatalog([{
            "id": self.encode("studio-hub", "Qwen3.5-9B-4bit"),
            "max_output_tokens": 4096, "max_output_tokens_source": "gateway",
            "context_window_tokens": 32768, "context_window_tokens_source": "provider",
        }])
        row = self.admin.qualification_envelope()["rows"][0]
        self.assertEqual(row["max_completion_tokens"], 4096)
        self.assertEqual(row["stated"]["max_output_tokens"], 4096)
        self.assertEqual(row["stated"]["max_output_tokens_source"], "gateway")
        self.assertEqual(row["source"], "gateway")

    def test_a_cloud_lane_keeps_its_own_provider_id(self) -> None:
        self._ran("openrouter-main", "deepseek/deepseek-flash-latest", 15139, 500.0)
        self.admin.catalog = _StubCatalog([{
            "id": self.encode("openrouter-main", "deepseek/deepseek-flash-latest"),
            "max_output_tokens": 943718, "max_output_tokens_source": "provider",
        }])
        row = self.admin.qualification_envelope()["rows"][0]
        self.assertEqual(row["stated"]["max_output_tokens"], 943718)

    def test_a_model_absent_from_the_catalogue_reads_as_unknown(self) -> None:
        # Disabled or withheld. `null` is unknown, never unlimited — and the
        # measured figures are still published, because the work really happened.
        self._ran("cloud-a", "gone", 500, 10.0)
        self.admin.catalog = _StubCatalog([])
        row = self.admin.qualification_envelope()["rows"][0]
        self.assertIsNone(row["stated"])
        self.assertEqual(row["max_completion_tokens"], 500)

    def test_the_answer_says_what_it_covers(self) -> None:
        self._ran("cloud-a", "m", 500, 10.0)
        self.admin.catalog = _StubCatalog([])
        body = self.admin.qualification_envelope(limit=25)
        self.assertEqual(body["window"], 25)
        self.assertEqual(body["measured_rows"], 1)
        self.assertIn("unknown rather than unlimited", body["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
