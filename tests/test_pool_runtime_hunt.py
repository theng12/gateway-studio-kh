#!/usr/bin/env python3
"""Runtime hunt verification for pool changes:
1. Image pool advertised in Create using actual member metadata, yet no phantom alias when no eligible member.
2. Disabled providers do not add capacity in pool budget.
3. Image pool max attempt check: no route_pool call past limit, uncounted not-ready/hand-back attempt unaffected.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-pool-hunt-")

from gateway import admin, concurrency, fleet, image_assignment, pools, worker as worker_module  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.definitions import ProviderDefinition  # noqa: E402
from starlette.requests import Request  # noqa: E402


class _Site:
    def __init__(self, site_id: str) -> None:
        self.id = f"studio-hub-{site_id}"
        self.label = site_id


class _PoolStore:
    """In-memory store matching test_job_retry_policy _PoolStore."""

    def __init__(self, job: dict[str, Any]) -> None:
        self.job = dict(job)
        self.log: list[dict[str, Any]] = []
        self.tried: list[str] = []
        self.routes: list[str] = []
        self.deferred: list[dict[str, Any]] = []

    def get(self, job_id: str) -> dict[str, Any]:
        return dict(self.job, attempt_log=self.log, attempted_locations=self.tried)

    def record_attempt(self, job_id: str, *, location_id: str, code: str, detail: str) -> list[dict[str, Any]]:
        self.log.append({
            "location": location_id, "code": code, "detail": detail,
            "at": 1.0 + len(self.log),
        })
        if location_id and location_id not in self.tried:
            self.tried.append(location_id)
        return list(self.log)

    def record_route(self, job_id: str, provider_id: str, *, model_id: str | None = None) -> bool:
        self.routes.append(provider_id)
        return True

    def defer(self, job_id: str, *, delay_s: float, reason: str, new_round: bool = False,
              unsent: bool = False) -> bool:
        self.deferred.append({"delay_s": delay_s, "reason": reason, "unsent": unsent})
        return True


class CreateImagePoolMetadataTests(unittest.TestCase):
    """Verify Create modal metadata handling for image pools."""

    def test_image_pool_advertised_in_create_using_actual_member_metadata(self) -> None:
        def_hub = ProviderDefinition(
            id="studio-hub-local", display_name="Studio Hub", category="language",
            kind="studio_hub", base_url="http://127.0.0.1:8787", modalities=("image",),
            discovery="static", data_use="none", free_tier=True,
        )
        model_info = {"model_id": "z-image-turbo-6b-mlx-4bit"}
        pool_def = {"name": "standard-image-fleet", "display_name": "Standard Image Fleet"}
        catalog_rows = [
            {"id": "studio-hub::z-image-turbo-6b-mlx-4bit", "model_id": "z-image-turbo-6b-mlx-4bit", "chargeable": False},
            {"id": "studio-hub::standard-image-fleet", "model_id": "standard-image-fleet", "chargeable": False},
        ]

        req = MagicMock(spec=Request)
        req.headers = {}
        req.cookies = {}

        with patch.object(admin, "_guest_paid_allowed", return_value=True), \
                patch.object(pools, "image_pools", return_value=[pool_def]), \
                patch.object(image_assignment, "pool_models", return_value=["z-image-turbo-6b-mlx-4bit"]), \
                patch.object(registry.models, "enabled_models", return_value=[(def_hub, model_info)]), \
                patch.object(admin.catalog, "rows", return_value=catalog_rows):
            res = admin.create_models(req)

        image_rows = res["by_modality"]["image"]
        pool_row = next((r for r in image_rows if r["id"] == "studio-hub::standard-image-fleet"), None)
        self.assertIsNotNone(pool_row, "standard-image-fleet must be advertised in Create when member is enabled")
        self.assertEqual(pool_row["model_id"], "standard-image-fleet")
        self.assertEqual(pool_row["provider_id"], "studio-hub")
        self.assertEqual(pool_row["provider_name"], fleet.FLEET_DISPLAY_NAME)
        self.assertTrue(pool_row["free_lane"])
        self.assertEqual(pool_row["data_use"], "none")

    def test_no_phantom_alias_when_no_eligible_member(self) -> None:
        pool_def = {"name": "standard-image-fleet", "display_name": "Standard Image Fleet"}
        catalog_rows = [
            {"id": "studio-hub::standard-image-fleet", "model_id": "standard-image-fleet", "chargeable": False},
        ]

        req = MagicMock(spec=Request)
        req.headers = {}
        req.cookies = {}

        # No enabled models in registry
        with patch.object(admin, "_guest_paid_allowed", return_value=True), \
                patch.object(pools, "image_pools", return_value=[pool_def]), \
                patch.object(image_assignment, "pool_models", return_value=["z-image-turbo-6b-mlx-4bit"]), \
                patch.object(registry.models, "enabled_models", return_value=[]), \
                patch.object(admin.catalog, "rows", return_value=catalog_rows):
            res = admin.create_models(req)

        image_ids = [r["id"] for r in res["by_modality"]["image"]]
        self.assertNotIn(
            "studio-hub::standard-image-fleet", image_ids,
            "Pool alias must NOT be advertised when no member is eligible/enabled"
        )


class DisabledProvidersCapacityTests(unittest.TestCase):
    """Verify disabled providers are filtered out and do not add concurrency capacity."""

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_disabled_providers_do_not_add_capacity_in_pool_budget(self) -> None:
        with patch.object(concurrency, "_settings_providers",
                          return_value={"prov_a": {"limit": 5}, "prov_b": {"limit": 10}}), \
                patch.object(registry.credentials, "is_enabled", side_effect=lambda p: p == "prov_a"):
            budget = concurrency.pool_budget(["prov_a::m1", "prov_b::m2"])
            self.assertEqual(budget.limit, 5)
            self.assertIn("prov_a 5", budget.detail)
            self.assertNotIn("prov_b", budget.detail)

    def test_all_disabled_providers_yield_zero_budget(self) -> None:
        with patch.object(concurrency, "_settings_providers",
                          return_value={"prov_a": {"limit": 5}, "prov_b": {"limit": 10}}), \
                patch.object(registry.credentials, "is_enabled", return_value=False):
            budget = concurrency.pool_budget(["prov_a::m1", "prov_b::m2"])
            self.assertEqual(budget.limit, 0)
            self.assertEqual(budget.detail, "the pool's members' providers: ")

    def test_worker_provider_budget_excludes_disabled_providers(self) -> None:
        from gateway.worker import Worker

        with patch.object(pools, "members", return_value=["prov_a::m1", "prov_b::m2"]), \
                patch.object(concurrency, "_settings_providers",
                             return_value={"prov_a": {"limit": 4}, "prov_b": {"limit": 6}}), \
                patch.object(registry.credentials, "is_enabled", side_effect=lambda p: p == "prov_b"):
            budget = Worker(None, interval_s=10.0)._provider_budget("pool", "chat", "free")
            self.assertEqual(budget.limit, 6)


class ImagePoolMaxAttemptRerouteTests(unittest.TestCase):
    """Verify image pool reroute honors max_attempts ceiling while uncounted attempts remain unaffected."""

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.addCleanup(worker_module._ROOM_HOOKS.clear)

    def test_image_pool_max_attempts_exceeded_prevents_route_pool_call(self) -> None:
        store = _PoolStore({
            "id": "pool-job-1",
            "modality": "image",
            "params": json.dumps({"_pool_id": "standard-image-fleet"}),
            "state": "queued",
        })
        store.record_attempt("pool-job-1", location_id="studio-hub-a", code="PROVIDER_FAILED", detail="err1")
        store.record_attempt("pool-job-1", location_id="studio-hub-b", code="PROVIDER_FAILED", detail="err2")

        route_pool_called = False

        def route_pool(*args: Any, **kwargs: Any) -> tuple[_Site, str, str]:
            nonlocal route_pool_called
            route_pool_called = True
            return _Site("c"), "member-model", "note"

        with patch.object(worker_module, "_pool_members_for", return_value=["member-model"]), \
                patch.object(worker_module.fleet, "route_pool", route_pool):
            result = worker_module._next_machine(
                store, "pool-job-1", "member-model", "image", "studio-hub-c",
                code="PROVIDER_FAILED", detail="err3",
            )

        self.assertIsNone(result, "Exceeding max_attempts must yield None")
        self.assertFalse(route_pool_called, "fleet.route_pool must NOT be called when max_attempts is reached")

    def test_uncounted_not_ready_attempt_does_not_consume_max_attempts_budget(self) -> None:
        store = _PoolStore({
            "id": "pool-job-2",
            "modality": "image",
            "params": json.dumps({"_pool_id": "standard-image-fleet"}),
            "state": "queued",
        })
        # 1 uncounted attempt (NO_AVAILABLE_MACHINE), 1 counted failure
        store.record_attempt("pool-job-2", location_id="studio-hub-a", code="NO_AVAILABLE_MACHINE", detail="not ready")
        store.record_attempt("pool-job-2", location_id="studio-hub-b", code="PROVIDER_FAILED", detail="err1")

        route_pool_called = False

        def route_pool(*args: Any, **kwargs: Any) -> tuple[_Site, str, str]:
            nonlocal route_pool_called
            route_pool_called = True
            return _Site("d"), "member-model", "note"

        with patch.object(worker_module, "_pool_members_for", return_value=["member-model"]), \
                patch.object(worker_module.fleet, "route_pool", route_pool):
            # 3rd total attempt, but only 2nd counted attempt (< max_attempts of 3)
            result = worker_module._next_machine(
                store, "pool-job-2", "member-model", "image", "studio-hub-c",
                code="PROVIDER_FAILED", detail="err2",
            )

        self.assertEqual(result, "studio-hub-d")
        self.assertTrue(route_pool_called, "fleet.route_pool must be called when counted attempts < max_attempts")


if __name__ == "__main__":
    unittest.main()
