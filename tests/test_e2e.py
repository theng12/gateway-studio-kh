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

#: The value this run pretends is this Mac's Hub operator token. Deliberately
#: unmistakable: it must never appear on the mock's wire, and if it ever does,
#: the assertion should be readable rather than a comparison of empty strings.
OPERATOR_TOKEN = "OPERATOR-TOKEN-MUST-NOT-LEAVE-THIS-MAC"

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

    # A clean app ships no private recordings. Seed this suite's own two voices
    # instead of depending on source-Mac metadata that used to ship accidentally.
    import hashlib as _fixture_hashlib
    import wave as _fixture_wave
    fixture_voices = []
    for index in range(2):
        reference = tmp / f"fixture-voice-{index}.wav"
        with _fixture_wave.open(str(reference), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * (1600 + index * 160))
        digest = _fixture_hashlib.sha256(reference.read_bytes()).hexdigest()
        fixture_voices.append({
            "id": f"voice-{digest[:12]}", "display_name": f"Fixture {index}",
            "language": "en", "engine_id": "qwen3-tts", "engine_name": "Qwen3 TTS",
            "tts_model_id": "Qwen3-TTS-12Hz-0.6B-Base-4bit", "reference_path": str(reference),
            "reference_transcript": "fixture voice", "audio_sha256": digest,
            "permission_acknowledged": False, "license": "",
        })
    (tmp / "voices.json").write_text(json.dumps({"voices": fixture_voices}))

    # The owner's curated limits, for lanes that publish none. Placed before the
    # gateway starts, because the file is read at catalogue build.
    #
    # Three entries, and each one is a different half of the rule:
    #   * `limits-probe` is not a discovered model at all — it is the fixture for
    #     the pure `limits.gap()` checks;
    #   * `mock-chat-plain` is a real row whose provider states nothing, so the
    #     curated value is the one that must appear in the catalogue;
    #   * `mock-chat-1` states its own values, and the file deliberately
    #     contradicts them, so that "a provider's value always wins" is proven
    #     through the catalogue rather than only inside `gap()`.
    (tmp / "model-limits.local.json").write_text(json.dumps({
        "models": {
            "mock-remote::limits-probe": {
                "context_window_tokens": 1000000, "max_output_tokens": 128000,
            },
            "mock-remote::mock-chat-plain": {
                "context_window_tokens": 65536, "max_output_tokens": 4096,
            },
            "mock-remote::mock-chat-1": {
                "context_window_tokens": 999999, "max_output_tokens": 999999,
            },
            "mock-remote::mock-tts-plain": {
                "maximum_input_characters": 12345,
            },
        },
    }))

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
            {
                # Google's adapter is the OpenAI-compatible shim, which serves
                # chat/image/tts/transcription — and Google *declares* six
                # modalities. This provider declares a lane the adapter cannot
                # serve so the gate is proven on the catalogue rather than only on
                # the mapping table: the chat model must publish and the video
                # model, with nothing behind it, must not.
                "id": "mock-google",
                "display_name": "Mock Google",
                "category": "multi",
                "kind": "google",
                "base_url": f"http://127.0.0.1:{MOCK_PORT}",
                "modalities": ["chat", "video"],
                "discovery": "static",
                "description": "Test provider.",
                "static_models": [
                    {"id": "gemini-2.5-pro", "display_name": "Gemini Pro", "modality": "chat"},
                    {"id": "veo-3", "display_name": "Veo 3", "modality": "video"},
                ],
            },
            {
                # A **location**, pointed at the mock rather than the owner's real
                # Hub — which is why the id carries the location prefix: a
                # location publishes its models under `studio-hub::`, and that
                # published id is the one Story Studio asks voices with. It serves
                # `model-packages` (installed) and `catalog` (what each model *is*),
                # which is what the local voice lane reads to decide which models a
                # recording may be used with.
                "id": "studio-hub-mock",
                "display_name": "Mock Hub",
                "category": "multi",
                "kind": "studio_hub",
                "base_url": f"http://127.0.0.1:{MOCK_PORT}",
                # `chat` is declared because Hub 0.6.63 opened its chat peer route
                # to a Fleet credential, so this location serves the lane — which is
                # what makes the dispatch below reachable at all. `video` is declared
                # because the Hub grew a real Video worker, and `withhold_reason` reads
                # the *definition* before the kind table: a location that does not
                # declare the lane withholds every video row it owns.
                "modalities": ["tts", "chat", "video"],
                "discovery": "studio_hub",
                "description": "Test location.",
                # Declared, and optional, exactly as the built-in local provider
                # does. A user-defined provider with no `credential_fields` gets
                # the loader's default of one **required** `api_key`, and a
                # location has no key to give: it would stay unconfigured, and an
                # unconfigured provider publishes nothing however many of its
                # models are switched on.
                "credential_fields": [
                    {"key": "hub_token", "label": "Hub token", "secret": True,
                     "required": False},
                ],
            },
            {
                # ElevenLabs' import route copies a library voice into the owner's
                # account and answers JSON. This provider exists so the route can
                # be pointed at a 200 that is *not* JSON, which used to raise out
                # of the handler after the voice had already been added upstream.
                "id": "mock-eleven",
                "display_name": "Mock ElevenLabs",
                "category": "voice",
                "kind": "elevenlabs",
                "base_url": f"http://127.0.0.1:{MOCK_PORT}",
                "modalities": ["tts"],
                "discovery": "static",
                "description": "Test provider.",
            },
            {
                # Kie's unified task route. Images, videos and **music** all land
                # on `POST /api/v1/jobs/createTask`; music differs only in the
                # top-level api-model name (`ai-music-api/generate`) and the
                # `resultJson` shape (a `data` array of tracks). This provider is
                # how that shape is exercised without spending the owner's
                # credits — the real route was measured by hand once.
                "id": "mock-kie",
                "display_name": "Mock Kie",
                "category": "multi",
                "kind": "kie",
                "base_url": f"http://127.0.0.1:{MOCK_PORT}",
                "modalities": ["music"],
                "discovery": "static",
                "description": "Test provider.",
                "static_models": [
                    {"id": "V6", "display_name": "Mock Suno V6", "modality": "music"},
                ],
            },
        ]
    }))

    env = {
        **os.environ,
        "GATEWAY_DATA": str(tmp),
        "GATEWAY_APP_SUPPORT": str(tmp),
        # **The key lives in `.env`, not in the process environment.**
        #
        # `get()` prefers `os.environ`, so a key passed here would shadow `.env`
        # completely and the rotation check in section 13b could not exist. `.env`
        # is also how the owner really supplies one — the documented no-UI way —
        # so it is the path worth exercising. The cost is that a `.env` that fails
        # to load leaves a *keyless* gateway, which accepts any token: section 13b
        # asserts a wrong key is refused, which is what makes that loud rather
        # than silent.
        "GATEWAY_PORT": str(GW_PORT),
        # A known build stamp, which is also how the app passes its own: the menu
        # bar app sets GATEWAY_BUILD when it spawns the gateway, and Check Gateway
        # compares that answer against the app's Info.plist. A fixed value here
        # makes the reading deterministic instead of depending on whether a BUILD
        # file happens to sit beside the package.
        "GATEWAY_BUILD": "test-build-0000",
        # A clean slate: without this the gateway would migrate the developer's
        # real credentials and configured providers into the test run.
        "GATEWAY_NO_MIGRATE": "1",
        # **Point the local Hub discovery at nothing.**
        #
        # `hub.find_ready_file()` falls back to the owner's real
        # `~/Library/Application Support/StudioHub Standalone/hub-ready.json`, so
        # without this the isolated gateway sees his live Hub as its *local
        # location* — and a suite documented as "cannot disturb the live config"
        # could reach it. It did: an earlier version of the shared-voice fan-out
        # test pushed a voice onto his real Hub and had to have it deleted. The
        # only reason nothing else broke is that no other section selects the
        # local lane; that was luck, not isolation.
        "STUDIOHUB_READY_FILE": str(tmp / "no-such-hub-ready.json"),
        # **A known operator token, so "was it sent to a remote?" is answerable.**
        #
        # `hub_headers` used to be `token or hub_token()`, so a controller with no
        # credential of its own was handed this Mac's *operator* token and it was
        # sent to the address the owner had typed into the Fleet page. With no
        # local Hub in this run, `hub_token()` would otherwise be `""` and the
        # leak would be invisible: a test that passes both before and after the
        # fix is not a test. The value is unmistakable so any appearance of it on
        # the mock's wire is unambiguous.
        "STUDIOHUB_HUB_TOKEN": OPERATOR_TOKEN,
        "PYTHONPATH": str(ROOT),
        "PYTHONUNBUFFERED": "1",
    }

    # The suite's own process does the unit-level checks, and `config.get` reads
    # `os.environ` live, so it needs the same sentinel the gateway was given.
    os.environ["STUDIOHUB_HUB_TOKEN"] = OPERATOR_TOKEN

    # The gateway's key, where the gateway actually looks for it. Written before
    # the process starts, because `require_auth` reads it per request from whatever
    # `.env` says at that moment — that is the behaviour section 13b pins.
    (tmp / ".env").write_text(
        "# Written by tests/test_e2e.py; the key lives here so rotation is testable.\n"
        f"GATEWAY_API_KEY={GW_KEY}\n"
    )

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
            [PY, "-c", """
from pathlib import Path
from gateway import retention
import uvicorn
import sys
sandbox = Path(sys.argv[1])
recycled = sandbox / "macos-trash"
recycled.mkdir(exist_ok=True)
def recycle(path):
    if not path.resolve().is_relative_to(sandbox.resolve()):
        raise RuntimeError("Test recycle path escaped sandbox")
    target = recycled / path.name
    count = 0
    while target.exists():
        count += 1
        target = recycled / f"{path.name}.{count}"
    path.rename(target)
    return True
retention._recycle = recycle
uvicorn.run("gateway.api:app", host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
""", str(tmp), str(GW_PORT)],
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
            # The stamp the app compares against its own. Without it a gateway
            # adopted from a previous build is indistinguishable from the current
            # one, which is how four rebuilds "took effect" without taking effect.
            check("health says which build is running",
                  health.get("build") == "test-build-0000", repr(health.get("build")))
            # Counted from the shipped list rather than hardcoded: adding a
            # provider is a legitimate change, and a literal here fails for a
            # reason that has nothing to do with what the check is about.
            from gateway.providers.definitions import PROVIDER_DEFINITIONS as _SHIPPED
            expected_providers = len(_SHIPPED) + 7  # + the seven custom ones above
            check(f"{len(_SHIPPED)} built-in providers + 7 custom",
                  health["providers"]["total"] == expected_providers,
                  f"got {health['providers']['total']}, expected {expected_providers}")
            # A kind having an adapter is not the same claim as that adapter
            # serving every modality the provider declares, so this asks the
            # per-provider question too: `mock-google` declares video and the
            # adapter serves none, and the console is told which lanes those are.
            _providers = httpx.get(f"{BASE}/admin/api/providers", headers=AUTH).json()["providers"]
            _google = next((p for p in _providers if p["id"] == "mock-google"), None)
            check("a provider declaring an unservable lane is reported as having one",
                  _google is not None and _google["unserved_modalities"] == ["video"],
                  f"{_google and _google.get('unserved_modalities')}")
            # Kie declares four modalities and its adapter serves three: `chat` is
            # the lane with no curated model and no handler, so it is the one the
            # console must report — and *music* must not be among them, because a
            # lane listed here is a lane a row could be added to that can never run.
            _kie = next((p for p in _providers if p["id"] == "kie-ai-images"), None)
            check("Kie reports chat as its only unserved lane, not music",
                  _kie is not None and _kie["unserved_modalities"] == ["chat"],
                  f"{_kie and _kie.get('unserved_modalities')}")
            # **A field nothing draws is the same defect as a control that does
            # nothing.** `unserved_modalities` is computed for the console, so the
            # console's own source has to reference it; this is the level at which
            # a served SPA can be checked without a browser, and it is deliberately
            # a source assertion rather than a claim about rendering.
            _ui = httpx.get(f"{BASE}/admin/").text
            check("the console actually renders the unserved lanes it is told about",
                  "unserved_modalities" in _ui,
                  "the field is computed and never drawn")
            check("and the console's toggle paths re-read instead of trusting the click",
                  "await loadModels();" in _ui and "state.models[id] = await api" in _ui,
                  "a toggle must draw the server's answer, not the click")
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
            check("discovery found every mock model", len(found) == 9, f"got {sorted(found)}")
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
            # Scoped to *this* provider rather than comparing the total, which grows
            # every time the fixture gains another mock provider — a check that fails
            # for a reason unrelated to what it is about. What it is about: the
            # search reaches a provider's models by id, name and provider, and finds
            # all of them.
            _from_remote = [m for m in listed["models"] if m["provider_id"] == "mock-remote"]
            check("model browser search finds them across providers",
                  len(_from_remote) == len(found)
                  and len({m["provider_id"] for m in listed["models"]}) > 1,
                  f"matched {listed['matched']}, mock-remote {len(_from_remote)} of {len(found)}")

            print("\n\033[1m3. Story Studio catalogue contract\033[0m")
            cat = req(client, "GET", "/v1/chat/models").json()
            check("chat catalogue non-empty once enabled", len(cat["models"]) > 0)
            check("catalogue envelope has freshness",
                  {"stale", "ageMs", "loadedAt"} <= set(cat["freshness"]))
            check("freshness not stale", cat["freshness"]["stale"] is False)
            check("freshness ageMs credible", cat["freshness"]["ageMs"] < 30 * 60 * 1000)
            # **Two questions, two numbers.** `ageMs` times the last *assembly*,
            # and it can never reach the 30-minute gate: `freshness()` calls
            # `rebuild()` first, which resets the clock, and `rebuild()` runs no
            # discovery — it re-sorts a cache whose per-provider `fetched_at` can be
            # days old. Asserted as it behaves rather than as it reads, so nobody
            # later mistakes `stale: false` for a measurement of the inventory.
            check("the assemble age cannot reach the staleness gate",
                  cat["freshness"]["ageMs"] < 20 * 1000,
                  f"{cat['freshness']['ageMs']}ms vs a 30-minute gate")
            check("so the inventory age is published as its own field",
                  "inventoryAgeMs" in cat["freshness"]
                  and "inventoryOldestProvider" in cat["freshness"],
                  f"{sorted(cat['freshness'])}")
            check("and it is never younger than the assembly it describes",
                  cat["freshness"]["inventoryAgeMs"] is None
                  or cat["freshness"]["inventoryAgeMs"] >= cat["freshness"]["ageMs"],
                  f"inventory={cat['freshness']['inventoryAgeMs']} assemble={cat['freshness']['ageMs']}")
            # Keyed by id, not by position. `cat["models"][0]` was the whole
            # "row contract" section, and every claim in it was about whichever
            # row happened to sort first — add a model and the checks silently
            # start describing a different one.
            by_id = {r["id"]: r for r in cat["models"]}
            positioned = by_id.get("mock-remote::mock-chat-1")
            check("the row the contract is checked against is present",
                  positioned is not None, f"{sorted(by_id)[:6]}")
            row = positioned or cat["models"][0]
            check("row publishes namespaced id", "::" in row["id"], row["id"])
            check("row publishes zero price fields",
                  row["input_credits_per_million_tokens"] == 0
                  and row["output_credits_per_million_tokens"] == 0
                  and row["minimum_credits"] == 0)
            check("row publishes price_version + cost_tier",
                  bool(row["price_version"]) and bool(row["cost_tier"]))
            check("every row's customer_handle is its own id",
                  all(r["customer_handle"] == r["id"] for r in cat["models"]),
                  f"{[r['id'] for r in cat['models'] if r['customer_handle'] != r['id']][:3]}")
            check("a cloud vendor row carries no aliases",
                  row["legacy_ids"] == [], f"{row['legacy_ids']}")
            check("row publishes context window from provider",
                  row["context_window_tokens"] == 32000, f"{row['context_window_tokens']}")
            check("and a second model publishes its own, different window",
                  by_id["mock-remote::mock-chat-2"]["context_window_tokens"] == 128000,
                  f"{by_id.get('mock-remote::mock-chat-2', {}).get('context_window_tokens')}")

            # A staged voice reference belongs to the controller that minted it.
            # Keyed by voice alone, a reference staged on one location would be
            # handed to another that had never seen the asset — invisible with one
            # location, broken with several.
            #
            # And it belongs to the *recording* that was staged. A re-import keeps
            # the voice's id and changes its audio, so a placement cached for the
            # old clip answered for the new one for up to six hours: the controller
            # used a recording that no longer exists, and nothing said so.
            from gateway.adapters.studio_hub import _reference_cache_key
            check("a staged reference is cached per location, not per voice",
                  _reference_cache_key("v1", "http://a:1", "sha-old")
                  != _reference_cache_key("v1", "http://b:1", "sha-old"),
                  "same key for two controllers")
            check("the same location reuses its own staged reference",
                  _reference_cache_key("v1", "http://a:1", "sha-old")
                  == _reference_cache_key("v1", "http://a:1/", "sha-old"),
                  "trailing slash changed the key")
            check("but not for a different recording of the same voice",
                  _reference_cache_key("v1", "http://a:1", "sha-old")
                  != _reference_cache_key("v1", "http://a:1", "sha-new"),
                  "the placement outlived the audio it was made for")

            # The curated-limits file fills a provider's silence and never
            # overrides live data. Tested directly against the module, because a
            # check that depends on which mock models happen to be enabled is a
            # check that can silently not run — which is how the first version of
            # this passed while asserting nothing.
            import importlib
            os.environ["GATEWAY_MODEL_LIMITS"] = str(tmp / "model-limits.local.json")
            from gateway import limits as limits_module
            importlib.reload(limits_module)
            check("the curated file is read",
                  limits_module.for_model("mock-remote", "limits-probe")
                  .get("context_window_tokens") == 1000000)
            check("a gap is filled from the curated file",
                  limits_module.gap("max_output_tokens", None, "mock-remote", "limits-probe")
                  == 128000)
            check("a published value beats the curated file",
                  limits_module.gap("max_output_tokens", 8192, "mock-remote", "limits-probe")
                  == 8192)
            check("an unstated model stays unknown",
                  limits_module.gap("max_output_tokens", None, "nobody", "nothing") is None)
            check("a provider's own value is not overridden by the curated file",
                  row["context_window_tokens"] == 32000, f"{row['context_window_tokens']}")
            # ...and the file still does its job where the provider said nothing.
            # Both halves have to be checked through the catalogue: the pure
            # `gap()` checks above prove the function, not that the catalogue
            # consults it, and the old version of this check pointed at a model
            # that was not in the curated file at all, so it proved neither.
            plain = by_id.get("mock-remote::mock-chat-plain")
            check("the curated file fills a gap the provider left",
                  plain is not None and plain["context_window_tokens"] == 65536,
                  f"{plain and plain.get('context_window_tokens')}")
            bare = by_id.get("mock-remote::mock-chat-bare")
            check("a model neither source knows stays unknown, not zero",
                  bare is not None and bare["context_window_tokens"] is None,
                  f"{bare and bare.get('context_window_tokens')}")

            # **Whose number is it.** Story Studio renders these as the model's own
            # window and cannot tell a provider's figure from one this gateway
            # chose: the owner's curated file has 24 rows carrying the identical
            # `1000000/128000`, which is a flat default wearing the clothes of
            # per-model knowledge. The value is still published — nulling it makes
            # Story Studio substitute its own 8,192, which is worse — but now with
            # a companion field saying where it came from.
            check("a provider's own window is labelled as the provider's",
                  row.get("context_window_tokens_source") == "provider",
                  f"{row.get('context_window_tokens_source')}")
            check("a window the curated file supplied is labelled as curated",
                  plain.get("context_window_tokens_source") == "curated",
                  f"{plain.get('context_window_tokens_source')}")
            check("and one nobody supplied is labelled as ours, not as a fact",
                  bare.get("context_window_tokens_source") == "gateway",
                  f"{bare.get('context_window_tokens_source')}")
            # A published 0 is a claim that the model accepts nothing, which is a
            # different statement from "the provider did not say". Story Studio's
            # own rule forbids the two from sharing a value, so an unpublished
            # window must arrive as null.
            # `supports_vision` was hardcoded false and `input_modalities` hardcoded
            # to ["text"] for every chat model, while 273 of OpenRouter's 445
            # models accept image input — so the catalogue claimed no model had
            # vision and Story Studio hid the control that uses it. The two fields
            # must now agree with each other and with the provider's own answer.
            #
            # This has to require a *stated* corpus. With every model silent, both
            # lists were empty and passed — and the hardcoding it exists to catch
            # would have published `["text"]`/`false`, which agree with each other,
            # so it would have passed then too.
            stated = [r for r in cat["models"] if isinstance(r.get("input_modalities"), list)]
            check("some models state their input modalities, so the check means something",
                  len(stated) >= 2, f"{[r['id'] for r in stated]}")
            disagreed = [
                r["id"] for r in stated
                if r.get("supports_vision") != ("image" in r["input_modalities"])
            ]
            check("vision support agrees with the published input modalities",
                  len(stated) >= 2 and not disagreed, f"{disagreed[:3]}")
            check("a model the provider says takes images is published with vision",
                  any(r.get("supports_vision") is True for r in stated),
                  f"{[(r['id'], r.get('supports_vision')) for r in stated]}")
            check("and a model it does not is published without",
                  any(r.get("supports_vision") is False for r in stated),
                  f"{[(r['id'], r.get('supports_vision')) for r in stated]}")
            unstated = [
                r["id"] for r in cat["models"]
                if r.get("input_modalities") is None and r.get("supports_vision") is not None
            ]
            check("vision is null when the provider states no modalities",
                  unstated == [], f"{unstated[:3]}")
            check("and there really is a model that states none",
                  any(r.get("input_modalities") is None for r in cat["models"]))

            zero_ctx = [r["id"] for r in cat["models"] if r.get("context_window_tokens") == 0]
            check("no chat row publishes a zero context window", not zero_ctx, f"{zero_ctx[:3]}")
            # The output allowance was hardcoded to one number for every model on
            # every provider, so a model allowing 131,072 was described as 8,192.
            # A single shared value across a real catalogue is that bug returning.
            #
            # It has to be judged on the rows that *state* one, and it has to
            # require more than one of them: `{None}` has exactly one distinct
            # value, so the old check passed on a catalogue where nobody stated
            # anything at all.
            allow_stated = [
                r for r in cat["models"] if isinstance(r.get("max_output_tokens"), int)
            ]
            allowances = {r["max_output_tokens"] for r in allow_stated}
            check("several models state their own output allowance",
                  len(allow_stated) >= 2, f"{[(r['id'], r['max_output_tokens']) for r in allow_stated]}")
            check("output allowance is per model, not one shared constant",
                  len(allowances) > 1, f"distinct values: {sorted(allowances)}")
            check("and a model that states none publishes null rather than a constant",
                  any(r.get("max_output_tokens") is None for r in cat["models"]),
                  f"{[(r['id'], r.get('max_output_tokens')) for r in cat['models']]}")

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
            # **Three answers, read from the model's own statement.** This used to
            # be a definite `True` for a location and a definite `False` for every
            # cloud kind, while *every* row's `operations` claimed
            # `image-to-image` — so a Hub whose capabilities say `txt2img` was
            # advertised as taking references, and a `text-to-image` endpoint was
            # advertised as serving an image-to-image operation. The contradiction
            # was the tell; the rule is now one read with three answers.
            from gateway.providers.definitions import image_reference_state as _ref_state
            from gateway.catalog import _image_row as _image_row_for
            from gateway.providers.definitions import PROVIDERS_BY_ID as _P_img
            _txt = _image_row_for(
                "openai/gpt-image-2.5/flare/text-to-image", _P_img["fal-ai"],
                {"model_id": "openai/gpt-image-2.5/flare/text-to-image",
                 "raw": {"category": "text-to-image"}})
            check("a stated text-to-image model does not claim references or an edit operation",
                  _txt["supports_reference_images"] is False
                  and _txt["operations"] == ["text-to-image"]
                  and _txt["max_reference_images"] == 0,
                  f"{_txt['supports_reference_images']} {_txt['operations']}")
            _unstated_img = _image_row_for(
                "mystery-image", _P_img["kie-ai-images"],
                {"model_id": "mystery-image", "raw": {}})
            check("an image model that states nothing publishes null, not a definite no",
                  _unstated_img["supports_reference_images"] is None
                  and _unstated_img["max_reference_images"] is None
                  and _unstated_img["operations"] == ["text-to-image"],
                  f"{_unstated_img['supports_reference_images']}")
            check("the Hub's own capabilities are what a location row is described by",
                  _ref_state("studio_hub", "x", {"capabilities": ["txt2img"]}) is False
                  and _ref_state("studio_hub", "x", {"capabilities": ["img2img"]}) is True
                  and _ref_state("studio_hub", "x", {}) is None,
                  "capabilities drive the answer, and absence means unstated")
            # And a stated image-to-image endpoint on a lane that cannot send an
            # image is withheld, because fal's queue accepts the body and only
            # fails at the worker — the `:batch` shape.
            from gateway.providers.credentials import CredentialStore as _Credentials
            from gateway.providers.models import ModelInventory as _Inventory
            # An inventory, **not** the bare class. This used to call
            # `_Inventory.can_publish(None, definition, model)` under the note that
            # "`can_publish` reads nothing off `self`" — which stopped being true
            # when the reason and the verdict became one function: `withhold_reason`
            # reads the credential store for the batch-route branch. That branch is
            # not reached from an image row, which is the only reason the unbound
            # call ever worked. Ask it the way the gateway asks it.
            _inventory = _Inventory(
                _Credentials(path=tmp / "credentials.json"),
                path=tmp / "models-cache.json",
            )

            def _can_publish(provider_id: str, model_id: str) -> bool:
                return _inventory.can_publish(
                    _P_img[provider_id],
                    {"model_id": model_id, "modality": "image", "raw": {}})

            check("an image-to-image row on a lane that cannot send an image is not offered",
                  not _can_publish("fal-ai", "fal-ai/flux/dev/image-to-image"),
                  "the fal image body sends no reference image")
            check("but the same classification on a lane that can is not withheld",
                  _can_publish("kie-ai-images", "anything/edit"),
                  "Kie forwards image_url, so its edit rows can still publish")

            print("\n\033[1m4. Chat generation end to end\033[0m")
            model_id = row["id"]
            r = req(client, "POST", "/v1/chat/responses",
                    json={"model_id": model_id, "input": [{"role": "user", "content": "hello"}]},
                    headers={"Idempotency-Key": f"e2e-chat-{int(time.time())}",
                             # Who asked. Asked live on 2026-09-25 and unanswerable:
                             # forty-one jobs failed in a minute and nothing recorded
                             # whether Story Studio or a script had sent them.
                             "X-Caller": "end-to-end-suite"})
            check("chat submit returns 202", r.status_code == 202, f"got {r.status_code}")
            job = r.json()
            check("chat job carries balanced zero settlement",
                  (job["reserved_credits"], job["settled_credits"], job["released_credits"]) == (0, 0, 0))
            # **The whole chain, on the surface the Jobs window actually reads.** A
            # first cut stored the caller and published it nowhere: the store had it,
            # `/admin/api/jobs` re-shapes every row by hand and left it out, so the
            # field existed and no reader could see it.
            listed = (req(client, "GET", "/admin/api/jobs?limit=5").json().get("jobs") or [])
            mine = next((row for row in listed if row.get("id") == job["id"]), None)
            check("the job the caller asked for is in the admin list", mine is not None)
            check("and it says who asked", (mine or {}).get("caller") == "end-to-end-suite",
                  repr((mine or {}).get("caller")))
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
            #
            # Driven through the converter directly, because the corpus that
            # exposed this bug is an ElevenLabs account that is not configured
            # here. The old version of this check read `.get("supported_languages")`
            # from whatever rows happened to exist — all of which published an
            # empty list, so it passed without looking at a language at all.
            from gateway.catalog import _language_codes
            check("a provider's language objects become plain codes",
                  _language_codes([{"language_id": "en", "name": "English"},
                                   {"language_id": "af", "name": "Afrikaans"}]) == ["en", "af"],
                  f"{_language_codes([{'language_id': 'en', 'name': 'English'}])}")
            check("a provider's bare language strings work too",
                  _language_codes(["en", "ja"]) == ["en", "ja"])
            check("and no code is ever a Python repr",
                  not any("{" in c or "'" in c
                          for c in _language_codes([{"language_id": "en"}, {"code": "de"}])))
            tts_rows_now = req(client, "GET", "/v1/tts/models").json().get("models", [])
            bad_langs = [
                entry
                for r in tts_rows_now
                for entry in (r.get("supported_languages") or [])
                if not isinstance(entry, str) or "{" in entry or "'" in entry
            ]
            check("and no live row carries a stringified language",
                  not bad_langs, f"{bad_langs[:2]}")

            # The per-request character limit is per model. It was one invented
            # number (40,000) for every TTS model, which over-promised the local
            # lane and Fish by eight times — a request that long simply fails.
            #
            # Checked through the row builder, because the isolated corpus holds
            # one TTS model and the old check's `or len(...) == 1` escape hatch
            # made it pass by definition. A provider that states its own limit has
            # to win, and the local lane has to get the agreed 25,000.
            from gateway.catalog import (
                _tts_characters, _TTS_CHARACTERS_DEFAULT, _TTS_CHARACTERS_LOCAL,
            )
            from gateway.providers.definitions import PROVIDERS_BY_ID as _DEFS
            import dataclasses as _dc
            _local = _DEFS["studio-hub-local"]
            # `mock-remote` is a user-defined provider, so it lives in the temp
            # `providers.local.json` the gateway subprocess reads — not in this
            # process's static map, so a relabelled copy stands in for it.
            #
            # **The kind has to be relabelled too.** It used to say that only `.id`
            # is read, and that was true until `_tts_characters` started asking
            # whether a provider is a Studio Hub *location* — at which point a copy
            # of the local definition still carrying `kind="studio_hub"` was a
            # location wearing another provider's id, and this suite caught it as a
            # cloud row publishing the local lane's number.
            _mock = _dc.replace(_local, id="mock-remote", kind="openai_compatible")
            check("a provider that states its own limit is published with it",
                  _tts_characters("m", {"raw": {"maximum_text_length_per_request": 5000}},
                                  _mock) == (5000, "provider"))
            # The limit and where it came from, from one function: they answer one
            # question, so they are decided once.
            # **The local lane presents one number, and the Hub is why.**
            # Chunking is Studio Hub's job — it takes the complete script and
            # renders sentence-safe sections — so the gateway states what may be
            # submitted, not what any one engine can take. Mirroring the Hub's
            # audited per-model contract (10,000–40,000 across the installed set)
            # made the picker's limit vary for a difference the caller cannot act
            # on and should never have to know about.
            check("every local model presents the same number, whatever is behind it",
                  _tts_characters("m", {"raw": {}}, _local) == (_TTS_CHARACTERS_LOCAL, "gateway")
                  and _TTS_CHARACTERS_LOCAL == 15000,
                  f"{_tts_characters('m', {'raw': {}}, _local)}")
            check("and it holds across the families, not just the bare case",
                  {_tts_characters("m", {"raw": {"family": f,
                                                 "long_form_delivery": {"section_max_characters": n}}},
                                   _local)[0]
                   for f, n in (("qwen3-tts", 288), ("kokoro-mlx", 3000),
                                ("chatterbox-mlx", 500), ("omnivoice", 288),
                                ("voxcpm-mlx", 400), ("voxtral-tts", None))}
                  == {15000},
                  "a local model presented something other than the lane's number")
            check("while a cloud lane that states nothing keeps the wider default",
                  _tts_characters("m", {"raw": {}}, _mock) == (_TTS_CHARACTERS_DEFAULT, "gateway")
                  and _TTS_CHARACTERS_DEFAULT == 25000)
            check("a provider's stated limit wins over the curated file",
                  _tts_characters("mock-tts-1", {"raw": {"maximum_text_length_per_request": 5000}},
                                  _mock) == (5000, "provider"))
            check("the owner's curated limit is honoured where nobody stated one",
                  _tts_characters("mock-tts-plain", {"raw": {}}, _mock) == (12345, "curated"),
                  f"{_tts_characters('mock-tts-plain', {'raw': {}}, _mock)}")

            # An engine the Hub declares as `hard-cap` silences past its cliff, and
            # the Hub does not serve that number over its API — so it is recorded
            # here and labelled as ours rather than published as the model's own.
            # Orpheus published 25,000 while its engine stops at 170, which is the
            # one item in Shape 3 that was *already wrong* rather than latent.
            check("a hard-cap engine is not over-promised (Orpheus: 170)",
                  _tts_characters("m", {"raw": {"family": "orpheus"}}, _local) == (170, "gateway"))
            check("and Bark's cliff is recorded too",
                  _tts_characters("m", {"raw": {"family": "bark"}}, _local) == (150, "gateway"))
            # The served half of the decision, and it is what sequences the Hub
            # work: `long_form_delivery` present means the Hub sections the script
            # itself, so the cliff stops applying and the model joins the rest of
            # the lane at 15,000 — **automatically**, the moment the Hub starts
            # sectioning it. Until then Orpheus keeps its engine's 170, so nobody
            # publishes 15,000 for an engine that would silence.
            check("a family the Hub long-form splits is not capped by a recorded cliff",
                  _tts_characters("m", {"raw": {"family": "orpheus",
                                                "long_form_delivery": {"split_method": "sentence_safe"}}},
                                  _local) == (15000, "gateway"))
            # `spark-tts-mlx` is 750 in the Hub's table and `auto-split`: that is
            # where quality is best, not a limit on what may be sent. Publishing it
            # as a maximum would refuse text that works.
            check("an auto-split family's soft maximum is not published as a limit",
                  _tts_characters("m", {"raw": {"family": "spark-tts-mlx"}}, _local)
                  == (15000, "gateway"))
            check("a provider that states its own limit still beats the recorded cliff",
                  _tts_characters("m", {"raw": {"family": "orpheus",
                                                "maximum_text_length_per_request": 90}},
                                  _local) == (90, "provider"))
            check("the mock model's stated limit reaches the catalogue",
                  any(r.get("maximum_input_characters") == 5000
                      for r in tts_rows_now if "mock-tts-1" in str(r.get("id"))),
                  f"{[(r.get('id'), r.get('maximum_input_characters')) for r in tts_rows_now]}")
            check("and a curated limit reaches the catalogue on its own row",
                  any(r.get("maximum_input_characters") == 12345
                      for r in tts_rows_now if "mock-tts-plain" in str(r.get("id"))),
                  f"{[(r.get('id'), r.get('maximum_input_characters')) for r in tts_rows_now]}")
            check("and the catalogue says where each limit came from",
                  (next((r for r in tts_rows_now if "mock-tts-1" in str(r.get("id"))), {})
                   .get("maximum_input_characters_source") == "provider")
                  and (next((r for r in tts_rows_now if "mock-tts-plain" in str(r.get("id"))), {})
                       .get("maximum_input_characters_source") == "curated"),
                  f"{[(r.get('id'), r.get('maximum_input_characters_source')) for r in tts_rows_now]}")
            check("no live tts row publishes the old flat 40,000",
                  all(r.get("maximum_input_characters") != 40000 for r in tts_rows_now),
                  f"{[(r.get('id'), r.get('maximum_input_characters')) for r in tts_rows_now]}")
            check("every tts row publishes a positive character limit",
                  all(isinstance(r.get("maximum_input_characters"), int)
                      and r["maximum_input_characters"] > 0 for r in tts_rows_now),
                  f"{[(r.get('id'), r.get('maximum_input_characters')) for r in tts_rows_now[:3]]}")
            # The old flat 40,000 also survived in the quote route, which meant
            # Story Studio validated a narration against a limit that contradicted
            # the model row it came from.
            quoted = req(client, "POST", "/v1/tts/quotes",
                         json={"model_id": tts_rows_now[0]["id"], "text": "hello"}).json()
            check("the quote publishes the same limit as the model row",
                  quoted["maximum_input_characters"] == tts_rows_now[0]["maximum_input_characters"],
                  f"quote {quoted.get('maximum_input_characters')} vs row "
                  f"{tts_rows_now[0].get('maximum_input_characters')}")
            check("and says where that limit came from, as the row does",
                  quoted.get("maximum_input_characters_source")
                  == tts_rows_now[0].get("maximum_input_characters_source"),
                  f"quote {quoted.get('maximum_input_characters_source')} vs row "
                  f"{tts_rows_now[0].get('maximum_input_characters_source')}")

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

            print("\n\033[1m10b. A bulk action must say which models it means\033[0m")
            # Incident, and it was mine: a type-fuzz sent `{}` here and the
            # handler treated it as "no filter", matched every model in the
            # catalogue — 1,624 — and disabled all of them. One malformed
            # request was enough to empty the picker. A bulk action is only ever
            # meant to act on a set the owner described, so this asserts the
            # refusal *and* that the refusal changed nothing, because a guard
            # that still writes would be worse than no guard.
            before = req(client, "GET", "/admin/api/models?limit=1000").json()
            listable_total = before["listable_total"]
            enabled_before = before["enabled_total"]
            refused = httpx.post(f"{BASE}/admin/api/models/bulk", headers=AUTH, json={})
            check("a bulk action that names nothing is refused",
                  refused.status_code == 400, f"got {refused.status_code}")
            check("and the refusal explains how to mean everything on purpose",
                  "scope" in refused.text, refused.text[:160])
            after = req(client, "GET", "/admin/api/models?limit=1000").json()
            check("and the refusal changed nothing at all",
                  after["enabled_total"] == enabled_before,
                  f"{enabled_before} -> {after['enabled_total']}")
            check("the whole catalogue is still there after the refusal",
                  after["listable_total"] == listable_total,
                  f"{listable_total} -> {after['listable_total']}")

            described = req(client, "GET",
                            "/admin/api/models?provider_id=mock-remote").json()["matched"]
            ok = httpx.post(f"{BASE}/admin/api/models/bulk", headers=AUTH,
                            json={"provider_id": "mock-remote", "enabled": True})
            check("a described bulk action is accepted",
                  ok.status_code == 200, ok.text[:160])
            check("and it acts on exactly the set it matched",
                  ok.json()["matched"] == described,
                  f"matched {ok.json().get('matched')}, filter matched {described}")

            # The confirmation names a number, so the number acted on has to be
            # that one. `only_enabled` used to be hardcoded off here while the
            # page sent it, so a button reading "Disable all 23 matching" would
            # have disabled all 445.
            httpx.post(f"{BASE}/admin/api/models/bulk", headers=AUTH,
                       json={"provider_id": "mock-remote", "enabled": True})
            httpx.post(f"{BASE}/admin/api/models/enabled", headers=AUTH,
                       json={"items": [{"provider_id": "mock-remote",
                                        "model_id": "mock-chat-2"}], "enabled": False})
            subset = httpx.post(f"{BASE}/admin/api/models/bulk", headers=AUTH,
                                json={"provider_id": "mock-remote", "enabled": False,
                                      "only_enabled": True})
            check("only_enabled acts on the enabled subset, not on every match",
                  subset.json()["matched"] < described,
                  f"matched {subset.json().get('matched')} of {described}")

            everything = httpx.post(f"{BASE}/admin/api/models/bulk", headers=AUTH,
                                    json={"scope": "all", "enabled": True})
            check("'all of them' is accepted when it is said in a word of its own",
                  everything.status_code == 200 and everything.json()["matched"] == listable_total,
                  everything.text[:160])

            # The console has to send that word, or the deliberate path is
            # unreachable from the app and the guard has broken the button it
            # was meant to protect.
            page = req(client, "GET", "/admin/").text
            check("the console says 'all' out loud when no filter is set",
                  "body.scope = 'all'" in page)

            print("\n\033[1m10c. The model browser's filters, sorting and facets\033[0m")
            # The Models page is a browser over the whole catalogue, so its filters
            # and ordering are computed server-side: a client-side sort could only
            # reorder the page it was handed, and a facet that counted its own
            # filtered set would read zero the moment it was used.
            full = req(client, "GET", "/admin/api/models?limit=2000").json()
            check("every operation facet totals the whole catalogue",
                  sum(full["facets"]["modality"].values()) == full["listable_total"],
                  f"{full['facets']['modality']} vs {full['listable_total']}")
            for modality in full["modalities"]:
                one = req(client, "GET",
                          f"/admin/api/models?modality={modality}&limit=1").json()
                check(f"the {modality} chip counts exactly the rows it would show",
                      one["facets"]["modality"].get(modality) == one["matched"],
                      f"{one['facets']['modality'].get(modality)} vs {one['matched']}")
                check(f"and every operation is still counted while {modality} is chosen",
                      len(one["facets"]["modality"]) == len(full["modalities"]),
                      f"{one['facets']['modality']}")

            for name, field in (("lane", "free"), ("provider_lane", "off"),
                                ("privacy", "non_private")):
                filtered = req(client, "GET",
                               f"/admin/api/models?{name}={field}&limit=1").json()
                check(f"the {name} facet counts the set its own filter would show",
                      filtered["facets"][name].get(field) == filtered["matched"],
                      f"{filtered['facets'][name].get(field)} vs {filtered['matched']}")

            # Sorting has to be applied to the full match before the page is cut,
            # so the visible slice is already in order.
            ordered = req(client, "GET",
                          "/admin/api/models?sort=modality&limit=2000").json()["models"]
            modalities_shown = [r["modality"] for r in ordered]
            check("sort by operation orders the page by operation",
                  modalities_shown == sorted(modalities_shown), f"{modalities_shown[:6]}")
            names_shown = [r["display_name"].lower() for r in
                           req(client, "GET",
                               "/admin/api/models?sort=name&limit=2000").json()["models"]]
            check("sort by name orders the page by name",
                  names_shown == sorted(names_shown), f"{names_shown[:6]}")
            flags_shown = [r["enabled"] for r in
                           req(client, "GET",
                               "/admin/api/models?sort=status&limit=2000").json()["models"]]
            check("sort by status puts the enabled rows first",
                  flags_shown == sorted(flags_shown, reverse=True), f"{flags_shown[:6]}")
            descending = req(client, "GET",
                             "/admin/api/models?sort=name&order=desc&limit=2000").json()
            check("and the direction is honoured and echoed back",
                  descending["order"] == "desc" and descending["sort"] == "name",
                  f"{descending['sort']}/{descending['order']}")
            check("an unknown sort is ignored, not echoed as applied",
                  req(client, "GET",
                      "/admin/api/models?sort=nonsense&limit=5").json()["sort"] is None)

            # `status` is the tri-state the console's State chips send, and it has
            # to keep the older `only_enabled` working rather than replace it.
            enabled_only = req(client, "GET",
                               "/admin/api/models?status=enabled&limit=2000").json()
            check("status=enabled matches the older only_enabled spelling",
                  enabled_only["matched"] == req(
                      client, "GET",
                      "/admin/api/models?only_enabled=true&limit=2000").json()["matched"])
            check("and it is exactly what enabled_total already counted",
                  enabled_only["matched"] == full["enabled_total"],
                  f"{enabled_only['matched']} vs {full['enabled_total']}")
            disabled_only = req(client, "GET",
                                "/admin/api/models?status=disabled&limit=2000").json()
            check("status=disabled is the other half, with nothing lost between them",
                  enabled_only["matched"] + disabled_only["matched"] == full["listable_total"],
                  f"{enabled_only['matched']} + {disabled_only['matched']}"
                  f" != {full['listable_total']}")

            # A new filter must reach the bulk route too, or the confirmation's
            # number and the set it acts on part company — the `only_enabled`
            # incident with a new face.
            free_matched = req(client, "GET",
                               "/admin/api/models?lane=free&limit=2000").json()["matched"]
            lane_bulk = httpx.post(f"{BASE}/admin/api/models/bulk", headers=AUTH,
                                   json={"lane": "free", "enabled": False})
            check("a bulk action on the free-lane filter acts on that filter's set",
                  lane_bulk.status_code == 200 and lane_bulk.json()["matched"] == free_matched,
                  lane_bulk.text[:160])
            httpx.post(f"{BASE}/admin/api/models/bulk", headers=AUTH,
                       json={"scope": "all", "enabled": True})

            # **An id that is not a provider cannot be written.** The published
            # namespace `studio-hub` is what Story Studio reads for every location,
            # and enable state written under it was never swept — it outlived the
            # locations it was about. `set_many` resolves through `_definition`
            # exactly like `set_model_enabled` always has, and the console row path
            # skips what does not resolve.
            bogus = req(client, "POST", "/admin/api/models/enabled",
                        json={"items": [{"provider_id": "studio-hub",
                                         "model_id": "some-model"}],
                              "enabled": True})
            check("enablement is not written under the published location namespace",
                  bogus.status_code == 200 and bogus.json()["changed"] == 0,
                  f"{bogus.status_code} {bogus.text[:120]}")

            # **The curated file is read in both namespaces.** The README documents
            # the key as the *visible* `studio-hub::<model>`, while a location's
            # rows are stored under `studio-hub-<controller>` — so an entry written
            # the documented way was silently ignored for every location.
            from gateway import limits as _limits
            _saved_all = _limits._all
            _limits._all = lambda: {
                "studio-hub::published-only": {"context_window_tokens": 111},
                "studio-hub::both-forms": {"context_window_tokens": 111},
                "studio-hub-local::both-forms": {"context_window_tokens": 222},
            }
            try:
                check("a curated entry keyed by the published location id is read",
                      _limits.for_model("studio-hub-local", "published-only")
                      .get("context_window_tokens") == 111,
                      f"{_limits.for_model('studio-hub-local', 'published-only')}")
                check("and the concrete id still wins when both are present",
                      _limits.for_model("studio-hub-local", "both-forms")
                      .get("context_window_tokens") == 222,
                      f"{_limits.for_model('studio-hub-local', 'both-forms')}")
            finally:
                _limits._all = _saved_all

            # A preview must name a model the provider currently offers. ElevenLabs
            # publishes **no** `static_models`, so `_preview_model` returned `""`
            # and every preview for a discovered voice was synthesised against an
            # empty model id — a control that always fails.
            from unittest import mock as _mock
            from gateway import voices as _voices_module
            from gateway.providers.definitions import PROVIDERS_BY_ID as _P_prev

            class _OnlyDiscovered:
                def models_for(self, provider_id, include_disabled=False):
                    return [{"model_id": "discovered-tts", "modality": "tts",
                             "enabled": True}]

            with _mock.patch.object(_voices_module.registry, "models", _OnlyDiscovered()):
                _preview = _voices_module.VoiceRegistry._preview_model(
                    _P_prev["elevenlabs-direct"])
            check("a voice preview names a discovered model, not an empty id",
                  _preview == "discovered-tts", f"{_preview!r}")

            # Video modes are read from what the provider states and **labelled**;
            # an image model that states nothing publishes `null`, not a definite
            # "cannot"; and the machine headline can account for every machine.
            from gateway.fleet import _summarise as _summarise
            _machine_rows = [{
                "id": "x", "label": "X", "local": False, "reachable": True,
                "machines": [{"machine": "a"}, {"machine": "b"}, {"machine": "c"}],
                "activity": {"pulse": {}, "machines": [
                    {"machine": "a", "state": "ready"},
                    {"machine": "b", "state": "offline"},
                    {"machine": "c"},
                ]},
                "probe_machines_hidden": 0, "running_jobs": [], "capacity": {},
            }]
            _summary = _summarise(_machine_rows)
            check("a machine with no state is neither online nor offline, and is published",
                  _summary["machines_online"] == 1
                  and _summary["machines_offline"] == 1
                  and _summary["machines_unknown"] == 1
                  and _summary["machines"] == 3,
                  f"{_summary['machines_online']}/{_summary['machines_offline']}"
                  f"/{_summary['machines_unknown']} of {_summary['machines']}")
            check("and the headline and the table agree on how many machines there are",
                  _summary["machines"] == len(_machine_rows[0]["machines"]),
                  f"{_summary['machines']}")

            print("\n\033[1m10c. Bad input is a 4xx, and a lane with nothing behind it is not offered\033[0m")
            # Every one of these was a 500, or a wrong answer, found by audit.
            huge = req(client, "GET", "/v1/account/jobs?page_size=99999999999999999999999")
            check("an absurd page_size is a 4xx, not a 500",
                  400 <= huge.status_code < 500, f"got {huge.status_code} {huge.text[:100]}")
            neg = req(client, "GET", "/v1/account/jobs?page_size=-1")
            check("a negative page_size is a 4xx, not 'unlimited'",
                  400 <= neg.status_code < 500, f"got {neg.status_code}")
            ok_page = req(client, "GET", "/v1/account/jobs?page_size=5")
            check("a sensible page_size still works", ok_page.status_code == 200)

            # A lane the adapter cannot serve must never reach the picker: the
            # model is enabled, and it is still not offered.
            #
            # `mock-google` needs a credential to be publishable at all — nothing
            # is offered from an unconfigured provider — so it has one for the same
            # reason `mock-remote` does.
            req(client, "POST", "/admin/api/providers/mock-google/credentials",
                json={"values": {"api_key": "mock-google-key"}})
            req(client, "POST", "/admin/api/providers/mock-google/models/enabled",
                json={"model_ids": ["gemini-2.5-pro", "veo-3"], "enabled": True})
            veo = req(client, "GET", "/admin/api/models?q=veo-3").json()["models"]
            # It is enabled in the store but **not listed**, because a row you can
            # tick that can never publish is a control that does nothing. The
            # owner asked for exactly that after OpenRouter's Lyria rows.
            check("an unservable model is not listed as something to tick",
                  veo == [], f"{veo}")
            chat_ids = {m["id"] for m in req(client, "GET", "/v1/chat/models").json()["models"]}
            video_ids = {m["id"] for m in req(client, "GET", "/v1/videos/models").json()["models"]}
            check("the lane its adapter does serve is published",
                  any("gemini-2.5-pro" in i for i in chat_ids), f"{sorted(chat_ids)[:8]}")
            check("a lane the adapter cannot serve is withheld, not offered then refused",
                  not any("veo-3" in i for i in video_ids), f"{sorted(video_ids)}")

            # **Video modes are read, then labelled.** The id heuristic alone
            # published every model as image-to-video, so fal's own
            # `category: "text-to-video"` endpoints were offered an image
            # control they cannot honour — and a model that states nothing got
            # a definite mode anyway.
            from gateway.catalog import _video_row as _video_row_for
            from gateway.providers.definitions import PROVIDERS_BY_ID as _P_video
            _stated = _video_row_for(
                "fal-ai/veo3.1", _P_video["fal-ai"],
                {"model_id": "fal-ai/veo3.1", "raw": {"category": "text-to-video"}})
            check("a provider's stated video mode is read, not inferred from the id",
                  _stated["modes"] == ["text-to-video"]
                  and _stated["modes_source"] == "provider",
                  f"{_stated['modes']} {_stated['modes_source']}")
            _unstated = _video_row_for(
                "fal-ai/mystery-video", _P_video["fal-ai"],
                {"model_id": "fal-ai/mystery-video", "raw": {}})
            check("and a video model that states no mode asserts none, with no variants",
                  _unstated["modes"] == [] and _unstated["modes_source"] is None
                  and _unstated["variants"] == [],
                  f"{_unstated['modes']} {_unstated['modes_source']}")
            _ours = _video_row_for(
                "grok-imagine-video", _P_video["xai-direct"],
                {"model_id": "grok-imagine-video", "raw": {}})
            check("a mode only our adapter can vouch for is labelled as ours",
                  _ours["modes"] == ["text-to-video", "image-to-video"]
                  and _ours["modes_source"] == "gateway",
                  f"{_ours['modes']} {_ours['modes_source']}")

            # An idempotency key identifies one request; a request has a modality.
            chat_id = next((i for i in sorted(chat_ids) if "mock-chat-1" in i), "")
            img_id = req(client, "GET", "/v1/image/models").json()["models"][0]["id"]
            shared_key = f"e2e-cross-modality-{int(time.time())}"
            took = req(client, "POST", "/v1/chat/responses",
                       json={"model_id": chat_id, "input": [{"role": "user", "content": "x"}]},
                       headers={"Idempotency-Key": shared_key})
            check("a chat request takes the key", took.status_code in (200, 202),
                  f"{took.status_code}")
            clash = req(client, "POST", "/v1/images/generations",
                        json={"model_id": img_id, "prompt": "x"},
                        headers={"Idempotency-Key": shared_key})
            check("reusing that key for an image request is refused, not answered with a chat job",
                  clash.status_code == 409,
                  f"got {clash.status_code}: {clash.text[:140]}")

            # The model that was quoted is the model that runs. Confirm used to
            # ignore the quote's model and run the first enabled transcription row.
            stt_now = req(client, "GET", "/v1/transcription/models").json()["models"]
            if stt_now:
                picked = stt_now[-1]["id"]
                q2 = req(client, "POST", "/v1/audio/transcriptions/quotes",
                         files={"file": ("clip.mp3", b"\xff\xfb\x90\x00" + b"\x00" * 512,
                                         "audio/mpeg")},
                         data={"model": picked})
                qid = q2.json().get("id", "")
                # No `model` field on the confirm at all: it has to come from the
                # quote, not from a fallback.
                c2 = req(client, "POST", f"/v1/audio/transcriptions/quotes/{qid}/confirm",
                         headers={"Idempotency-Key": f"e2e-stt-model-{int(time.time())}"})
                check("confirm returns 202 without being told the model",
                      c2.status_code == 202, c2.text[:160])
                if c2.status_code == 202:
                    check("and the job runs the model that was quoted",
                          c2.json()["model_id"] == picked,
                          f"quoted {picked}, ran {c2.json()['model_id']}")
                    poll(client, f"/v1/transcriptions/{c2.json()['id']}")

            # A price-only quote holds no audio, and saying so is different from
            # claiming the quote expired. Both used to be 410, so a caller that
            # had merely quoted was told to re-quote — and would fail again.
            price_only = req(client, "POST", "/v1/audio/transcriptions/quotes",
                             json={"model": stt_now[0]["id"] if stt_now else "x"})
            po_id = price_only.json().get("id", "")
            po_confirm = req(client, "POST",
                             f"/v1/audio/transcriptions/quotes/{po_id}/confirm",
                             headers={"Idempotency-Key": f"e2e-stt-po-{int(time.time())}"})
            check("confirming a price-only quote says the audio is missing, not that it expired",
                  po_confirm.status_code == 400, f"got {po_confirm.status_code}")
            check("and the reason names the missing audio",
                  "audio" in po_confirm.text.lower(), po_confirm.text[:140])

            # An upload past the ceiling is refused with the number, not queued
            # and then failed by the provider. `UploadFile` spools to disk, so the
            # assertion is about the *answer*, not about memory.
            from gateway.api import _MAX_UPLOAD_BYTES
            oversized = req(client, "POST", "/v1/audio/transcriptions/quotes",
                            files={"file": ("big.mp3", b"\x00" * (_MAX_UPLOAD_BYTES + 1024),
                                            "audio/mpeg")},
                            data={"model": stt_now[0]["id"] if stt_now else "x"})
            check("an oversized upload is refused, not queued", oversized.status_code == 413,
                  f"got {oversized.status_code} {oversized.text[:120]}")
            check("and it says what the limit is",
                  str(_MAX_UPLOAD_BYTES // (1024 * 1024)) in oversized.text,
                  oversized.text[:140])

            # The documented rollback transport. Nothing exercised it, so a broken
            # finish_reason or usage mapping here would have been found by Story
            # Studio rather than by us.
            sync = req(client, "POST", "/v1/chat/completions",
                       json={"model": chat_id, "messages": [{"role": "user", "content": "hi"}]})
            check("the synchronous completions transport answers", sync.status_code == 200,
                  f"{sync.status_code} {sync.text[:140]}")
            if sync.status_code == 200:
                body = sync.json()
                check("it is OpenAI-shaped", body.get("object") == "chat.completion"
                      and isinstance(body.get("choices"), list) and body["choices"])
                check("finish_reason is a clean stop the consumer accepts",
                      body["choices"][0].get("finish_reason") in {"stop", "length"},
                      f"{body['choices'][0].get('finish_reason')}")
                check("usage is passed through, not invented",
                      isinstance(body.get("usage"), dict), f"{body.get('usage')}")

            # A provider's own vocabulary has to be *translated*, and half a
            # translation is worse than none: Anthropic's map had two identity
            # entries, so a successful Claude completion published `end_turn`,
            # which the consumer refuses as truncated — every working Claude job
            # would have read as a cut-off answer. The values are the contract's
            # words, and `max_tokens` must keep mapping to the one reason that is
            # genuinely truncated.
            from gateway.adapters.openai_compat import ANTHROPIC_STOP_MAP as _STOP
            check("Anthropic's clean stops are translated to the contract's word",
                  _STOP["end_turn"] == "stop" and _STOP["stop_sequence"] == "stop",
                  f"{_STOP}")
            check("and a genuinely truncated generation is still reported as truncated",
                  _STOP["max_tokens"] == "length", f"{_STOP}")
            check("no finish reason is passed through in a provider's own vocabulary",
                  all(v == "stop" or v == "length" for v in _STOP.values()),
                  f"{_STOP}")

            # Groq was added for cheap cloud transcription. It is OpenAI-compatible,
            # so it needs no adapter of its own — the check is that it is wired as a
            # provider at all, with both lanes, and that the modality gate agrees.
            from gateway.adapters import serves as _serves
            from gateway.providers.definitions import PROVIDERS_BY_ID as _ALL
            groq = _ALL.get("groq-main")
            check("Groq is a known provider",
                  groq is not None, f"{[k for k in _ALL if 'groq' in k]}")
            if groq is not None:
                check("it offers chat and transcription",
                      set(groq.modalities) == {"chat", "transcription"}, f"{groq.modalities}")
                check("and its adapter serves both",
                      _serves(groq, "chat") and _serves(groq, "transcription"))

            print("\n\033[1m10d. Cancelling, and the account surface Story Studio reads\033[0m")
            # Cancellation was reachable from the app and had no test at all. The
            # invariant worth pinning is that a cancel must never rewrite a job
            # that has already finished — `store.cancel` only touches a
            # non-terminal state, and a regression there would silently turn a
            # succeeded generation into `cancelled` and lose the artifact.
            for route in ("/v1/chat/responses/no-such-job/cancel",
                          "/v1/images/no-such-job/cancel",
                          "/v1/jobs/no-such-job/cancel"):
                missing = req(client, "POST", route)
                check(f"cancelling an unknown job is a typed 404, not a 500 ({route.split('/')[2]})",
                      missing.status_code == 404 and "JOB_NOT_FOUND" in missing.text,
                      f"HTTP {missing.status_code} {missing.text[:100]}")

            finished = req(client, "POST", "/v1/images/generations",
                           json={"model_id": img_id, "prompt": "cancel me"},
                           headers={"Idempotency-Key": f"e2e-cancel-{int(time.time())}"})
            if finished.status_code in (200, 202):
                job_id = finished.json()["id"]
                done = poll(client, f"/v1/images/{job_id}")
                check("the job finished before we cancel it", done["state"] == "succeeded",
                      done.get("state", ""))
                again = req(client, "POST", f"/v1/images/{job_id}/cancel")
                check("cancelling a finished job leaves it finished",
                      again.status_code == 200 and again.json()["state"] == "succeeded",
                      f"{again.status_code} {again.json().get('state')}")
                check("and its artifact is still served",
                      req(client, "GET", f"/v1/images/{job_id}/file").status_code == 200)
                twice = req(client, "POST", f"/v1/images/{job_id}/cancel")
                check("cancelling twice is idempotent, not an error",
                      twice.status_code == 200 and twice.json()["state"] == "succeeded",
                      f"{twice.status_code} {twice.json().get('state')}")

            # Story Studio reads these on its account and store pages, so their
            # shape is a contract even though the gateway charges nothing.
            overview = req(client, "GET", "/v1/account/overview").json()
            check("the account overview publishes the types the loader expects",
                  isinstance(overview.get("paid_concurrency_limit"), int)
                  and isinstance(overview.get("credit_balance"), int)
                  and isinstance(overview.get("max_queued_jobs"), int),
                  f"{ {k: overview.get(k) for k in ('paid_concurrency_limit', 'credit_balance', 'max_queued_jobs')} }")
            activity = req(client, "GET", "/v1/account/credit-activity").json()
            check("credit activity publishes a paginated shape",
                  {"items", "page", "page_size", "total"} <= set(activity)
                  and activity["total"] == 0,
                  f"{sorted(activity)}")
            check("and its page_size is bounded like the jobs one",
                  400 <= req(client, "GET",
                             "/v1/account/credit-activity?page_size=99999999999999999999999")
                  .status_code < 500,
                  "an unbounded page_size is how the jobs route used to 500")
            # Archive possession is not commerce ownership. Until a real trusted
            # Store is connected, no route can claim a purchase or receipt exists.
            for method, route in (("GET", "account"), ("POST", "quotes"),
                                  ("POST", "purchases"), ("GET", "receipts")):
                response = req(client, method, f"/v1/content/{route}",
                               **({"json": {"product_id": "mock-product"}} if method == "POST" else {}))
                check(f"content {route} refuses fabricated ownership/settlement",
                      response.status_code == 503
                      and response.json().get("code") == "CONTENT_STORE_NOT_CONNECTED"
                      and response.json().get("state") != "completed"
                      and "receipt" not in response.json(), str(response.json()))

            # The admission permit is what bounds image concurrency on the Story
            # Studio side. Nothing here rations — every lane is pre-owned — but the
            # permit still has to be a shape the caller can hold and reason about.
            permit = req(client, "POST", "/v1/images/admission/permits",
                         json={"model_id": img_id, "operation": "text-to-image"}).json()
            check("an admission permit names the model and the operation",
                  permit.get("model_id") == img_id
                  and permit.get("operation") == "text-to-image"
                  and len(str(permit.get("id") or "")) >= 8,
                  f"{permit}")
            # **This check used to assert `expires_at is None` — "unlimited rather than
            # pretending to expire". C3 changed the opposite way, deliberately:** a permit that
            # never expires reserves nothing, and `live_capacity` is now the number it reserves
            # against (the owner's decision D). The permit names its capacity and its source so a
            # caller can see which ceiling it was granted under.
            check("and it reserves against a named capacity rather than reserving nothing",
                  isinstance(permit.get("expires_at"), (int, float))
                  and permit["expires_at"] > 0
                  and isinstance(permit.get("capacity"), dict)
                  and isinstance(permit["capacity"].get("total"), int)
                  and permit["capacity"].get("source"),
                  f"{permit}")
            check("a permit for a malformed body is a 4xx, not a 500",
                  400 <= req(client, "POST", "/v1/images/admission/permits",
                             content=b"not json",
                             headers={"Content-Type": "application/json"}).status_code < 500)

            # The admin routes the menu bar app calls. `/admin/api/jobs` bound its
            # `limit` straight into SQL `LIMIT ?`, so an absurd value answered
            # **500** on the live gateway and `limit=-1` meant "every job ever
            # recorded" — the same defect as `/v1/account/jobs`, in the route the
            # Jobs window uses.
            huge_limit = req(client, "GET", "/admin/api/jobs?limit=99999999999999999999999")
            check("an absurd job limit is a 4xx, not a 500",
                  400 <= huge_limit.status_code < 500, f"got {huge_limit.status_code}")
            check("and a negative one cannot mean 'every job ever recorded'",
                  400 <= req(client, "GET", "/admin/api/jobs?limit=-1").status_code < 500)
            jobs_page = req(client, "GET", "/admin/api/jobs?limit=5")
            check("the Jobs window's own request still works",
                  jobs_page.status_code == 200 and isinstance(jobs_page.json().get("jobs"), list),
                  f"{jobs_page.status_code}")
            check("and the model browser's limit is bounded too",
                  400 <= req(client, "GET",
                             "/admin/api/models?limit=99999999999999999999999").status_code < 500)

            # **The measured record, over the wire.** Its bound lives in `Annotated`
            # rather than in a `Query(...)` default, because a `Query` default is what
            # the object *is* when the function is called directly — it reached SQLite
            # as `type 'Query' is not supported` and the route was reachable only
            # through HTTP.
            measured = req(client, "GET", "/admin/api/qualification")
            check("the measured record answers", measured.status_code == 200,
                  f"{measured.status_code} {measured.text[:140]}")
            check("and its window is bounded",
                  400 <= req(client, "GET",
                             "/admin/api/qualification?limit=0").status_code < 500)
            measured_body = measured.json()
            measured_rows = measured_body.get("rows") or []
            # The lane that has really run here. `test_e2e.py` has **no local Hub**
            # (`STUDIOHUB_READY_FILE` points into the temp dir), so the measured rows
            # are the mock cloud provider's — naming the Hub's own model id here was
            # the first version of this check, and it failed against correct code.
            check("a model that has really run is in it",
                  any(r.get("model_id") == "mock-chat-1" for r in measured_rows),
                  f"{[r.get('model_id') for r in measured_rows]}")
            a_measured = next(
                (r for r in measured_rows if r.get("model_id") == "mock-chat-1"), {})
            check("with the completion the worker actually reported",
                  a_measured.get("max_completion_tokens") == 7,
                  f"{a_measured.get('max_completion_tokens')} "
                  "(the mock's OpenAI-compatible lane answers completion_tokens 7)")
            check("and it is labelled as ours, never as the provider's",
                  a_measured.get("source") == "gateway", f"{a_measured.get('source')}")
            check("while the stated ceiling travels beside it, not merged into it",
                  isinstance(a_measured.get("stated"), dict)
                  and a_measured["stated"].get("max_output_tokens") is not None,
                  f"{a_measured.get('stated')}")
            check("a model that has never run is absent rather than zeroed",
                  all(r.get("samples") for r in measured_rows))

            # **The same record, on the surface Story Studio actually reads.** The
            # admin route proves the measurement exists; this proves it is published
            # where a caller can act on it, and that it stays out of the ceiling it
            # sits beside.
            published = req(client, "GET", "/v1/chat/models").json().get("models") or []
            measured_row = next(
                (r for r in published if "mock-chat-1" in str(r.get("id"))), {})
            check("the catalogue row carries the measurement to Story Studio",
                  isinstance(measured_row.get("measured"), dict)
                  and measured_row["measured"].get("max_completion_tokens") == 7,
                  f"{measured_row.get('measured')}")
            check("and it says the number is ours, not the producer's",
                  (measured_row.get("measured") or {}).get("source") == "gateway")
            check("while max_output_tokens stays exactly what the producer stated",
                  measured_row.get("max_output_tokens") == 8192
                  and measured_row.get("max_output_tokens_source") == "provider",
                  f"{measured_row.get('max_output_tokens')} "
                  f"{measured_row.get('max_output_tokens_source')}")
            check("a model nobody has run publishes measured: null, not zeros",
                  all(r.get("measured") is None for r in published
                      if "mock-chat-1" not in str(r.get("id"))))
            # **Prose and structured work are not blended.** Measured on the local
            # model 2026-09-26: prose 1.21 tokens/word against 1.50 for a JSON
            # contract. The headline is still the whole envelope — a ceiling decision
            # reads the largest that ran, whatever it was — and this says which kind
            # of work produced it.
            shapes = (measured_row.get("measured") or {}).get("by_output_shape")
            check("the measurement says which kind of work produced it",
                  isinstance(shapes, dict) and sorted(shapes) == ["structured", "text"],
                  f"{shapes}")
            check("and both shapes are present whether or not one has ever run",
                  all(isinstance(v, dict) and "samples" in v for v in (shapes or {}).values()),
                  f"{shapes}")


            # The rest of the app-facing routes, each of which the Voices or Jobs
            # window calls on a click.
            a_voice = (req(client, "GET", "/v1/voices").json().get("voices") or [{}])[0].get("id", "")
            check("there is a voice to exercise the window's actions against",
                  bool(a_voice), f"{a_voice!r}")
            check("the voice library reports a missing key rather than failing",
                  req(client, "GET", "/admin/api/voices/library?provider_id=elevenlabs-direct")
                  .status_code in (200, 409),
                  "a 409 says 'no key configured', which the window shows")
            check("and an unknown provider there is a 404, not a 500",
                  req(client, "GET", "/admin/api/voices/library?provider_id=nobody")
                  .status_code == 404)
            refreshed = req(client, "POST", "/admin/api/voices/refresh")
            check("the Voices window's Refresh returns per-provider outcomes",
                  refreshed.status_code == 200 and isinstance(refreshed.json().get("outcomes"), dict),
                  refreshed.text[:140])
            check("refreshing an unknown provider is refused, not silently ignored",
                  req(client, "POST", "/admin/api/voices/refresh?provider_id=nobody")
                  .status_code == 404)
            saved = req(client, "POST", f"/v1/voices/{a_voice}/save")
            check("a voice can be marked saved", saved.status_code == 200
                  and saved.json().get("is_saved") is True, saved.text[:120])
            check("and unsaved again",
                  req(client, "DELETE", f"/v1/voices/{a_voice}/save").status_code == 200)
            check("the app's catalog view answers",
                  req(client, "GET", "/admin/api/catalog").status_code == 200)

            # A per-model capability, not a per-kind one. The gateway asks every
            # cloud transcription lane for `response_format: "srt"`; OpenAI's
            # `gpt-4o-transcribe`/`gpt-transcribe` families accept `json`/`text`
            # only and answer 400, and the `-diarize`/`-live`/`realtime` ones are
            # not timestamped-transcription endpoints at all. Tickable, they were
            # models that fail the moment anything uses them — the same trap as
            # OpenRouter's `:batch` variants.
            from gateway.providers.definitions import transcription_can_serve as _stt_ok
            check("a model that can return SRT is publishable",
                  _stt_ok("whisper-1") and _stt_ok("mlx-community/whisper-base-mlx"),
                  "whisper-1 and local Whisper are the lanes that work")
            check("and one that cannot is withheld rather than offered then refused",
                  not _stt_ok("gpt-4o-transcribe")
                  and not _stt_ok("gpt-4o-mini-transcribe")
                  and not _stt_ok("gpt-4o-transcribe-diarize"),
                  "these accept json/text only")
            # Groq's Whisper is the same trap and was found by *running* it, not by
            # reading: measured live 2026-09-20, `HTTP 400: response_format must be
            # one of [json text verbose_json]`. This list matches the bare id, so the
            # check that matters as much as the withhold is that the local Hub's
            # namespaced Whisper still serves.
            check("and Groq's Whisper, which refuses SRT outright, is withheld too",
                  not _stt_ok("whisper-large-v3")
                  and not _stt_ok("whisper-large-v3-turbo")
                  and _stt_ok("mlx-community/whisper-large-v3-turbo"),
                  "the local lane's namespaced id must survive the rule")
            # A version suffix is not a new capability. `gpt-4o-mini-transcribe-api-ev3`
            # was published and failed with *"response_format 'srt' is not compatible
            # with model …"* while four of its siblings were already listed — so the
            # rule matches the family, and the check names a spelling nobody listed.
            check("a new spelling of a family that cannot do SRT is withheld unlisted",
                  not _stt_ok("gpt-4o-mini-transcribe-api-ev3")
                  and not _stt_ok("gpt-5-transcribe-preview-2030")
                  and _stt_ok("whisper-1")
                  and _stt_ok("some-other-vendors-transcriber"),
                  "the family rule must not swallow ids that only look similar")
            # The rule has to reach the catalogue, not just the helper.
            published_stt = {m["id"] for m in
                             req(client, "GET", "/v1/transcription/models").json()["models"]}
            check("no published transcription row is one the gateway cannot read back",
                  all(_stt_ok(i.split("::", 1)[-1]) for i in published_stt),
                  f"{sorted(published_stt)}")
            # **A chat catalogue is not a list of chat models.** NVIDIA NIM answers
            # every one of its 82 rows with only `{id, owned_by}` — measured live on
            # 2026-09-20 — so its embeddings, safety classifiers, reward model, OCR
            # parsers and translators carried no type at all and were offered as
            # `modality: chat`, publishable. Ticking one reached Story Studio and
            # failed on use. Same shape as the SRT rule above; third time.
            from gateway.providers.definitions import chat_can_serve as _chat_ok
            check("a non-chat endpoint listed in a chat catalogue is withheld",
                  not _chat_ok("nvidia/embed-qa-4")
                  and not _chat_ok("nvidia/nemotron-3-embed-1b")
                  and not _chat_ok("nvidia/nvclip")
                  and not _chat_ok("nvidia/nemotron-3.5-content-safety")
                  and not _chat_ok("nvidia/llama-3.1-nemoguard-8b-topic-control")
                  and not _chat_ok("nvidia/nemotron-4-340b-reward")
                  and not _chat_ok("nvidia/nemotron-parse")
                  and not _chat_ok("nvidia/riva-translate-4b-instruct-v2")
                  and not _chat_ok("nvidia/ising-calibration-1.5-31b")
                  and not _chat_ok("google/deplot"),
                  "embeddings, guards, reward, parsers and translators are not chat")
            # A speech model in a chat catalogue is the same defect, and Groq's
            # `/models` really does list these: `canopylabs/orpheus-v1-english` and
            # `orpheus-arabic-saudi` are TTS. Withholding is scoped to `chat`, so the
            # local Orpheus **speech** lane must survive it.
            check("and a speech model listed in a chat catalogue is withheld as chat",
                  not _chat_ok("canopylabs/orpheus-v1-english")
                  and not _chat_ok("canopylabs/orpheus-arabic-saudi"),
                  "Groq lists Orpheus TTS models beside its chat models")
            # The rule also catches the same defect on other providers, which is why
            # it is not scoped to NVIDIA: these are classifiers and streaming
            # translators that were equally tickable.
            check("and the same rule catches it wherever else it appears",
                  not _chat_ok("openai/gpt-oss-safeguard-20b")
                  and not _chat_ok("meta-llama/llama-guard-4-12b")
                  and not _chat_ok("nvidia/nemotron-3.5-content-safety:free")
                  and not _chat_ok("gpt-realtime-translate")
                  and not _chat_ok("gemini-3.5-live-translate-preview"),
                  "guard/translator families across five more providers")
            # A vision model is a chat model that takes an image, so it must survive:
            # withholding these would remove working lanes, which is the opposite
            # mistake and the one that is easy to make with a word rule.
            check("but a vision-capable chat model is still a chat model",
                  _chat_ok("meta/llama-3.2-11b-vision-instruct")
                  and _chat_ok("microsoft/phi-3-vision-128k-instruct")
                  and _chat_ok("nvidia/vila")
                  and _chat_ok("nvidia/neva-22b")
                  and _chat_ok("nvidia/llama-3.1-nemotron-70b-instruct")
                  and _chat_ok("gemma-4-31b-it"),
                  "the rule must not withhold a working lane")
            # And it has to reach the console, or an operator can still tick one.
            chat_console = req(client, "GET", "/admin/api/models?limit=2000").json()["models"]
            guard_rows = [m["model_id"] for m in chat_console
                          if m["modality"] == "chat" and not _chat_ok(m["model_id"])]
            check("no non-chat family is listed as something to tick",
                  not guard_rows, f"{guard_rows[:6]}")
            # **A free lane is a trade, not just a price.** "Free" and "your prompts
            # may train someone's model" are the same offer on these providers, and
            # the owner has private voices — so the stance is published, and
            # `unverified` is published as itself rather than omitted, because
            # "nobody has checked" must not render as "safe".
            provs = {p["id"]: p for p in
                     req(client, "GET", "/admin/api/providers").json()["providers"]}
            check("a provider that states it may retain prompts is marked non-private",
                  provs.get("tokenharbor", {}).get("data_use") == "non_private"
                  and provs.get("google-ai-free", {}).get("data_use") == "non_private",
                  f"tokenharbor={provs.get('tokenharbor', {}).get('data_use')} "
                  f"google-ai-free={provs.get('google-ai-free', {}).get('data_use')}")
            check("and one nobody has checked says unverified, not safe",
                  provs.get("openai-direct", {}).get("data_use") == "unverified"
                  and provs.get("nvidia-nim-main", {}).get("data_use") == "unverified",
                  f"openai-direct={provs.get('openai-direct', {}).get('data_use')} "
                  f"nvidia={provs.get('nvidia-nim-main', {}).get('data_use')}")
            check("a provider that is the free tier is marked as one, a paid sibling is not",
                  provs.get("google-ai-free", {}).get("free_tier") is True
                  and provs.get("google-ai", {}).get("free_tier") is False
                  and provs.get("openrouter-main", {}).get("free_tier") is False,
                  f"free={provs.get('google-ai-free', {}).get('free_tier')} "
                  f"paid={provs.get('google-ai', {}).get('free_tier')}")
            # The per-row mark reads a declaration, not a price: a model that is
            # merely cheap is not free, and saying so would be a number the owner
            # acts on.
            from gateway.admin import _is_free_lane as _free
            _tier = next(d for d in _SHIPPED if d.id == "google-ai-free")
            _paid = next(d for d in _SHIPPED if d.id == "google-ai")
            check("a free row is read from what the provider states, not from a price",
                  _free(_tier, "gemini-3.8-flash") is True
                  and _free(_paid, "gemini-3.8-flash") is False
                  and _free(_paid, "deepseek/deepseek-v4-flash:free") is True
                  and _free(_paid, "s2.1-pro-free") is True
                  and _free(_paid, "deepseek-v4-flash") is False,
                  "free-tier provider, or a `:free`/`-free` id, and nothing else")
            # ...and it has to be drawn, or it is the "a field nothing renders is
            # the same defect as a control that does nothing" case.
            console_html = req(client, "GET", "/admin/").text
            check("and the console actually draws the mark on the rows it applies to",
                  "privacyBadge" in console_html
                  and "privacyBadge(r.data_use, r.free_lane)" in console_html
                  and "privacyBadge(p.data_use, p.free_tier)" in console_html
                  and "retention unverified" in console_html,
                  "the badge helper must be defined and called by both tables")
            # The app hosts this page in a WKWebView. A cached copy would serve the
            # *previous* console after an edit, so a styling fix would look like it
            # had not applied — which is how one gets reported as broken twice.
            check("and the console page is served no-store, so a reload shows the current UI",
                  "no-store" in (
                      req(client, "GET", "/admin/").headers.get("cache-control") or ""
                  ),
                  "a cached page makes a UI change invisible")

            print("\n\033[1m10e. Provider input shapes must match the provider's own schema\033[0m")
            # fal publishes a per-endpoint JSON schema, and these are read off it.
            # Both mistakes here were silent: fal's queue accepts *any* body and
            # validates at the worker, so a wrong field type reports the job as
            # COMPLETED and only fails when the result is fetched.
            from gateway.adapters.base import BackendRequest as _BR
            from gateway.adapters.fal import _input_for as _fal_input

            def fal_req(model_id, modality, **params):
                return _BR(provider_id="fal-ai", model_id=model_id, modality=modality,
                           params=params, values={"api_key": "x"},
                           base_url="https://queue.fal.run", display_name="fal")

            seedance = _fal_input(fal_req("bytedance/seedance-2.5/text-to-video", "video",
                                          prompt="x", duration_seconds=8))
            h3max = _fal_input(fal_req("minimax/h3-max/text-to-video", "video",
                                       prompt="x", duration_seconds=8))
            omni = _fal_input(fal_req("google/gemini-omni-flash/v1.1/text-to-video", "video",
                                      prompt="x", duration_seconds=8))
            check("a string-enum duration is sent as the string the schema declares",
                  seedance.get("duration") == "8" and isinstance(seedance["duration"], str),
                  f"{seedance.get('duration')!r}")
            check("and an integer duration is sent as an integer",
                  h3max.get("duration") == 8 and isinstance(h3max["duration"], int)
                  and omni.get("duration") == 8 and isinstance(omni["duration"], int),
                  f"h3max={h3max.get('duration')!r} omni={omni.get('duration')!r}")
            check("a required field with a default is still sent, because it is required",
                  h3max.get("prompt_expansion_mode") == "balanced", f"{h3max}")
            check("and a model that does not declare it is not sent it",
                  "prompt_expansion_mode" not in seedance, f"{seedance}")

            tts = _fal_input(fal_req("fal-ai/elevenlabs/tts/turbo-v2.5", "tts",
                                     text="hello", voice_id="fal-ai::Rachel", speed=1.0))
            check("fal TTS sends the text and the voice *name*, not our catalogue id",
                  tts.get("text") == "hello" and tts.get("voice") == "Rachel", f"{tts}")
            check("and it does not invent fields the endpoint does not declare",
                  "output_format" not in tts and "sync_mode" not in tts, f"{sorted(tts)}")
            # **A speed the gateway published must be a speed the adapter sends.**
            # The catalogue said 0.5–2.0 and this silently discarded anything
            # outside 0.7–1.2, so a caller picked a valid-looking speed and got
            # normal speed with no signal in either direction.
            check("a speed inside fal's range is sent",
                  _fal_input(fal_req("fal-ai/elevenlabs/tts/turbo-v2.5", "tts",
                                     text="hi", speed=1.2)).get("speed") == 1.2)
            try:
                _fal_input(fal_req("fal-ai/elevenlabs/tts/turbo-v2.5", "tts",
                                   text="hi", speed=2.0))
                _fal_refused = None
            except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
                _fal_refused = str(exc)
            check("and one outside it is refused, naming the range",
                  _fal_refused is not None and "0.7" in _fal_refused and "1.2" in _fal_refused,
                  f"{_fal_refused}")
            # Asked of the row builder, not of the live catalogue: whether fal is
            # enabled in *this* run is the environment's business, and a check that
            # skips itself when a provider is off is a check that can silently not
            # run — which is how the first version of this asserted nothing.
            from gateway.catalog import _tts_row as _tts_row_for
            from gateway.providers.definitions import PROVIDERS_BY_ID as _DEFS2
            _fal_row = _tts_row_for("fal-ai/elevenlabs/tts/turbo-v2.5", _DEFS2["fal-ai"], {
                "model_id": "fal-ai/elevenlabs/tts/turbo-v2.5", "display_name": "x",
                "modality": "tts", "raw": {},
            })
            check("so the published range is the accepted one",
                  (_fal_row["minimum_speed"], _fal_row["maximum_speed"]) == (0.7, 1.2),
                  f"{_fal_row['minimum_speed']}-{_fal_row['maximum_speed']}")
            check("and a lane with no such exception keeps the generic range",
                  (_tts_row_for("eleven_flash_v2_5", _DEFS2["elevenlabs-direct"], {
                      "model_id": "eleven_flash_v2_5", "display_name": "x",
                      "modality": "tts", "raw": {}})["minimum_speed"]) == 0.5)
            check("an unnamed voice falls back to fal's own default",
                  _fal_input(fal_req("fal-ai/elevenlabs/tts/turbo-v2.5", "tts",
                                     text="hi")).get("voice") == "Rachel")
            check("and an ElevenLabs voice id is passed through untouched, because fal takes it",
                  _fal_input(fal_req("fal-ai/elevenlabs/tts/turbo-v2.5", "tts", text="hi",
                                     voice_id="elevenlabs-direct::pNInz6obpgDQGcFmaJgB")
                             ).get("voice") == "pNInz6obpgDQGcFmaJgB")
            # fal proxies ElevenLabs, so its ElevenLabs lane may use voices from
            # the ElevenLabs provider — otherwise the picker offered only the
            # twenty names fal `example`s and a voice saved from the owner's own
            # account was invisible for that lane while working through it.
            from gateway.voices import accepted_voice_providers as _accepted
            check("fal's ElevenLabs lane also offers the ElevenLabs provider's voices",
                  "elevenlabs-direct" in _accepted("fal-ai", "fal-ai/elevenlabs/tts/turbo-v2.5"),
                  f"{sorted(_accepted('fal-ai', 'fal-ai/elevenlabs/tts/turbo-v2.5'))}")
            check("and a fal lane that is not ElevenLabs does not",
                  _accepted("fal-ai", "fal-ai/zonos2") == {"fal-ai"},
                  f"{sorted(_accepted('fal-ai', 'fal-ai/zonos2'))}")
            check("nor does any other provider borrow another's voices",
                  _accepted("studio-hub", "mlx/x") == {"studio-hub"})

            # The endpoint list has to be what the adapter can serve, one level
            # finer than the modality gate: fal's audio family includes a voice
            # *changer*, a realtime voice and speech-to-speech, none of which take
            # the `text` this lane sends.
            from gateway.providers.discovery import _FAL_TEXT_AUDIO_CATEGORIES as _FAL_CATS
            check("only text-input audio categories are discovered as TTS",
                  _FAL_CATS == {"text-to-speech", "text-to-audio"}, f"{_FAL_CATS}")
            from gateway.providers.definitions import PROVIDERS_BY_ID as _P
            from gateway.adapters import serves as _serves2
            check("fal declares and serves tts now",
                  "tts" in _P["fal-ai"].modalities and _serves2(_P["fal-ai"], "tts"),
                  f"{_P['fal-ai'].modalities}")

            print("\n\033[1m10f. xAI image and video, and Lyria music, are their own shapes\033[0m")
            # xAI's image API rejects `size` outright — measured live:
            # `HTTP 400: Argument not supported: size` — so the OpenAI-compatible
            # shim, which sends it for every provider, published three models that
            # failed on use. Its video API is deferred, which no OpenAI-shaped
            # handler is. Both have their own path now.
            from gateway.adapters.base import BackendRequest as _BR2
            from gateway.adapters.xai import _image_body as _xai_image, _video_body as _xai_video

            def xai_req(model_id, modality, **params):
                return _BR2(provider_id="xai-direct", model_id=model_id, modality=modality,
                            params=params, values={"api_key": "x"},
                            base_url="https://api.x.ai/v1", display_name="xAI")

            img = _xai_image(xai_req("grok-imagine-image", "image", prompt="a cup"))
            check("xAI image never sends `size`, which it rejects",
                  "size" not in img, f"{sorted(img)}")
            check("and asks for base64 so no second fetch is needed",
                  img.get("response_format") == "b64_json" and img.get("prompt") == "a cup",
                  f"{img}")

            vid = _xai_video(xai_req("grok-imagine-video-1.5", "video",
                                     prompt="a boat", duration_seconds=30))
            check("xAI video clamps a duration into the documented 1-15 range",
                  vid.get("duration") == 15, f"{vid.get('duration')!r}")
            check("and an image for image-to-video is an object, not `image_url`",
                  _xai_video(xai_req("grok-imagine-video-1.5", "video", prompt="x",
                                     reference_asset_url="https://e/x.png")).get("image")
                  == {"url": "https://e/x.png"})
            check("xAI declares and serves video now",
                  _serves2(_P["xai-direct"], "video") and "video" in _P["xai-direct"].modalities,
                  f"{_P['xai-direct'].modalities}")

            # Lyria is served through Gemini's own `:generateContent` and returns
            # **inline base64**, with the lyrics interleaved in the same parts array
            # — so the adapter scans every part rather than indexing the first.
            from gateway.adapters.google import _prompt_text as _lyria_text
            check("Lyria composes lyrics and title into the prompt, because it has no field for them",
                  "Lyrics:" in _lyria_text({"prompt": "p", "title": "T", "lyrics": "L"}),
                  _lyria_text({"prompt": "p", "title": "T", "lyrics": "L"}))
            check("and refuses rather than sending an empty prompt",
                  _lyria_text({}) == "", repr(_lyria_text({})))
            from gateway.providers.definitions import music_can_serve as _music_ok
            check("the WebSocket-only Lyria model is withheld, not offered",
                  not _music_ok("lyria-realtime-exp"), "it has no unary generate call")
            check("and the one-shot Lyria models are allowed",
                  _music_ok("lyria-3-clip-preview") and _music_ok("lyria-3.5"))
            check("Google serves music now, and the free tier does not declare it",
                  _serves2(_P["google-ai"], "music")
                  and "music" not in _P["google-ai-free"].modalities,
                  f"paid={_P['google-ai'].modalities} free={_P['google-ai-free'].modalities}")
            check("OpenRouter cannot serve music, so its Lyria rows stay withheld",
                  not _serves2(_P["openrouter-main"], "music"),
                  "its API publishes no music path at all")
            # **Kie's music is back, because it moved onto the route this adapter
            # already speaks.** Kie took its Suno family off the old dedicated
            # `/api/v1/generate*` API; it now rides the unified Market
            # `jobs/createTask` route under the api-model `ai-music-api/generate`,
            # with the Suno tier in `input.model`. Measured live 2026-09-20 (12
            # credits): the body queues, `recordInfo` reports success, and
            # `resultJson.data[]` carries the track urls. The earlier reading — that
            # music was a different API family this adapter could not speak — was
            # true of the *old* docs and is now out of date.
            kie = _P["kie-ai-images"]
            check("Kie declares music, and its adapter serves it",
                  "music" in kie.modalities and _serves2(kie, "music"),
                  f"{kie.modalities}")
            check("and the current Suno tiers are the curated rows",
                  {m["id"] for m in kie.static_models if m.get("modality") == "music"}
                  == {"V6", "V6_MINI", "V6_WILD"},
                  f"{[m['id'] for m in kie.static_models if m.get('modality') == 'music']}")
            from gateway.adapters.kie import _input_for as _kie_input
            from gateway.adapters.base import BackendRequest as _BackendRequest

            def _kie_music_req(**params):
                return _BackendRequest(
                    provider_id="kie-ai-images", model_id="V6", modality="music",
                    params=params, base_url="https://api.kie.ai", values={})

            check("the Kie adapter builds the Suno input for a music request",
                  _kie_input(_kie_music_req(lyrics="verse", style="Jazz",
                                            title="T", instrumental=False))
                  == {"model": "V6", "prompt": "verse", "instrumental": False,
                      "style": "Jazz", "title": "T", "custom_mode": True},
                  f"{_kie_input(_kie_music_req(lyrics='verse'))}")
            check("and a prompt-only request is Suno's non-custom mode",
                  _kie_input(_kie_music_req(prompt="calm piano")).get("custom_mode") is False,
                  f"{_kie_input(_kie_music_req(prompt='calm piano'))}")

            print("\n\033[1m10g. Saving a library voice by its id\033[0m")
            # The library *search* can only find what the provider will search
            # for, so a voice known from a share link had no way in. The API has
            # always taken an explicit id; the app had no field for it.
            bad = req(client, "POST", "/admin/api/voices/import",
                      json={"provider_id": "nobody", "provider_voice_id": "abc"})
            check("adding by id refuses an unknown provider", bad.status_code == 404,
                  f"{bad.status_code}")
            check("and refuses a missing id rather than saving nothing",
                  req(client, "POST", "/admin/api/voices/import",
                      json={"provider_id": "mock-remote"}).status_code == 422)
            # ElevenLabs is the one provider that needs more than the id, because
            # its import route is /v1/voices/add/{owner_id}/{voice_id}.
            owner = req(client, "POST", "/admin/api/voices/import",
                        json={"provider_id": "elevenlabs-direct", "provider_voice_id": "abc"})
            check("ElevenLabs asks for the owner id instead of 422-ing obscurely",
                  owner.status_code == 422 and "owner_id" in owner.text,
                  f"{owner.status_code} {owner.text[:120]}")
            # Its import route answers JSON, but a 200 whose body is not JSON (a
            # proxy, an HTML page served as 200) used to raise `JSONDecodeError`
            # out of the handler: a 500 with no sentence, *after* the voice had
            # already been added upstream — the worst moment to lose the answer.
            added_nonjson = req(client, "POST", "/admin/api/voices/import",
                                json={"provider_id": "mock-eleven",
                                      "provider_voice_id": "voice-added",
                                      "owner_id": "owner-1",
                                      "display_name": "Added Anyway"})
            check("a non-JSON 200 from the voice provider does not 500 the import",
                  added_nonjson.status_code == 200
                  and (added_nonjson.json().get("voice") or {})
                  .get("provider_voice_id") == "voice-added",
                  f"{added_nonjson.status_code} {added_nonjson.text[:160]}")
            added_by_id = req(client, "POST", "/admin/api/voices/import",
                              json={"provider_id": "mock-remote", "provider_voice_id": "voice-by-id",
                                    "display_name": "Saved By Id"})
            check("a voice can be saved from an explicit id alone",
                  added_by_id.status_code == 200
                  and (added_by_id.json().get("voice") or {}).get("enabled") is True,
                  added_by_id.text[:160])
            listed = req(client, "GET", "/admin/api/voices?provider_id=mock-remote").json()
            check("and it appears in the catalogue straight away",
                  any(v["provider_voice_id"] == "voice-by-id" for v in listed["voices"]),
                  f"{[v['provider_voice_id'] for v in listed['voices']]}")
            # Removed again: an added voice changes the counts section 15 checks,
            # and this test is about the route, not about owning a voice.
            saved_id = (added_by_id.json().get("voice") or {}).get("id", "")
            check("and can be removed again",
                  req(client, "DELETE", f"/admin/api/voices/{saved_id}").status_code == 200,
                  saved_id)

            # The console must not advertise a model that can never run: a row you
            # can tick that will never publish is a control that does nothing.
            console = req(client, "GET", "/admin/api/models?limit=2000").json()["models"]
            openrouter_music = [m for m in console
                                if m["provider_id"] == "openrouter-main" and m["modality"] == "music"]
            check("a model the provider cannot serve is not listed as something to tick",
                  not openrouter_music, f"{[m['model_id'] for m in openrouter_music]}")
            bad_stt = [m["model_id"] for m in console
                       if m["modality"] == "transcription"
                       and not _stt_ok(m["model_id"])]
            check("and no SRT-incapable transcription model is listed either",
                  not bad_stt, f"{bad_stt}")

            # A hand-written `providers.local.json` is the one provider list the
            # owner edits himself, so it is the one where a silent skip is
            # unrecoverable: the provider simply never appears, and there is
            # nothing to tell "my file did not load" from "nothing can run that
            # kind". Two things were wrong. The kind allow-list was a *second*
            # copy of `IMPLEMENTED_KINDS` and had already fallen behind — `xai`
            # was missing — so `"kind": "xai"` was dropped without a word; and
            # every other refusal was silent too.
            from gateway.providers import definitions as _defs

            check("the loadable kinds are the implemented kinds, not a stale copy",
                  _defs._VALID_KINDS is _defs.IMPLEMENTED_KINDS
                  and _defs.KIND_XAI in _defs._VALID_KINDS,
                  f"xai in allow-list: {_defs.KIND_XAI in _defs._VALID_KINDS}")

            _real_path = _defs.CUSTOM_PROVIDERS_PATH
            _saved_skips = list(_defs._SKIPPED_CUSTOM)
            _probe = tmp / "providers-probe.json"
            _probe.write_text(json.dumps({"providers": [
                {"id": "xai-probe", "kind": _defs.KIND_XAI, "base_url": "http://127.0.0.1:9",
                 "modalities": ["chat"], "discovery": "static", "static_models": []},
                {"id": "mystery-probe", "kind": "not-a-real-kind",
                 "base_url": "http://127.0.0.1:9"},
                {"id": "openai-direct", "kind": _defs.KIND_OPENAI_COMPATIBLE,
                 "base_url": "http://127.0.0.1:9"},
                # The bare published namespace, and a cloud provider wearing the
                # location prefix: both are refused *with a reason*. A
                # `studio_hub`-kind entry under the same prefix is a location by
                # every measure the code uses (the Fleet page writes exactly that
                # shape), so it must still load — a fix that rejected the prefix
                # outright would have broken the test location this suite uses.
                {"id": "studio-hub", "kind": _defs.KIND_STUDIO_HUB,
                 "base_url": "http://127.0.0.1:9", "modalities": ["tts"],
                 "discovery": "static", "static_models": []},
                {"id": "studio-hub-squat", "kind": _defs.KIND_OPENAI_COMPATIBLE,
                 "base_url": "http://127.0.0.1:9"},
                {"id": "studio-hub-location", "kind": _defs.KIND_STUDIO_HUB,
                 "base_url": "http://127.0.0.1:9", "modalities": ["tts"],
                 "discovery": "static", "static_models": []},
                "not an object",
                {"kind": _defs.KIND_OPENAI_COMPATIBLE, "base_url": "http://127.0.0.1:9"},
            ]}))
            _defs.CUSTOM_PROVIDERS_PATH = _probe
            try:
                _loaded = {d.id for d in _defs._load_custom_providers()}
                _records = list(_defs.skipped_custom_providers())
                _skips = {s["id"]: s["reason"] for s in _records}
            finally:
                _defs.CUSTOM_PROVIDERS_PATH = _real_path
                _defs._SKIPPED_CUSTOM[:] = _saved_skips
            check("a provider of a kind that has an adapter does load",
                  _loaded == {"xai-probe", "studio-hub-location"}, f"{sorted(_loaded)}")
            check("a kind nothing can run is refused, and the reason names the kind",
                  "not-a-real-kind" in _skips.get("mystery-probe", ""), f"{_skips}")
            check("a built-in id cannot be redefined from the file, and says so",
                  "built-in" in _skips.get("openai-direct", ""), f"{_skips}")
            check("and an entry with no id is reported, not dropped in silence",
                  "no 'id'" in _skips.get("?", ""), f"{_skips}")
            check("the published location namespace cannot be claimed as a provider",
                  "reserved" in _skips.get("studio-hub", ""), f"{_skips}")
            check("and a non-location kind cannot wear the location prefix",
                  "prefix" in _skips.get("studio-hub-squat", ""), f"{_skips}")
            # Six refusals, listed — not five keys: two entries share the
            # placeholder id `?`, and a dict would silently hide one of them.
            check("every refusal is listed, and each says why",
                  len(_records) == 6 and all(r["reason"] for r in _records),
                  f"{_records}")

            print("\n\033[1m10h. What can be offered is what can be routed\033[0m")
            # "Enabled" is not "offerable", and the second consequence is the one a
            # *caller* feels. `routing_candidates` tested `is_enabled` plus a row
            # match while `enabled_models` -- the set the catalogue is built from --
            # also applies `is_configured` and `can_publish`. So `route` chose a
            # location on the weaker test and `catalog.resolve` then rejected that
            # pair on the stronger one: the request answered **MODEL_NOT_FOUND**
            # with a perfectly good copy of the same model enabled elsewhere, never
            # tried. Two reads of one rule, drifted apart.
            #
            # A controlled registry, because this process does not hold the temp
            # providers — the gateway subprocess loads them — and this must not
            # write to the owner's real model cache. Two locations look identical to
            # `models_for` and only one is in `enabled_models`, which is exactly the
            # asymmetry that produced the 404: a `models_for`-based reader returns
            # **both**, so this check fails for the old implementation and passes
            # for the new one.
            from unittest import mock as _mock
            from gateway import fleet as _fleet
            from gateway.providers import registry as _reg

            _good = _mock.Mock(id="studio-hub-good", kind="studio_hub")
            _bad = _mock.Mock(id="studio-hub-bad", kind="studio_hub")
            _one_model = {"model_id": "hub/model", "modality": "tts", "enabled": True, "raw": {}}

            def _both_reachable(allow_stale: bool = False) -> dict:
                return {"locations": [
                    {"id": loc, "label": loc.title(), "reachable": True,
                     "capacity": {"eligible_physical_machine_slots_total": 1,
                                  "queue_depth": 0}, "running_jobs": []}
                    for loc in ("good", "bad")
                ]}

            with _mock.patch.object(_fleet, "snapshot", _both_reachable), \
                 _mock.patch.object(_reg, "definitions", lambda: [_good, _bad]), \
                 _mock.patch.object(_reg.credentials, "is_enabled", lambda pid: True), \
                 _mock.patch.object(_reg.models, "models_for",
                                    lambda pid, include_disabled=True: [dict(_one_model)]), \
                 _mock.patch.object(_reg.models, "enabled_models",
                                    lambda modality: [(_good, dict(_one_model))]):
                _cands = _fleet.routing_candidates("hub/model", "tts")
            check("only a location the catalogue would offer can be routed to",
                  [c["location_id"] for c in _cands] == ["good"],
                  f"{[c['location_id'] for c in _cands]}")
            check("so a route is never chosen that resolution would then reject",
                  all(c["provider_id"] in {"studio-hub-good"} for c in _cands),
                  f"{[c['provider_id'] for c in _cands]}")

            # **A merged location row answers for every site that offers it.** Only
            # `locked` used to merge, so a model two Hubs described differently was
            # published as whichever site was iterated first: a cloning claim beside
            # an empty picker, or a character budget only one site honours. Each
            # field needs a direction, not a first-writer-wins.
            from gateway.catalog import _merge_capabilities as _merge_caps
            _first = {"supports_voice_cloning": False, "maximum_input_characters": 15000,
                      "maximum_input_characters_source": "provider", "supports_speed": True,
                      "minimum_speed": 0.5, "maximum_speed": 2.0}
            _second = {"supports_voice_cloning": True, "maximum_input_characters": 170,
                       "maximum_input_characters_source": "gateway", "supports_speed": True,
                       "minimum_speed": 0.7, "maximum_speed": 1.2}
            _merge_caps(_first, _second, "tts")
            check("a merged row unions cloning, and intersects what the caller picks",
                  _first["supports_voice_cloning"] is True
                  and _first["maximum_input_characters"] == 170
                  and _first["maximum_input_characters_source"] == "gateway"
                  and _first["minimum_speed"] == 0.7 and _first["maximum_speed"] == 1.2,
                  f"{_first}")
            _no_speed = {"supports_voice_cloning": True, "supports_speed": False,
                         "maximum_input_characters": 15000,
                         "maximum_input_characters_source": "provider"}
            _merge_caps(_no_speed, _second, "tts")
            check("and a site without speed removes the control from the merged row",
                  _no_speed["supports_speed"] is False, f"{_no_speed}")
            # And the record the voice picker reads is one a location can actually
            # publish, not the first copy in iteration order.
            from gateway import voices as _voices_mod

            _location_records = [
                {"model_id": "hub/m", "raw": {"catalogued": False}},
                {"model_id": "hub/m", "raw": {"catalogued": True, "reference_audio":
                                              {"declared": True, "supported": True}}},
            ]
            with _mock.patch.object(_voices_mod.registry.models, "models_for",
                                    lambda pid, include_disabled=True:
                                    [dict(r) for r in _location_records]):
                _picked = _voices_mod._model_record("studio-hub-local", "hub/m")
            check("the location record chosen for a merged model is one that can publish it",
                  ((_picked or {}).get("raw") or {}).get("catalogued") is True,
                  f"{_picked}")

            print("\n\033[1m10i. A voice the caller did not choose is not invented\033[0m")
            # **A missing voice used to become a specific named speaker** —
            # `English_expressive_narrator` on MiniMax, `alloy` on the
            # OpenAI-compatible shim — so the narration came back in a voice nobody
            # asked for and nothing said so. Silent in both directions: the caller
            # sees audio it did not choose, and has nothing to act on.
            from gateway.adapters.base import BackendRequest as _TTSReq
            from gateway.adapters.minimax import _speech as _mm_speech
            from gateway.adapters.openai_compat import _tts as _oa_tts

            def _tts_req(**params):
                return _TTSReq(provider_id="p", model_id="m", modality="tts",
                               params=params, values={}, base_url="http://127.0.0.1:9")

            for _label, _fn in (("minimax", _mm_speech), ("openai-compatible", _oa_tts)):
                try:
                    _fn(_tts_req(text="hello"))
                    _missing = None
                except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
                    _missing = str(exc)
                check(f"a {_label} TTS request with no voice is refused, not narrated anyway",
                      _missing is not None and "needs a voice" in _missing,
                      f"{_missing}")
            # And the guard does not over-refuse: a named voice gets *past* it. The
            # request then fails on the network, which is expected here — nothing
            # listens on port 9 — so what is asserted is which error came back.
            try:
                _mm_speech(_tts_req(text="hello", voice_id="mm-voice-1"))
                _named = "no error"
            except Exception as exc:  # noqa: BLE001
                _named = str(exc)
            check("and a named voice is not refused by the guard",
                  "needs a voice" not in _named, f"{_named}")

            # **A declared modality is not a served one.** "Add model manually"
            # accepted anything the provider *declares*, while `can_publish` also
            # requires the adapter to serve it — so a row could be added, enabled,
            # counted in the header, and never publish. A control that does nothing,
            # enforced in the console only is not enforced.
            _unserved = req(client, "POST", "/admin/api/providers/mock-google/models",
                            json={"model_id": "veo-probe", "display_name": "Veo Probe",
                                  "modality": "video"})
            check("a modality nothing can serve is refused, and says so",
                  _unserved.status_code == 422 and "could never run" in _unserved.text,
                  f"HTTP {_unserved.status_code} {_unserved.text[:140]}")
            check("and the row is not added",
                  not any(m["model_id"] == "veo-probe"
                          for m in req(client, "GET",
                                       "/admin/api/providers/mock-google/models").json()["models"]))
            _served = req(client, "POST", "/admin/api/providers/mock-google/models",
                          json={"model_id": "chat-probe", "display_name": "Chat Probe",
                                "modality": "chat"})
            check("while a modality it does serve still works",
                  _served.status_code == 200, _served.text[:140])
            req(client, "DELETE", "/admin/api/providers/mock-google/models/chat-probe")

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

            print("\n\033[1m12. BFL, MiniMax and Kie adapters\033[0m")
            # These providers have no keys on this machine, so their real API
            # shapes are exercised against the mock: BFL's async submit/poll/
            # download with its own state names, MiniMax's four dissimilar
            # endpoints including the hex-encoded speech response, and Kie's
            # unified task route in both its image and its music shape.
            wanted = {
                "mock-bfl": ["flux-pro-1.1"],
                "mock-minimax": ["image-01", "video-01", "music-01", "speech-2.8-turbo"],
                "mock-kie": ["V6"],
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
            check("BFL + MiniMax + Kie models enabled", enabled.status_code == 200,
                  enabled.text[:120])

            def run_generation(label, submit_path, poll_path, body, artifact_path, signature=None):
                """Run one lane to a fetched artifact, and return the settled envelope.

                The envelope comes back so a caller can assert on what the job
                *published* (an `outputs` list, say) and not only on the one artifact
                this helper happens to fetch. Returns `None` if the lane did not
                succeed.
                """
                key = f"e2e-{label}-{int(time.time())}"
                submitted = req(
                    client, "POST", submit_path, json=body, headers={"Idempotency-Key": key}
                )
                if submitted.status_code != 202:
                    check(f"{label}: submit accepted", False,
                          f"{submitted.status_code} {submitted.text[:160]}")
                    return None
                check(f"{label}: submit accepted", True)
                job_id = submitted.json()["id"]
                done = poll(client, poll_path.format(id=job_id))
                check(f"{label}: succeeded", done["state"] == "succeeded",
                      (done.get("failure_reason") or "")[:200])
                if done["state"] != "succeeded":
                    return None
                artifact = req(client, "GET", artifact_path.format(id=job_id))
                ok = artifact.status_code == 200 and (
                    signature(artifact.content) if signature else True
                )
                check(f"{label}: artifact valid ({len(artifact.content)} bytes)", ok)
                return done

            is_png = lambda b: b[1:4] == b"PNG"
            is_mp4 = lambda b: b[4:8] == b"ftyp"
            is_wav = lambda b: b[:4] == b"RIFF" and b[8:12] == b"WAVE"

            run_generation(
                "bfl-image",
                "/v1/images/generations", "/v1/images/{id}",
                {"model_id": "mock-bfl::flux-pro-1.1", "prompt": "a lighthouse"},
                "/v1/images/{id}/file", is_png,
            )
            # **`n` is a real count now, and every image comes back.** It was
            # hardcoded to 1 and the adapter took `entries[0]`, so a provider that
            # answered several images had the rest written nowhere. The model is
            # enabled for this one check and switched back off afterwards, because the
            # catalogue counts later sections assert are about the shipped set.
            httpx.post(f"{BASE}/admin/api/models/enabled", headers=AUTH,
                       json={"items": [{"provider_id": "mock-remote",
                                        "model_id": "mock-image-1"}], "enabled": True})
            try:
                two_images = run_generation(
                    "shim-images-n2",
                    "/v1/images/generations", "/v1/images/{id}",
                    {"model_id": "mock-remote::mock-image-1",
                     "prompt": "two lighthouses", "n": 2},
                    "/v1/images/{id}/file", is_png,
                )
                image_outputs = (two_images or {}).get("outputs") or []
                check("asking for two images gets two, and both are published",
                      [o.get("asset_id") for o in image_outputs] == ["0", "1"],
                      f"{image_outputs}")
                shot_a = req(client, "GET", image_outputs[0]["url"]) if image_outputs else None
                shot_b = req(client, "GET", image_outputs[1]["url"]) if len(image_outputs) > 1 else None
                check("and they are two different pictures, not the first one twice",
                      shot_a is not None and shot_b is not None
                      and shot_a.status_code == 200 and shot_b.status_code == 200
                      and is_png(shot_a.content) and is_png(shot_b.content)
                      and shot_a.content != shot_b.content,
                      f"{getattr(shot_a, 'status_code', '-')} "
                      f"{getattr(shot_b, 'status_code', '-')}")
                # A count no provider would take is refused with the range, rather
                # than clamped: silently returning fewer than were asked for is the
                # same defect as dropping the extras. The refusal happens in the
                # adapter, so the job is accepted and then settles `failed`.
                bad_submit = req(client, "POST", "/v1/images/generations",
                                 json={"model_id": "mock-remote::mock-image-1",
                                       "prompt": "x", "n": 99},
                                 headers={"Idempotency-Key": f"e2e-n99-{int(time.time())}"})
                bad_job = (poll(client, f"/v1/images/{bad_submit.json()['id']}")
                           if bad_submit.status_code == 202 else {})
                check("a count no provider would accept is refused, naming the range",
                      bad_submit.status_code == 202
                      and bad_job.get("state") == "failed"
                      and "between 1 and 10" in (bad_job.get("failure_reason") or ""),
                      f"{bad_submit.status_code} {bad_job.get('failure_reason')}")
            finally:
                httpx.post(f"{BASE}/admin/api/models/enabled", headers=AUTH,
                           json={"items": [{"provider_id": "mock-remote",
                                            "model_id": "mock-image-1"}], "enabled": False})
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
            # **Kie's music lane is the same route as its images, under a different
            # api-model name.** The body is the only thing that says which lane a
            # request is on: the top-level `model` is `ai-music-api/generate` and
            # the Suno tier rides in `input.model`. Asserted on the **wire** because
            # that is exactly what an earlier version got wrong — it sent the tier
            # as the top-level model, which the provider answers with *"The model
            # name you specified is not supported"* (measured live 2026-09-20).
            kie_music_envelope = run_generation(
                "kie-music",
                "/v1/music/generations", "/v1/music/jobs/{id}",
                {"model_id": "mock-kie::V6", "lyrics": "a calm piano piece",
                 "style": "Classical", "title": "Peaceful", "instrumental": False},
                "/v1/music/jobs/{id}/outputs/0", None,
            )
            # **Suno answers several variations per call and a job keeps all of
            # them.** Every url used to be parsed and then only `urls[0]` was
            # downloaded, so the second track was written nowhere, and the route that
            # exists to address a variation took an `asset_id` and ignored it — every
            # index answered track 0. That is precisely the result a caller could not
            # otherwise reach, so both halves are asserted here.
            music_outputs = (kie_music_envelope or {}).get("outputs") or []
            check("a music job publishes one output per track the provider returned",
                  [o.get("asset_id") for o in music_outputs] == ["0", "1"],
                  f"{music_outputs}")
            first_track = (req(client, "GET", music_outputs[0]["url"])
                           if len(music_outputs) > 0 else None)
            second_track = (req(client, "GET", music_outputs[1]["url"])
                            if len(music_outputs) > 1 else None)
            check("and both tracks are really there, with different audio in each",
                  first_track is not None and second_track is not None
                  and first_track.status_code == 200 and second_track.status_code == 200
                  and bool(first_track.content) and bool(second_track.content)
                  and first_track.content != second_track.content,
                  f"{getattr(first_track, 'status_code', '-')} "
                  f"{getattr(second_track, 'status_code', '-')}")
            if kie_music_envelope:
                missing_track = req(
                    client, "GET",
                    f"/v1/music/jobs/{kie_music_envelope['id']}/outputs/7")
                check("naming a variation the job does not have is refused, not answered with track 0",
                      missing_track.status_code == 404
                      and "ARTIFACT_NOT_FOUND" in missing_track.text,
                      f"{missing_track.status_code} {missing_track.text[:120]}")
                generic = req(
                    client, "GET",
                    f"/v1/jobs/{kie_music_envelope['id']}/outputs/1")
                check("and the modality-agnostic outputs route reaches the second track too",
                      generic.status_code == 200
                      and second_track is not None
                      and generic.content == second_track.content,
                      f"{generic.status_code}")
            else:
                check("naming a variation the job does not have is refused, not answered with track 0",
                      False, "the music lane did not succeed")
                check("and the modality-agnostic outputs route reaches the second track too",
                      False, "the music lane did not succeed")
            # **A kept artifact outranks the retention policy.** An asset created
            # here *directly* has no Story Studio project holding the copy that
            # matters, so the gateway's file is the only copy there is — and the
            # ordinary policy would delete it after a week. Proving that needs the
            # file to be old enough that the sweep must take it, and an unpinned file
            # of the same age to go, so the check cannot pass on a broken sweep.
            if kie_music_envelope:
                artifact_dir = Path(
                    req(client, "GET", "/admin/api/settings/retention/usage")
                    .json()["usage"]["path"]
                )
                unpinned = artifact_dir / "e2e-unpinned-old.bin"
                unpinned.write_bytes(b"z" * 64)
                os.utime(unpinned, (0, 0))
                pin_job = kie_music_envelope["id"]
                kept_reply = req(client, "POST", "/admin/api/artifacts/keep",
                                 json={"job_id": pin_job, "asset_id": "1", "keep": True})
                pinned_path = next(
                    (a["path"] for a in kept_reply.json().get("artifacts") or []
                     if a["job_id"] == pin_job and a["asset_id"] == "1"), "")
                check("an artifact can be kept, addressed by the id the envelope published",
                      kept_reply.status_code == 200 and bool(pinned_path),
                      kept_reply.text[:160])
                if pinned_path:
                    os.utime(pinned_path, (0, 0))
                pruned = req(client, "POST", "/admin/api/artifacts/prune").json()
                check("a kept artifact survives a prune old enough to have taken it",
                      bool(pinned_path) and Path(pinned_path).exists()
                      and pruned.get("pinned_files") == 1,
                      f"pinned={bool(pinned_path)} exists={Path(pinned_path).exists() if pinned_path else None} "
                      f"pinned_files={pruned.get('pinned_files')}")
                check("while an unkept artifact of the same age was taken, so the sweep did run",
                      not unpinned.exists() and pruned.get("deleted", 0) >= 1,
                      f"deleted={pruned.get('deleted')} unpinned_exists={unpinned.exists()}")
                released = req(client, "POST", "/admin/api/artifacts/keep",
                               json={"job_id": pin_job, "asset_id": "1", "keep": False})
                after_release = req(client, "POST", "/admin/api/artifacts/prune").json()
                check("and releasing the pin lets the policy take the file again",
                      released.status_code == 200
                      and released.json().get("removed") == 1
                      and after_release.get("deleted", 0) >= 1
                      and not Path(pinned_path).exists(),
                      f"removed={released.json().get('removed')} "
                      f"exists={Path(pinned_path).exists() if pinned_path else None}")
                # The console's own door to the bytes: `download=1` for Save, inline
                # otherwise, because an attachment disposition makes <img> and
                # <audio> fetch nothing.
                inline = req(client, "GET", f"/admin/api/artifacts/{pin_job}/outputs/0")
                forced = req(client, "GET",
                             f"/admin/api/artifacts/{pin_job}/outputs/0?download=1")
                check("the console can preview an artifact inline and force a download",
                      inline.status_code == 200
                      and "attachment" not in (inline.headers.get("content-disposition") or "")
                      and "attachment" in (forced.headers.get("content-disposition") or ""),
                      f"{inline.status_code} {inline.headers.get('content-disposition')!r} / "
                      f"{forced.headers.get('content-disposition')!r}")
                gone = req(client, "GET", f"/admin/api/artifacts/{pin_job}/outputs/9")
                check("and the console is told which indices exist rather than getting a blank file",
                      gone.status_code == 404 and "it has" in gone.text,
                      f"{gone.status_code} {gone.text[:120]}")

            print("\n\033[1m10h. The console can start a generation itself\033[0m")
            # The `/v1` routes want the Story Studio key, which a browser page does not
            # hold, so the console has its own door — wrapping **the same `_submit`**,
            # so the enabled set it offers is exactly the set it can run.
            cm = req(client, "GET", "/admin/api/create/models").json()
            check("the Create picker is built from the offered catalogue",
                  cm["counts"].get("chat", 0) > 0
                  and all(m["id"] == f"{m['provider_id']}::{m['model_id']}"
                          for m in cm["by_modality"]["chat"]),
                  f"{cm['counts']}")
            bad_modality = req(client, "POST", "/admin/api/create",
                               json={"modality": "telepathy", "model_id": "x"})
            check("and an unknown lane is refused, naming the ones that exist",
                  bad_modality.status_code == 422
                  and "modality must be one of" in bad_modality.text,
                  f"{bad_modality.status_code} {bad_modality.text[:120]}")
            not_offered = req(client, "POST", "/admin/api/create",
                              json={"modality": "chat", "model_id": "nobody::nothing",
                                    "params": {}})
            check("and a model the catalogue withholds cannot be started here either",
                  not_offered.status_code == 404 and "MODEL_NOT_FOUND" in not_offered.text,
                  f"{not_offered.status_code} {not_offered.text[:120]}")
            # The real thing: one generation all the way through the console's own
            # door, then polled on its own route.
            started = req(client, "POST", "/admin/api/create",
                          json={"modality": "chat",
                                "model_id": "mock-remote::mock-chat-1",
                                "params": {"messages": [{"role": "user", "content": "hi"}]},
                                "idempotency_key": f"e2e-console-{int(time.time())}"})
            check("a generation started from the console is accepted",
                  started.status_code == 200 and bool(started.json().get("id")),
                  f"{started.status_code} {started.text[:140]}")
            if started.status_code == 200:
                settled = poll(client, f"/admin/api/create/status/{started.json()['id']}")
                check("and it settles through the console's own poll route",
                      settled.get("state") == "succeeded",
                      f"{settled.get('state')} {(settled.get('failure_reason') or '')[:140]}")
            else:
                check("and it settles through the console's own poll route", False,
                      "it was never accepted")
            # The surface has to ship with the page, or the door exists with nothing
            # behind it — a control that does nothing, from the other side.
            create_html = req(client, "GET", "/admin/").text
            check("and the page carries the Create view with Studio Hub's six lanes",
                  'id="view-create"' in create_html
                  and "CREATE_TABS" in create_html
                  and all(f"key: '{k}'" in create_html for k in
                          ("audio", "subtitle", "image", "music", "video", "chat")),
                  "Create must be reachable at #create, not merely implemented")
            check("and a lane with nothing switched on says so instead of offering a dead button",
                  "No model is switched on for this lane" in create_html,
                  "an empty picker must explain itself")
            kie_tasks = httpx.get(
                f"http://127.0.0.1:{MOCK_PORT}/__test__/kie-tasks").json()["tasks"]
            kie_music = [t for t in kie_tasks
                         if t.get("model") == "ai-music-api/generate"]
            check("Kie music submits the music api-model, not the Suno tier",
                  len(kie_music) == 1 and kie_music[0]["input"].get("model") == "V6",
                  f"{kie_tasks}")
            check("and the Suno input carries the caller's lyrics, style and mode",
                  bool(kie_music)
                  and kie_music[0]["input"].get("prompt") == "a calm piano piece"
                  and kie_music[0]["input"].get("style") == "Classical"
                  and kie_music[0]["input"].get("title") == "Peaceful"
                  and kie_music[0]["input"].get("custom_mode") is True,
                  f"{kie_music[:1]}")
            # **A voice is named, because omitting one is now refused.** It used to
            # become `English_expressive_narrator` — a specific named English
            # speaker, chosen silently — so the narration came back in a voice the
            # caller never picked. The refusal itself is asserted in section 10i;
            # this lane check is about the job running when a voice *is* chosen.
            run_generation(
                "minimax-speech",
                "/v1/audio/jobs", "/v1/jobs/{id}",
                {"model_id": "mock-minimax::speech-2.8-turbo", "text": "hello",
                 "voice_id": "English_expressive_narrator"},
                "/v1/jobs/{id}/audio", is_wav,
            )

            music_cat = req(client, "GET", "/v1/music/models").json()
            check("music catalogue is reachable", "models" in music_cat)
            music_job = req(client, "POST", "/v1/music/generations",
                            json={"model_id": "mock-minimax::music-01", "prompt": "x"},
                            headers={"Idempotency-Key": f"e2e-music-shape-{int(time.time())}"})
            check("music submit returns 202", music_job.status_code == 202,
                  f"got {music_job.status_code}")

            print("\n\033[1m12b. The offerability rule is re-asked at execution\033[0m")
            # The worker re-checked **provider** enablement only. So a model switched
            # off after submit still ran, a Hub refresh that flipped `catalogued` or
            # `memory_eligible` mid-queue did not stop the job, and a modality the
            # adapter cannot serve was still dispatched. The catalogue's rule is one
            # function, so the worker asks it instead of re-implementing its gates.
            from gateway import worker as _worker

            class _Capture:
                def __init__(self):
                    self.failed: list[tuple[str, str, str]] = []
                    self.ran = False

                def fail(self, job_id, *, code="", detail=""):
                    self.failed.append((job_id, code, detail))

                def succeed(self, *a, **kw):
                    raise AssertionError("a job that should not run reached succeed()")

                def mark_running(self, *a, **kw):
                    pass

            _provider = "openrouter-main"   # a built-in, so this process knows it
            _offerable = [(_provider, {"model_id": "yes/model", "modality": "tts"})]
            _real_enabled = _reg.models.enabled_models
            _real_is_enabled = _reg.credentials.is_enabled
            _real_adapter = _worker.adapter_for

            class _StubAdapter:
                def generate(self, request):
                    raise AssertionError("a model the catalogue cannot offer was dispatched")

            def _job(model_id):
                return {"id": "job-x", "provider_id": _provider, "model_id": model_id,
                        "modality": "tts", "params": "{}"}

            _reg.models.enabled_models = lambda modality: [
                (_reg.definition(_provider), m) for _d, m in _offerable
            ]
            _reg.credentials.is_enabled = lambda _pid: True
            _worker.adapter_for = lambda _definition: _StubAdapter()
            try:
                _store = _Capture()
                _worker.execute_job(_job("no/model"), _store)
            finally:
                _reg.models.enabled_models = _real_enabled
                _reg.credentials.is_enabled = _real_is_enabled
                _worker.adapter_for = _real_adapter
            check("a model the catalogue can no longer offer is refused, not dispatched",
                  len(_store.failed) == 1
                  and _store.failed[0][1] == "MODEL_NOT_OFFERABLE"
                  and "no longer offered" in _store.failed[0][2],
                  f"{_store.failed}")
            check("and the refusal names the model, the provider and the modality",
                  all(part in _store.failed[0][2]
                      for part in ("no/model", _provider, "tts")),
                  _store.failed[0][2])

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

            print("\n\033[1m13b. A key edited into .env takes effect without a restart\033[0m")
            # `.env` is the documented no-UI way to give the gateway a key, so an
            # edit to it has to work — and it did not. `get()` returned a
            # module-level snapshot taken at import and `require_auth` compared
            # against its own import-time copy, so `GATEWAY_API_KEY` was frozen for
            # the life of the process. The failure that matters is a **rotation**:
            # change the key, restart nothing, and the old key still opens the door.
            # A rotation that appears to have happened and has not is worse than one
            # that visibly failed.
            #
            # The positive half first. Without it, a `.env` that failed to load
            # would leave a keyless gateway — which accepts *any* token — and every
            # assertion below would pass for the wrong reason.
            env_file = tmp / ".env"
            check("a wrong key is refused, so the key really is in force",
                  httpx.get(f"{BASE}/v1/chat/models",
                            headers={"Authorization": "Bearer not-the-key"}).status_code == 403,
                  "a keyless gateway would accept this and make the rotation vacuous")

            rotated = "rotated-key-b7f2"
            env_file.write_text(f"GATEWAY_API_KEY={rotated}\n")
            check("the new key is accepted once written to .env",
                  httpx.get(f"{BASE}/v1/chat/models",
                            headers={"Authorization": f"Bearer {rotated}"}).status_code == 200)
            check("and the key it replaced stops working at once",
                  httpx.get(f"{BASE}/v1/chat/models",
                            headers={"Authorization": f"Bearer {GW_KEY}"}).status_code == 403,
                  "the old key still worked, which is a rotation that did not happen")

            # Put it back, or every later section authenticates with a dead key.
            env_file.write_text(f"GATEWAY_API_KEY={GW_KEY}\n")
            check("restoring the key in .env restores access",
                  httpx.get(f"{BASE}/v1/chat/models", headers=AUTH).status_code == 200)
            check("and the withdrawn key is refused again",
                  httpx.get(f"{BASE}/v1/chat/models",
                            headers={"Authorization": f"Bearer {rotated}"}).status_code == 403)

            print("\n\033[1m13c. 'Studio Hub is running' is a measurement, not a file\033[0m")
            # `hub_status()` published `running: bool(hub_base_url())` — **file
            # existence**. So the Overview page and the console said the Hub was
            # running while the menu bar's own `/api/health` probe and `fleet._probe`
            # both reported it unreachable: two readers of one fact disagreeing, and
            # the one on the page that exists to say what is available was the weaker.
            from gateway import hub as _hub

            _real_base = _hub.hub_base_url
            try:
                # A ready-file with nothing behind it: the exact stale-file case.
                _hub.hub_base_url = lambda: "http://127.0.0.1:9"
                _hub._PROBE_CACHE["result"] = None
                _dead = _hub.hub_probe(timeout=0.6, force=True)
                check("a ready-file with nothing behind it is not 'running'",
                      _dead["running"] is False and _dead["advertised"] is True,
                      f"running={_dead['running']} advertised={_dead['advertised']}")
                check("and the detail says which of the two failures it is",
                      "advertises" in _dead["detail"] and "stale" in _dead["detail"],
                      _dead["detail"])
                _stale_status = _hub.hub_status()
                check("so the status page stops claiming a Hub that is not there",
                      _stale_status["running"] is False
                      and _stale_status["advertised"] is True,
                      f"{_stale_status}")
                # Annotation promised "refuses without a reachable Hub" and gated on
                # URL presence, so a stale file started the pass and every voice
                # failed one at a time with a network error, burying the one
                # actionable sentence under N of them. Imported here rather than
                # reusing the module-level name, which this function binds much
                # later — hence the `UnboundLocalError` this check first raised.
                from gateway import voice_import as _vi
                # `voice_import` binds `hub_base_url` at its own import, which can come
                # first (`voices` reads the clip length through it), so it is patched too.
                _vi_base = _vi.hub_base_url
                _vi.hub_base_url = _hub.hub_base_url
                try:
                    _refused = _vi.start_annotation()
                finally:
                    _vi.hub_base_url = _vi_base
                check("and the annotation pass refuses once, with the actionable reason",
                      _refused.get("running") is False
                      and "not reachable" in str(_refused.get("error")),
                      f"running={_refused.get('running')} error={_refused.get('error')}")
            finally:
                _hub.hub_base_url = _real_base
                _hub._PROBE_CACHE["result"] = None

            # And with nothing published at all, the detail says that instead.
            try:
                _hub.hub_base_url = lambda: ""
                _hub._PROBE_CACHE["result"] = None
                _nothing = _hub.hub_probe(timeout=0.6, force=True)
                check("no ready-file at all reads as no ready-file, not as a dead one",
                      _nothing["running"] is False and _nothing["advertised"] is False
                      and "no hub-ready.json" in _nothing["detail"],
                      f"{_nothing['detail']}")
            finally:
                _hub.hub_base_url = _real_base
                _hub._PROBE_CACHE["result"] = None

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
            # The full balanced zero set, not just one field — and the set is
            # *read from `constants.zero_pricing`*, because a hand-typed copy here
            # is exactly the drift this asserts against: the row carried four of
            # the six keys and was missing `cost_tier` and `standard_credits`.
            from gateway.constants import zero_pricing as _zero_pricing

            shape = _zero_pricing("tts")
            check("every Story voice publishes the full price shape",
                  bool(story) and all(
                      all(v.get(k) == value for k, value in shape.items() if k != "chargeable")
                      for v in story),
                  f"{shape} vs {[{k: v.get(k) for k in shape} for v in story[:2]]}")
            # `chargeable` is the truth, not the shape: these are the owner's own Macs' voices.
            check("and a local voice says it costs nothing (chargeable=false)",
                  bool(story) and all(v.get("chargeable") is False for v in story),
                  f"{[v.get('chargeable') for v in story[:4]]}")
            check("and no voice is published that Story Studio's parser would drop",
                  all(v["catalog_status"] == "published" and v["quote_enabled"]
                      and v["generation_enabled"] for v in story))

            # **A documented example is not the owner's account voice**, and a
            # container is the adapter's, not a guess from the provider id.
            from gateway.voices import (_fal_tts_voices as _fal_voices,
                                        story_row as _story_row,
                                        tts_output_container as _container)
            from gateway.providers.definitions import PROVIDERS_BY_ID as _P_voice
            _examples = _fal_voices(_P_voice["fal-ai"], {})
            check("fal's documented examples are not published as the owner's own voices",
                  bool(_examples) and all((not v.owned) and v.source == "example"
                                          for v in _examples),
                  f"{_examples[0].source if _examples else None} "
                  f"owned={_examples[0].owned if _examples else None}")
            _example_row = _story_row(_examples[0]) if _examples else {}
            check("so they are library voices, with a public id and no custom one",
                  _example_row.get("voice_tier") == "library"
                  and _example_row.get("custom_voice_id") is None
                  and _example_row.get("public_voice_id") == _examples[0].provider_voice_id,
                  f"{_example_row.get('voice_tier')}")
            check("and the container is the adapter's, not guessed from an id prefix",
                  _example_row.get("output_format") == "mp3"
                  and _container("elevenlabs-direct") == "mp3"
                  and _container("groq-main") == "wav",
                  f"fal={_example_row.get('output_format')}")

            httpx.post(f"{BASE}/admin/api/voices/enabled", headers=AUTH,
                       json={"items": [victim["id"]], "enabled": True})
            restored = req(client, "GET", "/v1/voices").json()["voices"]
            check("re-enabling restores it", victim["id"] in {v["id"] for v in restored})

            # Which models a local voice may be used with is the **model's**
            # statement, not the tier the voice was minted against. This pair used
            # to assert the opposite — that a voice is returned for its own
            # `tts_model_id` and for nothing else — which is exactly the rule that
            # hid all ten of the owner's voices from the other seven clone-capable
            # local rows, because every one of them was stamped with the same
            # Qwen3 tier at import. A model that *states* it takes no reference is
            # still withheld, and section 20 proves that against a Hub that says so.
            #
            # The mint stamp is still reported, under a name that is not a serving
            # claim: publishing it as `tts_model_id` made Story Studio drop every
            # local voice, because its own boundary check refuses a row that names
            # a different model than the one being asked about.
            model_id = str(victim.get("minted_against_model") or "")
            check("a local voice reports the tier it was minted against",
                  bool(model_id), victim.get("minted_against_model"))
            check("and does not publish that tier as the model serving it",
                  not victim.get("tts_model_id"), f"tts_model_id={victim.get('tts_model_id')!r}")
            if model_id:
                scoped = req(client, "GET", f"/v1/voices?model_id=studio-hub-local::{model_id}").json()
                check("a local voice is returned for its own model",
                      len(scoped["voices"]) > 0, f"got {len(scoped['voices'])}")
                # The story-facing row must name no model at all: Story Studio
                # treats a row naming a different model as unusable for this one,
                # so any value here that is not the exact model id silently empties
                # the narration picker.
                check("the story-facing row names no model, so the scoped endpoint decides",
                      all(not row.get("tts_model_id") for row in scoped["voices"]),
                      f"{[row.get('tts_model_id') for row in scoped['voices']][:3]}")
                other = req(client, "GET",
                            "/v1/voices?model_id=studio-hub-local::some-other-model").json()
                check("and, that model stating nothing, it is offered there too",
                      len(other["voices"]) == len(scoped["voices"]),
                      f"{len(other['voices'])} vs {len(scoped['voices'])}")

            print("\n\033[1m16. Stocking voices from a folder\033[0m")
            # Both shapes are things people actually have: the existing library's
            # per-voice folders, and loose audio files — the second being what you
            # get by copying a clip in and expecting it to become a voice, which the
            # old importer silently skipped.
            import struct
            import wave

            from gateway import voice_import

            def write_wav(path, seconds=0.2):
                with wave.open(str(path), "wb") as handle:
                    handle.setnchannels(1)
                    handle.setsampwidth(2)
                    handle.setframerate(16000)
                    handle.writeframes(struct.pack("<h", 0) * int(16000 * seconds))

            picked = tmp / "picked"
            # **Two different recordings, deliberately.** `write_wav` with the same
            # duration twice produces byte-identical files, and byte-identical means
            # *one* voice under the identity rule below — which quietly turned this
            # fixture into a two-imports-one-voice case and made three later
            # assertions mean something else. Different lengths keep them two.
            picked_parent_clip = picked / "Morgan.wav"   # the loose-file shape
            blue_clip = picked / "Blue Room" / "reference.wav"
            (picked / "Blue Room").mkdir(parents=True)
            write_wav(blue_clip, seconds=0.25)
            (picked / "Blue Room" / "metadata.json").write_text(
                json.dumps({"name": "Blue - Calm and Deep", "language": "en", "gender": "m"})
            )
            (picked / "Blue Room" / "transcript.txt").write_text("a blue reference")
            write_wav(picked_parent_clip, seconds=0.2)
            (picked / "Morgan.txt").write_text("a morgan reference")
            # A loose clip's facts come from a sibling JSON, the same way its
            # transcript comes from a sibling .txt. Without one the language is
            # empty — and a voice with no language cannot be pushed, because the Hub
            # requires one. This fixture wants a pushable voice.
            (picked / "Morgan.json").write_text(json.dumps({"language": "en"}))
            (picked / "notes.md").write_text("not audio")
            (picked / "empty folder").mkdir()

            added = req(client, "POST", "/admin/api/voices/add",
                        json={"folder": str(picked), "annotate": False}).json()
            names = sorted(v["display_name"] for v in added["imported"])
            check("a folder of voice folders is accepted", "Blue" in names, f"{names}")
            check("a loose audio file is accepted as a voice", "Morgan" in names, f"{names}")
            check("a folder with no audio is reported, not ignored",
                  [s["name"] for s in added["skipped"]] == ["empty folder"],
                  f"{[s['name'] for s in added['skipped']]}")
            check("a sidecar transcript is picked up",
                  any(v["has_transcript"] for v in added["imported"] if v["display_name"] == "Morgan"))
            check("a new voice still needs segments",
                  all(v["needs_segments"] for v in added["imported"]))

            library = Path(added["library"])
            catalog = json.loads(Path(added["catalog"]).read_text())
            imported_ids = {v["id"] for v in added["imported"]}
            ours = [v for v in catalog["voices"] if v["id"] in imported_ids]
            check("both shapes are imported as two voices",
                  len(added["imported"]) == 2 and len(ours) == 2,
                  f"{[(v['display_name'], v['id']) for v in added['imported']]}")
            copied = sorted(p.name for p in library.iterdir())
            check("the clips are copied into the gateway's own library",
                  copied == sorted(imported_ids), f"{copied} vs {sorted(imported_ids)}")

            # **The id is the clip's own bytes, not its name.** Deriving it from the
            # folder or file name meant the same recording re-imported under another
            # name became a second voice, and two clips whose names slugged alike
            # (`Morgan`, `morgan!`) silently replaced one another — the on-disk path
            # is the id, so the second overwrote the first's folder.
            stored = next(v for v in ours if v["display_name"] == "Morgan")
            # Sections 17 and 18 act on this voice by id, and the id is no longer
            # the name it came in under — it is the clip's own bytes.
            morgan_id = stored["id"]
            morgan_dir = library / morgan_id
            check("a voice's id is derived from its bytes, not the name it arrived under",
                  stored["id"] == "voice-" + str(stored["audio_sha256"])[:12],
                  f"id={stored['id']} sha={str(stored.get('audio_sha256'))[:12]}")
            check("so the folder names the audio, and nothing human-types it",
                  "morgan" not in stored["id"], stored["id"])
            check("the copy is where the catalogue says it is",
                  all(Path(v["reference_path"]).parent.parent == library for v in ours),
                  f"{[v.get('reference_path') for v in ours]}")
            ids = [v["id"] for v in catalog["voices"]]
            check("the catalogue keeps what was already there",
                  len(ids) > len(added["imported"]), f"{len(ids)} total")
            check("and has no duplicate ids", len(ids) == len(set(ids)), f"{ids}")
            check("the clip's own hash is stored for location matching",
                  len(str(stored.get("audio_sha256") or "")) == 64)
            # "Processed properly" means one self-contained folder — clip,
            # transcript, derived metadata — not a clip here and its meaning in a
            # catalogue somewhere else.
            files = {f.name for f in morgan_dir.iterdir()}
            check("a voice is a self-contained folder, not just a clip",
                  {"reference.wav", "metadata.json", "transcript.txt"} <= files, f"{sorted(files)}")

            again = req(client, "POST", "/admin/api/voices/add",
                        json={"folder": str(picked), "annotate": False}).json()
            after = json.loads(Path(added["catalog"]).read_text())
            check("re-adding the same folder updates rather than duplicates",
                  len(after["voices"]) == len(catalog["voices"]),
                  f"{len(catalog['voices'])} -> {len(after['voices'])}")
            # The same recordings again are skipped and named as such. Re-importing over them
            # used to reset each voice's language, gender and fleet permission.
            check("and reports them as already here, not as imported",
                  again["imported"] == [] and len(again["skipped"]) == 3
                  and sum(1 for s in again["skipped"] if s.get("duplicate")) == 2,
                  f"{again['imported']} {again['skipped']}")

            # The same recording, arriving under a name the catalogue has never
            # seen. Bytes are the identity, so this is that voice and not a second
            # one — the whole point of the change. The Blue clip, so the Morgan
            # voice the rest of this section reasons about is left alone.
            blue = next(v for v in ours if v["display_name"] == "Blue")
            elsewhere = tmp / "picked-renamed"
            elsewhere.mkdir()
            shutil.copy2(blue_clip, elsewhere / "Renamed Blue.wav")
            renamed = req(client, "POST", "/admin/api/voices/add",
                          json={"folder": str(elsewhere), "annotate": False}).json()
            after_rename = json.loads(Path(added["catalog"]).read_text())
            check("the same clip under a different name is the same voice",
                  renamed["imported"] == []
                  and [v.get("id") for v in renamed["skipped"]] == [blue["id"]]
                  and "same recording" in renamed["skipped"][0]["reason"],
                  f"{renamed['imported']} {renamed['skipped']}")
            check("and it does not add a row",
                  len(after_rename["voices"]) == len(catalog["voices"]),
                  f"{len(catalog['voices'])} -> {len(after_rename['voices'])}")
            check("and the voice it already was keeps its own name",
                  next(v for v in after_rename["voices"] if v["id"] == blue["id"])
                  ["display_name"] == "Blue",
                  f"{[v['display_name'] for v in after_rename['voices'] if v['id'] == blue['id']]}")

            # A *different* recording claiming a name that is already taken. It is not added
            # until the owner says how: a second "Morgan" identical in the list is how the
            # owner's duplicate Chris/Christian/Leo happened (2026-10-11).
            clash_dir = tmp / "picked-clash"
            clash_dir.mkdir()
            write_wav(clash_dir / "Morgan.wav", seconds=0.4)  # different bytes
            clash_file = str(clash_dir / "Morgan.wav")
            clashed = req(client, "POST", "/admin/api/voices/add",
                          json={"files": [clash_file], "annotate": False}).json()
            check("a different recording under a used name is asked about, not added",
                  clashed["imported"] == [] and len(clashed["conflicts"]) == 1
                  and clashed["conflicts"][0]["message"] == "A voice named \u201cMorgan\u201d already exists."
                  and clashed["conflicts"][0]["existing"][0]["id"] == stored["id"],
                  f"{clashed}")
            kept_both = req(client, "POST", "/admin/api/voices/add",
                            json={"files": [clash_file], "annotate": False,
                                  "resolutions": {clash_file: "keep_both"}}).json()
            after_clash = json.loads(Path(added["catalog"]).read_text())
            same_name = [v for v in after_clash["voices"] if v["display_name"] in {"Morgan", "Morgan 2"}]
            check("keep both adds it as Morgan 2, beside the older take",
                  [v["display_name"] for v in kept_both["imported"]] == ["Morgan 2"]
                  and stored["id"] in {v["id"] for v in same_name} and len(same_name) == 2,
                  f"{kept_both} {[v['id'] for v in same_name]}")

            # Put the catalogue back to the two voices the rest of this section
            # reasons about; the clash above is evidence, not state to keep.
            for extra in [v["id"] for v in same_name if v["id"] != stored["id"]]:
                gone = req(client, "DELETE", f"/admin/api/voices/studio-hub-local::{extra}")
                check("one of this Mac's own voices can be removed",
                      gone.status_code == 200 and gone.json().get("removed") == extra, gone.text[:200])
                listed = req(client, "GET", "/admin/api/voices/removed").json()["voices"]
                check("and is listed as restorable, with its length",
                      [v["id"] for v in listed] == [extra] and listed[0]["duration_seconds"],
                      f"{listed}")
                back = req(client, "POST", "/admin/api/voices/restore",
                           json={"backup": listed[0]["backup"]})
                check("restore brings it back under the same id",
                      back.status_code == 200 and any(
                          v["id"] == extra for v in json.loads(Path(added["catalog"]).read_text())["voices"]),
                      back.text[:200])
                req(client, "DELETE", f"/admin/api/voices/{extra}")
            catalog = json.loads(Path(added["catalog"]).read_text())
            check("the catalogue is back to one Morgan",
                  [v["display_name"] for v in catalog["voices"]].count("Morgan") == 1
                  and "Morgan 2" not in [v["display_name"] for v in catalog["voices"]],
                  f"{[v['display_name'] for v in catalog['voices']]}")

            # The folder that was picked is one the owner will empty later — a
            # Downloads folder, a temporary copy. The voice must not depend on it.
            kept = Path(stored["reference_path"])
            shutil.rmtree(picked)
            check("deleting the folder it came from leaves the voice intact",
                  kept.exists() and "voice-library" in str(kept), str(kept))
            check("and the catalogue still lists it",
                  any(v["id"] == morgan_id for v in catalog["voices"]),
                  f"{len(catalog['voices'])} entries")

            # **A language nobody stated is not English.** Every import used to store
            # "en", which was republished as the voice's own and reached the fleet as
            # a claim — and it made the readiness gate's "it has no language"
            # unreachable. The Hub genuinely requires one ("language is required"),
            # so the honest order is: store nothing, refuse the push locally in one
            # sentence, and let the owner state it.
            _lang_dir = tmp / "picked-nolang"
            _lang_dir.mkdir()
            write_wav(_lang_dir / "No Language.wav", seconds=0.3)
            _nl = req(client, "POST", "/admin/api/voices/add",
                      json={"folder": str(_lang_dir), "annotate": False}).json()
            _nl_row = next(v for v in json.loads(Path(added["catalog"]).read_text())["voices"]
                           if v["id"] in {i["id"] for i in _nl["imported"]})
            check("a clip whose language nobody stated does not claim English",
                  _nl_row["language"] == "", f"{_nl_row['language']!r}")
            # ...and the guard that was unreachable now fires, locally, once.
            _readiness = voice_import.shared_readiness(_nl_row)
            check("so a language-less voice is refused a fleet push, in one sentence",
                  not _readiness["ready"]
                  and any("no language" in p for p in _readiness["problems"]),
                  f"{_readiness['problems']}")
            # The sentinel `story_row` publishes for "many, unenumerated" is only
            # reachable when the stored language is genuinely empty.
            _nl_story = next(r for r in req(client, "GET", "/v1/voices").json()["voices"]
                             if r["id"].endswith(_nl_row["id"]))
            check("and Story Studio is told 'multilingual', not a definite language",
                  _nl_story["language"] == "multilingual", f"{_nl_story['language']}")

            # One voice, listed once. `voices()` concatenated what discovery had
            # cached with what the owner had saved and never de-duplicated, so a
            # voice known to both was listed **twice** — and `find()` takes the
            # first, so the preview and the tier you saw depended on which list
            # happened to hold it. A store built here, because no mock lane in this
            # suite both discovers a voice and saves one, so the overlap cannot be
            # staged through the API.
            from gateway.voices import VoiceRegistry

            overlapping = VoiceRegistry(tmp / "voices-overlap.json")
            overlapping._data["discovered"] = {"mock-remote": {
                "fetched_at": 1.0,
                "voices": [{
                    "id": "mock-remote::v1", "provider_voice_id": "v1",
                    "display_name": "From discovery", "language": "en",
                    "preview_url": "http://127.0.0.1:9/preview.mp3", "raw": {},
                }],
            }}
            overlapping._data["custom"] = {"mock-remote": [{
                "id": "mock-remote::v1", "provider_voice_id": "v1",
                "display_name": "Saved by the owner",
            }]}
            merged = overlapping._merged("mock-remote")
            check("a voice both discovered and saved is listed once, not twice",
                  len(merged) == 1, f"{[v.id for v in merged]}")
            check("and the owner's own entry is the one that survives",
                  merged and merged[0].display_name == "Saved by the owner",
                  f"{[v.display_name for v in merged]}")

            # Segments come from Studio Hub's transcription, so with no Hub there is
            # nothing to transcribe with. Saying that is the whole point: silently
            # doing nothing would look identical to success. Checked directly rather
            # than over HTTP, because this suite runs a mock Hub — the one case that
            # cannot be produced through the API here is "there is no Hub at all".
            real_base_url = voice_import.hub_base_url
            voice_import.hub_base_url = lambda: ""
            from gateway import hub as _hub
            _hub._PROBE_CACHE["result"] = None
            guard = voice_import.start_annotation()
            voice_import.hub_base_url = real_base_url
            _hub._PROBE_CACHE["result"] = None
            check("with no Hub it refuses to pretend the voices are usable",
                  guard.get("running") is False
                  and "not reachable" in str(guard.get("error"))
                  and "no hub-ready.json" in str(guard.get("error")),
                  f"running={guard.get('running')} error={guard.get('error')}")

            status = req(client, "GET", "/admin/api/voices/add/status").json()
            check("the progress endpoint reports a shape the window can poll",
                  {"running", "total", "done", "current", "results"} <= set(status),
                  f"{sorted(status)}")

            print("\n\033[1m17. Readiness for the fleet (shared voices)\033[0m")
            # Pure functions on a catalogue entry, deliberately: they must not care
            # which data directory the caller happens to be using.
            #
            # The Hub's shared-voice receiver validates hard, and its refusals name a
            # field rather than a voice — useless across a hundred-node push. These
            # are the gateway answering "which voice, and why" before sending.
            unacknowledged = {
                "id": morgan_id, "display_name": "Morgan", "language": "en",
                "gender": "f", "audio_sha256": "c" * 64,
                "reference_path": str(morgan_dir / "reference.wav"),
            }
            ready = voice_import.shared_readiness(unacknowledged)
            check("a voice with no acknowledgement is not ready to share",
                  not ready["ready"]
                  and any("acknowledged" in p for p in ready["problems"]),
                  f"{ready['problems']}")
            check("and its missing segments are named, because that breaks cloning",
                  any("segments" in p for p in ready["problems"]), f"{ready['problems']}")

            # The receiver enforces `voice_id must be exactly 12 lowercase hex
            # characters` (measured: 400 otherwise). A gateway id always carries a
            # `voice-` prefix, so it can never be pushed as-is; what makes it
            # pushable is the clip's digest, which is where the 12 hex comes from.
            shared_id = voice_import.shared_voice_id(
                {"id": morgan_id, "audio_sha256": "a" * 64}
            )
            check("the shared id is the 12 hex the receiver demands",
                  shared_id == "a" * 12, shared_id)
            check("an unhashed voice has no shareable id rather than a wrong one",
                  voice_import.shared_voice_id({"id": morgan_id}) == "")

            fields = voice_import.push_fields({
                "id": morgan_id, "display_name": "Morgan", "language": "en",
                "gender": "f", "audio_sha256": "b" * 64, "license": "self-owned",
                "permission_acknowledged": True, "reference_transcript": "hi",
                "reference_path": str(morgan_dir / "reference.wav"),
            })
            check("the push carries exactly what the receiver declares",
                  {"voice_id", "audio_sha256", "name", "language", "gender", "license",
                   "notes", "source_url", "transcript",
                   "permission_acknowledged"} <= set(fields),
                  f"{sorted(fields)}")
            check("an unknown gender becomes the receiver's 'n', not an empty string",
                  voice_import.push_fields({"gender": None})["gender"] == "n")

            print("\n\033[1m17b. The wire shapes that fail silently when wrong\033[0m")
            # Stored segments are {start, end, text} because the Hub's *receiver*
            # uses that; the shared-voice *route* wants start_seconds/end_seconds.
            # Sending the stored shape would be accepted and ignored — a voice that
            # installs everywhere and clones nowhere.
            converted = voice_import.shared_segments(
                {"reference_segments": [{"start": 1.5, "end": 2.5, "text": "hello"}]}
            )
            check("segments are converted to the shape the shared-voice route wants",
                  converted == [{"start_seconds": 1.5, "end_seconds": 2.5, "text": "hello"}],
                  f"{converted}")
            check("a segment with no text is dropped rather than sent blank",
                  voice_import.shared_segments(
                      {"reference_segments": [{"start": 0, "end": 1, "text": "  "}]}
                  ) == [])
            pushed = voice_import.push_fields({
                "id": "voice-x", "display_name": "X", "language": "en", "gender": "n",
                "audio_sha256": "d" * 64, "license": "self-owned",
                "permission_acknowledged": True, "reference_transcript": "hi",
                "reference_segments": [{"start": 0.0, "end": 1.0, "text": "hi"}],
                "reference_path": str(morgan_dir / "reference.wav"),
            })
            check("the push sends the timings as JSON in the route's own key",
                  json.loads(pushed["transcript_segments"])[0]["start_seconds"] == 0.0,
                  pushed.get("transcript_segments"))
            check("'pending' reads as a wait, never as a failure",
                  voice_import.summarise_targets([
                      {"status": "synced"}, {"status": "pending"}, {"status": "pending"},
                  ]) == "1 installed, 2 will install when that Mac is next on",
                  voice_import.summarise_targets([{"status": "pending"}]))

            print("\n\033[1m18. Permission to clone and distribute is deliberate\033[0m")
            # The owner has private voices. A default of "allowed" would distribute
            # them by accident, so adding a clip must never be mistaken for granting
            # permission, and withdrawing must be as easy as granting.
            added_entry = next(
                (v for v in json.loads(Path(added["catalog"]).read_text())["voices"]
                 if v["id"] == morgan_id), {}
            )
            check("a newly added voice is private until the owner says otherwise",
                  added_entry.get("permission_acknowledged") is False
                  and not added_entry.get("license"),
                  f"ack={added_entry.get('permission_acknowledged')} license={added_entry.get('license')}")
            check("and it is therefore not shareable",
                  not voice_import.shared_readiness(added_entry)["ready"])

            granted = req(client, "POST", "/admin/api/voices/shared",
                          json={"items": [morgan_id], "acknowledged": True}).json()
            check("permission can be granted for one voice",
                  granted.get("changed") == 1, f"{granted}")
            after_grant = next(
                v for v in json.loads(Path(added["catalog"]).read_text())["voices"]
                if v["id"] == morgan_id
            )
            check("granting records a license, which the Hub requires",
                  after_grant.get("license") == "self-owned"
                  and after_grant.get("permission_acknowledged") is True,
                  f"license={after_grant.get('license')}")
            # The folder carries it too, so the permission travels with the voice.
            meta = json.loads((library / morgan_id / "metadata.json").read_text())
            check("the permission is written into the voice's own folder",
                  meta.get("permission_acknowledged") is True and meta.get("license") == "self-owned",
                  f"{meta}")
            check("and the gateway can list what is shareable",
                  [v["id"] for v in
                   req(client, "GET", "/admin/api/voices/shared").json()["voices"]] == [morgan_id])

            revoked = req(client, "POST", "/admin/api/voices/shared",
                          json={"items": [morgan_id], "acknowledged": False}).json()
            after_revoke = next(
                v for v in json.loads(Path(added["catalog"]).read_text())["voices"]
                if v["id"] == morgan_id
            )
            check("permission can be withdrawn again",
                  revoked.get("changed") == 1
                  and after_revoke.get("permission_acknowledged") is False,
                  f"{after_revoke.get('permission_acknowledged')}")
            check("withdrawing drops the license with it, so nothing looks authorised",
                  not after_revoke.get("license"), f"license={after_revoke.get('license')}")
            check("and the voice is private again in the list",
                  req(client, "GET", "/admin/api/voices/shared").json()["voices"] == [])
            # The permission is the owner's to grant. Share must not be a way round
            # it — asking for a voice that was never allowed is refused, not queued.
            refused = req(client, "POST", "/admin/api/voices/share",
                          json={"items": [morgan_id]})
            check("a voice without permission cannot be shared, even when asked for",
                  refused.status_code == 403 and "not been allowed" in refused.text,
                  f"HTTP {refused.status_code} {refused.text[:140]}")

            print("\n\033[1m18a. The row a reader actually gets, and both spellings of an id\033[0m")
            # Two readers — the menu bar app and this suite — believed these fields
            # lived in `raw`. `raw` is provider data and is **never serialised**, so
            # both were reading a key that does not exist: the app sent no owner for
            # an ElevenLabs library voice (every import 422'd, and it discarded the
            # error, so it looked like nothing happened) and every local voice
            # rendered "Private" for ever, even after the owner granted it. A field a
            # reader needs is part of the contract, not an implementation detail.
            rows = req(client, "GET", "/admin/api/voices").json()["voices"]
            morgan_row = next(r for r in rows if r["provider_voice_id"] == morgan_id)
            check("a voice row carries no `raw` at all, so nothing may read it",
                  "raw" not in morgan_row, f"{sorted(morgan_row)}")
            check("permission, owner and licence are published where a reader can see them",
                  {"owner_id", "permission_acknowledged", "license"} <= set(morgan_row),
                  f"{sorted(morgan_row)}")
            check("and this Mac's own voice says `False` — a real answer, not `None`",
                  morgan_row["permission_acknowledged"] is False,
                  repr(morgan_row["permission_acknowledged"]))

            req(client, "POST", "/admin/api/voices/shared",
                json={"items": [morgan_id], "acknowledged": True})
            regranted = next(r for r in req(client, "GET", "/admin/api/voices").json()["voices"]
                             if r["provider_voice_id"] == morgan_id)
            check("a grant is visible in the row the app renders",
                  regranted["permission_acknowledged"] is True
                  and regranted["license"] == "self-owned",
                  f"{regranted['permission_acknowledged']} {regranted['license']}")

            # The app sends the **published** id (`studio-hub-local::<catalogue id>`);
            # the catalogue stores the bare one. The routes matched the stored form
            # exactly, so the app's grant reported `changed: 0` — success at nothing
            # — and its Share walked straight past the 403 below.
            published = regranted["id"]
            check("the published id is namespaced by the maker",
                  published.endswith("::" + morgan_id), published)
            by_namespace = req(client, "POST", "/admin/api/voices/shared",
                               json={"items": [published], "acknowledged": False}).json()
            check("withdrawing by the published id reaches the same voice",
                  by_namespace.get("changed") == 1, f"{by_namespace}")
            check("and the bare id and the namespaced id name one voice",
                  voice_import.catalogue_id(published)
                  == voice_import.catalogue_id(morgan_id)
                  == morgan_id)
            bypass = req(client, "POST", "/admin/api/voices/share",
                         json={"items": [published]})
            check("so a namespaced id cannot spell its way past the permission",
                  bypass.status_code == 403 and "not been allowed" in bypass.text,
                  f"HTTP {bypass.status_code} {bypass.text[:140]}")
            check("an id that is not there normalises to nothing, never to a match",
                  voice_import.catalogue_id(None) == ""
                  and voice_import.catalogue_id("") == "")
            # Only this Mac's namespace is ours to strip. Stripping whatever
            # precedes `::` would fold one provider's `voice-a` onto another's, and
            # two different voices would answer to one id.
            check("another provider's namespace is left alone, not folded onto ours",
                  voice_import.catalogue_id("elevenlabs-direct::voice-a")
                  == "elevenlabs-direct::voice-a",
                  voice_import.catalogue_id("elevenlabs-direct::voice-a"))
            check("so two providers' voices with the same tail stay two voices",
                  voice_import.catalogue_id("elevenlabs-direct::voice-a")
                  != voice_import.catalogue_id("volc::voice-a"))

            # A provider voice is not the owner's to grant, so its answer is `None`
            # — "this question does not apply to me" — and `False` would render it
            # as "Private", a definite claim about a question nobody asked. Built
            # directly because no mock lane serves a library voice.
            from gateway.voices import SOURCE_ACCOUNT, Voice as _Voice
            provider_row = _Voice(
                id="elevenlabs-direct::abc", provider_id="elevenlabs-direct",
                provider_voice_id="abc", display_name="A library voice",
                source=SOURCE_ACCOUNT, raw={"owner_id": "owner-1"},
            ).as_dict()
            check("a provider voice says `None`, not `False`, about permission",
                  provider_row["permission_acknowledged"] is None,
                  repr(provider_row["permission_acknowledged"]))
            check("and its owner id is published at the top level, where the app reads it",
                  provider_row["owner_id"] == "owner-1", repr(provider_row["owner_id"]))

            print("\n\033[1m18b. Sharing a voice actually reaches a controller\033[0m")
            # The fan-out is the most heavily specified path in this codebase and
            # had NO endpoint-level test: only its pure helpers were covered, so the
            # two-token distinction — the thing AGENTS.md says "is what keeps a sold
            # node from being a sold controller" — was asserted nowhere. The mock
            # upstream serves a Hub's shared-voice routes, so the whole path runs
            # here without writing a voice to the owner's real Hub.
            mock = f"http://127.0.0.1:{MOCK_PORT}"
            httpx.post(f"{mock}/__test__/reset")

            # The route requires aligned timestamps, and `push_to_controller`
            # refuses before sending when readiness has problems — so give the voice
            # the timings the clone needs. Written the way the annotation step
            # writes them, since there is no Hub here to transcribe against.
            catalog_path = Path(added["catalog"])
            payload = json.loads(catalog_path.read_text())
            for row in payload["voices"]:
                if row["id"] == morgan_id:
                    row["reference_transcript"] = "a morgan reference"
                    row["reference_segments"] = [
                        {"start": 0.0, "end": 1.0, "text": "a morgan reference"}
                    ]
            catalog_path.write_text(json.dumps(payload, indent=2) + "\n")
            req(client, "POST", "/admin/api/voices/shared",
                json={"items": [morgan_id], "acknowledged": True})
            shareable = json.loads(catalog_path.read_text())
            morgan = next(v for v in shareable["voices"] if v["id"] == morgan_id)
            check("the voice is now ready to share",
                  not voice_import.shared_readiness(morgan)["problems"],
                  f"{voice_import.shared_readiness(morgan)['problems']}")

            # A location whose *fleet* token is set but whose Hub token is not.
            ghost = req(client, "POST", "/admin/api/fleet/controllers",
                        json={"label": "Mock Hub", "url": mock,
                              "token": "mock-fleet-token"}).json()["controller"]
            ghost_id = ghost["id"]

            first = req(client, "POST", "/admin/api/voices/share",
                        json={"items": [morgan_id]}).json()
            row = next((r for r in first["results"] if r["location"] == "Mock Hub"), {})
            check("a location with no Hub token is skipped, and the reason names why",
                  row.get("ok") is False
                  and "fleet token on purpose" in str(row.get("detail")),
                  f"{row}")
            recorded = httpx.get(f"{mock}/__test__/shared-voices").json()
            check("and nothing at all was pushed to it", recorded["voices"] == [],
                  f"{recorded}")
            check("so the fleet token was never used for a shared-voice push",
                  "mock-fleet-token" not in recorded["tokens"], f"{recorded['tokens']}")

            # Now give it the Hub token and push for real.
            stored_hub = req(client, "PUT", f"/admin/api/fleet/controllers/{ghost_id}/hub-token",
                             json={"token": "mock-hub-token"})
            check("a Hub token can be stored for a location",
                  stored_hub.status_code == 200, stored_hub.text[:140])
            second = req(client, "POST", "/admin/api/voices/share",
                         json={"items": [morgan_id]}).json()
            row = next((r for r in second["results"] if r["location"] == "Mock Hub"), {})
            check("the controller is pushed to", row.get("ok") is True, f"{row}")
            recorded = httpx.get(f"{mock}/__test__/shared-voices").json()
            check("the controller really received the voice",
                  len(recorded["voices"]) == 1, f"{recorded}")
            check("and the push used the **Hub** token, never the fleet token",
                  recorded["tokens"] == ["mock-hub-token"], f"{recorded['tokens']}")

            sent = recorded["voices"][0]
            check("the clip itself was uploaded, not just its metadata",
                  int(sent.get("__file_bytes__") or 0) > 0, f"{sent}")
            check("the id on the wire is the 12-hex the receiver demands",
                  len(str(sent.get("voice_id") or "")) == 12, f"{sent.get('voice_id')}")
            check("the push carries the clip's own hash, so a re-push refreshes",
                  str(sent.get("audio_sha256")) == str(morgan.get("audio_sha256")),
                  f"{sent.get('audio_sha256')} vs {morgan.get('audio_sha256')}")
            check("and the timings in the route's own key and shape",
                  json.loads(sent.get("transcript_segments") or "[]")[0]["start_seconds"] == 0.0,
                  f"{sent.get('transcript_segments')}")
            check("a node that is off reads as a wait, not as a failure",
                  "will install when that Mac is next on" in str(row.get("detail") or ""),
                  f"{row.get('detail')}")
            # Controllers only: the gateway never talks to a node. This Mac is a
            # location too, and it has no Hub token — so it must be *reported* as
            # skipped rather than silently omitted, and it must never be pushed to
            # as though it were a controller. With `STUDIOHUB_READY_FILE` pointed
            # at nothing there is no local Hub here, which is what makes this
            # assertion mean something.
            local_rows = [r for r in second["results"] if r["location"] != "Mock Hub"]
            check("this Mac is listed as a location and reported as not reachable",
                  len(local_rows) == 1 and local_rows[0]["ok"] is False,
                  f"{second['results']}")
            check("so the fan-out pushed to exactly one controller",
                  len([r for r in second["results"] if r["ok"]]) == 1,
                  f"{[(r['location'], r['ok']) for r in second['results']]}")
            # **A controller is identified by the controller, not by the address.**
            # `localhost` and `127.0.0.1` are the same mock answering with the same
            # `site_id`, so this must land on the existing row. Matching the
            # address alone minted a *second* location — orphaning the first one's
            # credential and every per-model enable decision with it.
            same_site = req(client, "POST", "/admin/api/fleet/controllers",
                            json={"label": "Mock Hub Moved",
                                  "url": mock.replace("127.0.0.1", "localhost"),
                                  "token": "mock-fleet-token"}).json()["controller"]
            check("a controller re-added at another address is the same location",
                  same_site["id"] == ghost_id and same_site.get("site_id") == "mock-site",
                  f"{same_site} vs {ghost_id}")
            check("and no second location was minted for it",
                  len(req(client, "GET", "/admin/api/fleet/controllers")
                      .json()["controllers"]) == 1,
                  f"{req(client, 'GET', '/admin/api/fleet/controllers').json()['controllers']}")
            req(client, "DELETE", f"/admin/api/fleet/controllers/{ghost_id}")
            check("the test location is removed again",
                  req(client, "GET", "/admin/api/fleet/controllers").json()["controllers"] == [])

            print("\n\033[1m20. A location must be a real address\033[0m")
            # Anything used to be accepted: `http://7` and `http://not a url` became
            # real locations with credentials, polled on every fleet read. Worse,
            # each one re-published the local Mac's voice catalogue, so three junk
            # locations turned ten voices into thirty identical rows.
            for body, why in (
                ({"url": 7}, "a number is not an address"),
                ({"url": "not a url"}, "spaces are not a host"),
                ({"url": "http://host:notaport"}, "a port has to be a number"),
                ({"url": "http://host:99999"}, "a port has to be in range"),
                ({"url": "http://"}, "there has to be a host"),
                ({"url": ""}, "an address is required"),
                ({"label": 5, "url": "http://127.0.0.1:9"}, "the label must be text"),
            ):
                r = req(client, "POST", "/admin/api/fleet/controllers", json=body)
                check(f"refused: {why}", r.status_code == 400, f"HTTP {r.status_code} {r.text[:80]}")
            check("and none of them were added",
                  req(client, "GET", "/admin/api/fleet/controllers").json()["controllers"] == [],
                  f"{req(client, 'GET', '/admin/api/fleet/controllers').json()}")

            # A location's row carries **two** identities and they are different
            # strings: the location is `local`, the provider whose models it offers
            # is `studio-hub-local`. Anything acting on a location's models needs
            # the second, and deriving it by prefixing in the client is how a label
            # becomes an identity. The Fleet page's Refresh does exactly this.
            _fleet_rows = httpx.get(f"{BASE}/admin/api/fleet", headers=AUTH).json()["locations"]
            check("a location publishes the provider its models are managed under",
                  [r.get("provider_id") for r in _fleet_rows] == ["studio-hub-local"],
                  f"{[(r.get('id'), r.get('provider_id')) for r in _fleet_rows]}")
            check("and it is not the same string as the location id",
                  all(r.get("provider_id") != r.get("id") for r in _fleet_rows),
                  f"{[(r.get('id'), r.get('provider_id')) for r in _fleet_rows]}")

            # The gateway's voice list belongs to the gateway, not to each location.
            # A location used to contribute the *local* catalogue, so every added
            # controller multiplied every voice.
            before = len(req(client, "GET", "/v1/voices").json()["voices"])
            added2 = req(client, "POST", "/admin/api/fleet/controllers",
                         json={"label": "Voice Multiplication Check",
                               "url": "http://127.0.0.1:9", "token": "x"}).json()
            after = req(client, "GET", "/v1/voices").json()["voices"]
            check("adding a location does not multiply the voice list",
                  len(after) == before, f"{before} voices became {len(after)}")
            ids = [v["id"] for v in after]
            check("and no voice is listed twice", len(ids) == len(set(ids)),
                  f"{[i for i in ids if ids.count(i) > 1][:3]}")
            req(client, "DELETE",
                f"/admin/api/fleet/controllers/{added2['controller']['id']}")

            print("\n\033[1m19. Received artifacts are temporary\033[0m")
            # The gateway is a receiver, not the owner: Story Studio keeps the copy
            # that matters. Nothing removed these at all before, so the directory
            # grew without bound.
            import time as _time

            artifacts = tmp / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            now = _time.time()
            day = 86400.0

            def stage(name: str, age_days: float, size: int = 1024) -> Path:
                path = artifacts / name
                path.write_bytes(b"x" * size)
                stamp = now - age_days * day
                os.utime(path, (stamp, stamp))
                return path

            ancient = stage("ancient.mp4", 30)          # over any sane age limit
            old = stage("old.mp4", 9)                   # over 3 days
            recent = stage("recent.mp4", 1)             # inside 3 days
            fresh = stage("fresh.mp4", 0.01)            # inside the protected hour

            got = req(client, "GET", "/admin/api/settings/retention").json()
            check("the defaults are the ones asked for: 3 days or 10 GB",
                  got["max_age_days"] == 3 and got["max_bytes"] == 10 * 1024**3,
                  f"{got['max_age_days']} days / {got['max_bytes']} bytes")
            # **The limits read must not walk the store.** It used to carry `usage`, whose walk of
            # every artifact and trash file took seconds — measured 3.1 s warm and past 15 s cold on
            # a real store — and the app abandons a read at 15 s, which blanked the whole Settings
            # form. The numbers are one small file; the report is its own route.
            check("and the limits read answers without walking the store",
                  "usage" not in got, f"keys={sorted(got)}")
            check("and it reports what is actually stored",
                  req(client, "GET", "/admin/api/settings/retention/usage")
                  .json()["usage"]["files"] >= 4, f"{got}")

            out = req(client, "POST", "/admin/api/artifacts/prune", json={}).json()
            check("pruning removes what is past the age limit",
                  not ancient.exists() and not old.exists(),
                  f"ancient={ancient.exists()} old={old.exists()}")
            trash = tmp / "_trash"
            check("pruned files go to the Trash Folder, not oblivion",
                  sorted(p.name for p in trash.rglob("*.mp4")) == ["ancient.mp4", "old.mp4"]
                  and req(client, "GET", "/admin/api/settings/retention/usage")
                  .json()["usage"]["trash_files"] >= 2,
                  f"{sorted(trash.rglob('*'))}")
            check("and keeps what is inside it", recent.exists() and fresh.exists())
            check("a file newer than the protection window is never removed",
                  fresh.exists(), "the newest artifact must survive even an aggressive cap")
            check("it reports what it removed rather than only that it ran",
                  out["deleted"] == 2 and out["deleted_by_age"] == 2 and out["moved_bytes"] > 0 and out["freed_bytes"] == 0,
                  f"{ {k: out[k] for k in ('deleted','deleted_by_age','deleted_by_size','moved_bytes','freed_bytes')} }")

            # The size limit is the backstop, and it takes the oldest first.
            big = [stage(f"bulk-{i}.bin", age_days=2 - i * 0.5, size=900 * 1024) for i in range(3)]
            saved = req(client, "PUT", "/admin/api/settings/retention",
                        json={"max_bytes": 2 * 1024 * 1024}).json()
            check("the limits can be edited", saved["max_bytes"] == 2 * 1024 * 1024, f"{saved}")
            after = req(client, "POST", "/admin/api/artifacts/prune", json={}).json()
            check("the size cap removes the oldest first",
                  not big[0].exists() and big[-1].exists(),
                  f"oldest kept={big[0].exists()} newest kept={big[-1].exists()}")
            check("and says which limit did it",
                  after["deleted_by_size"] >= 1, f"{after}")
            check("the newest is still protected even when the cap is exceeded",
                  fresh.exists() and after["protected_files"] >= 1, f"protected={after['protected_files']}")
            check("a limit of zero days is clamped rather than deleting everything",
                  req(client, "PUT", "/admin/api/settings/retention",
                      json={"max_age_days": 0}).json()["max_age_days"] > 0)
            check("a cap too small to hold one video is raised to a workable floor",
                  req(client, "PUT", "/admin/api/settings/retention",
                      json={"max_bytes": 700 * 1024}).json()["max_bytes"] == 1024 * 1024)
            check("the defaults can be restored",
                  req(client, "PUT", "/admin/api/settings/retention",
                      json={"max_age_days": 7, "max_bytes": 10 * 1024**3}).json()["max_age_days"] == 7)

            # ------------------------------------------------------------------
            print("\n\033[1m20a. A location never borrows this Mac's operator token\033[0m")
            # `hub_headers` was `token or hub_token()`. A controller whose credential
            # was absent — added with no token, or its token since forgotten — was
            # therefore handed **this Mac's operator token**, which was sent in
            # `X-Hub-Token` to the address the owner had typed into the Fleet page.
            # That token is full operator scope on this Mac's Hub: it can change its
            # settings and identity, reach its storage and shut it down.
            #
            # Nothing catches this from the outside. A Hub that accepts the token
            # just works, and the owner never learns his own Hub's operator token is
            # being broadcast to a site he added months ago.
            from gateway.hub import hub_headers, local_hub_headers

            check("a location's own credential is what its header carries",
                  hub_headers("controller-fleet-token") ==
                  {"X-Hub-Token": "controller-fleet-token"})
            check("an absent credential stays absent, rather than becoming this Mac's",
                  hub_headers("") == {} and hub_headers(None) == {},
                  f"{hub_headers('')} {hub_headers(None)}")
            check("and this Mac's own Hub still gets the operator token",
                  local_hub_headers() == {"X-Hub-Token": OPERATOR_TOKEN},
                  f"{local_hub_headers()}")

            # The adapter is a **second** call site with its own idea of which
            # provider it is calling, so it is asserted separately: fixing only the
            # discovery path would leave every dispatched job leaking.
            from gateway.adapters.base import BackendRequest as _HubRequest
            from gateway.adapters.studio_hub import _headers as _hub_headers_for

            def _hub_head(provider_id: str, token: str) -> dict:
                return _hub_headers_for(_HubRequest(
                    provider_id=provider_id, model_id="m", modality="image",
                    params={}, values={"hub_token": token} if token else {},
                    base_url="http://127.0.0.1:9",
                ))

            check("a dispatched job to a location sends that location's credential",
                  _hub_head("studio-hub-abc123", "loc-token")
                  == {"X-Hub-Token": "loc-token"})
            check("and a location with no credential sends none, not this Mac's",
                  _hub_head("studio-hub-abc123", "") == {}
                  and _hub_head("studio-hub-abc123", "") != local_hub_headers(),
                  f"{_hub_head('studio-hub-abc123', '')}")
            check("while a job for this Mac's own Hub still carries the operator token",
                  _hub_head("studio-hub-local", "") == {"X-Hub-Token": OPERATOR_TOKEN},
                  f"{_hub_head('studio-hub-local', '')}")

            # And on the wire. The helper could be right while a caller passes the
            # wrong flag, so this is the assertion that holds however the header was
            # built: a location with no credential, refreshed for real, must put
            # nothing — and above all not that sentinel — on the mock's socket.
            httpx.post(f"{mock}/__test__/reset")
            bare = httpx.post(f"{BASE}/admin/api/fleet/controllers",
                              headers=AUTH,
                              json={"label": "No-token Location", "url": mock})  # no token
            bare_id = bare.json().get("controller", {}).get("id") or ""
            check("a location can be added with no credential at all",
                  bare.status_code == 200 and bool(bare_id), bare.text[:140])
            httpx.post(f"{BASE}/admin/api/providers/studio-hub-{bare_id}/refresh",
                       headers=AUTH)
            seen = httpx.get(f"{mock}/__test__/hub-tokens").json()["tokens"]
            check("refreshing that location really reached the mock",
                  any(t["path"].startswith("/api/hub/") for t in seen),
                  f"{[t['path'] for t in seen]}")
            check("no request to it carried this Mac's operator token",
                  all(t["token"] != OPERATOR_TOKEN for t in seen),
                  f"{[t for t in seen if t['token'] == OPERATOR_TOKEN]}")
            check("and none carried a credential at all, because it has none",
                  all(t["token"] == "" for t in seen), f"{seen}")

            # The other half, and not a formality: "sends nothing" must not be the
            # only thing this code can do. Give the location a token and the mock
            # has to see exactly that.
            httpx.put(f"{BASE}/admin/api/fleet/controllers/{bare_id}/credential",
                      headers=AUTH, json={"token": "location-fleet-token"})
            httpx.post(f"{mock}/__test__/reset")
            httpx.post(f"{BASE}/admin/api/providers/studio-hub-{bare_id}/refresh",
                       headers=AUTH)
            seen = httpx.get(f"{mock}/__test__/hub-tokens").json()["tokens"]
            check("once it holds a credential, that is what the location is sent",
                  bool(seen) and all(t["token"] == "location-fleet-token" for t in seen),
                  f"{seen}")
            httpx.delete(f"{BASE}/admin/api/fleet/controllers/{bare_id}", headers=AUTH)
            check("and the test location is removed again",
                  httpx.get(f"{BASE}/admin/api/fleet/controllers",
                            headers=AUTH).json()["controllers"] == [])

            print("\n\033[1m20b. Re-discovering one provider is not re-discovering them all\033[0m")
            # The console's re-discover button posted `/refresh-all` whatever the filter said,
            # so a press meant for one provider re-read every provider's catalogue. The owner,
            # 2026-10-19: *"i don't know if it runs across all the providers or the one
            # selected... i only needed magpie refresh."* A scoped press now takes the
            # per-provider route, and the sentence the page prints ("Magpie: 175 models found",
            # or the error) is built from exactly this payload — so the shape is asserted on
            # the wire here, not only against the page's own reader.
            scoped = httpx.post(f"{BASE}/admin/api/providers/studio-hub-mock/refresh",
                                headers=AUTH)
            scoped_body = scoped.json()
            check("the per-provider route answers the count the scoped button prints",
                  scoped.status_code == 200
                  and isinstance(scoped_body.get("discovered"), int)
                  and scoped_body.get("provider_id") == "studio-hub-mock",
                  scoped.text[:140])
            check("and carries `error` either way, so 'found none' cannot read as 'failed'",
                  "error" in scoped_body, scoped.text[:140])
            check("a provider that does not exist is refused, not answered with a zero",
                  httpx.post(f"{BASE}/admin/api/providers/nobody/refresh",
                             headers=AUTH).status_code == 404)

            print("\n\033[1m20. A local voice belongs to whichever models clone\033[0m")
            # The local lane is the one place a recording is the *input*, so
            # "may this voice be used with this model?" is the model's own
            # statement — not the tier the voice happened to be minted against.
            # Every voice this Mac owns was stamped with one Qwen3 tier at import,
            # and matching on that string alone hid all of them from the other
            # clone-capable rows.
            httpx.post(f"{BASE}/admin/api/providers/studio-hub-mock/credentials",
                       headers=AUTH, json={"values": {"hub_token": "mock-token"}})
            httpx.post(f"{BASE}/admin/api/providers/studio-hub-mock/enabled",
                       headers=AUTH, json={"enabled": True})
            # Asked for explicitly: every field this kind declares is optional, so
            # storing them does not by itself mark the provider configured, and the
            # credentials route only auto-discovers a provider it considers
            # configured. Without this the model list is empty and reads as "the
            # location has nothing" rather than "nobody asked it".
            httpx.post(f"{BASE}/admin/api/providers/studio-hub-mock/refresh", headers=AUTH)
            hub_payload = httpx.get(f"{BASE}/admin/api/providers/studio-hub-mock/models",
                                    headers=AUTH).json()
            installed = {m["model_id"]: m for m in hub_payload["models"]}
            # Only the token is stored. The address lives on the definition, as it
            # does for every controller the Fleet page adds — and `_studio_hub`
            # used to skip that fallback and go straight to this Mac's own Hub, so
            # every remote location discovered the *local* machine's models. This
            # check is what catches that: a location pointed at the mock must find
            # the mock's models, and this run has no local Hub at all.
            check("a location discovers its own Hub's models, not the local one",
                  len(installed) == 8,
                  f"{sorted(installed)} error="
                  f"{hub_payload.get('error') or hub_payload.get('discovery_error')}")
            # **A lane the Hub gained on its own.** The gateway used to map a
            # package to a modality by its *engine name* through a table in this
            # repo, and `continue` past anything else — so the day the Hub served a
            # model on a new engine it vanished, with no row, no count and no
            # reason anywhere. Three readers now exist, strongest first: the Hub's
            # `modality` on the package row (0.6.61 — no second read, and it cannot
            # fail because a worker is down), then its per-model `hub_modality`,
            # then that engine table as the fallback for a Hub that says nothing.
            check("the Hub's own modality on the package row is what places a row",
                  ((installed.get("mock-hub/clone-model") or {}).get("raw") or {})
                  .get("modality_source") == "package",
                  f"{(installed.get('mock-hub/clone-model') or {}).get('raw')}")
            check("and a row that predates it falls back to the catalogue's statement",
                  ((installed.get("mock-hub/video-model") or {}).get("raw") or {})
                  .get("modality_source") == "stated",
                  f"{(installed.get('mock-hub/video-model') or {}).get('raw')}")
            check("a model on an engine this gateway has no table entry for is discovered, not dropped",
                  (installed.get("mock-hub/video-model") or {}).get("modality") == "video",
                  f"{sorted(installed)}")
            check("the lane's own contract is carried verbatim, deadline included",
                  (((installed.get("mock-hub/clone-model") or {}).get("raw") or {})
                   .get("lane_contract") or {}).get("deadline_seconds") == 900.0,
                  f"{((installed.get('mock-hub/clone-model') or {}).get('raw') or {}).get('lane_contract')}")
            check("and one nothing can place is reported by engine, not swallowed",
                  hub_payload.get("skipped") == [
                      # The fixture's own pair, on an engine the gateway has never
                      # had a table entry for. They were dropped in silence before
                      # this change too — which is the point: nobody could tell
                      # "the Hub listed nothing usable" from "the gateway did not
                      # understand what it listed".
                      {"engine": "fleet-mock", "count": 2,
                       "model_ids": ["vision-one-pkg", "voice-one-pkg"],
                       "reason": ("this gateway has no lane for it: the package states no "
                                  "modality and its engine is not in the engine table")},
                      # This one the **Hub itself** refused to place, so its own
                      # sentence is reported — the one that names the allowlist a
                      # person has to edit.
                      {"engine": "mystery-mlx", "count": 1,
                       "model_ids": ["mock-hub/unplaceable-model"],
                       "reason": ("the manifest engine 'mystery-mlx' has no worker or "
                                  "modality; add it to _ENGINE_ROUTE in "
                                  "components/hub/standalone.py and to the engine "
                                  "allowlist in components/model_packages.py")},
                  ],
                  f"{hub_payload.get('skipped')}")
            check("a catalogued model carries the reference contract the Hub stated",
                  ((installed.get("mock-hub/clone-model") or {}).get("raw") or {})
                  .get("reference_audio", {}).get("maximum_duration_seconds") == 15,
                  f"{(installed.get('mock-hub/clone-model') or {}).get('raw')}")
            check("an installed model the catalogue omits keeps no contract",
                  "reference_audio" not in
                  ((installed.get("mock-hub/uncatalogued-model") or {}).get("raw") or {}),
                  f"{(installed.get('mock-hub/uncatalogued-model') or {}).get('raw')}")
            check("membership and the machine verdict are recorded when the catalogue answers",
                  ((installed.get("mock-hub/clone-model") or {}).get("raw") or {}).get("catalogued") is True
                  and ((installed.get("mock-hub/uncatalogued-model") or {}).get("raw") or {}).get("catalogued") is False
                  and ((installed.get("mock-hub/toobig-model") or {}).get("raw") or {}).get("memory_eligible") is False,
                  f"{[(k, (v.get('raw') or {}).get('catalogued'), (v.get('raw') or {}).get('memory_eligible')) for k, v in installed.items()]}")

            httpx.post(f"{BASE}/admin/api/models/enabled", headers=AUTH, json={
                "items": [{"provider_id": "studio-hub-mock", "model_id": mid}
                          for mid in installed],
                "enabled": True,
            })


            # One local voice, written where the catalogue is read from. A short
            # silent wav is enough: nothing here decodes it, the *facts* are what
            # the matching and the report are about.
            import wave as _wave
            clip = tmp / "reference.wav"
            with _wave.open(str(clip), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(24000)
                handle.writeframes(b"\x00\x00" * 24000)
            (tmp / "voices.json").write_text(json.dumps({"voices": [{
                "id": "voice-mock",
                "display_name": "Mock Voice",
                "language": "en",
                "description": "Mock Voice",
                "gender": "m",
                "engine_name": "Qwen3 TTS",
                "engine_id": "qwen3-tts",
                "tts_model_id": "Qwen3-TTS-12Hz-0.6B-Base-4bit",
                "reference_path": str(clip),
                "reference_transcript": "one two three",
                "reference_segments": [
                    {"start": 0.0, "end": 4.0, "text": "one two"},
                    {"start": 5.0, "end": 11.0, "text": "three"},
                ],
                "duration_seconds": 11.0,
                "audio_sha256": "0" * 64,
            }]}))

            def voices_for(model_id: str) -> list:
                return req(client, "GET", "/v1/voices",
                           params={"model_id": model_id}).json()["voices"]

            cloned = voices_for("studio-hub::mock-hub/clone-model")
            preset = voices_for("studio-hub::mock-hub/preset-model")
            unknown = voices_for("studio-hub::mock-hub/uncatalogued-model")
            caps_only = voices_for("studio-hub::mock-hub/capability-only-model")
            check("a voice is offered for a model that declares cloning",
                  len(cloned) == 1, f"{len(cloned)}")
            check("and is withheld from a model that declares it takes no reference",
                  len(preset) == 0, f"{len(preset)}: {[v.get('id') for v in preset]}")
            check("a model the catalogue does not describe is unverified, not excluded",
                  len(unknown) == 1, f"{len(unknown)}")
            # A model the Hub catalogues with **no `reference_audio` block** states
            # its capabilities and does not name cloning there. The catalogue used
            # to answer `true` for it anyway (the per-kind default for a location)
            # while the picker answered `false`, so Story Studio offered a voice
            # picker with nothing in it. One read of the rule now answers both.
            check("a model whose only statement omits cloning is withheld",
                  len(caps_only) == 0, f"{len(caps_only)}: {[v.get('id') for v in caps_only]}")
            # Two different questions, deliberately answered differently. *Voices*
            # ask "does this clone?" and an uncatalogued model is unverified, so
            # its voices stay offered. *Publication* asks "is this a model at
            # all?" — and the answer there comes from the same catalogue, because
            # a location's package list cannot tell a codec from a model.

            # The publication follows the same statement, because a row claiming
            # cloning for a preset-speaker model offers a picker with nothing in it.
            published = {r["id"]: r for r in req(client, "GET", "/v1/tts/models").json()["models"]}
            check("the catalogue tells Story Studio the same thing",
                  published.get("studio-hub::mock-hub/clone-model", {})
                  .get("supports_voice_cloning") is True
                  and published.get("studio-hub::mock-hub/preset-model", {})
                  .get("supports_voice_cloning") is False,
                  f"{[(k, v.get('supports_voice_cloning')) for k, v in published.items() if 'mock-hub' in k]}")
            check("and the same thing for a model that only lists its capabilities",
                  published.get("studio-hub::mock-hub/capability-only-model", {})
                  .get("supports_voice_cloning") is False,
                  f"{[(k, v.get('supports_voice_cloning')) for k, v in published.items() if 'mock-hub' in k]}")
            # **One rule, two readers, and they must agree.** This is the shape the
            # duplicate implementation broke: the flag said "clones" and the picker
            # said nothing was there. Checked on the wire, for every location row.
            disagreements = []
            for row_id, row in published.items():
                if "mock-hub" not in row_id:
                    continue
                offered = len(voices_for(row_id))
                claims = row.get("supports_voice_cloning") is True
                if claims != (offered > 0):
                    disagreements.append((row_id, claims, offered))
            check("every location row's cloning claim matches its voice picker",
                  not disagreements, f"{disagreements}")
            check("a package the catalogue does not describe as a model is not published",
                  "studio-hub::mock-hub/uncatalogued-model" not in published,
                  f"{[k for k in published if 'mock-hub' in k]}")
            check("and a model the Hub says does not fit this machine is not published",
                  "studio-hub::mock-hub/toobig-model" not in published,
                  f"{[k for k in published if 'mock-hub' in k]}")
            check("a preset-voice model is still published — not cloning is not the same as not being a model",
                  "studio-hub::mock-hub/preset-model" in published,
                  f"{[k for k in published if 'mock-hub' in k]}")
            # The lane the adapter gained. A local video model is discovered, enabled
            # and **published on its own catalogue** — and what it publishes is the
            # contract the worker sent: its modes, its canvases and its durations, not
            # this gateway's flat 5s/10s@720p fallback. Video is deliberately absent
            # from `published` above because that dict is the TTS catalogue; a video
            # row appearing there would be the modality leak this check now pins.
            video_published = {
                m["id"]: m
                for m in httpx.get(f"{BASE}/v1/videos/models", headers=AUTH).json()["models"]
            }
            local_video = video_published.get("studio-hub::mock-hub/video-model")
            check("a local video lane publishes on the video catalogue",
                  local_video is not None,
                  f"{sorted(video_published)}")
            check("and it is not leaked into the voice catalogue",
                  "studio-hub::mock-hub/video-model" not in published,
                  f"{[k for k in published if 'mock-hub' in k]}")
            check("a video model the Hub says this machine cannot run is withheld",
                  "studio-hub::mock-hub/video-toobig-model" not in video_published,
                  f"{sorted(video_published)}")
            if local_video is not None:
                check("its modes are the lane's own statement, not a gateway guess",
                      local_video["modes"] == ["image-to-video", "text-to-video"]
                      and local_video["modes_source"] == "provider",
                      f"{local_video['modes']} {local_video['modes_source']}")
                check("its aspect ratios come from the contract's canvases, not its enums",
                      local_video["aspect_ratios"] == ["7:4", "4:7", "1:1", "11:7"],
                      f"{local_video['aspect_ratios']}")
                check("its variants are the published PAIRS, each carrying its own ratio",
                      {(v["duration_seconds"], v["resolution"], v.get("aspect_ratio"))
                       for v in local_video["variants"]}
                      == {(2, "256p", "7:4"), (4, "256p", "7:4"),
                          (2, "256p", "4:7"), (4, "256p", "4:7"),
                          (2, "448p", "1:1"), (4, "448p", "1:1"),
                          (2, "448p", "11:7")},
                      f"{local_video['variants']}")
                check("and no variant is offered for a pair the lane does not publish",
                      {(v["aspect_ratio"], v["resolution"]) for v in local_video["variants"]}
                      .issubset({("7:4", "256p"), ("4:7", "256p"), ("1:1", "448p"),
                                 ("11:7", "448p")}),
                      f"{[(v['aspect_ratio'], v['resolution']) for v in local_video['variants']]}")
                check("its audio claim is the worker's own measurement",
                      local_video["generates_audio"] is True,
                      f"{local_video['generates_audio']}")

            # ── the Hub chat lane, end to end ──────────────────────────────
            #
            # Hub 0.6.63 opened `POST /studio/chat/v1/chat/completions` to a Fleet
            # credential, which is what makes a Hub LLM dispatchable at all: the
            # gateway posts **through the Hub** (admission, machine lease and worker
            # signing all apply), not to a worker's own route and not to the Hub's
            # Hub-token-only spelling. `finish_reason` and `usage` travel as the
            # worker stated them, and the worker's own generation time is published
            # as `generation_seconds` — never as this gateway's round trip.
            hub_chat = "studio-hub::mock-hub/chat-model"
            chat_rows = req(client, "GET", "/v1/chat/models").json()["models"]
            chat_listed = {row["id"] for row in chat_rows}
            check("the Hub's chat model is offered once the lane is served",
                  hub_chat in chat_listed, str(sorted(i for i in chat_listed if "hub" in i)))
            hub_row = next((row for row in chat_rows if row["id"] == hub_chat), {})
            check("and it publishes the Hub's own limits, not a curated default",
                  hub_row.get("context_window_tokens") == 262144
                  and hub_row.get("context_window_tokens_source") == "provider",
                  f"{hub_row.get('context_window_tokens')} "
                  f"{hub_row.get('context_window_tokens_source')}")
            check("and it says the token counts are passed through, not re-measured",
                  hub_row.get("verified_token_usage") is False,
                  f"{hub_row.get('verified_token_usage')}")

            # The dispatch path itself is proved in
            # `tests/test_hub_chat_lane.py`, against the wire: a *custom* provider
            # of kind `studio_hub` is not a location on the fleet roster, so a job
            # sent to it cannot route — correctly, since locations come from the
            # roster. That file drives `StudioHubBackend.generate` directly and
            # asserts the path, the token and the body the mock received.

            # The header count used to be the raw enabled count, so it read "live"
            # for models the gate withholds. Two questions, two numbers, and the
            # withheld one is reported rather than dropped.
            header = httpx.get(f"{BASE}/health", timeout=30).json()["models"]
            # **Three counts, three questions.** `total_models` was labelled
            # "discovered" on the Overview tile while counting only models on
            # providers that are switched on, so turning a provider off made
            # "discovered" fall — the opposite of what the word means. And the walk
            # behind `publishable_models` was missing two of the catalogue's gates,
            # so a provider switched on with its key cleared inflated "offered".
            _m = req(client, "GET", "/admin/api/status").json()["models"]
            check("discovered is every model, not just the switched-on ones",
                  _m["discovered_models"] >= _m["total_models"]
                  and _m["total_models"] >= _m["enabled_models"] >= _m["publishable_models"],
                  f"{ {k: v for k, v in _m.items() if k != 'by_modality'} }")
            check("and offered plus withheld is exactly what is switched on",
                  _m["publishable_models"] + _m["withheld_models"] == _m["enabled_models"],
                  f"{_m['publishable_models']} + {_m['withheld_models']} != {_m['enabled_models']}")
            # Derived from `enabled_models`, the one function the catalogue is built
            # from, so the header and the catalogue cannot drift apart again.
            # **The gate that was missing.** `summary()` counted a provider that is
            # switched on with nothing configured: it has models, and it can offer
            # none of them. Proven by making every provider unconfigured — the old
            # walk never asked, so its counts stayed non-zero.
            from gateway.providers.registry import registry as _reg
            _real_configured = _reg.credentials.is_configured
            _reg.credentials.is_configured = lambda _pid: False
            try:
                _none = _reg.models.summary()
            finally:
                _reg.credentials.is_configured = _real_configured
            check("a provider that cannot serve contributes nothing to what is counted",
                  _none["total_models"] == 0 and _none["enabled_models"] == 0
                  and _none["publishable_models"] == 0
                  and _none["discovered_models"] > 0,
                  f"{ {k: v for k, v in _none.items() if k != 'by_modality'} }")

            _catalogue_tts = len(httpx.get(f"{BASE}/v1/tts/models", headers=AUTH).json()["models"])
            check("by_modality counts what the catalogue actually publishes",
                  _m["by_modality"].get("tts", 0) == _catalogue_tts,
                  f"header tts={_m['by_modality'].get('tts')} catalogue tts={_catalogue_tts}")

            check("the header separates offered from enabled and accounts for the difference",
                  header.get("publishable_models", -1) + header.get("withheld_models", -1)
                  == header.get("enabled_models")
                  and header.get("withheld_models", 0) >= 2,
                  f"{ {k: header.get(k) for k in ('enabled_models', 'publishable_models', 'withheld_models')} }")
            check("and by_modality counts what is offered, not what is switched on",
                  sum(header["by_modality"].values()) == header.get("publishable_models")
                  and sum(header["by_modality"].values()) != header.get("enabled_models"),
                  f"{header['by_modality']} sums to {sum(header['by_modality'].values())}, "
                  f"publishable={header.get('publishable_models')}, enabled={header.get('enabled_models')}")

            report = req(client, "GET",
                         "/admin/api/voices/studio-hub-local::voice-mock/reference").json()
            by_model = {m["model_id"]: m for m in report["models"]}
            stated = by_model.get("studio-hub::mock-hub/clone-model") or {}
            check("the reference report repeats the Hub's numbers, unrounded",
                  (stated.get("contract") or {}).get("maximum_duration_seconds") == 15
                  and (stated.get("contract") or {}).get("target_duration_seconds") == 8,
                  f"{stated.get('contract')}")
            check("it says a 11s recording is used whole under a 15s maximum",
                  "used whole" in str(stated.get("verdict")), f"{stated.get('verdict')}")
            check("it says which model takes no reference at all",
                  "takes no reference" in str(by_model.get(
                      "studio-hub::mock-hub/preset-model", {}).get("verdict")),
                  f"{by_model.get('studio-hub::mock-hub/preset-model')}")
            # The report covers what Story Studio can actually pick, so the two
            # packages the new gate withholds are absent from it. The "unverified"
            # verdict below still exists for a catalogued model that states no
            # contract at all — but such a model cannot reach the picker, so this
            # fixture no longer contains one.
            check("the report covers published models only",
                  "studio-hub::mock-hub/uncatalogued-model" not in by_model
                  and "studio-hub::mock-hub/toobig-model" not in by_model,
                  f"{sorted(by_model)}")
            stated_verdict = str((by_model.get("studio-hub::mock-hub/clone-model") or {}).get("verdict"))
            check("a stated maximum is reported as the Hub's own number",
                  "15" in stated_verdict, stated_verdict)
            check("it reports the clip's own measurements",
                  report["clip"]["duration_seconds"] == 11.0
                  and report["clip"]["segments"] == 2
                  and report["clip"]["has_transcript"] is True,
                  f"{report['clip']}")

            # ------------------------------------------------------------------
            print("\n\033[1m21. How a reference clip is read, and how it is reported\033[0m")
            # `_transcribe` asks the Hub for word timings, then reads the job's own
            # `metadata.segments` (the SRT artifact route cannot carry them at all).
            # Measured against the real Hub: whisper-base-mlx returns an empty
            # `words` list, so cue passthrough is today's real path and the word
            # expansion is what a future engine will use.
            from gateway.voice_import import _segments_from_metadata, _span

            hub_shape = {"metadata": {"segments": [
                {"id": 0, "start": 0.92, "end": 4.82, "text": "But at his feet, a small fox.", "words": []},
                {"id": 1, "start": 6.14, "end": 9.6, "text": "Its hind leg was bent.", "words": []},
            ]}}
            cues = _segments_from_metadata(hub_shape)
            check("cue-level segments pass through unchanged",
                  cues == [{"start": 0.92, "end": 4.82, "text": "But at his feet, a small fox."},
                           {"start": 6.14, "end": 9.6, "text": "Its hind leg was bent."}],
                  f"{cues}")

            wordy = {"metadata": {"segments": [{
                "start": 0.0, "end": 2.0, "text": "Hello there friend",
                "words": [{"word": "Hello", "start": 0.0, "end": 0.6},
                          {"word": "there", "start": 0.7, "end": 1.1},
                          {"word": "friend", "start": 1.2, "end": 2.0}]}]}}
            check("word timings are expanded, because window selection cuts on them",
                  [s["text"] for s in _segments_from_metadata(wordy)] == ["Hello", "there", "friend"],
                  f"{_segments_from_metadata(wordy)}")
            # All-or-nothing: one cue offering words while another offers none
            # would describe audio the other cue does not agree with.
            mixed = {"metadata": {"segments": [
                {"start": 0.0, "end": 1.0, "text": "one", "words": [{"word": "one", "start": 0, "end": 1}]},
                {"start": 1.0, "end": 2.0, "text": "two three", "words": []},
            ]}}
            check("a partial word list falls back to the cues",
                  [s["text"] for s in _segments_from_metadata(mixed)] == ["one", "two three"],
                  f"{_segments_from_metadata(mixed)}")

            check("a span that cannot be trusted is dropped, not sent to be refused",
                  _span("x", 1, "t") is None and _span(2, 1, "t") is None
                  and _span(0, 1, "   ") is None and _span(-1, 1, "t") is None,
                  f"{_span(2, 1, 't')} / {_span(0, 1, '   ')}")
            check("no metadata yields no segments rather than an exception",
                  _segments_from_metadata({}) == []
                  and _segments_from_metadata({"metadata": None}) == []
                  and _segments_from_metadata({"metadata": {"segments": "nonsense"}}) == [])

            # A voice with no clip at all must be told so. Computing a verdict per
            # model from a clip that does not exist answered "used whole" for every
            # one of them — a provider voice, described as though its recording had
            # been measured here.
            payload = json.loads((tmp / "voices.json").read_text())
            payload["voices"].append({
                "id": "voice-no-clip", "display_name": "No Clip Voice",
                "language": "en", "description": "No Clip Voice",
                "reference_transcript": "", "reference_segments": [],
            })
            (tmp / "voices.json").write_text(json.dumps(payload))
            bare = req(client, "GET",
                       "/admin/api/voices/studio-hub-local::voice-no-clip/reference").json()
            check("a voice with no clip reports that instead of a verdict per model",
                  bare["models"] == [] and bare["clip"]["present"] is False
                  and "no reference clip" in str(bare.get("note")),
                  f"{bare.get('note')} / models={len(bare['models'])}")
            check("and a voice with a clip still reports present",
                  req(client, "GET",
                      "/admin/api/voices/studio-hub-local::voice-mock/reference")
                  .json()["clip"]["present"] is True)

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
