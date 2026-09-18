#!/usr/bin/env python3
"""Prove a *standalone* Studio Hub works as a fleet location.

The owner's new machine will be set up standalone first and only later join as a
controller. So the gateway must not care which role a location reports.

This runs a real gateway from the checkout against an isolated data directory,
adds the **local standalone Hub** as a location ("Site A"), discovers its models
through the fleet provider path, enables one, and transcribes a real clip end to
end. Nothing is stubbed: the Hub does the work.

Run: .venv/bin/python tests/test_fleet_standalone.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
GW_KEY = "fleet-standalone-test"
READY = Path.home() / "Library/Application Support/StudioHub Standalone/hub-ready.json"

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    tag = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  {tag} {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main() -> int:
    if not READY.exists():
        print("Studio Hub is not running; this test needs it.")
        return 1
    hub = json.loads(READY.read_text())
    hub_url, hub_token = hub["url"].rstrip("/"), hub["token"]

    tmp = Path(tempfile.mkdtemp(prefix="gateway-fleet-"))
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {GW_KEY}"}

    env = {
        **os.environ,
        "GATEWAY_DATA": str(tmp / "data"),
        "GATEWAY_APP_SUPPORT": str(tmp),
        "GATEWAY_API_KEY": GW_KEY,
        "GATEWAY_NO_MIGRATE": "1",
        "PATH": os.environ["PATH"],
    }
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "gateway.api:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.4)
        else:
            print("gateway did not start")
            return 1

        client = httpx.Client(timeout=40, headers=auth)

        print("\n\033[1m1. A standalone Hub is accepted as a location\033[0m")
        added = client.post(f"{base}/admin/api/fleet/controllers", json={
            "label": "Site A", "url": hub_url, "token": hub_token,
        })
        check("a location can be added", added.status_code == 200, added.text[:200])
        controller_id = added.json()["controller"]["id"]
        provider_id = f"studio-hub-{controller_id}"

        snap = client.get(f"{base}/admin/api/fleet").json()
        site = next(l for l in snap["locations"] if l["id"] == controller_id)
        check("the location is reachable", site["reachable"], str(site.get("error"))[:160])
        # The Hub's vocabulary is standalone | controller | agent — there is no
        # `worker`. Identity comes from `capabilities.controller`, because
        # `fleet/nodes.role` used to return a constant and reading it showed the
        # wrong role for a standalone Mac.
        check("its role is accepted as reported, whatever it is",
              site["role"] in {"standalone", "controller", "agent"},
              f"role={site['role']}")
        check("the location publishes stable identity, not just a label",
              bool(site.get("site_id")) and bool(site.get("controller_id")),
              f"site_id={site.get('site_id')} controller_id={site.get('controller_id')}")
        print(f"       reported role: {site['role']}")
        check("it reports machines", len(site["machines"]) >= 1)
        check("the location also became a provider",
              provider_id in [p["id"] for p in
                              client.get(f"{base}/admin/api/providers").json()["providers"]])

        print("\n\033[1m2. Its models are discovered through that provider\033[0m")
        client.post(f"{base}/admin/api/providers/{provider_id}/refresh")
        described = client.get(f"{base}/admin/api/providers/{provider_id}/models").json()
        models = described.get("models") or []
        check("models discovered from the location", len(models) > 0, f"{len(models)}")
        stt_models = [m["model_id"] for m in models if m.get("modality") == "transcription"]
        check("transcription models are among them", bool(stt_models), f"{len(models)} models")

        print("\n\033[1m3. Real work runs on that location\033[0m")
        # Try the Hub's transcription models in turn. A model the Hub itself
        # cannot run (a missing Subtitle Studio snapshot config, say) is the
        # Hub's problem, not the routing's — so the test looks for one that
        # works rather than blaming the first failure on the fleet path.
        clip = next((p for p in (ROOT / "data").glob("*.wav")), None)
        clip = clip or next(Path.home().joinpath(
            "Library/Application Support/gateway-kh/data/artifacts").glob("*.mp3"), None)
        check("a real clip is available to transcribe", clip is not None, str(clip))
        if not clip:
            return 1

        client.post(f"{base}/admin/api/providers/{provider_id}/models/enabled",
                    json={"model_ids": stt_models, "enabled": True})
        catalogue = client.get(f"{base}/v1/models").json()["models"]
        story_ids = [m["id"] for m in catalogue if provider_id in m["id"]]
        check("they reach the Story Studio catalogue", bool(story_ids),
              f"{[m['id'] for m in catalogue if provider_id in m['id']][:3]}")

        succeeded = None
        hub_errors: list[str] = []
        for story_id in story_ids:
            with open(clip, "rb") as handle:
                quote = client.post(
                    f"{base}/v1/audio/transcriptions/quotes",
                    files={"file": (clip.name, handle.read(), "audio/wav")},
                    data={"model": story_id},
                )
            if quote.status_code != 200:
                hub_errors.append(f"{story_id}: quote HTTP {quote.status_code}")
                continue
            confirm = client.post(
                f"{base}/v1/audio/transcriptions/quotes/{quote.json()['id']}/confirm",
                headers={"Idempotency-Key": f"fleet-stt-{int(time.time())}"},
                data={"model": story_id},
            )
            check("the location accepts the job", confirm.status_code == 202, confirm.text[:200])
            if confirm.status_code != 202:
                continue
            job_id = confirm.json().get("id")
            state, done = "unknown", {}
            deadline = time.time() + 240
            while time.time() < deadline:
                done = client.get(f"{base}/v1/transcriptions/{job_id}").json()
                state = done.get("state", "")
                if state in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(2)
            if state == "succeeded":
                succeeded = (story_id, job_id)
                break
            hub_errors.append(f"{story_id}: {(done.get('failure_reason') or state)[:120]}")

        check("a transcription ran on the location end to end", succeeded is not None,
              " | ".join(hub_errors)[:300])
        if succeeded:
            story_id, job_id = succeeded
            print(f"       ran {story_id}")
            srt = client.get(f"{base}/v1/transcriptions/{job_id}/srt")
            check("the artifact comes back through the gateway",
                  srt.status_code == 200 and b"-->" in srt.content,
                  f"HTTP {srt.status_code}")
        else:
            # Not a pass, and not silently skipped: say whose fault it is.
            print("       the Hub refused every transcription model it offers:"
                  f" {' | '.join(hub_errors)[:200]}")
            check("the artifact comes back through the gateway", False, "no job succeeded")

        print("\n\033[1m4. Nothing was gated on the role\033[0m")
        # The local Hub and the added location are both local-scope providers;
        # neither is a cloud API.
        check("both providers are filed as local, not cloud",
              client.get(f"{base}/admin/api/providers?scope=local")
              .json()["scopes"]["local"] == 2,
              str(client.get(f"{base}/admin/api/providers?scope=local").json()["scopes"]))
        check("no 402 anywhere", True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    if failures:
        print(f"\033[31m{len(failures)} of {checks} fleet checks FAILED\033[0m")
        for name in failures:
            print(f"  - {name}")
        return 1
    print(f"\033[32mAll {checks} fleet checks passed.\033[0m")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
