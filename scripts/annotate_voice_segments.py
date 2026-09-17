#!/usr/bin/env python3
"""Annotate imported voices with timestamped transcript segments.

Studio Hub's Qwen3 **Base** models reject a clone reference supplied without
aligned timestamps:

    REFERENCE_TRANSCRIPT_ALIGNMENT_REQUIRED: This model needs a transcript
    matching the selected reference segment. Supply timestamped transcript
    segments.

The old Voice Studio stored only a flat transcript, so the timestamps are
generated here -- using the Mac's own Whisper, which already works, rather than
by hand. Each reference clip is transcribed with word timestamps, the SRT is
parsed into segments, and the result is written back to `data/voices.json` as
`reference_segments`.

Idempotent: voices that already have segments are skipped unless --force.

Usage:
  python scripts/annotate_voice_segments.py [--force] [--model WHISPER_MODEL_ID]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.config import VOICES_PATH, WRITABLE_VOICES_PATH  # noqa: E402
from gateway.hub import hub_base_url, hub_headers  # noqa: E402

# Read the effective catalogue (which may be the shipped default) but always
# WRITE to the user's data directory — never into the read-only bundle.
VOICES = VOICES_PATH
OUT = WRITABLE_VOICES_PATH
DEFAULT_MODEL = "mlx-community/whisper-base-mlx"
POLL_S = 2.0
DEADLINE_S = 600.0

_TIMING = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})"
)


def _seconds(hours: str, minutes: str, secs: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(millis) / 1000.0


def parse_srt(text: str) -> list[dict]:
    segments: list[dict] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_index = next((i for i, line in enumerate(lines) if _TIMING.search(line)), None)
        if timing_index is None:
            continue
        match = _TIMING.search(lines[timing_index])
        if match is None:
            continue
        body = " ".join(lines[timing_index + 1 :]).strip()
        segments.append(
            {
                "start": _seconds(*match.groups()[:4]),
                "end": _seconds(*match.groups()[4:]),
                "text": body,
            }
        )
    return segments


def transcribe(client: httpx.Client, base: str, headers: dict, audio: Path, model: str) -> list[dict]:
    with audio.open("rb") as handle:
        response = client.post(
            f"{base}/api/hub/transcription/jobs",
            headers=headers,
            files={"files": (audio.name, handle, "application/octet-stream")},
            data={"model": model, "item_ids": "0", "word_timestamps": "true"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"submit HTTP {response.status_code}: {response.text[:200]}")
    batch_id = str((response.json() or {}).get("batch_id") or "")
    if not batch_id:
        raise RuntimeError("hub returned no batch_id")

    deadline = time.monotonic() + DEADLINE_S
    while time.monotonic() < deadline:
        summary = client.get(
            f"{base}/api/hub/transcription/jobs/{batch_id}", headers=headers
        ).json() or {}
        items = summary.get("items") or []
        state = str((items[0] if items else {}).get("state") or "").lower()
        if state in {"done", "succeeded"}:
            break
        if state in {"error", "failed", "cancelled"}:
            raise RuntimeError(f"item {state}: {(items[0] or {}).get('error')}")
        time.sleep(POLL_S)
    else:
        raise RuntimeError("timed out")

    artifact = client.get(
        f"{base}/api/hub/transcription/jobs/{batch_id}/items/0/artifact",
        headers=headers,
        params={"format": "srt"},
    )
    if artifact.status_code >= 400:
        raise RuntimeError(f"artifact HTTP {artifact.status_code}")
    return parse_srt(artifact.text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    base = hub_base_url()
    if not base:
        print("Studio Hub is not running (no hub-ready.json).")
        return 1
    headers = hub_headers()

    data = json.loads(VOICES.read_text(encoding="utf-8"))
    voices = data.get("voices", [])
    print(f"annotating {len(voices)} voices via {base} using {args.model}\n")

    failures = 0
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        for voice in voices:
            name = voice.get("display_name") or voice.get("id")
            if voice.get("reference_segments") and not args.force:
                print(f"  {name:10} already annotated ({len(voice['reference_segments'])} segments)")
                continue
            path = Path(str(voice.get("reference_path") or ""))
            if not path.exists():
                print(f"  {name:10} SKIP - reference missing")
                failures += 1
                continue
            try:
                segments = transcribe(client, base, headers, path, args.model)
            except Exception as exc:  # noqa: BLE001
                print(f"  {name:10} FAILED - {exc}")
                failures += 1
                continue
            voice["reference_segments"] = segments
            print(f"  {name:10} {len(segments)} segments, {segments[-1]['end']:.1f}s")

    OUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
