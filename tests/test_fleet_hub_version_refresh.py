"""A Hub that changes build must have its models re-read — once, by itself.

A Studio Hub updates itself, and this gateway's memory of what a location serves
is the discovery cache. Nothing re-read that memory when the Hub changed: the
fleet refresher already probed every location and already knew the version, and
it did nothing with a change, so a Hub that gained a lane kept being described
from a stale list until somebody pressed Refresh.

Measured 2026-09-25 on this fleet: five controllers were pushed 0.6.64 and each
one's new chat contract (`lane_contract.structured_output`) stayed invisible
until a manual Refresh — which is the exact delay this module exists to remove.

The rule these tests hold to: **refresh on a change, never on a repeat, and never
record a version beside rows a failed fetch could not refresh.** The last one is
what makes the next pass retry instead of marking stale rows current.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import fleet  # noqa: E402
from gateway.providers import models as models_module  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.credentials import CredentialStore  # noqa: E402
from gateway.providers.discovery import DiscoveryResult  # noqa: E402


class _Inventory:
    """The discovery cache, with no file behind it."""

    def __init__(self, records: dict | None = None) -> None:
        self.records = dict(records or {})

    def cached(self, provider_id: str) -> dict:
        return dict(self.records.get(provider_id) or {})


class _Harness:
    """Run the detector against a fake cache and a recording refresh."""

    def __init__(self, rows, *, cached=None, error=None, raises=()) -> None:
        self.rows = rows
        self.inventory = _Inventory(cached)
        self.calls: list[tuple[str, str | None]] = []
        self._error = error
        self._raises = set(raises)

    def _refresh(self, provider_id: str, *, hub_version: str | None = None) -> DiscoveryResult:
        self.calls.append((provider_id, hub_version))
        if provider_id in self._raises:
            raise RuntimeError("this location's discovery blew up")
        return DiscoveryResult(models=[], error=self._error)

    def run(self) -> dict[str, str]:
        with patch.object(registry, "models", self.inventory), \
                patch.object(registry, "refresh", self._refresh):
            return fleet.refresh_models_when_hub_changes(self.rows)


class RefreshOnChangeTests(unittest.TestCase):
    def test_a_location_with_no_record_yet_is_refreshed_once(self) -> None:
        # The seed. Without it the feature would be inert on a gateway that had
        # never recorded a version, which is every gateway until this ships.
        h = _Harness([{"provider_id": "studio-hub-a", "version": "0.6.64"}])
        self.assertEqual(h.run(), {"studio-hub-a": "0.6.64"})
        self.assertEqual(h.calls, [("studio-hub-a", "0.6.64")])

    def test_the_same_build_is_not_refreshed_again(self) -> None:
        # The steady state. A trigger that fired every pass would be a discovery
        # loop against every controller, every 15 seconds.
        h = _Harness(
            [{"provider_id": "studio-hub-a", "version": "0.6.64"}],
            cached={"studio-hub-a": {"hub_version": "0.6.64"}},
        )
        self.assertEqual(h.run(), {})
        self.assertEqual(h.calls, [])

    def test_a_new_build_is_refreshed_and_its_version_recorded(self) -> None:
        h = _Harness(
            [{"provider_id": "studio-hub-a", "version": "0.6.64"}],
            cached={"studio-hub-a": {"hub_version": "0.6.63"}},
        )
        self.assertEqual(h.run(), {"studio-hub-a": "0.6.64"})
        self.assertEqual(h.calls, [("studio-hub-a", "0.6.64")])

    def test_an_unstated_version_is_not_a_change(self) -> None:
        # A Hub that is down, one too old to publish `studiohub_version`, and a
        # row with no provider id all belong here: none of them is evidence that
        # anything changed, and treating them as one re-discovers on every pass.
        h = _Harness([
            {"provider_id": "studio-hub-a", "version": None},
            {"provider_id": "studio-hub-b", "version": ""},
            {"provider_id": "studio-hub-c", "version": "   "},
            {"provider_id": "", "version": "0.6.64"},
        ])
        self.assertEqual(h.run(), {})
        self.assertEqual(h.calls, [])

    def test_a_failed_discovery_is_not_reported_as_refreshed(self) -> None:
        # So the next pass tries again rather than believing it is current.
        h = _Harness(
            [{"provider_id": "studio-hub-a", "version": "0.6.64"}],
            error="ConnectTimeout",
        )
        self.assertEqual(h.run(), {})
        self.assertEqual(h.calls, [("studio-hub-a", "0.6.64")])

    def test_one_bad_location_does_not_stop_the_others(self) -> None:
        h = _Harness(
            [
                {"provider_id": "studio-hub-a", "version": "0.6.64"},
                {"provider_id": "studio-hub-b", "version": "0.6.64"},
            ],
            raises={"studio-hub-a"},
        )
        self.assertEqual(h.run(), {"studio-hub-b": "0.6.64"})

    def test_only_the_location_that_changed_is_refreshed(self) -> None:
        h = _Harness(
            [
                {"provider_id": "studio-hub-a", "version": "0.6.64"},
                {"provider_id": "studio-hub-b", "version": "0.6.63"},
            ],
            cached={
                "studio-hub-a": {"hub_version": "0.6.64"},
                "studio-hub-b": {"hub_version": "0.6.63"},
            },
        )
        self.assertEqual(h.run(), {})
        self.assertEqual(h.calls, [])


class TheCacheRecordsWhichBuildItDescribesTests(unittest.TestCase):
    """`ModelInventory.refresh` is where the baseline is written."""

    PROVIDER = "studio-hub-local"

    def setUp(self) -> None:
        self.assertTrue(
            models_module.PROVIDERS_BY_ID.get(self.PROVIDER),
            f"{self.PROVIDER} must stay a static provider for this test to mean anything",
        )

    def _inventory(self, tmp_path: Path) -> models_module.ModelInventory:
        return models_module.ModelInventory(
            CredentialStore(path=tmp_path / "credentials.json"),
            path=tmp_path / "models-cache.json",
        )

    def test_a_successful_discovery_records_the_build_it_came_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = self._inventory(Path(tmp))
            with patch.object(
                models_module, "discover", return_value=DiscoveryResult(models=[], error=None)
            ):
                inventory.refresh(self.PROVIDER, hub_version="0.6.64")
            self.assertEqual(inventory.cached(self.PROVIDER).get("hub_version"), "0.6.64")

    def test_a_failed_discovery_leaves_the_recorded_build_alone(self) -> None:
        # The rule that matters: a fetch that did not answer learned nothing, so
        # it must not stamp the *new* version onto rows it could not refresh —
        # that would mark stale rows current and the retry would never happen.
        with tempfile.TemporaryDirectory() as tmp:
            inventory = self._inventory(Path(tmp))
            with patch.object(
                models_module, "discover", return_value=DiscoveryResult(models=[], error=None)
            ):
                inventory.refresh(self.PROVIDER, hub_version="0.6.63")
            self.assertEqual(inventory.cached(self.PROVIDER).get("hub_version"), "0.6.63")

            with patch.object(
                models_module, "discover",
                return_value=DiscoveryResult(models=[], error="ConnectTimeout"),
            ):
                inventory.refresh(self.PROVIDER, hub_version="0.6.64")
            self.assertEqual(inventory.cached(self.PROVIDER).get("hub_version"), "0.6.63")

    def test_a_caller_that_cannot_name_a_version_leaves_it_alone(self) -> None:
        # Every cloud lane. Recording a blank would erase a real baseline.
        with tempfile.TemporaryDirectory() as tmp:
            inventory = self._inventory(Path(tmp))
            with patch.object(
                models_module, "discover", return_value=DiscoveryResult(models=[], error=None)
            ):
                inventory.refresh(self.PROVIDER, hub_version="0.6.64")
                inventory.refresh(self.PROVIDER)
            self.assertEqual(inventory.cached(self.PROVIDER).get("hub_version"), "0.6.64")


if __name__ == "__main__":
    unittest.main(verbosity=2)
