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
                "modalities": ["tts"],
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
        ]
    }))

    env = {
        **os.environ,
        "GATEWAY_DATA": str(tmp),
        "GATEWAY_APP_SUPPORT": str(tmp),
        "GATEWAY_API_KEY": GW_KEY,
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
            # The stamp the app compares against its own. Without it a gateway
            # adopted from a previous build is indistinguishable from the current
            # one, which is how four rebuilds "took effect" without taking effect.
            check("health says which build is running",
                  health.get("build") == "test-build-0000", repr(health.get("build")))
            # Counted from the shipped list rather than hardcoded: adding a
            # provider is a legitimate change, and a literal here fails for a
            # reason that has nothing to do with what the check is about.
            from gateway.providers.definitions import PROVIDER_DEFINITIONS as _SHIPPED
            expected_providers = len(_SHIPPED) + 5  # + the five custom ones above
            check(f"{len(_SHIPPED)} built-in providers + 5 custom",
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
            check("model browser search finds them across providers",
                  listed["matched"] == len(found), f"matched {listed['matched']}")

            print("\n\033[1m3. Story Studio catalogue contract\033[0m")
            cat = req(client, "GET", "/v1/chat/models").json()
            check("chat catalogue non-empty once enabled", len(cat["models"]) > 0)
            check("catalogue envelope has freshness",
                  {"stale", "ageMs", "loadedAt"} <= set(cat["freshness"]))
            check("freshness not stale", cat["freshness"]["stale"] is False)
            check("freshness ageMs credible", cat["freshness"]["ageMs"] < 30 * 60 * 1000)
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
            from gateway.adapters.studio_hub import _reference_cache_key
            check("a staged reference is cached per location, not per voice",
                  _reference_cache_key("v1", "http://a:1")
                  != _reference_cache_key("v1", "http://b:1"),
                  "same key for two controllers")
            check("the same location reuses its own staged reference",
                  _reference_cache_key("v1", "http://a:1")
                  == _reference_cache_key("v1", "http://a:1/"),
                  "trailing slash changed the key")

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
            from gateway.catalog import _tts_characters, _TTS_CHARACTERS_DEFAULT
            from gateway.providers.definitions import PROVIDERS_BY_ID as _DEFS
            import dataclasses as _dc
            _local = _DEFS["studio-hub-local"]
            # `mock-remote` is a user-defined provider, so it lives in the temp
            # `providers.local.json` the gateway subprocess reads — not in this
            # process's static map. Only `.id` is read for the curated lookup, so a
            # relabelled copy is the honest stand-in.
            _mock = _dc.replace(_local, id="mock-remote")
            check("a provider that states its own limit is published with it",
                  _tts_characters("m", {"raw": {"maximum_text_length_per_request": 5000}},
                                  _mock) == 5000)
            check("the local lane gets the agreed 25,000 default",
                  _tts_characters("m", {"raw": {}}, _local) == 25000
                  == _TTS_CHARACTERS_DEFAULT)
            check("a provider's stated limit wins over the curated file",
                  _tts_characters("mock-tts-1", {"raw": {"maximum_text_length_per_request": 5000}},
                                  _mock) == 5000)
            check("the owner's curated limit is honoured where nobody stated one",
                  _tts_characters("mock-tts-plain", {"raw": {}}, _mock) == 12345,
                  f"{_tts_characters('mock-tts-plain', {'raw': {}}, _mock)}")
            check("the mock model's stated limit reaches the catalogue",
                  any(r.get("maximum_input_characters") == 5000
                      for r in tts_rows_now if "mock-tts-1" in str(r.get("id"))),
                  f"{[(r.get('id'), r.get('maximum_input_characters')) for r in tts_rows_now]}")
            check("and a curated limit reaches the catalogue on its own row",
                  any(r.get("maximum_input_characters") == 12345
                      for r in tts_rows_now if "mock-tts-plain" in str(r.get("id"))),
                  f"{[(r.get('id'), r.get('maximum_input_characters')) for r in tts_rows_now]}")
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
            total = before["total"]
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
                  after["total"] == total, f"{total} -> {after['total']}")

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
                  everything.status_code == 200 and everything.json()["matched"] == total,
                  everything.text[:160])

            # The console has to send that word, or the deliberate path is
            # unreachable from the app and the guard has broken the button it
            # was meant to protect.
            page = req(client, "GET", "/admin/").text
            check("the console says 'all' out loud when no filter is set",
                  "body.scope = 'all'" in page)

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
            account = req(client, "GET", "/v1/content/account").json()
            check("the content account publishes balance and empty collections",
                  isinstance(account.get("credit_balance"), int)
                  and account.get("entitlements") == [] and account.get("purchases") == [],
                  f"{account}")
            quote = req(client, "POST", "/v1/content/quotes",
                        json={"product_id": "mock-product"}).json()
            check("a content quote is zero-priced and explicit about ownership",
                  quote.get("credits") == 0 and quote.get("price_version") == "flat"
                  and quote.get("sufficient_credits") is True,
                  f"{quote}")
            purchase = req(client, "POST", "/v1/content/purchases",
                           json={"product_id": "mock-product"}).json()
            # The names are the contract's, not guessed: Story Studio refuses a
            # purchase payload missing any of `ZERO_PURCHASE`, and the credit
            # fields must be strict integers.
            required = ("credits_charged", "discount_credits", "balance_before",
                        "balance_after", "sufficient_credits", "discount", "receipt")
            check("a content purchase settles with every field the parser needs",
                  all(k in purchase for k in required)
                  and purchase["credits_charged"] == 0
                  and purchase["discount_credits"] == 0
                  and purchase["balance_before"] == 0
                  and purchase["balance_after"] == 0
                  and isinstance(purchase["credits_charged"], int)
                  and purchase["sufficient_credits"] is True
                  and purchase["state"] == "completed"
                  and purchase["product_id"] == "mock-product",
                  f"{purchase}")
            check("and receipts come back as an empty paginated list",
                  req(client, "GET", "/v1/content/receipts").json().get("total") == 0)

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
            check("and it is unlimited rather than pretending to expire",
                  permit.get("expires_at") is None, f"{permit.get('expires_at')}")
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
            # The rule has to reach the catalogue, not just the helper.
            published_stt = {m["id"] for m in
                             req(client, "GET", "/v1/transcription/models").json()["models"]}
            check("no published transcription row is one the gateway cannot read back",
                  all(_stt_ok(i.split("::", 1)[-1]) for i in published_stt),
                  f"{sorted(published_stt)}")

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
            # The full balanced zero set, not just one field. The encoder's own
            # docstring claimed "the price fields ride along" while publishing only
            # `credits_per_1000_characters` — and a second, dead voice encoder in
            # `catalog.py` had the same gap, so fixing one would not have fixed the
            # other. There is one encoder now; this pins what it must carry.
            check("every Story voice publishes the full price shape",
                  bool(story) and all(
                      v["credits_per_1000_characters"] == 0
                      and v["minimum_credits"] == 0
                      and v["price_version"] == "flat"
                      and v["chargeable"] is True
                      for v in story),
                  f"{[{k: v.get(k) for k in ('credits_per_1000_characters', 'minimum_credits', 'price_version', 'chargeable')} for v in story[:2]]}")
            check("and no voice is published that Story Studio's parser would drop",
                  all(v["catalog_status"] == "published" and v["quote_enabled"]
                      and v["generation_enabled"] for v in story))

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
            model_id = str(victim.get("tts_model_id") or "")
            check("a local voice reports the model it was minted against", bool(model_id), model_id)
            if model_id:
                scoped = req(client, "GET", f"/v1/voices?model_id=studio-hub-local::{model_id}").json()
                check("a local voice is returned for its own model",
                      len(scoped["voices"]) > 0, f"got {len(scoped['voices'])}")
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
            (picked / "Blue Room").mkdir(parents=True)
            write_wav(picked / "Blue Room" / "reference.wav")
            (picked / "Blue Room" / "metadata.json").write_text(
                json.dumps({"name": "Blue - Calm and Deep", "language": "en", "gender": "m"})
            )
            (picked / "Blue Room" / "transcript.txt").write_text("a blue reference")
            write_wav(picked / "Morgan.wav")
            (picked / "Morgan.txt").write_text("a morgan reference")
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
            copied = sorted(p.name for p in library.iterdir())
            check("the clips are copied into the gateway's own library",
                  copied == ["voice-blue-room", "voice-morgan"], f"{copied}")

            catalog = json.loads(Path(added["catalog"]).read_text())
            ours = [v for v in catalog["voices"] if v["id"] in {"voice-blue-room", "voice-morgan"}]
            check("the copy is where the catalogue says it is",
                  len(ours) == 2 and all(Path(v["reference_path"]).parent.parent == library
                                         for v in ours),
                  f"{[v.get('reference_path') for v in ours]}")
            ids = [v["id"] for v in catalog["voices"]]
            check("the catalogue keeps what was already there",
                  len(ids) > len(added["imported"]), f"{len(ids)} total")
            check("and has no duplicate ids", len(ids) == len(set(ids)), f"{ids}")
            stored = next(v for v in catalog["voices"] if v["id"] == "voice-morgan")
            check("the clip's own hash is stored for location matching",
                  len(str(stored.get("audio_sha256") or "")) == 64)
            # "Processed properly" means one self-contained folder — clip,
            # transcript, derived metadata — not a clip here and its meaning in a
            # catalogue somewhere else.
            files = {f.name for f in (library / "voice-morgan").iterdir()}
            check("a voice is a self-contained folder, not just a clip",
                  {"reference.wav", "metadata.json", "transcript.txt"} <= files, f"{sorted(files)}")

            again = req(client, "POST", "/admin/api/voices/add",
                        json={"folder": str(picked), "annotate": False}).json()
            after = json.loads(Path(added["catalog"]).read_text())
            check("re-adding the same folder updates rather than duplicates",
                  len(after["voices"]) == len(catalog["voices"]),
                  f"{len(catalog['voices'])} -> {len(after['voices'])}")
            check("and reports what it saw again", len(again["imported"]) == 2)

            # The folder that was picked is one the owner will empty later — a
            # Downloads folder, a temporary copy. The voice must not depend on it.
            kept = Path(stored["reference_path"])
            shutil.rmtree(picked)
            check("deleting the folder it came from leaves the voice intact",
                  kept.exists() and "voice-library" in str(kept), str(kept))
            check("and the catalogue still lists it",
                  any(v["id"] == "voice-morgan" for v in after["voices"]),
                  f"{len(after['voices'])} entries")

            # Segments come from Studio Hub's transcription, so with no Hub there is
            # nothing to transcribe with. Saying that is the whole point: silently
            # doing nothing would look identical to success. Checked directly rather
            # than over HTTP, because this suite runs a mock Hub — the one case that
            # cannot be produced through the API here is "there is no Hub at all".
            real_base_url = voice_import.hub_base_url
            voice_import.hub_base_url = lambda: ""
            guard = voice_import.start_annotation()
            voice_import.hub_base_url = real_base_url
            check("with no Hub it refuses to pretend the voices are usable",
                  guard.get("running") is False
                  and "Studio Hub is not running" in str(guard.get("error")),
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
                "id": "voice-morgan", "display_name": "Morgan", "language": "en",
                "gender": "f", "audio_sha256": "c" * 64,
                "reference_path": str(library / "voice-morgan" / "reference.wav"),
            }
            ready = voice_import.shared_readiness(unacknowledged)
            check("a voice with no acknowledgement is not ready to share",
                  not ready["ready"]
                  and any("acknowledged" in p for p in ready["problems"]),
                  f"{ready['problems']}")
            check("and its missing segments are named, because that breaks cloning",
                  any("segments" in p for p in ready["problems"]), f"{ready['problems']}")

            # The receiver enforces `voice_id must be exactly 12 lowercase hex
            # characters` (measured: 400 otherwise), so a gateway id like
            # `voice-morgan` cannot be pushed at all.
            shared_id = voice_import.shared_voice_id(
                {"id": "voice-morgan", "audio_sha256": "a" * 64}
            )
            check("the shared id is the 12 hex the receiver demands",
                  shared_id == "a" * 12, shared_id)
            check("an unhashed voice has no shareable id rather than a wrong one",
                  voice_import.shared_voice_id({"id": "voice-morgan"}) == "")

            fields = voice_import.push_fields({
                "id": "voice-morgan", "display_name": "Morgan", "language": "en",
                "gender": "f", "audio_sha256": "b" * 64, "license": "self-owned",
                "permission_acknowledged": True, "reference_transcript": "hi",
                "reference_path": str(library / "voice-morgan" / "reference.wav"),
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
                "reference_path": str(library / "voice-morgan" / "reference.wav"),
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
                 if v["id"] == "voice-morgan"), {}
            )
            check("a newly added voice is private until the owner says otherwise",
                  added_entry.get("permission_acknowledged") is False
                  and not added_entry.get("license"),
                  f"ack={added_entry.get('permission_acknowledged')} license={added_entry.get('license')}")
            check("and it is therefore not shareable",
                  not voice_import.shared_readiness(added_entry)["ready"])

            granted = req(client, "POST", "/admin/api/voices/shared",
                          json={"items": ["voice-morgan"], "acknowledged": True}).json()
            check("permission can be granted for one voice",
                  granted.get("changed") == 1, f"{granted}")
            after_grant = next(
                v for v in json.loads(Path(added["catalog"]).read_text())["voices"]
                if v["id"] == "voice-morgan"
            )
            check("granting records a license, which the Hub requires",
                  after_grant.get("license") == "self-owned"
                  and after_grant.get("permission_acknowledged") is True,
                  f"license={after_grant.get('license')}")
            # The folder carries it too, so the permission travels with the voice.
            meta = json.loads((library / "voice-morgan" / "metadata.json").read_text())
            check("the permission is written into the voice's own folder",
                  meta.get("permission_acknowledged") is True and meta.get("license") == "self-owned",
                  f"{meta}")
            check("and the gateway can list what is shareable",
                  [v["id"] for v in
                   req(client, "GET", "/admin/api/voices/shared").json()["voices"]] == ["voice-morgan"])

            revoked = req(client, "POST", "/admin/api/voices/shared",
                          json={"items": ["voice-morgan"], "acknowledged": False}).json()
            after_revoke = next(
                v for v in json.loads(Path(added["catalog"]).read_text())["voices"]
                if v["id"] == "voice-morgan"
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
                          json={"items": ["voice-morgan"]})
            check("a voice without permission cannot be shared, even when asked for",
                  refused.status_code == 403 and "not been allowed" in refused.text,
                  f"HTTP {refused.status_code} {refused.text[:140]}")

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
                if row["id"] == "voice-morgan":
                    row["reference_transcript"] = "a morgan reference"
                    row["reference_segments"] = [
                        {"start": 0.0, "end": 1.0, "text": "a morgan reference"}
                    ]
            catalog_path.write_text(json.dumps(payload, indent=2) + "\n")
            req(client, "POST", "/admin/api/voices/shared",
                json={"items": ["voice-morgan"], "acknowledged": True})
            shareable = json.loads(catalog_path.read_text())
            morgan = next(v for v in shareable["voices"] if v["id"] == "voice-morgan")
            check("the voice is now ready to share",
                  not voice_import.shared_readiness(morgan)["problems"],
                  f"{voice_import.shared_readiness(morgan)['problems']}")

            # A location whose *fleet* token is set but whose Hub token is not.
            ghost = req(client, "POST", "/admin/api/fleet/controllers",
                        json={"label": "Mock Hub", "url": mock,
                              "token": "mock-fleet-token"}).json()["controller"]
            ghost_id = ghost["id"]

            first = req(client, "POST", "/admin/api/voices/share",
                        json={"items": ["voice-morgan"]}).json()
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
                         json={"items": ["voice-morgan"]}).json()
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
            old = stage("old.mp4", 9)                   # over 7 days
            recent = stage("recent.mp4", 2)             # inside 7 days
            fresh = stage("fresh.mp4", 0.01)            # inside the protected hour

            got = req(client, "GET", "/admin/api/settings/retention").json()
            check("the defaults are the ones asked for: 7 days or 10 GB",
                  got["max_age_days"] == 7 and got["max_bytes"] == 10 * 1024**3,
                  f"{got['max_age_days']} days / {got['max_bytes']} bytes")
            check("and it reports what is actually stored",
                  got["usage"]["files"] >= 4, f"{got['usage']}")

            out = req(client, "POST", "/admin/api/artifacts/prune", json={}).json()
            check("pruning removes what is past the age limit",
                  not ancient.exists() and not old.exists(),
                  f"ancient={ancient.exists()} old={old.exists()}")
            check("and keeps what is inside it", recent.exists() and fresh.exists())
            check("a file newer than the protection window is never removed",
                  fresh.exists(), "the newest artifact must survive even an aggressive cap")
            check("it reports what it removed rather than only that it ran",
                  out["deleted"] == 2 and out["deleted_by_age"] == 2 and out["freed_bytes"] > 0,
                  f"{ {k: out[k] for k in ('deleted','deleted_by_age','deleted_by_size','freed_bytes')} }")

            # The size limit is the backstop, and it takes the oldest first.
            big = [stage(f"bulk-{i}.bin", age_days=3 - i * 0.5, size=900 * 1024) for i in range(3)]
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
                  len(installed) == 4,
                  f"{sorted(installed)} error="
                  f"{hub_payload.get('error') or hub_payload.get('discovery_error')}")
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
            check("a voice is offered for a model that declares cloning",
                  len(cloned) == 1, f"{len(cloned)}")
            check("and is withheld from a model that declares it takes no reference",
                  len(preset) == 0, f"{len(preset)}: {[v.get('id') for v in preset]}")
            check("a model the catalogue does not describe is unverified, not excluded",
                  len(unknown) == 1, f"{len(unknown)}")
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
            check("a package the catalogue does not describe as a model is not published",
                  "studio-hub::mock-hub/uncatalogued-model" not in published,
                  f"{[k for k in published if 'mock-hub' in k]}")
            check("and a model the Hub says does not fit this machine is not published",
                  "studio-hub::mock-hub/toobig-model" not in published,
                  f"{[k for k in published if 'mock-hub' in k]}")
            check("a preset-voice model is still published — not cloning is not the same as not being a model",
                  "studio-hub::mock-hub/preset-model" in published,
                  f"{[k for k in published if 'mock-hub' in k]}")

            # The header count used to be the raw enabled count, so it read "live"
            # for models the gate withholds. Two questions, two numbers, and the
            # withheld one is reported rather than dropped.
            header = httpx.get(f"{BASE}/health", timeout=30).json()["models"]
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
