#!/usr/bin/env python3
"""A site's memory verdict is any Mac that fits, not whichever Mac the catalogue listed last.

Measured 2026-10-04: the Hub's aggregate catalogue holds one row per worker, and discovery kept
the last. An 8 GB node listed last at 1100/1300/1400 published `memory_eligible: false` for
VoxCPM2/Breeze/Qwen3 1.7B, so those 16 GB+ sites were withheld from routing and every job of
those models waited on 1500/1600.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-site-memory-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
        "GATEWAY_API_KEY": "",
    }
)

from gateway.providers import discovery  # noqa: E402

REPO = "mlx-community/VoxCPM2-8bit"


def _row(machine: str, fits: bool | None) -> dict:
    return {"repo": REPO, "hub_machine": machine, "memory_eligible": fits, "min_unified_memory_gb": 16}


def _catalog(*rows: dict) -> dict:
    response = SimpleNamespace(status_code=200, json=lambda: {"models": list(rows)})
    with patch.object(discovery.httpx, "get", lambda *a, **k: response):
        return discovery._hub_catalog("http://site.test", "token")


class SiteMemoryVerdictTests(unittest.TestCase):
    def test_a_small_node_listed_last_does_not_withhold_the_site(self) -> None:
        facts = _catalog(_row("local", True), _row("node-1104", True), _row("node-1109-8gb", False))
        self.assertIs(facts[REPO]["memory_eligible"], True)

    def test_a_site_where_no_mac_fits_still_says_so(self) -> None:
        facts = _catalog(_row("local", False), _row("node-8gb", False))
        self.assertIs(facts[REPO]["memory_eligible"], False)


if __name__ == "__main__":
    unittest.main()
