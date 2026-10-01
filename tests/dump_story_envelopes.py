"""Write the job envelopes this gateway really publishes, for Story Studio's parser tests.

    .venv/bin/python tests/dump_story_envelopes.py <out.json>

Every envelope comes from `api._job_envelope` over a real `JobStore` row, one per modality and
state (plus a waiting job and a held, unknown-outcome one), so the file is the producer's own
output rather than a hand-written guess at it. Story Studio keeps a copy at
`electron/server/__tests__/fixtures/gateway-envelopes.json` and replays it through its own
parsers (`genstudio-gateway-live-envelopes.test.ts`); regenerate that copy when this file's
envelope changes. Not a test (no `test_` prefix), so `unittest discover` does not run it.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_studio_hub_contract  # noqa: F401,E402  (builds the isolated test profile)

from gateway import api  # noqa: E402


def make(modality: str, state: str, **extra) -> dict:
    row, _ = api.store.submit(modality=modality, model_id="m/x", params={}, provider_id="studio-hub-local",
                              idempotency_key=f"dump-{uuid.uuid4()}")
    if state in ("running", "succeeded", "failed"):
        api.store.mark_running(row["id"])
        api.store.begin_dispatch(row["id"])
    if state == "running" and extra.get("waiting"):
        api.store.record_upstream_acceptance(row["id"], "batch1")
        api.store.record_waiting(row["id"], extra["waiting"])
    if state == "succeeded":
        path = pathlib.Path(tempfile.mkdtemp()) / "a.bin"
        path.write_bytes(b"x")
        api.store.succeed(
            row["id"], asset_path=str(path), media_type="audio/wav",
            artifacts=[{"asset_id": "0", "path": str(path), "media_type": "audio/wav", "bytes": 1}],
            result={"text": "hello", "payload": {"finish_reason": "stop", "generation_seconds": 3.2,
                                                  "usage": {"total_tokens": 5}}})
    if state == "failed":
        api.store.fail(row["id"], code=extra.get("code", "PROVIDER_FAILED"), detail=extra.get("detail", "boom"),
                       retryable=False, recovery_state=extra.get("recovery_state"),
                       recovery_detail=extra.get("recovery_detail"))
    if state == "cancelled":
        api.store.cancel(row["id"])
    return api._job_envelope(api.store.get(row["id"]))


def main(out: str) -> None:
    envelopes: dict[str, dict] = {}
    for modality in ("chat", "image", "tts", "music", "transcription", "video"):
        for state in ("accepted", "running", "succeeded", "failed", "cancelled"):
            envelopes[f"{modality}.{state}"] = make(modality, state)
    envelopes["tts.waiting"] = make("tts", "running", waiting={
        "reason": "waiting for a free machine: local is reserved for other work on this Mac",
        "next_try_at": time.time() + 30, "since": time.time() - 60})
    envelopes["tts.held"] = make("tts", "failed", code="UNKNOWN_OUTCOME", detail="The gateway restarted",
                                 recovery_state="unknown_outcome", recovery_detail="batch x")
    # Ids and times differ on every run; the parser tests do not look at them.
    pathlib.Path(out).write_text(json.dumps(envelopes, indent=1, sort_keys=True, default=str) + "\n")
    print(f"{len(envelopes)} envelopes -> {out}")


if __name__ == "__main__":
    main(sys.argv[1])
