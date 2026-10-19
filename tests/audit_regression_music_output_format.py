#!/usr/bin/env python3
"""Regression — a Studio Hub music lane's *published* `output_formats` must be accepted
by the gateway's own admission check.

Audit artifact. READ-ONLY source review; prepared for the developer's serial test lane.
Do not run in a production worker. No application source is edited by this file.

Pinned source: gateway-kh @ 63c4d9559b03c40da2bf34f3a2ba1c3c6d16c463 (0.2.188).

What this asserts (the observable contract, and nothing else):
    Every value a music catalogue row publishes in `output_formats` is accepted by the
    gateway's own admission check for that modality. A row must not advertise a delivery
    format the door refuses.

Why it can be red (source):
  * `gateway/catalog.py:322 _output_formats()` re-publishes the Hub lane's declared
    per-model `output_format` enum as the row's `output_formats` (`_music_row` merges it
    at `catalog.py:1674`). For a YuE2 music lane the Hub declares `enum: ["flac", "m4a"]`
    (Hub `components/music/app/backend/generation.py:398 output_format_parameter`,
    `_native_format` -> `YUE2_AUDIO_FORMAT = "flac"`).
  * `gateway/api.py:1274 _validate_output_format()` refuses anything outside the hardcoded
    `gateway/api.py:1251 OUTPUT_FORMATS["music"] = ("wav", "m4a")` with HTTP 400
    `INVALID_PARAMS`.

This file deliberately does NOT assert the exact advertised set, and does NOT assert that
the current door refuses `flac` — both would contradict one of the two legitimate fixes.
It fails today only because the invariant is violated, and passes under EITHER fix:
  (a) the door accepts every format the row advertises (per-row bound), or
  (b) the catalogue stops advertising a format the door refuses (intersect with the
      modality allowlist).

The controls are green before and after, so a red here is the invariant, not the harness.

Run (from the pinned/isolated gateway tree; GATEWAY_REPO is required, no fallback):
    GATEWAY_REPO=/path/to/gateway-repo .venv/bin/python regression-music-output-format.test.py
"""

from __future__ import annotations

import os
import socket as _socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# ── GATEWAY_REPO is required — never fall back to a live/main checkout ────────────
_REPO_ENV = os.environ.get("GATEWAY_REPO")
if not _REPO_ENV:
    raise SystemExit(
        "GATEWAY_REPO must name the pinned gateway tree (no fallback to main). "
        "Set it to the isolated worktree the developer is testing against."
    )
_REPO = Path(_REPO_ENV)
if not (_REPO / "gateway" / "api.py").is_file():
    raise SystemExit(f"GATEWAY_REPO does not look like a gateway tree: {_REPO}")

# ── disable real network before importing the application ────────────────────────
_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


class _NetworkDisabled(RuntimeError):
    pass


_orig_connect = _socket.socket.connect


def _guarded_connect(self, address):  # type: ignore[no-untyped-def]
    host = address[0] if isinstance(address, (tuple, list)) and address else ""
    if host not in _LOOPBACK:
        raise _NetworkDisabled(f"network disabled for this regression: {address!r}")
    return _orig_connect(self, address)


def _no_create_connection(*_a, **_k):  # type: ignore[no-untyped-def]
    raise _NetworkDisabled("network disabled for this regression")


_socket.socket.connect = _guarded_connect        # type: ignore[assignment]
_socket.create_connection = _no_create_connection  # type: ignore[assignment]

# ── isolated temp profile, mirroring tests/test_studio_hub_contract.py ────────────
_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-music-ofmt-regression-"))
sys.path.insert(0, str(_REPO))
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
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_API_KEY": "fixture-key",
        "GATEWAY_FLEET": str(_TEMP_ROOT / "fleet.json"),
        "GATEWAY_VOICES": str(_TEMP_ROOT / "voices.json"),
        "GATEWAY_VOICES_OUT": str(_TEMP_ROOT / "voices.json"),
    }
)

from gateway import api, catalog  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB  # noqa: E402

# A Studio Hub location definition — `_output_formats` reads only `.kind`.
_HUB = SimpleNamespace(id="studio-hub-test", kind=KIND_STUDIO_HUB)


def _hub_raw(parameters: list[dict]) -> dict:
    """The `raw` a Studio Hub model carries, shaped as the Hub publishes `lane_contract`."""
    return {"lane_contract": {"parameters": parameters}}


def _output_format_param(enum: list[str]) -> dict:
    return {"name": "output_format", "type": "string", "required": False,
            "enum": enum, "request_only": True}


# Hub-declared enums (verified against the Hub checkout):
#   YuE2 music lane native is FLAC -> ["flac","m4a"]; Stable Audio 3 native is WAV -> ["wav","m4a"].
_YUE2 = _hub_raw([_output_format_param(["flac", "m4a"])])
_SA3 = _hub_raw([_output_format_param(["wav", "m4a"])])


class MusicOutputFormatsAdvertisedAreAccepted(unittest.TestCase):
    def test_advertised_formats_are_accepted(self) -> None:
        """THE RED ASSERTION (single failure before the fix; green under either fix).

        Every value the catalogue publishes in a music row's `output_formats` must be
        accepted by `_validate_output_format("music", ...)`. On 63c4d955 the YuE2 row
        advertises `flac`, which raises GatewayError(400) here.
        """
        published = catalog._output_formats(_HUB, _YUE2)["output_formats"]
        self.assertTrue(published, "the YuE2 row must advertise at least one format")
        for value in published:
            with self.subTest(advertised=value):
                api._validate_output_format("music", {"output_format": value})

    def test_unsupported_format_is_still_refused(self) -> None:
        """Control (green before and after): a format no row advertises is refused."""
        with self.assertRaises(api.GatewayError) as ctx:
            api._validate_output_format("music", {"output_format": "mp3"})
        self.assertEqual(ctx.exception.status, 400)

    def test_stable_audio_3_control(self) -> None:
        """Control (green before and after): the other music lane's advertised set is accepted."""
        published = catalog._output_formats(_HUB, _SA3)["output_formats"]
        self.assertTrue(published)
        for value in published:
            with self.subTest(advertised=value):
                api._validate_output_format("music", {"output_format": value})

    def test_image_and_voice_controls(self) -> None:
        """Controls (green before and after): image and voice enums already agree with the door."""
        for value in ("png", "jpeg"):
            api._validate_output_format("image", {"output_format": value})
        for value in ("wav", "m4a"):
            api._validate_output_format("tts", {"output_format": value})
        with self.assertRaises(api.GatewayError):
            api._validate_output_format("image", {"output_format": "webp"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
