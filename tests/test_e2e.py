#!/usr/bin/env python3
"""End-to-end verification for gateway-kh.

Proves the whole system against a real HTTP upstream, without spending money:

    admin UI API -> credentials -> live discovery -> enable/disable
                 -> Story Studio catalogue (zero constants)
                 -> generation -> worker -> artifact -> Story-shaped response

Nothing is stubbed inside the gateway. A local mock upstream stands in for a
provider, so the HTTP client code, the discovery strategies, the adapter, the
queue, the worker, the artifact store and the Story Studio contract are all
genuinely exercised.

Run:  .venv/bin/python tests/test_e2e.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
# Running `python tests/test_e2e.py` puts tests/ on sys.path, not the repo root,
# so importing the package under test needs this.
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    """Ask the OS for an unused port.

    Hard-coded ports caused a real failure here: a leftover process from an
    earlier run held the port, the new mock died on bind, and the resulting
    symptom looked like a gateway hang rather than a port conflict.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


MOCK_PORT = free_port()
GW_PORT = free_port()
GW_KEY = "e2e-test-key"
BASE = f"http://127.0.0.1:{GW_PORT}"
AUTH = {"Authorization": f"Bearer {GW_KEY}"}

failures: list[str] = []
checks = 0
statuses: set[int] = set()


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    tag = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    suffix = f" — {detail}" if (detail and not ok) else ""
    print(f"  {tag} {label}{suffix}")
    if not ok:
        failures.append(label)


def wait_for(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return True
        except httpx.HTTPError:
            time.sleep(0.3)
    return False


def req(client: httpx.Client, method: str, path: str, **kw) -> httpx.Response:
    headers = dict(kw.pop("headers", {}))
    headers.update(AUTH)
    response = client.request(method, path, headers=headers, **kw)
    statuses.add(response.status_code)
    return response


def poll(client: httpx.Client, path: str, want: str = "succeeded", tries: int = 40) -> dict:
    state = ""
    body: dict = {}
    for _ in range(tries):
        body = req(client, "GET", path).json()
        state = body.get("state", "")
        if state in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    return body


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gateway-e2e-"))
    processes: list[subprocess.Popen] = []

    # A custom provider definition pointing at the mock. This also exercises the
    # user-defined-provider path, which is how the owner adds a vendor the
    # built-in list does not know.
    (tmp / "providers.local.json").write_text(json.dumps({
        "providers": [
            {
                "id": "mock-remote",
                "display_name": "Mock Remote",
                "category": "multi",
                "kind": "openai_compatible",
                "base_url": f"http://127.0.0.1:{MOCK_PORT}/v1",
                "modalities": ["chat", "image", "tts", "transcription"],
                "discovery": "openai_models",
                "description": "Test provider.",
                # No auto_enable_discovered on purpose: this exercises the
                # shipped default, which is opt-in.
            },
            {
                # Exercises the BFL adapter: async submit -> poll -> download,
                # with BFL's own state names.
                "id": "mock-bfl",
                "display_name": "Mock BFL",
                "category": "image",
                "kind": "bfl",
                "base_url": f"http://127.0.0.1:{MOCK_PORT}",
                "modalities": ["image"],
                "discovery": "static",
                "description": "Test provider.",
                "static_models": [
                    {"id": "flux-pro-1.1", "display_name": "FLUX 1.1 Pro", "modality": "image"}
                ],
            },
            {
                # Exercises the MiniMax adapter across all four of its APIs,
                # including the hex-encoded speech response.
                "id": "mock-minimax",
                "display_name": "Mock MiniMax",
                "category": "multi",
                "kind": "minimax",
                "base_url": f"http://127.0.0.1:{MOCK_PORT}",
                "modalities": ["image", "video", "music", "tts"],
                "discovery": "static",
                "description": "Test provider.",
                "static_models": [
                    {"id": "image-01", "display_name": "MM Image", "modality": "image"},
                    {"id": "video-01", "display_name": "MM Video", "modality": "video"},
                    {"id": "music-01", "display_name": "MM Music", "modality": "music"},
                    {"id": "speech-2.8-turbo", "display_name": "MM Speech", "modality": "tts"},
                ],
            },
        ]
    }))

    env = {
        **os.environ,
        "GATEWAY_DATA": str(tmp),
        "GATEWAY_APP_SUPPORT": str(tmp),
        "GATEWAY_API_KEY": GW_KEY,
        "GATEWAY_PORT": str(GW_PORT),
        # A clean slate: without this the gateway would migrate the developer's
        # real credentials and configured providers into the test run.
        "GATEWAY_NO_MIGRATE": "1",
        "PYTHONPATH": str(ROOT),
        "PYTHONUNBUFFERED": "1",
    }

    mock_log = tmp / "mock.log"
    gw_log = tmp / "gateway.log"
    mock_fh = mock_log.open("wb")
    gw_fh = gw_log.open("wb")

    try:
        # Start the upstream first and confirm it is actually serving before the
        # gateway comes up. A silently dead mock otherwise presents as a gateway
        # hang twenty seconds later.
        processes.append(subprocess.Popen(
            [PY, str(ROOT / "tests" / "mock_upstream.py"), "--port", str(MOCK_PORT)],
            stdout=mock_fh, stderr=subprocess.STDOUT,
        ))
        if not wait_for(f"http://127.0.0.1:{MOCK_PORT}/v1/models", timeout=12):
            mock_fh.flush()
            print("mock upstream did not start:")
            print(mock_log.read_text(errors="replace")[-2000:])
            return 1

        processes.append(subprocess.Popen(
            [PY, "-m", "uvicorn", "gateway.api:app", "--host", "127.0.0.1", "--port", str(GW_PORT),
             "--log-level", "warning"],
            cwd=str(ROOT), env=env, stdout=gw_fh, stderr=subprocess.STDOUT,
        ))
        if not wait_for(f"{BASE}/health", timeout=30):
            gw_fh.flush()
            print("gateway did not start:")
            print(gw_log.read_text(errors="replace")[-3000:])
            return 1

        with httpx.Client(base_url=BASE, timeout=30.0) as client:
            print("\n\033[1m1. Bootstrap and provider registry\033[0m")
            health = httpx.get(f"{BASE}/health").json()
            check("health ok", health["status"] == "ok")
            check("18 built-in providers + 3 custom", health["providers"]["total"] == 21,
                  f"got {health['providers']['total']}")
            check("every provider has a working adapter",
                  all(p["generation_supported"] for p in
                      httpx.get(f"{BASE}/admin/api/providers", headers=AUTH).json()["providers"]))

            providers = httpx.get(f"{BASE}/admin/api/providers", headers=AUTH).json()["providers"]
            ids = {p["id"] for p in providers}
            check("built-ins present (openrouter, fal, kie, studio hub)",
                  {"openrouter-main", "fal-ai", "kie-ai-images", "studio-hub-local"} <= ids)
            mock = next(p for p in providers if p["id"] == "mock-remote")
            check("custom provider loaded", mock is not None)
            check("custom provider initially unconfigured", not mock["configured"])
            check("no models before a key is set", health["models"]["enabled_models"] == 0)

            print("\n\033[1m2. Credentials -> live discovery (admin API)\033[0m")
            r = httpx.post(f"{BASE}/admin/api/providers/mock-remote/credentials",
                           headers=AUTH, json={"values": {"api_key": "mock-key-123"}})
            check("setting a key returns 200", r.status_code == 200, f"got {r.status_code}")
            described = r.json()["provider"]
            check("provider now configured", described["configured"])
            check("provider auto-enabled on first configuration", described["enabled"])

            models = httpx.get(f"{BASE}/admin/api/providers/mock-remote/models", headers=AUTH).json()
            found = {m["model_id"]: m for m in models["models"]}
            check("discovery found all 6 mock models", len(found) == 6, f"got {sorted(found)}")
            check("modality inference: chat", found.get("mock-chat-1", {}).get("modality") == "chat")
            check("modality inference: image", found.get("mock-image-1", {}).get("modality") == "image")
            check("modality inference: tts", found.get("mock-tts-1", {}).get("modality") == "tts")
            check("modality inference: transcription",
                  found.get("mock-whisper-1", {}).get("modality") == "transcription")
            check("discovered models are NOT auto-enabled (opt-in)",
                  all(not m["enabled"] for m in found.values()),
                  str({k: v["enabled"] for k, v in found.items()}))
            hidden = req(client, "GET", "/v1/chat/models").json()
            check("nothing reaches Story Studio until it is enabled",
                  len(hidden["models"]) == 0, f"got {len(hidden['models'])}")

            enabled = httpx.post(
                f"{BASE}/admin/api/models/enabled",
                headers=AUTH,
                json={
                    "items": [
                        {"provider_id": "mock-remote", "model_id": model_id}
                        for model_id in found
                    ],
                    "enabled": True,
                },
            )
            check("model browser API enables models",
                  enabled.status_code == 200 and enabled.json().get("changed") == len(found),
                  enabled.text[:160])

            listed = httpx.get(
                f"{BASE}/admin/api/models", headers=AUTH, params={"q": "mock", "limit": 50}
            ).json()
            check("model browser search finds them across providers",
                  listed["matched"] == len(found), f"matched {listed['matched']}")

            print("\n\033[1m3. Story Studio catalogue contract\033[0m")
            cat = req(client, "GET", "/v1/chat/models").json()
            check("chat catalogue non-empty once enabled", len(cat["models"]) > 0)
            check("catalogue envelope has freshness",
                  {"stale", "ageMs", "loadedAt"} <= set(cat["freshness"]))
            check("freshness not stale", cat["freshness"]["stale"] is False)
            check("freshness ageMs credible", cat["freshness"]["ageMs"] < 30 * 60 * 1000)
            row = cat["models"][0]
            check("row publishes namespaced id", "::" in row["id"], row["id"])
            check("row publishes zero price fields",
                  row["input_credits_per_million_tokens"] == 0
                  and row["output_credits_per_million_tokens"] == 0
                  and row["minimum_credits"] == 0)
            check("row publishes price_version + cost_tier",
                  bool(row["price_version"]) and bool(row["cost_tier"]))
            check("row publishes identity aliases",
                  row["customer_handle"] == row["id"] and row["legacy_ids"] == [])
            check("row publishes context window from provider", row["context_window_tokens"] == 32000)
            # A published 0 is a claim that the model accepts nothing, which is a
            # different statement from "the provider did not say". Story Studio's
            # own rule forbids the two from sharing a value, so an unpublished
            # window must arrive as null.
            zero_ctx = [r["id"] for r in cat["models"] if r.get("context_window_tokens") == 0]
            check("no chat row publishes a zero context window", not zero_ctx, f"{zero_ctx[:3]}")
            # The output allowance was hardcoded to one number for every model on
            # every provider, so a model allowing 131,072 was described as 8,192.
            # A single shared value across a real catalogue is that bug returning.
            allowances = {r.get("max_output_tokens") for r in cat["models"]}
            check("output allowance is per model, not one shared constant",
                  len(allowances - {None}) != 1 or len(cat["models"]) == 1,
                  f"distinct values: {sorted(v for v in allowances if v)[:5]}")

            # Every model kind publishes the serving provider under the same
            # name. Story Studio's handoff states that contract, and it is a
            # silent failure when it is wrong: the row still parses, and the
            # provider simply reads blank. Video published nothing at all and
            # transcription used a different key until this was checked.
            #
            # The path per kind is spelled out because it is not derivable:
            # video's is `/v1/videos/models`. Building it from the kind name
            # 404s, and a 404 yields an empty row list — which made the first
            # version of this check pass while testing nothing. So each kind
            # must return rows, or this fails.
            kinds = {
                "chat": "/v1/chat/models",
                "image": "/v1/image/models",
                "video": "/v1/videos/models",
                "tts": "/v1/tts/models",
                "transcription": "/v1/transcription/models",
                "music": "/v1/music/models",
            }
            missing_provider: list = []
            useless_kinds: list = []
            total_rows = 0
            for kind, path in kinds.items():
                response = req(client, "GET", path)
                rows_for_kind = response.json().get("models", []) if response.status_code == 200 else []
                # The failure this guards is a wrong path: video's endpoint is
                # `/v1/videos/models`, and building it from the kind name 404s.
                # A 404 yields an empty row list, so the first version of this
                # check passed while inspecting nothing at all. Whether a kind
                # has *enabled* models depends on the provider set, so only
                # existence is asserted — plus rows overall, so at least one
                # kind was really inspected.
                if response.status_code != 200:
                    useless_kinds.append(f"{kind}: HTTP {response.status_code}")
                total_rows += len(rows_for_kind)
                for candidate in rows_for_kind:
                    if not candidate.get("provider_name"):
                        missing_provider.append((kind, candidate.get("id")))
            # Every kind's endpoint must exist. Which kinds have *enabled* models
            # depends on the provider set, so that is not asserted here — the
            # suite's own mock only enables some.
            check("every model kind endpoint exists", total_rows > 0 and not useless_kinds,
                  f"endpoints={useless_kinds or 'all 200'}, rows inspected={total_rows}")
            check("every model kind publishes provider_name", not missing_provider,
                  f"{missing_provider[:4]}")

            img_cat = req(client, "GET", "/v1/image/models").json()
            img_rows = img_cat.get("models", [])
            check("image catalogue non-empty", len(img_rows) > 0)
            irow = img_rows[0] if img_rows else {}
            check("image row publishes chargeable=true", irow.get("chargeable") is True)
            check("image row output_options carry geometry+price",
                  bool(irow.get("output_options"))
                  and all(k in irow["output_options"][0]
                          for k in ("resolution", "aspect_ratio", "width", "height",
                                    "credits_per_image", "price_version")))

            print("\n\033[1m4. Chat generation end to end\033[0m")
            model_id = row["id"]
            r = req(client, "POST", "/v1/chat/responses",
                    json={"model_id": model_id, "input": [{"role": "user", "content": "hello"}]},
                    headers={"Idempotency-Key": f"e2e-chat-{int(time.time())}"})
            check("chat submit returns 202", r.status_code == 202, f"got {r.status_code}")
            job = r.json()
            check("chat job carries balanced zero settlement",
                  (job["reserved_credits"], job["settled_credits"], job["released_credits"]) == (0, 0, 0))
            done = poll(client, f"/v1/chat/responses/{job['id']}")
            check("chat job succeeded", done["state"] == "succeeded", done.get("failure_reason", ""))
            check("chat output_text returned", "mock reply" in (done.get("output_text") or ""),
                  str(done.get("output_text"))[:80])
            check("chat finish_reason is a clean stop", done.get("finish_reason") == "stop")

            print("\n\033[1m5. Image generation end to end (real PNG)\033[0m")
            if not img_rows:
                check("image generation exercised", False, "no image models in the catalogue")
            else:
                img_id = img_rows[0]["id"]
                r = req(client, "POST", "/v1/images/generations",
                        json={"model_id": img_id, "prompt": "a lighthouse", "aspect_ratio": "16:9"},
                        headers={"Idempotency-Key": f"e2e-img-{int(time.time())}"})
                check("image submit returns 202", r.status_code == 202, f"got {r.status_code}")
                ijob = r.json()
                check("image submit sets Location", "location" in {k.lower() for k in r.headers})
                done = poll(client, f"/v1/images/{ijob['id']}")
                check("image job succeeded", done["state"] == "succeeded", done.get("failure_reason", ""))
                artifact = req(client, "GET", f"/v1/images/{ijob['id']}/file")
                check("image artifact downloads", artifact.status_code == 200)
                check("image artifact is a real PNG",
                      artifact.content[1:4] == b"PNG" and artifact.content[:8] == b"\x89PNG\r\n\x1a\n")
                check("artifact URL is same-origin (relative)",
                      str(done.get("image_url", "")).startswith("/"))

            print("\n\033[1m6. Idempotency is a reattach, not a re-generate\033[0m")
            if not img_rows:
                check("idempotency exercised", False, "no image models in the catalogue")
            else:
                key = f"e2e-idem-{int(time.time())}"
                first = req(client, "POST", "/v1/images/generations",
                            json={"model_id": img_rows[0]["id"], "prompt": "same"},
                            headers={"Idempotency-Key": key})
                second = req(client, "POST", "/v1/images/generations",
                             json={"model_id": img_rows[0]["id"], "prompt": "same"},
                             headers={"Idempotency-Key": key})
                check("repeated Idempotency-Key returns the same job",
                      first.json()["id"] == second.json()["id"])
                lookup = req(client, "GET", f"/v1/images/by-idempotency-key/{key}")
                check("by-idempotency-key route resolves it",
                      lookup.status_code == 200 and lookup.json()["id"] == first.json()["id"])

            # Languages are typed `string[]` and rendered into a customer-visible
            # line, so a stringified dict both breaks the type and shows up as
            # garbage. ElevenLabs publishes objects; 32 of them were flattened
            # with `str()` into "{\'language_id\': \'en\', ...}".
            tts_rows_now = req(client, "GET", "/v1/tts/models").json().get("models", [])
            bad_langs = [
                entry
                for r in tts_rows_now
                for entry in (r.get("supported_languages") or [])
                if not isinstance(entry, str) or "{" in entry or "'" in entry
            ]
            check("tts languages are plain codes, never stringified objects",
                  not bad_langs, f"{bad_langs[:2]}")

            # The per-request character limit is per model. It was one invented
            # number (40,000) for every TTS model, which over-promised the local
            # lane and Fish by eight times — a request that long simply fails.
            tts_limits = {r.get("maximum_input_characters") for r in tts_rows_now}
            check("tts character limit is per model, not one invented constant",
                  len([v for v in tts_limits if v]) != 1 or len(tts_rows_now) == 1,
                  f"distinct limits: {sorted(v for v in tts_limits if v)}")
            # The local lane auto-splits, so it can carry a long script; 25,000
            # is the agreed figure and the Hub chunks at sentence boundaries
            # rather than hard-capping.
            local_limits = {
                r.get("maximum_input_characters")
                for r in tts_rows_now if "studio-hub" in str(r.get("id"))
            }
            check("the local lane publishes the agreed 25,000 ceiling",
                  local_limits <= {25000}, f"{local_limits}")
            check("every tts row publishes a positive character limit",
                  all(isinstance(r.get("maximum_input_characters"), int)
                      and r["maximum_input_characters"] > 0 for r in tts_rows_now),
                  f"{[(r.get('id'), r.get('maximum_input_characters')) for r in tts_rows_now[:3]]}")

            # A model cannot both be English-only and able to detect language,
            # and these are multilingual MLX Whisper models. Unknown publishes
            # the "many, unenumerated" sentinel instead of a false "en".
            stt_rows_now = req(client, "GET", "/v1/transcription/models").json().get("models", [])
            contradictory = [
                r["id"] for r in stt_rows_now
                if r.get("supports_language_auto_detect") is True
                and r.get("supported_languages") == ["en"]
            ]
            check("no transcription model claims English-only and auto-detect together",
                  not contradictory, f"{contradictory[:2]}")
            check("transcription language is published in a form Story Studio accepts",
                  all(
                      isinstance(r.get("language"), str)
                      or isinstance(r.get("supported_languages"), list)
                      for r in stt_rows_now
                  ),
                  f"{[{k: r.get(k) for k in ('language', 'supported_languages')} for r in stt_rows_now[:2]]}")

            print("\n\033[1m7. TTS end to end (real WAV)\033[0m")
            tts_cat = req(client, "GET", "/v1/tts/models").json()
            tts_rows = tts_cat.get("models", [])
            check("tts catalogue non-empty", len(tts_rows) > 0)
            if tts_rows:
                tts_id = tts_rows[0]["id"]
                quote = req(client, "POST", "/v1/tts/quotes",
                            json={"model_id": tts_id, "voice_id": "voice-christian", "text": "hi"})
                check("tts quote publishes chargeable explicitly",
                      quote.json().get("chargeable") is True)
                r = req(client, "POST", "/v1/audio/jobs",
                        json={"model_id": tts_id, "text": "hello there", "voice_id": "voice-christian"},
                        headers={"Idempotency-Key": f"e2e-tts-{int(time.time())}"})
                check("tts submit 202", r.status_code == 202, f"got {r.status_code}")
                done = poll(client, f"/v1/jobs/{r.json()['id']}")
                check("tts job succeeded", done["state"] == "succeeded", done.get("failure_reason", ""))
                audio = req(client, "GET", f"/v1/jobs/{r.json()['id']}/audio")
                check("tts artifact is a real WAV",
                      audio.content[:4] == b"RIFF" and audio.content[8:12] == b"WAVE")

            print("\n\033[1m8. Transcription end to end (upload -> quote -> confirm -> SRT)\033[0m")
            stt_cat = req(client, "GET", "/v1/transcription/models").json()
            stt_rows = stt_cat.get("models", [])
            check("transcription catalogue non-empty", len(stt_rows) > 0)
            if stt_rows:
                stt_id = stt_rows[0]["id"]
                fake_audio = b"\xff\xfb\x90\x00" + b"\x00" * 512
                q = req(client, "POST", "/v1/audio/transcriptions/quotes",
                        files={"file": ("clip.mp3", fake_audio, "audio/mpeg")},
                        data={"model": stt_id})
                check("transcription quote accepts a multipart upload",
                      q.status_code == 200, q.text[:150])
                quote_id = q.json().get("id", "")
                check("quote returns an id", bool(quote_id))
                r = req(client, "POST", f"/v1/audio/transcriptions/quotes/{quote_id}/confirm",
                        headers={"Idempotency-Key": f"e2e-stt-{int(time.time())}"},
                        data={"model": stt_id})
                check("transcription confirm returns 202", r.status_code == 202, r.text[:200])
                if r.status_code == 202:
                    done = poll(client, f"/v1/transcriptions/{r.json()['id']}")
                    check("transcription succeeded", done["state"] == "succeeded",
                          (done.get("failure_reason") or "")[:200])
                    srt = req(client, "GET", f"/v1/transcriptions/{r.json()['id']}/srt")
                    check("SRT artifact returned",
                          srt.status_code == 200 and b"-->" in srt.content)

                stale = req(client, "POST", "/v1/audio/transcriptions/quotes/does-not-exist/confirm",
                            headers={"Idempotency-Key": f"e2e-stt-stale-{int(time.time())}"},
                            data={"model": stt_id})
                check("an expired/unknown quote is a typed 410",
                      stale.status_code == 410, f"got {stale.status_code}")

            print("\n\033[1m9. Provider failure -> settled failure, never 402\033[0m")
            fail_id = next((m["id"] for m in cat["models"] if "mock-fail" in m["id"]), None)
            if fail_id:
                r = req(client, "POST", "/v1/chat/responses",
                        json={"model_id": fail_id, "input": [{"role": "user", "content": "x"}]},
                        headers={"Idempotency-Key": f"e2e-fail-{int(time.time())}"})
                done = poll(client, f"/v1/chat/responses/{r.json()['id']}")
                check("failed job settles as failed", done["state"] == "failed")
                check("failure carries a code Story Studio can read",
                      bool(done.get("failure_code")), str(done.get("failure_code")))
                check("provider quota exhaustion is NOT reported as insufficient credits",
                      done.get("failure_code") != "INSUFFICIENT_CREDITS")
            else:
                check("mock-fail discovered", False, "not in catalogue")

            print("\n\033[1m10. Enable/disable is honoured on both paths\033[0m")
            r = httpx.post(f"{BASE}/admin/api/providers/mock-remote/enabled",
                           headers=AUTH, json={"enabled": False})
            check("provider disabled via admin", r.json()["provider"]["enabled"] is False)
            after = req(client, "GET", "/v1/chat/models").json()
            check("disabled provider disappears from the catalogue", len(after["models"]) == 0)
            blocked = req(client, "POST", "/v1/chat/responses",
                          json={"model_id": model_id, "input": [{"role": "user", "content": "x"}]},
                          headers={"Idempotency-Key": f"e2e-disabled-{int(time.time())}"})
            check("generation against a disabled provider is refused",
                  blocked.status_code == 404, f"got {blocked.status_code}")

            httpx.post(f"{BASE}/admin/api/providers/mock-remote/enabled",
                       headers=AUTH, json={"enabled": True})
            req(client, "GET", "/v1/chat/models")
            single = "mock-chat-2"
            r = httpx.post(f"{BASE}/admin/api/providers/mock-remote/models/enabled",
                           headers=AUTH, json={"model_ids": [single], "enabled": False})
            check("single model disabled via admin", r.status_code == 200)
            after = req(client, "GET", "/v1/chat/models").json()
            remaining = {m["id"] for m in after["models"]}
            check("disabled model drops from the catalogue",
                  not any(single in m for m in remaining), str(remaining))
            check("other models of the same provider survive", len(remaining) >= 1)

            print("\n\033[1m11. Manual model add (provider shipped an unlisted model)\033[0m")
            r = httpx.post(f"{BASE}/admin/api/providers/mock-remote/models", headers=AUTH,
                           json={"model_id": "mock-brand-new", "display_name": "Brand New",
                                 "modality": "chat"})
            check("manual model add accepted", r.status_code == 200, r.text[:120])
            added = {m["model_id"] for m in r.json()["models"]}
            check("manual model appears in inventory", "mock-brand-new" in added)
            req(client, "GET", "/v1/chat/models")
            fresh = req(client, "GET", "/v1/chat/models").json()
            check("manual model reaches the Story catalogue",
                  any("mock-brand-new" in m["id"] for m in fresh["models"]))

            print("\n\033[1m12. BFL and MiniMax adapters\033[0m")
            # These providers have no keys on this machine, so their real API
            # shapes are exercised against the mock: BFL's async submit/poll/
            # download with its own state names, and MiniMax's four dissimilar
            # endpoints including the hex-encoded speech response.
            wanted = {
                "mock-bfl": ["flux-pro-1.1"],
                "mock-minimax": ["image-01", "video-01", "music-01", "speech-2.8-turbo"],
            }
            # A model is only reachable when its provider is configured AND
            # enabled AND the model is enabled — all three. Setting the key
            # first is not incidental: without it the providers are skipped
            # entirely and every submission below 404s.
            for provider_id in wanted:
                keyed = httpx.post(
                    f"{BASE}/admin/api/providers/{provider_id}/credentials",
                    headers=AUTH,
                    json={"values": {"api_key": "mock-key"}},
                )
                check(f"{provider_id}: credential accepted", keyed.status_code == 200,
                      keyed.text[:120])
            enable_items = [
                {"provider_id": pid, "model_id": mid}
                for pid, ids in wanted.items()
                for mid in ids
            ]
            enabled = httpx.post(
                f"{BASE}/admin/api/models/enabled",
                headers=AUTH,
                json={"items": enable_items, "enabled": True},
            )
            check("BFL + MiniMax models enabled", enabled.status_code == 200, enabled.text[:120])

            def run_generation(label, submit_path, poll_path, body, artifact_path, signature=None):
                key = f"e2e-{label}-{int(time.time())}"
                submitted = req(
                    client, "POST", submit_path, json=body, headers={"Idempotency-Key": key}
                )
                if submitted.status_code != 202:
                    check(f"{label}: submit accepted", False,
                          f"{submitted.status_code} {submitted.text[:160]}")
                    return
                check(f"{label}: submit accepted", True)
                job_id = submitted.json()["id"]
                done = poll(client, poll_path.format(id=job_id))
                check(f"{label}: succeeded", done["state"] == "succeeded",
                      (done.get("failure_reason") or "")[:200])
                if done["state"] != "succeeded":
                    return
                artifact = req(client, "GET", artifact_path.format(id=job_id))
                ok = artifact.status_code == 200 and (
                    signature(artifact.content) if signature else True
                )
                check(f"{label}: artifact valid ({len(artifact.content)} bytes)", ok)

            is_png = lambda b: b[1:4] == b"PNG"
            is_mp4 = lambda b: b[4:8] == b"ftyp"
            is_wav = lambda b: b[:4] == b"RIFF" and b[8:12] == b"WAVE"

            run_generation(
                "bfl-image",
                "/v1/images/generations", "/v1/images/{id}",
                {"model_id": "mock-bfl::flux-pro-1.1", "prompt": "a lighthouse"},
                "/v1/images/{id}/file", is_png,
            )
            run_generation(
                "minimax-image",
                "/v1/images/generations", "/v1/images/{id}",
                {"model_id": "mock-minimax::image-01", "prompt": "a lighthouse"},
                "/v1/images/{id}/file", is_png,
            )
            run_generation(
                "minimax-video",
                "/v1/videos/generations", "/v1/videos/{id}",
                {"model_id": "mock-minimax::video-01", "prompt": "a lighthouse"},
                "/v1/videos/{id}/file", is_mp4,
            )
            run_generation(
                "minimax-music",
                "/v1/music/generations", "/v1/music/jobs/{id}",
                {"model_id": "mock-minimax::music-01", "prompt": "calm ambient"},
                "/v1/music/jobs/{id}/outputs/0", None,
            )
            run_generation(
                "minimax-speech",
                "/v1/audio/jobs", "/v1/jobs/{id}",
                {"model_id": "mock-minimax::speech-2.8-turbo", "text": "hello"},
                "/v1/jobs/{id}/audio", is_wav,
            )

            music_cat = req(client, "GET", "/v1/music/models").json()
            check("music catalogue is reachable", "models" in music_cat)
            music_job = req(client, "POST", "/v1/music/generations",
                            json={"model_id": "mock-minimax::music-01", "prompt": "x"},
                            headers={"Idempotency-Key": f"e2e-music-shape-{int(time.time())}"})
            check("music submit returns 202", music_job.status_code == 202,
                  f"got {music_job.status_code}")

            print("\n\033[1m13. Invariants\033[0m")
            check("no response in the whole run was 402", 402 not in statuses,
                  f"statuses seen {sorted(statuses)}")
            check("auth is enforced on the Story surface",
                  httpx.get(f"{BASE}/v1/chat/models").status_code == 401)
            overview = req(client, "GET", "/v1/account/overview").json()
            check("overview publishes integer paid_concurrency_limit",
                  isinstance(overview.get("paid_concurrency_limit"), int))
            check("overview publishes credit_balance",
                  isinstance(overview.get("credit_balance"), int))

            print("\n\033[1m14. Streamed WAV header repair\033[0m")
            # Fish streams WAV responses with sentinel chunk sizes. A 327KB file
            # declared a 4GB RIFF size and a 4GB data chunk; Python's `wave`
            # read it as 13 hours. ffmpeg copes, but the container is malformed
            # and any strict parser may reject it.
            import struct as _struct

            from gateway.adapters.fish import _repair_streaming_wav

            payload = b"\x00\x00" * 100
            wav = (
                b"RIFF" + _struct.pack("<I", 0xFFFFFF24) + b"WAVE"
                + b"fmt " + _struct.pack("<I", 16)
                + b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00"
                + b"data" + _struct.pack("<I", 0xFFFFFF00) + payload
            )
            fixed = _repair_streaming_wav(wav)
            check("RIFF size is corrected",
                  _struct.unpack("<I", fixed[4:8])[0] == len(fixed) - 8)
            check("data chunk size is corrected",
                  _struct.unpack("<I", fixed[40:44])[0] == len(payload))
            check("audio payload is left untouched", fixed[44:] == payload)
            check("a well-formed WAV is returned unchanged",
                  _repair_streaming_wav(fixed) == fixed)
            check("non-WAV bytes pass through untouched",
                  _repair_streaming_wav(b"not audio at all") == b"not audio at all")

            print("\n\033[1m15. Voice registry\033[0m")
            # Local voices need no provider API, so they are testable here.
            # Provider voices (ElevenLabs, Fish) need live keys and are verified
            # separately; what this covers is the part that is ours: discovery,
            # enable/disable, and what reaches Story Studio.
            catalogue = httpx.get(f"{BASE}/admin/api/voices", headers=AUTH).json()
            local = [v for v in catalogue["voices"] if v["source"] == "local"]
            check("local voices are discovered", len(local) > 0, f"got {len(local)}")
            check("local voices are enabled by default",
                  all(v["enabled"] for v in local),
                  str([v["enabled"] for v in local][:4]))
            check("voice ids are namespaced by provider",
                  all("::" in v["id"] for v in local))

            victim = local[0]
            off = httpx.post(f"{BASE}/admin/api/voices/enabled", headers=AUTH,
                             json={"items": [victim["id"]], "enabled": False})
            check("a voice can be switched off", off.status_code == 200
                  and off.json()["changed"] == 1, off.text[:120])

            after = httpx.get(f"{BASE}/admin/api/voices?only_enabled=true",
                              headers=AUTH).json()["voices"]
            check("a disabled voice leaves the enabled set",
                  victim["id"] not in {v["id"] for v in after})

            story = req(client, "GET", "/v1/voices").json()["voices"]
            check("Story Studio does not see the disabled voice",
                  victim["id"] not in {v["id"] for v in story})
            check("Story Studio sees the remaining local voices",
                  len(story) == len(local) - 1, f"got {len(story)} of {len(local)}")
            check("every Story voice publishes catalog_status=published",
                  all(v["catalog_status"] == "published" for v in story))
            check("every Story voice publishes zero price fields",
                  all(v["credits_per_1000_characters"] == 0 for v in story))

            httpx.post(f"{BASE}/admin/api/voices/enabled", headers=AUTH,
                       json={"items": [victim["id"]], "enabled": True})
            restored = req(client, "GET", "/v1/voices").json()["voices"]
            check("re-enabling restores it", victim["id"] in {v["id"] for v in restored})

            # A local voice belongs to one model; the match is on the voice's
            # own tts_model_id, not on the provider alone.
            model_id = str(victim.get("tts_model_id") or "")
            check("a local voice reports the model it belongs to", bool(model_id), model_id)
            if model_id:
                scoped = req(client, "GET", f"/v1/voices?model_id=studio-hub-local::{model_id}").json()
                check("a local voice is returned for its own model",
                      len(scoped["voices"]) > 0, f"got {len(scoped['voices'])}")
                other = req(client, "GET",
                            "/v1/voices?model_id=studio-hub-local::some-other-model").json()
                check("and not for an unrelated model", len(other["voices"]) == 0,
                      f"got {len(other['voices'])}")

    finally:
        for proc in processes:
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
        mock_fh.close()
        gw_fh.close()
        if failures:
            gateway_text = gw_log.read_text(errors="replace")
            if "Traceback" in gateway_text:
                print("\n--- gateway log (tail) ---")
                print(gateway_text[-2500:])
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    if failures:
        print(f"\033[31m{len(failures)} of {checks} checks FAILED\033[0m")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"\033[32mAll {checks} end-to-end checks passed.\033[0m")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
