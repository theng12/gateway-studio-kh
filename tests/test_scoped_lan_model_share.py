"""Explicit destination scope for the normal fleet model-install API.

The optional ``targets`` array contains canonical ``location_id/machine`` keys.
It narrows destinations only: verified cached Macs outside the scope remain LAN
sources, while omission retains the existing fleet-wide plan.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_lan_model_share import Case, FakeHubs, FISH, _SANDBOX, snapshot  # noqa: E402
from gateway import admin, config, fleet, model_share, runtime, startup_control  # noqa: E402

_SANDBOX_ROOT = Path(_SANDBOX.name).resolve()
_BOOTSTRAP_PATHS = (
    config.APP_SUPPORT,
    config.DB_PATH,
    config.ENV_PATH,
    config.CREDENTIALS_PATH,
    config.MODEL_CACHE_PATH,
    config.ARTIFACT_DIR,
    runtime.store.path,
)
assert all(Path(path).resolve().is_relative_to(_SANDBOX_ROOT) for path in _BOOTSTRAP_PATHS), (
    "Gateway bootstrap paths escaped the inherited test sandbox"
)
assert config.APP_SUPPORT.resolve() != (
    Path.home() / "Library" / "Application Support" / "gateway-kh"
).resolve(), "Gateway bootstrap reached the owner's default Application Support path"

SOURCE_LOCATION = "4bb919aee2e4"
SOURCE_MACHINE = "shop-cache"
TARGET_LOCATION = "b8ccda585b97"
TARGET_MACHINE = "terranash-1101-m4-24-256-hub"
TARGET = f"{TARGET_LOCATION}/{TARGET_MACHINE}"
SKIPPED_LOCATION = "2c7a2403564b"
SKIPPED_MACHINE = "terranash-1603-m2-16-256-hub"
SKIPPED = f"{SKIPPED_LOCATION}/{SKIPPED_MACHINE}"


def scoped_snapshot(*, target: dict | None = None) -> dict:
    """Current network-C shape: one verified source and two missing destinations."""
    out = snapshot(
        (SOURCE_LOCATION, SOURCE_MACHINE, True),
        (TARGET_LOCATION, TARGET_MACHINE, False, target or {}),
        (SKIPPED_LOCATION, SKIPPED_MACHINE, False),
    )
    returned = next(row for row in out["machines"] if row["location_id"] == TARGET_LOCATION
                    and row["machine"] == TARGET_MACHINE)
    returned.update(chip="Apple M4", memory_gb=25.77)
    return out


class ScopedAdmissionTest(Case):
    def setUp(self) -> None:
        super().setUp()
        self.hubs = FakeHubs(self)

    def raw_plan(self) -> dict:
        return model_share.read_plans()[0]

    def test_admin_post_scopes_the_plan_to_the_named_canonical_target(self) -> None:
        fleet_read = scoped_snapshot()
        with patch.object(fleet, "snapshot", lambda: fleet_read):
            answer = admin.fleet_install_everywhere({"package_id": FISH, "targets": [TARGET]})

        self.assertIsInstance(answer, dict)
        self.assertEqual(answer["plan"]["total"], 1)
        self.assertEqual(self.raw_plan()["targets"], [TARGET])
        self.assertEqual([entry["key"] for entry in self.raw_plan()["macs"]], [TARGET])

    def test_admin_post_without_targets_keeps_fleet_wide_admission(self) -> None:
        fleet_read = scoped_snapshot()
        with patch.object(fleet, "snapshot", lambda: fleet_read):
            answer = admin.fleet_install_everywhere({"package_id": FISH})

        self.assertIsInstance(answer, dict)
        waiting = {entry["key"] for entry in self.raw_plan()["macs"] if entry["state"] == "waiting"}
        self.assertEqual(waiting, {TARGET, SKIPPED})

    def test_cached_mac_outside_scope_lends_to_the_only_destination_with_strict_lan(self) -> None:
        fleet_read = scoped_snapshot()
        model_share.start(FISH, fleet_read, targets=[TARGET])
        model_share.tick(fleet_read)

        self.assertEqual(self.hubs.installs, [(TARGET, f"http://{SOURCE_MACHINE}/share")])
        self.assertEqual(self.hubs.peer_only, [True])
        self.assertEqual(self.hubs.shares, [f"{SOURCE_LOCATION}/{SOURCE_MACHINE}"])
        self.assertNotIn(SKIPPED, {entry["key"] for entry in self.raw_plan()["macs"]})

    def test_persisted_scope_survives_restart_and_later_ticks(self) -> None:
        admitted = scoped_snapshot()
        model_share.start(FISH, admitted, targets=[TARGET])
        stored = json.loads(model_share.plans_path().read_text(encoding="utf-8"))["plans"][0]
        self.assertEqual(stored["targets"], [TARGET])
        self.assertEqual([entry["key"] for entry in stored["macs"]], [TARGET])

        with patch.object(model_share, "_ensure_thread") as resumed:
            model_share.resume_on_startup()
        resumed.assert_called_once_with()

        later = scoped_snapshot()
        later["machines"].append({
            "location_id": TARGET_LOCATION,
            "machine": "another-returned-mac",
            "label": "another-returned-mac",
            "reachable": True,
            "hub_version": "0.6.177",
            "installable_models": [{"package_id": FISH, "label": "Fish S2"}],
        })
        next(location for location in later["locations"] if location["id"] == TARGET_LOCATION)["local_models"].append({
            "machine": "another-returned-mac",
            "package_id": FISH,
            "label": "Fish S2",
            "cache_state": "absent",
            "memory_eligible": True,
        })
        model_share.tick(later)

        self.assertEqual([target for target, _ in self.hubs.installs], [TARGET])
        self.assertEqual([entry["key"] for entry in self.raw_plan()["macs"]], [TARGET])

    def test_invalid_scopes_are_rejected_before_any_mutation_or_relay(self) -> None:
        fleet_read = scoped_snapshot()
        cases = {
            "null": None,
            "empty": [],
            "mapping": {"target": TARGET},
            "string": TARGET,
            "tuple": (TARGET,),
            "non-string member": [1],
            "leading space": [f" {TARGET}"],
            "machine without location": [TARGET_MACHINE],
            "missing machine": [f"{TARGET_LOCATION}/"],
            "extra segment": [f"{TARGET}/extra"],
            "credential-bearing": [f"{TARGET}?token=secret"],
            "unknown": [f"{TARGET_LOCATION}/unknown-machine"],
            "duplicate": [TARGET, TARGET],
        }
        for label, targets in cases.items():
            with self.subTest(label=label), patch.object(model_share, "_ensure_thread") as ensure_thread:
                with self.assertRaises(model_share.ShareError) as refused:
                    model_share.start(FISH, fleet_read, targets=targets)
                self.assertGreaterEqual(refused.exception.status, 400)
                self.assertLess(refused.exception.status, 500)
                ensure_thread.assert_not_called()
                self.assertFalse(model_share.plans_path().exists())
                self.assertFalse(model_share.networks_path().exists())
                self.assertFalse(startup_control.audit_path().exists())
                self.assertEqual((self.hubs.shares, self.hubs.installs), ([], []))

    def test_explicit_null_at_the_admin_boundary_is_not_treated_as_omission(self) -> None:
        fleet_read = scoped_snapshot()
        with patch.object(fleet, "snapshot", lambda: fleet_read), \
                patch.object(model_share, "_ensure_thread") as ensure_thread:
            answer = admin.fleet_install_everywhere({"package_id": FISH, "targets": None})

        self.assertNotIsInstance(answer, dict)
        self.assertGreaterEqual(answer.status_code, 400)
        self.assertLess(answer.status_code, 500)
        ensure_thread.assert_not_called()
        self.assertFalse(model_share.plans_path().exists())
        self.assertFalse(model_share.networks_path().exists())
        self.assertFalse(startup_control.audit_path().exists())
        self.assertEqual((self.hubs.shares, self.hubs.installs), ([], []))

    def test_same_package_active_plan_blocks_a_second_scoped_plan(self) -> None:
        fleet_read = scoped_snapshot()
        model_share.start(FISH, fleet_read, targets=[TARGET])

        with self.assertRaises(model_share.ShareError) as refused:
            model_share.start(FISH, fleet_read, targets=[SKIPPED])

        self.assertEqual(refused.exception.body["code"], "ALREADY_RUNNING")
        self.assertEqual(len(model_share.read_plans()), 1)
        self.assertEqual([entry["key"] for entry in self.raw_plan()["macs"]], [TARGET])

    def test_scoped_target_still_has_to_be_online_and_memory_eligible(self) -> None:
        cases = {
            "offline": {"reachable": False},
            "below package tier": {"fits": False},
        }
        for label, target in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(model_share.ShareError) as refused:
                    model_share.start(FISH, scoped_snapshot(target=target), targets=[TARGET])
                self.assertEqual(refused.exception.body["code"], "NOTHING_TO_DO")
                self.assertEqual(model_share.read_plans(), [])
                self.assertEqual((self.hubs.shares, self.hubs.installs), ([], []))


class ScopedExplicitOffTest(Case):
    def setUp(self) -> None:
        super().setUp()
        self.hubs = FakeHubs(self)

    def test_scoped_install_does_not_override_an_explicit_model_off(self) -> None:
        from gateway.providers import models as models_module, registry
        from gateway.providers.credentials import CredentialStore
        from gateway.providers.definitions import DISCOVERY_STUDIO_HUB, KIND_STUDIO_HUB, ProviderDefinition

        model_id = "org/fish-4bit"
        provider_id = fleet.provider_id_for(TARGET_LOCATION)
        definition = ProviderDefinition(
            id=provider_id,
            display_name="returned M4",
            category="multi",
            kind=KIND_STUDIO_HUB,
            base_url="http://x.invalid",
            modalities=("tts",),
            discovery=DISCOVERY_STUDIO_HUB,
        )
        inventory = models_module.ModelInventory(
            CredentialStore(path=self.data / "credentials.json"),
            path=self.data / "models-cache.json",
            definition_lookup=lambda key: definition if key == provider_id else None,
            definitions_lookup=lambda: [definition],
        )
        inventory.set_model_enabled(provider_id, model_id, False)
        fleet_read = snapshot((TARGET_LOCATION, TARGET_MACHINE, False))
        fleet_read["locations"][0]["local_models"][0]["model"] = model_id

        with patch.object(registry, "models", inventory):
            model_share.start(FISH, fleet_read, targets=[TARGET])
            model_share.tick(fleet_read)
            self.hubs.finish_all()
            model_share.tick(fleet_read)

        decisions = json.loads((self.data / "models-cache.json").read_text(encoding="utf-8"))["enabled"]
        self.assertIs(decisions[provider_id][model_id], False)


if __name__ == "__main__":
    unittest.main()
