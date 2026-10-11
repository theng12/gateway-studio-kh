#!/usr/bin/env python3
"""Import voices into gateway-kh.

Kept as a script because it is useful from a terminal and for a bulk first-time
import, but the work lives in `gateway/voice_import.py` so the app's **Add voices…**
button and this cannot drift apart. Prefer the app: it copies each clip into the
gateway's own library, records the clip's hash, and then transcribes it through
Studio Hub — without which a voice cannot clone at all.

Accepts a folder of per-voice folders (the old Voice Studio layout) or a folder of
loose audio files. A voice is a folder with reference audio, or an audio file, plus
an optional transcript (`transcript.txt`, or a sidecar beside a loose file).

Usage:
  python scripts/import_voices.py [--source DIR] [--annotate]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import voice_import  # noqa: E402

DEFAULT_SOURCE = Path.home() / "pinokio/api/voicestudio-mac.git/app/voices"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--annotate", action="store_true",
                        help="also transcribe them, which is what makes them usable")
    parser.add_argument("--model", default=voice_import.DEFAULT_TRANSCRIPTION_MODEL)
    args = parser.parse_args()

    result = voice_import.import_folder(args.source)
    print(f"imported {len(result['imported'])} voices -> {result['catalog']}")
    for voice in result["imported"]:
        note = "transcript" if voice["has_transcript"] else "NO transcript"
        print(f"  {voice['display_name']:12} {note}")
    for item in result["skipped"]:
        print(f"  skipped {item['name']}: {item['reason']}")
    for item in result["conflicts"]:
        print(f"  NOT added {item['name']}: {item['message']} (use the app to keep both, replace or skip)")

    if not args.annotate:
        print("\nrun with --annotate (or use the app) to transcribe them; a voice "
              "without segments cannot be cloned from")
        return 0

    state = voice_import.start_annotation(args.model)
    if state.get("error"):
        print(state["error"])
        return 1
    while True:
        state = voice_import.annotation_state()
        if not state["running"]:
            break
        time.sleep(1.0)
    for item in state["results"]:
        print(f"  {'ok  ' if item['ok'] else 'FAIL'} {item['name']:12} {item['detail']}")
    return 0 if all(item["ok"] for item in state["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
