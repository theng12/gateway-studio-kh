"""The admin trace read follows a stored Hub job and exposes only safe evidence."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-upstream-trace-")

from gateway import admin  # noqa: E402
from gateway.adapters import studio_hub  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB  # noqa: E402


class UpstreamTraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job = {
            "id": "gateway-job-1",
            "provider_id": "studio-hub-site-a",
            "upstream_job_id": "hub-batch-1",
            "modality": "music",
        }
        self.definition = SimpleNamespace(
            id=self.job["provider_id"],
            kind=KIND_STUDIO_HUB,
            base_url="http://definition.invalid",
        )

    def test_success_uses_stored_mapping_and_returns_only_allowlisted_trace(self) -> None:
        raw_trace = {
            "version": 1,
            "blocked_at": 10.0,
            "queued_items": [0],
            "candidates": [{
                "machine": "1603",
                "studio": "music@1603",
                "gate": "peer_not_dispatch_ready",
                "detail": "token=never-return-this",
            }],
            "selected": {
                "at": 11.0,
                "item_index": 0,
                "machine": "local",
                "studio": "music",
                "prompt": "never return this",
            },
            "truncated": False,
        }
        snapshot = {
            "known": True,
            "state": "done",
            "item": {"prompt": "private prompt"},
            "summary": {
                "dispatch_trace": raw_trace,
                "artifact_url": "http://worker.invalid/private",
                "prompt": "private prompt",
            },
        }
        values = {
            "hub_token": "top-secret-token",
            "base_url": "http://site-a.internal",
        }
        with patch.object(admin.store, "get", return_value=self.job), \
                patch.object(admin.registry, "definition", return_value=self.definition), \
                patch.object(admin.registry.credentials, "resolved", return_value=values), \
                patch.object(studio_hub, "batch_snapshot", return_value=snapshot) as called:
            answer = admin.upstream_job_trace(self.job["id"])

        called.assert_called_once_with(
            values,
            values["base_url"],
            self.job["upstream_job_id"],
            kind="music",
            local=False,
        )
        self.assertEqual(set(answer), {
            "job_id", "state", "selected_machine", "dispatch_trace"
        })
        self.assertEqual(answer["job_id"], self.job["id"])
        self.assertEqual(answer["state"], "done")
        self.assertEqual(answer["selected_machine"], "local")
        self.assertEqual(answer["dispatch_trace"]["candidates"], [{
            "machine": "1603",
            "studio": "music@1603",
            "gate": "peer_not_dispatch_ready",
        }])
        serialized = json.dumps(answer)
        for forbidden in (
            "top-secret-token", "site-a.internal", "worker.invalid", "private prompt", "detail"
        ):
            self.assertNotIn(forbidden, serialized)

    def test_projection_rejects_oversized_numerics_and_enforces_final_byte_bound(self) -> None:
        huge = 10 ** 4000
        trace = admin._bounded_dispatch_trace({
            "version": 1,
            "blocked_at": huge,
            "queued_items": [huge] * 65,
            "candidates": [{
                "machine": "m" * 80,
                "studio": "s" * 80,
                "gate": "catalog_missing",
            }] * 65,
            "selected": {
                "at": float("inf"),
                "item_index": huge,
                "machine": "local",
                "studio": "music",
            },
            "truncated": False,
        })

        self.assertIsNotNone(trace)
        self.assertLessEqual(
            len(json.dumps(trace, separators=(",", ":")).encode()), 32 * 1024
        )
        self.assertEqual(trace["queued_items"], [])
        self.assertNotIn("blocked_at", trace)
        self.assertNotIn("at", trace["selected"])
        self.assertNotIn("item_index", trace["selected"])
        self.assertTrue(trace["truncated"])

    def test_rejects_unknown_non_hub_and_unaccepted_jobs(self) -> None:
        cases = (
            (None, self.definition, 404),
            (self.job, None, 409),
            (self.job, SimpleNamespace(kind="openai_compat", base_url=""), 409),
            ({**self.job, "upstream_job_id": ""}, self.definition, 409),
        )
        for row, definition, status in cases:
            with self.subTest(row=row, definition=definition), \
                    patch.object(admin.store, "get", return_value=row), \
                    patch.object(admin.registry, "definition", return_value=definition):
                self.assertEqual(admin.upstream_job_trace("gateway-job-1").status_code, status)

    def test_distinguishes_unknown_hub_batch_from_hub_outage(self) -> None:
        for snapshot, status in (({"known": False}, 404), (None, 502)):
            with self.subTest(snapshot=snapshot), \
                    patch.object(admin.store, "get", return_value=self.job), \
                    patch.object(admin.registry, "definition", return_value=self.definition), \
                    patch.object(admin.registry.credentials, "resolved", return_value={}), \
                    patch.object(studio_hub, "batch_snapshot", return_value=snapshot):
                self.assertEqual(admin.upstream_job_trace(self.job["id"]).status_code, status)

    def test_route_is_admin_authenticated(self) -> None:
        route = next(
            route for route in admin.router.routes
            if getattr(route, "path", "") == "/admin/api/jobs/{job_id}/upstream"
        )
        self.assertIn(admin.require_admin, [dependency.call for dependency in route.dependant.dependencies])


if __name__ == "__main__":
    unittest.main()
