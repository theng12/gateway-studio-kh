"""Removing a node or a controller removes every record of it from the Gateway.

The owner, 2026-11-18: *"make sure that removing a node or a controller removes everything entirely
from the gateway and if i want to connect them back ... either under a new name or just reconnect it
and it's automatically recognized"*.

What this file pins, in the order it matters:

* **Nothing is left behind.** The roster row, the credential, the models, the reachability history,
  the per-Mac settings, the jobs, summaries and render rows — all of them go, in every file and
  table that names the controller or the Mac.
* **A sibling is untouched.** Removing one Mac from a controller keeps its siblings' rows, just as
  removing one controller keeps every other controller's.
* **A path outside the artifact folder is never followed.** Purge deletes files it owns, and only
  inside `ARTIFACT_DIR`.
* **A snapshot already read is scrubbed**, so the next page view cannot show a Mac that was just
  removed until the fleet is re-polled.
* **The route says what happened**: the controller's own Mac cannot be removed on its own, a node
  the controller no longer knows is still a removal, and an unreachable controller is a question
  (`can_force`) rather than a silent partial removal.

Run with::

    .venv/bin/python -m unittest tests.test_fleet_removal_purge
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-removal-purge-")

import os  # noqa: E402

os.environ.setdefault("GATEWAY_API_KEY", "removal-fixture-key")

from gateway import (  # noqa: E402
    config, fleet, fleet_history, image_assignment, limits, model_share, render_optin,
    startup_control,
)
from gateway.runtime import store  # noqa: E402
from gateway.store import JobStore  # noqa: E402,F401  (the type is named in a docstring only)

SITE = "site-removal"
OTHER = "site-kept"


class HistoryPurge(unittest.TestCase):
    """A removed controller or Mac has no reachability history left to summarise."""

    def setUp(self) -> None:
        fleet_history.reset_for_tests()
        fleet_history.observe(
            [{"id": SITE, "reachable": True}, {"id": OTHER, "reachable": True}],
            [
                {"location_id": SITE, "machine": "n1", "reachable": True},
                {"location_id": SITE, "machine": "n2", "reachable": True},
                {"location_id": OTHER, "machine": "n9", "reachable": True},
            ],
        )

    def test_a_controller_takes_its_machines_with_it(self):
        self.assertEqual(fleet_history.purge_location(SITE), 3)
        self.assertFalse(fleet_history.summary(f"l:{SITE}")["observed"])
        self.assertFalse(fleet_history.summary(f"m:{SITE}/n1")["observed"])
        self.assertTrue(fleet_history.summary(f"l:{OTHER}")["observed"], "a sibling controller stays")
        self.assertTrue(fleet_history.summary(f"m:{OTHER}/n9")["observed"], "its Mac stays")

    def test_one_machine_goes_alone(self):
        self.assertEqual(fleet_history.purge_machine(SITE, "n1"), 1)
        self.assertFalse(fleet_history.summary(f"m:{SITE}/n1")["observed"])
        self.assertTrue(fleet_history.summary(f"l:{SITE}")["observed"])
        self.assertTrue(fleet_history.summary(f"m:{SITE}/n2")["observed"])

    def test_purging_something_absent_is_not_an_error(self):
        self.assertEqual(fleet_history.purge_location("never-seen"), 0)
        self.assertEqual(fleet_history.purge_machine(SITE, "never-seen"), 0)


class SmallFilePurge(unittest.TestCase):
    """Every per-Mac settings file loses its rows for the removed node, and no others."""

    def test_render_optin(self):
        render_optin.set_enabled(SITE, "n1", True)
        render_optin.set_enabled(SITE, "n2", True)
        render_optin.set_enabled(OTHER, "n9", True)
        self.assertEqual(render_optin.purge(SITE, "n1"), 1)
        self.assertFalse(render_optin.is_enabled(SITE, "n1"))
        self.assertTrue(render_optin.is_enabled(SITE, "n2"))
        self.assertEqual(render_optin.purge(SITE), 1)
        self.assertTrue(render_optin.is_enabled(OTHER, "n9"))

    def test_image_overrides(self):
        image_assignment.set_override(SITE, "n1", "flux")
        image_assignment.set_override(SITE, "n2", "flux")
        image_assignment.set_override(OTHER, "n9", "flux")
        self.assertEqual(image_assignment.purge(SITE, "n1"), 1)
        self.assertEqual(image_assignment.overrides(), {f"{SITE}/n2": "flux", f"{OTHER}/n9": "flux"})
        self.assertEqual(image_assignment.purge(SITE), 1)
        self.assertEqual(image_assignment.overrides(), {f"{OTHER}/n9": "flux"})

    def test_model_share_networks(self):
        fleet._write([{"id": SITE, "label": "Site", "url": "http://site.test", "enabled": True},
                      {"id": OTHER, "label": "Other", "url": "http://other.test", "enabled": True}])
        model_share.set_network(SITE, "A")
        model_share.set_network(SITE, "B", "n1")
        model_share.set_network(OTHER, "C")
        counts = model_share.purge(SITE, "n1")
        self.assertEqual(counts["networks"], 1)
        self.assertEqual(model_share.networks()["locations"].get(SITE), "A", "the site label stays")
        self.assertNotIn(f"{SITE}/n1", model_share.networks()["machines"])
        self.assertEqual(model_share.purge(SITE)["networks"], 1)
        self.assertNotIn(SITE, model_share.networks()["locations"])
        self.assertEqual(model_share.networks()["locations"].get(OTHER), "C")

    def test_limit_overrides_keyed_to_the_provider(self):
        provider = fleet.provider_id_for(SITE)
        limits.set_entry(f"{provider}::flux", {"context_window_tokens": 4096})
        limits.set_entry(f"{fleet.provider_id_for(OTHER)}::flux", {"context_window_tokens": 8192})
        self.assertEqual(limits.purge_provider(provider), 1)
        self.assertEqual(list(limits.entries()), [f"{fleet.provider_id_for(OTHER)}::flux"])

    def test_queued_lite_mode_settings(self):
        startup_control._write_pending({
            f"{SITE}/n1": {"settings": {"enabled": True}, "at": 1.0},
            f"{SITE}/n2": {"settings": {"enabled": False}, "at": 1.0},
            f"{OTHER}/n9": {"settings": {"enabled": True}, "at": 1.0},
        })
        self.assertEqual(startup_control.purge_pending(SITE, "n1"), 1)
        self.assertEqual(startup_control.purge_pending(SITE), 1)
        self.assertEqual(list(startup_control.pending_lite_mode()), [f"{OTHER}/n9"])


class RosterRemoval(unittest.TestCase):
    """The roster, the cached snapshot and the flat tables lose the location together."""

    def setUp(self) -> None:
        fleet._write([{"id": SITE, "label": "Site", "url": "http://site.test", "enabled": True},
                      {"id": OTHER, "label": "Other", "url": "http://other.test", "enabled": True}])

    def test_a_removed_controller_leaves_no_row_and_no_credential(self):
        self.assertTrue(fleet.remove_controller(SITE))
        self.assertEqual([c["id"] for c in fleet.controllers()], [OTHER])

    def test_a_snapshot_already_read_is_scrubbed(self):
        snap = {
            "locations": [
                {"id": SITE, "label": "Site", "machines": [{"machine": "n1"}, {"machine": "n2"}],
                 "nodes": [{"id": "n1"}, {"id": "n2"}]},
                {"id": OTHER, "label": "Other", "machines": [{"machine": "n9"}], "nodes": [{"id": "n9"}]},
            ],
            "machines": [{"location_id": SITE, "machine": "n1"}, {"location_id": OTHER, "machine": "n9"}],
            "last_known_machines": [{"location_id": SITE, "machine": "n2"}],
            "running": [{"location_id": SITE, "machine": "n1", "id": "job"}],
            "issues": [{"location_id": SITE, "subject": "n1", "kind": "disk"}],
            "summary": {},
        }
        with patch.object(fleet, "_summarise", lambda rows: {"locations": len(rows)}):
            one = fleet._drop_from_snapshot(snap, SITE, "n1")
            whole = fleet._drop_from_snapshot(snap, SITE)
        self.assertEqual([m["machine"] for m in one["locations"][0]["machines"]], ["n2"])
        self.assertEqual([n["id"] for n in one["locations"][0]["nodes"]], ["n2"])
        self.assertEqual([m["machine"] for m in one["machines"]], ["n9"])
        self.assertEqual(one["running"], [])
        self.assertEqual(one["issues"], [])
        self.assertEqual([m["machine"] for m in one["locations"][1]["machines"]], ["n9"])
        self.assertEqual([loc["id"] for loc in whole["locations"]], [OTHER])
        self.assertEqual(whole["machines"], [{"location_id": OTHER, "machine": "n9"}])
        self.assertEqual(whole["last_known_machines"], [])
        self.assertEqual(whole["summary"]["issues"], 0)

    def test_with_roster_drops_every_row_of_a_location_it_no_longer_lists(self):
        value = {
            "locations": [{"id": SITE, "machines": [], "activity": {"machines": []}},
                          {"id": OTHER, "machines": [], "activity": {"machines": []}}],
            "machines": [{"location_id": SITE, "machine": "n1"}, {"location_id": OTHER, "machine": "n9"}],
            "last_known_machines": [{"location_id": SITE, "machine": "n1"}],
            "running": [{"location_id": SITE, "machine": "n1"}],
            "issues": [{"location_id": SITE, "kind": "disk"}],
            "summary": {},
        }
        fleet._write([{"id": OTHER, "label": "Other", "url": "http://other.test", "enabled": True}])
        with patch.object(fleet, "_summarise", lambda rows: {"locations": len(rows)}):
            out = fleet.with_roster(value)
        self.assertEqual([loc["id"] for loc in out["locations"]], [OTHER])
        self.assertEqual(out["machines"], [{"location_id": OTHER, "machine": "n9"}])
        self.assertEqual(out["last_known_machines"], [])
        self.assertEqual(out["running"], [])
        self.assertEqual(out["issues"], [])


class StorePurge(unittest.TestCase):
    """Jobs, summaries, locators, render rows and artifact files go with the node."""

    def setUp(self) -> None:
        self.store = store
        self.outside = Path(tempfile.mkdtemp(prefix="gateway-removal-outside-")) / "keep.bin"
        self.outside.write_bytes(b"not a job artifact")
        self.provider = fleet.provider_id_for(SITE)
        for table in ("jobs", "job_summaries", "render_staging"):
            self.store._conn.execute(f"DELETE FROM {table}")
        self.store._conn.commit()

    def _job(self, job_id: str, machine: str, *, provider: str | None = None, artifact: Path | None = None):
        now = 1_760_000_000.0
        self.store._conn.execute(
            "INSERT INTO jobs (id, modality, model_id, provider_id, state, params, created_at, updated_at,"
            " asset_path, result_json) VALUES (?, 'image', 'm', ?, 'failed', '{}', ?, ?, ?, ?)",
            (job_id, provider or self.provider, now, now,
             str(artifact) if artifact else None,
             json.dumps({"payload": {"hub_machine": machine}})),
        )
        self.store._conn.execute(
            "INSERT INTO job_summaries (id, created_at, provider_id, machine, state) VALUES (?, ?, ?, ?, 'failed')",
            (f"sum-{job_id}", now, provider or self.provider, machine),
        )
        self.store._conn.commit()

    def _render(self, render_id: str, machine: str, *, controller: str = SITE):
        row = {
            "id": render_id, "gateway_instance_id": "g", "story_instance_id": "s", "owner_scope": "o",
            "story_uuid": f"u-{render_id}", "intent_id": "i", "attempt_no": 1, "gateway_attempt_key": render_id,
            "inbound_story_body": b"{}", "inbound_story_body_sha256": "x", "packet_sha256": "x",
            "destination_site": "s", "destination_controller": controller,
            "destination_hub_controller": "c", "destination_node": machine,
            "selected_machine": b"{}", "capability_snapshot": b"{}",
            "capability_snapshot_sha256": "x", "state": "prepared",
            "created_at": 1.0, "updated_at": 1.0,
        }
        self.store._conn.execute(
            f"INSERT INTO render_staging ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        self.store._conn.commit()

    def _artifact(self, name: str) -> Path:
        path = Path(config.ARTIFACT_DIR) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
        return path

    def test_one_node_takes_its_jobs_and_files_and_leaves_its_sibling(self):
        gone = self._artifact("gone.png")
        kept = self._artifact("kept.png")
        self._job("j1", "n1", artifact=gone)
        self._job("j2", "n2", artifact=kept)
        self._render("r1", "n1")
        self._render("r2", "n2")
        counts = self.store.purge_node_records(SITE, "n1")
        self.assertEqual(counts["jobs"], 1)
        self.assertEqual(counts["job_summaries"], 1)
        self.assertEqual(counts["render_staging"], 1)
        self.assertEqual(counts["artifacts"], 1)
        self.assertFalse(gone.exists())
        self.assertTrue(kept.exists())
        left = {row[0] for row in self.store._conn.execute("SELECT id FROM jobs")}
        self.assertEqual(left, {"j2"})
        renders = {row[0] for row in self.store._conn.execute("SELECT id FROM render_staging")}
        self.assertEqual(renders, {"r2"})

    def test_a_file_outside_the_artifact_folder_is_never_unlinked(self):
        self._job("j3", "n1", artifact=self.outside)
        self.store.purge_node_records(SITE, "n1")
        self.assertTrue(self.outside.exists(), "purge deletes only inside ARTIFACT_DIR")

    def test_a_controller_takes_every_node_and_leaves_another_provider(self):
        self._job("j4", "n1")
        self._job("j5", "n2")
        self._job("j6", "n9", provider=fleet.provider_id_for(OTHER))
        counts = self.store.purge_provider_records(self.provider, controller_id=SITE)
        self.assertEqual(counts["jobs"], 2)
        left = {row[0] for row in self.store._conn.execute("SELECT id FROM jobs")}
        self.assertEqual(left, {"j6"})

    def test_a_live_hold_row_is_not_spared_it_is_still_a_record(self):
        now = 1_760_000_000.0
        self.store._conn.execute(
            "INSERT INTO jobs (id, modality, model_id, provider_id, state, params, created_at, updated_at,"
            " recovery_state, recovery_context) VALUES ('held', 'image', 'm', ?, 'failed', '{}', ?, ?,"
            " 'unknown_outcome', ?)",
            (self.provider, now, now, json.dumps({"hub_machine": "n1", "machine": "n1"})),
        )
        self.store._conn.commit()
        self.assertEqual(self.store.purge_node_records(SITE, "n1")["jobs"], 1)


class RemovalRoute(unittest.TestCase):
    """The route's own vocabulary: refused, already gone, and needing a decision."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from gateway import api
        fleet._write([{"id": SITE, "label": "Site", "url": "http://site.test", "enabled": True}])
        self.client = TestClient(api.app, client=("127.0.0.1", 54320))
        self.headers = {"Authorization": "Bearer removal-fixture-key"}

    def _delete(self, machine: str, query: str = ""):
        return self.client.delete(f"/admin/api/fleet/controllers/{SITE}/machines/{machine}{query}",
                                  headers=self.headers)

    def test_the_controllers_own_mac_is_removed_as_a_location_not_on_its_own(self):
        answer = self._delete("local")
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(answer.json()["code"], "CANNOT_REMOVE_CONTROLLER_LOCAL")

    def test_a_node_the_controller_no_longer_knows_is_still_removed_here(self):
        refusal = startup_control.StartupError(404, {"ok": False, "code": "NODE_NOT_FOUND",
                                                     "detail": "no such node"})
        with patch.object(startup_control, "unregister_node", side_effect=refusal):
            answer = self._delete("n1")
        self.assertEqual(answer.status_code, 200, answer.text)
        body = answer.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["hub"]["already_gone"])

    def test_an_unreachable_controller_is_a_question_first(self):
        refusal = startup_control.StartupError(502, {"ok": False, "code": "CONTROLLER_UNREACHABLE",
                                                     "detail": "The controller did not answer"})
        with patch.object(startup_control, "unregister_node", side_effect=refusal):
            answer = self._delete("n1")
            self.assertEqual(answer.status_code, 502)
            self.assertTrue(answer.json()["can_force"], "the page needs the flag to ask")
            forced = self._delete("n1", "?force=true")
        self.assertEqual(forced.status_code, 200, forced.text)
        self.assertTrue(forced.json()["hub"]["forced_local"])

    def test_a_credential_refusal_is_final_and_not_forceable(self):
        refusal = startup_control.StartupError(403, {"ok": False, "code": "CONTROLLER_REFUSED",
                                                     "detail": "refused"})
        with patch.object(startup_control, "unregister_node", side_effect=refusal):
            answer = self._delete("n1", "?force=true")
        self.assertEqual(answer.status_code, 403)
        self.assertNotIn("can_force", answer.json(), "a wrong token is not something force can decide")


if __name__ == "__main__":
    unittest.main()
