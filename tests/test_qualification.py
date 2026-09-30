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


class ThePublishedRecordTest(unittest.TestCase):
    """What the catalogue hands Story Studio: keyed by the id a caller sees."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self._n = 0

    def _ran(self, provider, model, completion, seconds, prompt=None, machine=None):
        self._n += 1
        row, _ = self.store.submit(
            modality="chat", model_id=model, params={},
            idempotency_key=f"p{self._n}", provider_id=provider,
        )
        payload = {"usage": {"completion_tokens": completion},
                   "generation_seconds": seconds}
        if prompt is not None:
            payload["usage"]["prompt_tokens"] = prompt
        if machine:
            payload["hub_machine"] = machine
        self.store.succeed(row["id"], result={"text": "x", "payload": payload})

    def test_a_locations_work_is_published_under_the_maker(self) -> None:
        # The join that matters. A job routed to one controller is catalogue row
        # `studio-hub::~<model>`; keying on the raw provider id left every local row
        # reading as unmeasured, which is the one lane the question is about.
        self._ran("studio-hub-9701220bf30f", "Qwen3.5-9B-4bit", 4096, 244.0)
        index = qualification.published_index(self.store)
        self.assertIn(("studio-hub", "Qwen3.5-9B-4bit"), index)
        self.assertNotIn(("studio-hub-9701220bf30f", "Qwen3.5-9B-4bit"), index)

    def test_six_locations_are_one_row_summarised_across_all_of_them(self) -> None:
        # One Mac's slowest run is not the fleet's envelope, and one Mac's fastest is
        # not either. The largest that ran and the sample count are both fleet facts.
        self._ran("studio-hub-aaa", "m", 841, 59.2)
        self._ran("studio-hub-bbb", "m", 2350, 141.6)
        self._ran("studio-hub-ccc", "m", 100, 8.6)
        row = qualification.published_index(self.store)[("studio-hub", "m")]
        self.assertEqual(row["samples"], 3)
        self.assertEqual(row["max_completion_tokens"], 2350)
        self.assertEqual(row["sites"], ["studio-hub-aaa", "studio-hub-bbb", "studio-hub-ccc"])
        self.assertEqual(row["source"], "gateway")

    def test_a_cloud_lane_keeps_its_own_namespace(self) -> None:
        self._ran("openrouter-main", "deepseek/deepseek-v4-flash", 15139, 800.0)
        index = qualification.published_index(self.store)
        self.assertIn(("openrouter-main", "deepseek/deepseek-v4-flash"), index)
        self.assertEqual(index[("openrouter-main", "deepseek/deepseek-v4-flash")]["sites"],
                         ["openrouter-main"])

    def test_the_shape_does_not_change_with_the_number_of_contributors(self) -> None:
        # A reader that has to guess whether `sites` is present is a reader that will
        # guess wrong. One contributor and three publish the same keys.
        self._ran("studio-hub-aaa", "m", 100, 10.0)
        one = set(qualification.published_index(self.store)[("studio-hub", "m")])
        self._ran("studio-hub-bbb", "m", 200, 10.0)
        three = set(qualification.published_index(self.store)[("studio-hub", "m")])
        self.assertEqual(one, three)

    def test_a_machine_named_by_any_location_survives_the_merge(self) -> None:
        # The image, voice and music lanes do name a Mac; merging locations must not
        # drop the per-machine split, which is the whole point for those lanes.
        self._ran("studio-hub-aaa", "img", 10, 5.0, machine="mac-a")
        self._ran("studio-hub-bbb", "img", 20, 5.0, machine="mac-b")
        row = qualification.published_index(self.store)[("studio-hub", "img")]
        self.assertEqual(sorted(row["machines"]), ["mac-a", "mac-b"])

    def test_the_slowest_site_is_published_beside_the_median(self) -> None:
        # Measured on the owner's fleet: M1 12 tok/s, M2 14, M4 18. A median of 15.4
        # describes the fleet and hides the 12 — and the caller does not choose the
        # Mac, so anything sized against the median is sized against a machine the job
        # may not land on. Both numbers, or the reader cannot tell there is a spread.
        self._ran("studio-hub-aaa", "m", 1800, 100.0)   # 18 tok/s, an M4
        self._ran("studio-hub-bbb", "m", 1400, 100.0)   # 14 tok/s, an M2
        self._ran("studio-hub-ccc", "m", 1200, 100.0)   # 12 tok/s, an M1
        row = qualification.published_index(self.store)[("studio-hub", "m")]
        self.assertEqual(row["median_tokens_per_second"], 14.0)
        self.assertEqual(row["slowest_site_tokens_per_second"], 12.0)

    def test_the_slowest_site_is_absent_not_zero_when_no_rate_was_measured(self) -> None:
        # No `generation_seconds` means no rate, and `min()` over an empty set is not
        # zero — a lane nobody timed must not read as the slowest thing in the fleet.
        self._ran("studio-hub-aaa", "m", 100, 10.0)
        self._ran("studio-hub-bbb", "m", 200, 10.0)
        self.store._conn.execute("UPDATE jobs SET result_json = json_remove(result_json, '$.payload.generation_seconds')")
        self.store._conn.commit()
        row = qualification.published_index(self.store)[("studio-hub", "m")]
        self.assertIsNone(row["slowest_site_tokens_per_second"])
        self.assertIsNone(row["median_tokens_per_second"])

    def test_a_model_with_no_work_is_absent_from_the_index(self) -> None:
        # Which is how the catalogue publishes `measured: null` — unknown, not zero.
        self._ran("studio-hub-aaa", "m", 100, 10.0)
        index = qualification.published_index(self.store)
        self.assertIsNone(index.get(("studio-hub", "never-run")))


class TheStoreReadsItsOwnNumbersTest(unittest.TestCase):
    """`usage_samples` is the one reader, and it is read in SQL."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)

    def _job(self, result: str | None) -> str:
        row, _ = self.store.submit(modality="chat", model_id="m", params={},
                                   idempotency_key=f"k{result}", provider_id="cloud-a")
        if result is not None:
            self.store._conn.execute(
                "UPDATE jobs SET result_json = ? WHERE id = ?", (result, row["id"]))
            self.store._conn.commit()
        return row["id"]

    def test_a_malformed_row_does_not_take_the_record_with_it(self) -> None:
        # `json_extract` raises `malformed JSON` rather than returning NULL, and a
        # WHERE guard is not a promise about evaluation order. One unreadable row must
        # not cost every other row its measurement.
        self._job(json.dumps({"payload": {"usage": {"completion_tokens": 7},
                                          "generation_seconds": 1.0}}))
        self._job("{not json")
        self._job("[]")
        samples = self.store.usage_samples()
        self.assertEqual(len(samples), 3)
        self.assertEqual(sorted(s["completion_tokens"] for s in samples
                                if s["completion_tokens"] is not None), [7])

    def test_a_json_boolean_is_not_a_token_count(self) -> None:
        # `json_extract` returns 1 for `true`, so the boolean-ness is gone by the time
        # Python sees it — the guard that used to catch this in Python stopped working
        # the moment the read moved into SQL. The type is checked where it is still a
        # JSON type.
        self._job(json.dumps({"payload": {"usage": {"prompt_tokens": 30,
                                                    "completion_tokens": True}}}))
        sample = self.store.usage_samples()[0]
        self.assertIsNone(sample["completion_tokens"])
        self.assertEqual(sample["prompt_tokens"], 30)

    def test_a_stringified_number_is_not_a_token_count(self) -> None:
        self._job(json.dumps({"payload": {"usage": {"completion_tokens": "7"},
                                          "generation_seconds": "1.0"}}))
        sample = self.store.usage_samples()[0]
        self.assertIsNone(sample["completion_tokens"])
        self.assertIsNone(sample["generation_seconds"])

    def test_the_whole_generated_text_is_never_read(self) -> None:
        # The reason this query exists rather than `list_jobs`: `result_json` holds the
        # full completion, so materialising 500 of them to read four numbers would pull
        # megabytes of prose through Python on a path Story Studio polls.
        self._job(json.dumps({"text": "x" * 200000,
                              "payload": {"usage": {"completion_tokens": 7}}}))
        sample = self.store.usage_samples()[0]
        self.assertNotIn("result_json", sample)
        self.assertNotIn("text", sample)
        self.assertEqual(sample["completion_tokens"], 7)


class TheGenerationAnalyticsTest(unittest.TestCase):
    """What the Analytics page counts, and the three ways it could lie.

    The page exists to help decide where future work goes, so a wrong number here is worse
    than a missing one: it would send the next batch to the wrong Mac.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self._n = 0

    def _job(self, *, modality: str = "image", model: str = "flux", state: str = "succeeded",
             machine: str | None = "local", provider: str = "studio-hub-x",
             seconds: Any = None, size: Any = None) -> str:
        self._n += 1
        row, _ = self.store.submit(
            modality=modality, model_id=model, params={},
            idempotency_key=f"k{self._n}", provider_id=provider,
        )
        payload: dict[str, Any] = {}
        if machine is not None:
            payload["hub_machine"] = machine
        if seconds is not None:
            payload["generation_seconds"] = seconds
        if size is not None:
            payload["bytes"] = size
        self.store._conn.execute(
            "UPDATE jobs SET state = ?, result_json = ? WHERE id = ?",
            (state, json.dumps({"payload": payload}), row["id"]),
        )
        self.store._conn.commit()
        return row["id"]

    def test_the_median_survives_a_cold_load_the_mean_does_not(self) -> None:
        # The reason both are published. The first render after a model load is far slower
        # than the ones behind it, and a mean alone would describe that load rather than the
        # machine's rate — which is the number the owner is deciding on.
        for seconds in (300.0, 60.0, 62.0):
            self._job(seconds=seconds)
        model = self.store.generation_analytics()["by_model"][0]
        self.assertEqual(model["seconds_median"], 62.0)
        self.assertEqual(model["seconds_mean"], 140.67)
        self.assertEqual(model["seconds_max"], 300.0)

    def test_an_unstated_runtime_is_absent_not_zero(self) -> None:
        # "Unstated" and "instant" must not render alike: a zero would enter the average
        # and drag the model's real rate down while looking like a measurement.
        self._job(seconds=None)
        self._job(seconds=10.0)
        totals = self.store.generation_analytics()["totals"]
        self.assertEqual(totals["jobs"], 2)
        self.assertEqual(totals["timed_jobs"], 1)
        self.assertEqual(totals["seconds_mean"], 10.0)
        model = self.store.generation_analytics()["by_model"][0]
        self.assertEqual(model["timed_jobs"], 1)
        self.assertEqual(model["seconds_median"], 10.0)

    def test_a_group_with_no_timings_reports_none_not_zero(self) -> None:
        self._job(seconds=None)
        totals = self.store.generation_analytics()["totals"]
        self.assertIsNone(totals["seconds_mean"])
        self.assertIsNone(totals["seconds_median"])
        self.assertEqual(totals["timed_jobs"], 0)

    def test_a_json_boolean_is_not_a_duration(self) -> None:
        # `json_extract` returns 1 for `true`, so the same guard `usage_samples` needs
        # applies here: the type must be checked while it is still a JSON type.
        self._job(seconds=True)
        totals = self.store.generation_analytics()["totals"]
        self.assertEqual(totals["timed_jobs"], 0)
        self.assertIsNone(totals["seconds_mean"])

    def test_a_stringified_number_is_not_a_duration(self) -> None:
        self._job(seconds="64.9")
        self.assertEqual(self.store.generation_analytics()["totals"]["timed_jobs"], 0)

    def test_a_malformed_row_does_not_take_the_record_with_it(self) -> None:
        self._job(seconds=10.0)
        self._n += 1
        row, _ = self.store.submit(modality="image", model_id="flux", params={},
                                   idempotency_key="bad", provider_id="studio-hub-x")
        self.store._conn.execute(
            "UPDATE jobs SET state = 'succeeded', result_json = ? WHERE id = ?",
            ("{not json", row["id"]),
        )
        self.store._conn.commit()
        totals = self.store.generation_analytics()["totals"]
        self.assertEqual(totals["jobs"], 2)
        self.assertEqual(totals["timed_jobs"], 1)

    def test_a_controllers_own_work_is_not_read_as_a_joined_nodes(self) -> None:
        # `local` is the Hub's word for "this controller ran it itself", and a node id is
        # a joined Mac. Reporting one as the other is the exact confusion this page was
        # asked to clear up, so the provider travels with the machine.
        self._job(machine="local", seconds=10.0)
        self._job(machine="node-a", seconds=20.0)
        keys = {row["key"] for row in self.store.generation_analytics()["by_machine"]}
        self.assertEqual(keys, {"local @ studio-hub-x", "node-a @ studio-hub-x"})

    def test_only_settled_jobs_are_counted(self) -> None:
        # A queued or running job has no duration yet; counting one would drag every
        # average toward zero.
        self._job(state="succeeded", seconds=10.0)
        for state in ("queued", "running"):
            self._n += 1
            row, _ = self.store.submit(modality="image", model_id="flux", params={},
                                       idempotency_key=state, provider_id="studio-hub-x")
            self.store._conn.execute(
                "UPDATE jobs SET state = ? WHERE id = ?", (state, row["id"]))
        self.store._conn.commit()
        self.assertEqual(self.store.generation_analytics()["totals"]["jobs"], 1)

    def test_failures_are_counted_but_do_not_claim_output(self) -> None:
        self._job(state="succeeded", seconds=10.0, size=1000)
        self._job(state="failed", seconds=None, size=None)
        totals = self.store.generation_analytics()["totals"]
        self.assertEqual((totals["succeeded"], totals["failed"]), (1, 1))
        self.assertEqual(totals["bytes"], 1000)
        self.assertEqual(totals["bytes_jobs"], 1)


class TheOutputShapeTest(unittest.TestCase):
    """Prose and structured work are told apart, because they are not interchangeable.

    Measured on the local model 2026-09-26 the two differ by a quarter — 1.21 tokens
    per word for prose against 1.50 for a JSON contract — and this record published one
    blended median over both, which described neither. These checks exist so the
    headline cannot quietly become a blend again.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self._n = 0

    def _ran(self, completion, seconds, *, response_format="__absent__", model="m"):
        self._n += 1
        params = {} if response_format == "__absent__" else {"response_format": response_format}
        row, _ = self.store.submit(
            modality="chat", model_id=model, params=params,
            idempotency_key=f"s{self._n}", provider_id="cloud-a",
        )
        self.store.succeed(row["id"], result={"text": "x", "payload": {
            "usage": {"completion_tokens": completion}, "generation_seconds": seconds}})

    def _row(self):
        return qualification.envelope(self.store)[0]

    def test_an_absent_response_format_is_prose(self) -> None:
        # A caller that asked for nothing asked for text. Reading absent as anything
        # else would make the commonest case the unknown one.
        self._ran(100, 10)
        self.assertEqual(self._row()["by_output_shape"]["text"]["samples"], 1)
        self.assertEqual(self._row()["by_output_shape"]["structured"]["samples"], 0)

    def test_both_json_spellings_count_as_structured(self) -> None:
        for fmt in ({"type": "json_object"}, {"type": "json_schema"}):
            with self.subTest(fmt=fmt):
                self._ran(100, 10, response_format=fmt)
        self.assertEqual(self._row()["by_output_shape"]["structured"]["samples"], 2)

    def test_the_twelve_thousand_token_pack_does_not_become_the_prose_rate(self) -> None:
        # The case that caused this: a scene-prompt pack and a chapter in one row. The
        # headline still reports the largest that ran — that is what a ceiling decision
        # reads — but each shape keeps its own rate beside it.
        self._ran(1500, 100)                                    # prose, 15 tok/s
        self._ran(12090, 900, response_format={"type": "json_object"})   # structured, 13.4
        row = self._row()
        self.assertEqual(row["max_completion_tokens"], 12090)
        self.assertEqual(row["by_output_shape"]["text"]["median_tokens_per_second"], 15.0)
        self.assertEqual(row["by_output_shape"]["structured"]["median_tokens_per_second"], 13.4)
        self.assertEqual(row["by_output_shape"]["text"]["max_completion_tokens"], 1500)

    def test_both_shapes_are_always_present(self) -> None:
        # A key that appears only once there is data is a key a reader has to guess at,
        # and a missing `by_output_shape` is exactly the state this field replaced.
        self._ran(100, 10)
        shapes = self._row()["by_output_shape"]
        self.assertEqual(sorted(shapes), ["structured", "text"])
        empty = shapes["structured"]
        self.assertEqual(empty["samples"], 0)
        self.assertIsNone(empty["max_completion_tokens"])
        self.assertIsNone(empty["median_tokens_per_second"])

    def test_a_shape_rolls_up_across_locations_too(self) -> None:
        # The fleet merge must carry the split, not just the per-location envelope.
        for provider in ("studio-hub-aaa", "studio-hub-bbb"):
            self._n += 1
            row, _ = self.store.submit(
                modality="chat", model_id="m", params={"response_format": {"type": "json_object"}},
                idempotency_key=f"m{self._n}", provider_id=provider)
            self.store.succeed(row["id"], result={"text": "x", "payload": {
                "usage": {"completion_tokens": 100}, "generation_seconds": 10.0}})
        merged = qualification.published_index(self.store)[("studio-hub", "m")]
        self.assertEqual(merged["samples"], 2)
        self.assertEqual(merged["by_output_shape"]["structured"]["samples"], 2)
        self.assertEqual(merged["by_output_shape"]["text"]["samples"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
