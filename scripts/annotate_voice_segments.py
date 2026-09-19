#!/usr/bin/env python3
"""Annotate imported voices with timestamped transcript segments.

Kept as a script because it is useful from a terminal, but the work lives in
`gateway/voice_import.py` so the app's **Add voices…** button and this cannot drift
apart. Qwen3 **Base** refuses a reference without aligned timestamps
(`REFERENCE_TRANSCRIPT_ALIGNMENT_REQUIRED`), so this is what makes a stocked voice
usable rather than merely present.

The clips are transcribed **through Studio Hub**, so the Hub has to be running and
have a working transcription model. With no Hub there is nothing to transcribe with,
and this says so rather than reporting success.

Usage:
  python scripts/annotate_voice_segments.py [--force] [--model WHISPER_MODEL_ID]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import voice_import  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="re-transcribe voices that already have segments")
    parser.add_argument("--model", default=voice_import.DEFAULT_TRANSCRIPTION_MODEL)
    args = parser.parse_args()

    state = voice_import.start_annotation(args.model, force=args.force)
    if state.get("error"):
        print(state["error"])
        return 1
    if state["total"] == 0:
        print("every voice already has segments (use --force to redo them)")
        return 0

    print(f"annotating {state['total']} voices using {state['transcription_model']}\n")
    while True:
        state = voice_import.annotation_state()
        if not state["running"]:
            break
        if state["current"]:
            print(f"  {state['current']}")
        time.sleep(1.0)

    failures = 0
    for result in state["results"]:
        mark = "ok  " if result["ok"] else "FAIL"
        failures += 0 if result["ok"] else 1
        print(f"  {mark} {result['name']:12} {result['detail']}")
    if state.get("error"):
        print(state["error"])
        return 1
    print(f"\n{len(state['results']) - failures} annotated, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
