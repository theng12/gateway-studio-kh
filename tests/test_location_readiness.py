#!/usr/bin/env python3
"""A controller that cannot serve a model is handled at the site, not the job.

Two halves:

* **Prevent** — `can_publish` withholds a model the Hub reports as not
  dispatchable (`dispatch_state` present and not `eligible`), so the picker never
  offers work the chosen controller will refuse. Absence stays unverified.
* **Handle** — the worker recognises a Hub `LOCATION_NOT_READY` refusal at submit
  and tries the next location that offers the same model, unless a voice pins the
  route to the one controller that holds the reference clip.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-location-readiness-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_ARTIFACTS": str(_TEMP_ROOT / "data" / "artifacts"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from gateway import fleet  # noqa: E402
from gateway.adapters.base import BackendError, BackendResult  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402
import gateway.worker as worker_module  # noqa: E402


MODEL = "mlx-community/heavy-model"


def _definition(provider_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=provider_id, kind="studio_hub", base_url=f"https://{provider_id}",
                           display_name=provider_id)


class _TwoLocations:
    """Both locations offer the model; the worker must be able to try the second."""

    credentials = SimpleNamespace(is_enabled=lambda _provider_id: True, resolved=lambda _provider_id: {})
    models = SimpleNamespace(enabled_models=lambda _modality: [
        (_definition("studio-hub-a"), {"model_id": MODEL}),
        (_definition("studio-hub-b"), {"model_id": MODEL}),
    ])

    @staticmethod
    def definition(provider_id: str):
        return _definition(provider_id)


def _snapshot(allow_stale: bool = False) -> dict:
    return {"locations": [
        {"id": location_id, "label": location_id.upper(), "reachable": True,
         "capacity": {}, "model_supply": {MODEL: {"eligible": 1, "available": 1}},
         "machines": [], "machine_summary": {"reachable": 1}, "running_jobs": []}
        for location_id in ("a", "b")
    ]}


class _RefuseFirstLocation:
    """Refuses the first site with the Hub's not-ready code, then succeeds."""

    def __init__(self) -> None:
        self.tried: list[str] = []

    def generate(self, request):
        self.tried.append(request.provider_id)
        if request.provider_id == "studio-hub-a":
            raise BackendError(
                "the location cannot serve this model right now (MODEL_NOT_READY)",
                code="LOCATION_NOT_READY",
                status=409,
                retryable=True,
                upstream_accepted=False,
            )
        return BackendResult(content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01", media_type="image/png", payload={})


class _BusyFirstLocation:
    """Refuses the first site with the Hub's `MACHINE_BUSY` sentence it is given."""

    def __init__(self, said: str) -> None:
        self.said = said
        self.tried: list[str] = []

    def generate(self, request):
        self.tried.append(request.provider_id)
        if request.provider_id == "studio-hub-a":
            raise BackendError(
                f"the location cannot serve this model right now (MACHINE_BUSY): {self.said}",
                code="LOCATION_NOT_READY",
                status=409,
                retryable=True,
                upstream_accepted=False,
            )
        return BackendResult(content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01", media_type="image/png", payload={})


class CanPublishReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definition = registry.definition("studio-hub-local")
        self.model = {
            "modality": "image",
            "model_id": "mlx-community/heavy-model",
            "raw": {"catalogued": True, "memory_eligible": True},
        }

    def _publishable(self, dispatch_state: str | None) -> bool:
        raw = dict(self.model["raw"])
        if dispatch_state is not None:
            raw["dispatch_state"] = dispatch_state
        return registry.models.can_publish(self.definition, {**self.model, "raw": raw})

    def test_an_ineligible_controller_withholds_the_model(self) -> None:
        self.assertFalse(self._publishable("ineligible"))

    def test_an_eligible_controller_offers_it(self) -> None:
        self.assertTrue(self._publishable("eligible"))

    def test_an_absent_dispatch_state_stays_unverified(self) -> None:
        # A Hub that does not publish it must not lose its whole catalogue.
        self.assertTrue(self._publishable(None))

    def _ready_on_a_mac(self, ready: bool | None) -> bool:
        """The live Hub 0.6.206 row shape for one model at studio-1600 (2026-10-30)."""
        raw = {
            "catalogued": True,
            # The package list is the **controller's own disk**: the 1600 controller does not
            # have these files, a joined Mac does.
            "dispatch_state": "ineligible",
            "files_state": "not_installed",
            "memory_eligible": True,
            "ready_on_a_mac": ready,
        }
        return registry.models.can_publish(self.definition, {**self.model, "raw": raw})

    def test_a_joined_mac_that_has_the_model_publishes_despite_the_controllers_disk(self) -> None:
        # Measured live: `studio-hub::mlx-community/gemma-4-26B-A4B-it-qat-4bit` is published
        # while the 1600 controller's own row says `ineligible`/`not_installed`, because the
        # mini at that site reports it cached, ready and fitting. The controller's own disk must
        # not speak for the whole site.
        self.assertTrue(self._ready_on_a_mac(True))

    def test_a_site_where_no_mac_has_it_stays_withheld(self) -> None:
        self.assertFalse(self._ready_on_a_mac(False))

    def test_an_older_hub_that_states_no_per_mac_fact_keeps_the_dispatch_rule(self) -> None:
        self.assertFalse(self._ready_on_a_mac(None))


class DependencyTypeTests(unittest.TestCase):
    """The Hub types Whisper turbo `dependency` (two Qwen3 packages list it) yet catalogues it as
    the Subtitle model; only the catalogued one may be offered. Real companions are uncatalogued."""

    def _publishable(self, **raw) -> bool:
        model = {"modality": "transcription", "model_id": "mlx-community/whisper-large-v3-turbo",
                 "raw": {"package_type": "dependency", "dispatch_state": "eligible", **raw}}
        return registry.models.can_publish(registry.definition("studio-hub-local"), model)

    def test_a_dependency_the_catalogue_describes_is_offered(self) -> None:
        self.assertTrue(self._publishable(catalogued=True, catalogue_type=None))

    def test_a_dependency_the_catalogue_omits_stays_withheld(self) -> None:
        self.assertFalse(self._publishable(catalogued=False))

    def test_a_dependency_when_the_catalogue_did_not_answer_stays_withheld(self) -> None:
        self.assertFalse(self._publishable())

    def test_a_catalogue_that_says_dependency_stays_withheld(self) -> None:
        self.assertFalse(self._publishable(catalogued=True, catalogue_type="dependency"))


class RerouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(_TEMP_ROOT / f"reroute-{id(self)}.sqlite3")

    def tearDown(self) -> None:
        self.store.close()

    def _run(self, params: dict, provider_id: str = "studio-hub-a"):
        row, _ = self.store.submit(
            modality="image",
            model_id=MODEL,
            params=params,
            idempotency_key=f"reroute-{id(self)}-{provider_id}-{len(params)}",
            provider_id=provider_id,
        )
        self.assertTrue(self.store.mark_running(row["id"]))
        adapter = _RefuseFirstLocation()
        with patch.object(worker_module, "registry", _TwoLocations()), \
                patch.object(worker_module, "adapter_for", return_value=adapter), \
                patch.object(fleet, "snapshot", _snapshot), \
                patch.object(registry.models, "enabled_models",
                             _TwoLocations.models.enabled_models):
            worker_module.execute_job(row, self.store)
        return adapter, self.store.get(row["id"])

    def test_a_site_refusal_moves_the_job_to_the_next_location(self) -> None:
        adapter, settled = self._run({"prompt": "a cat"})
        self.assertEqual(adapter.tried, ["studio-hub-a", "studio-hub-b"])
        self.assertEqual(settled["state"], "succeeded", settled)
        # The row now names the site that actually took the work.
        self.assertEqual(settled["provider_id"], "studio-hub-b")

    def test_a_voice_is_not_re_routed_to_a_location_without_the_clip(self) -> None:
        adapter, settled = self._run({"prompt": "a cat", "voice_id": "studio-hub-a::voice-1"})
        self.assertEqual(adapter.tried, ["studio-hub-a"], "a pinned voice must stay put")
        # **And then it waits, rather than failing.** This asserted `failed` until
        # the queue learned to hold transient site refusals — which is what a busy
        # controller is. The subject of this test is that a pinned voice is never
        # sent to a location without its reference clip, and that is still exactly
        # what it checks: one location was tried. The outcome moved because the
        # honest one for "that site is busy right now" is to wait, not to report the
        # caller's work as broken.
        self.assertEqual(settled["state"], "accepted", settled)
        # The reason is the sentence the Jobs window shows, not the code — which is
        # the point of storing it: a reader should not have to know the vocabulary.
        self.assertIn("cannot serve this model right now", settled["defer_reason"] or "")
        self.assertIsNone(settled.get("error_code"), "a waiting job carries no failure")


class UpdateDrainTests(unittest.TestCase):
    """Kind I (2026-10-19) — a location installing an update is not a candidate until it is back.

    The Hub says so itself: *"this Mac is finishing its current work to install an update"* —
    885 lines across the two live gateway logs on 2026-10-19. The selection re-offered it
    anyway, and job `97bdce60-90f4-4f47-b789-073744e87ea7` was sent to the same installing Mac
    **124 times** (`attempt_count` 126) in 80 minutes before failing with a sentence naming that
    one machine 124 times — a Mac that was going to be back in minutes.
    """

    DRAIN_SAID = "this Mac is finishing its current work to install an update"
    BUSY_SAID = "local is reserved for other work on this Mac"

    def setUp(self) -> None:
        self.store = JobStore(_TEMP_ROOT / f"drain-{id(self)}.sqlite3")
        self.addCleanup(self.store.close)
        fleet._DRAIN_COOLDOWN.clear()
        self.addCleanup(fleet._DRAIN_COOLDOWN.clear)

    def _dispatch_once(self, said: str):
        row, _ = self.store.submit(
            modality="image", model_id=MODEL, params={"prompt": "a cat"},
            idempotency_key=f"drain-{id(self)}-{said[:8]}", provider_id="studio-hub-a",
        )
        self.assertTrue(self.store.mark_running(row["id"]))
        adapter = _BusyFirstLocation(said)
        with patch.object(worker_module, "registry", _TwoLocations()), \
                patch.object(worker_module, "adapter_for", return_value=adapter), \
                patch.object(fleet, "snapshot", _snapshot), \
                patch.object(registry.models, "enabled_models",
                             _TwoLocations.models.enabled_models):
            worker_module.execute_job(row, self.store)
        return adapter

    def _candidates(self) -> set[str]:
        with patch.object(fleet, "snapshot", _snapshot), \
                patch.object(registry.models, "enabled_models",
                             _TwoLocations.models.enabled_models):
            return {c["location_id"] for c in fleet.routing_candidates(MODEL, "image")}

    def test_an_installing_location_is_rested_and_offered_again_when_it_is_back(self) -> None:
        adapter = self._dispatch_once(self.DRAIN_SAID)
        self.assertEqual(adapter.tried, ["studio-hub-a", "studio-hub-b"],
                         "the job moved to the location that could take it")
        self.assertTrue(fleet.is_draining_provider("studio-hub-a"))
        self.assertEqual(self._candidates(), {"b"}, "and it is not offered again")
        # The rest is a window, not a permanent exclusion: the install finishes.
        later = time.time() + fleet.DRAIN_COOLDOWN_S + 1
        self.assertFalse(fleet.is_draining_provider("studio-hub-a", now=later))
        self.assertEqual(self._candidates(), {"a", "b"})

    def test_a_mac_that_is_merely_busy_stays_a_candidate(self) -> None:
        """`MACHINE_BUSY` is not the marker — a busy Mac is a moment, and the code says so.

        *"local is reserved for other work on this Mac"* (27 measured) frees in seconds; resting
        it for the whole window would delay work the fleet is about to run. Only the install is a
        deliberate stop.
        """
        self._dispatch_once(self.BUSY_SAID)
        self.assertFalse(fleet.is_draining_provider("studio-hub-a"))
        self.assertEqual(self._candidates(), {"a", "b"})

    def test_a_non_hub_lane_never_starts_a_rest(self) -> None:
        fleet.note_drain_refusal("pool", self.DRAIN_SAID)
        fleet.note_drain_refusal("openrouter-main", self.DRAIN_SAID)
        self.assertFalse(fleet.is_draining_provider("pool"))
        self.assertFalse(fleet.is_draining_provider("openrouter-main"))


class HealthHeaderTests(unittest.TestCase):
    """`/health` counts come from the cheap paths; they must agree with the full ones."""

    def test_provider_counts_match_describe_all(self) -> None:
        described = registry.describe_all()
        self.assertEqual(registry.provider_counts(), {
            "total": len(described),
            "configured": sum(1 for p in described if p["configured"]),
            "enabled": sum(1 for p in described if p["enabled"]),
        })

    def test_summary_offers_what_enabled_models_publishes(self) -> None:
        summary = registry.models.summary()
        for modality in {m for d in registry.definitions() for m in d.modalities}:
            self.assertEqual(summary["by_modality"].get(modality, 0),
                             len(registry.models.enabled_models(modality)), modality)


if __name__ == "__main__":
    unittest.main(verbosity=2)
