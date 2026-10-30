#!/usr/bin/env python3
"""The SFX tab and the Providers page's grouping — the owner's ask, 2026-10-25.

The owner: *"make it a 'provider' but have it in a new tab called SFX or something. This way we can
manage them easier. And also make sure all the providers are organised properly too."*

**A sound source is not a model provider.** It contributes no catalogue row, so it must never
appear in `providers/definitions.py` — adding one there broke `test_e2e`'s "every provider has a
working adapter", which is that gate doing its job. It is, though, a *credential scope* like a
provider, and this file pins that it is managed like one without being one:

* a key can be **saved and cleared** through the same console protections the provider routes use
  (loopback or the gateway key, and a signed-in browser's write must name this origin);
* the secret is **never echoed**, logged, or in error text — the projection masks it, and every
  refusal names a field, never a value;
* the **search adapter picks the token up** from the store on the next call, with no restart;
* grouping the Providers page by what each provider serves covers **every** provider exactly once,
  files each by a lane it can actually *serve* rather than one it merely declares, and changes
  nothing about dispatch.

Run with ``.venv/bin/python tests/test_sfx_providers.py``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

# Before the first `import gateway.*`: without it every store binds the owner's real Application
# Support, and this file writes credentials. `sandbox_env` deliberately sets no API key, so this
# file picks its own — a key in the process environment is what makes a *remote* peer need it,
# which is the auth this file has to measure.
_SANDBOX = sandbox_env("gateway-sfx-")
KEY = "sfx-fixture-key"
os.environ.update({
    "GATEWAY_HOST": "0.0.0.0",          # bound beyond loopback, as over Tailscale
    "GATEWAY_API_KEY": KEY,
    "OPENVERSE_ALLOW_INSECURE_URLS": "1",  # the mock upstream is plain http on loopback
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import api  # noqa: E402
from gateway import admin_session  # noqa: E402
from gateway import sound_library  # noqa: E402
from gateway.providers import definitions, registry  # noqa: E402
from gateway import sound_sources  # noqa: E402

AUTH = {"Authorization": f"Bearer {KEY}"}
REMOTE = ("100.64.0.9", 50123)          # a Tailscale peer, never loopback
CLIP = {
    "id": "cc0-clip-1",
    "title": "CC0 Rain",
    "creator": "Someone",
    "license": "cc0",
    "license_version": "1.0",
    "foreign_landing_url": "https://openverse.org/audio/cc0-1",
    "url": "https://example.invalid/clip.mp3",
    "duration": 5000,
    "filesize": 100,
    "filetype": "mp3",
    "attribution": "Someone, CC0",
    "tags": [{"name": "rain"}, {"name": "ambient"}],
    "category": "sound_effect",
    "genres": [{"name": "field recording"}],
}


class MockOpenverse(BaseHTTPRequestHandler):
    """The smallest upstream that answers a search, and remembers what it was asked."""

    authorizations: list[str] = []

    def log_message(self, *_args):  # keep the test output clean
        pass

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # The quota headers the real API sends, so the SFX card's quota line is exercised.
        self.send_header("X-RateLimit-Limit-anon_burst", "20/min")
        self.send_header("X-RateLimit-Available-anon_burst", "19")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        MockOpenverse.authorizations.append(self.headers.get("Authorization", ""))
        if "?" in self.path:
            self._send({"results": [CLIP]})
        else:
            self._send(CLIP)


class SfxTestCase(unittest.TestCase):
    """Shared fixture: a mock upstream, a remote console client, an empty credential entry."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockOpenverse)  # port 0: no fixed port to clash with
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        port = cls.server.server_address[1]
        # The adapter reads the module global, so pointing it at the mock is one assignment and
        # needs no environment shared with another test process.
        cls._real_base = sound_sources._OPENVERSE_BASE
        sound_sources._OPENVERSE_BASE = f"http://127.0.0.1:{port}/v1"

    @classmethod
    def tearDownClass(cls):
        sound_sources._OPENVERSE_BASE = cls._real_base
        cls.server.shutdown()

    def setUp(self):
        registry.credentials.clear(self.store_id())
        # The adapter is a singleton and caches each query for a minute, so a test that wants a
        # live call must clear it — the same reset the sound-library suite makes.
        for source in sound_sources._INSTANCES.values():
            source._cache.clear()
            source.quota.scopes = {}
            source.quota.last_seen_at = None
            source.quota.exhausted = False
        MockOpenverse.authorizations = []
        self.remote = TestClient(api.app, client=REMOTE)

    @staticmethod
    def store_id() -> str:
        return sound_sources.definition("openverse").store_id

    def no_logs(self):
        """Capture every log record for the duration of a with-block."""
        return _CapturedLogs()


class _CapturedLogs:
    def __init__(self):
        self.records: list[str] = []

    def __enter__(self):
        outer = self

        class Handler(logging.Handler):
            def emit(self, record):
                try:
                    outer.records.append(record.getMessage())
                except Exception:  # a broken record must not break the test
                    outer.records.append(str(record.msg))

        self.handler = Handler()
        self.root = logging.getLogger()
        self.level = self.root.level
        self.root.addHandler(self.handler)
        self.root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *_exc):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self.level)
        return False

    @property
    def text(self) -> str:
        return "\n".join(self.records)


# ---------------------------------------------------------------------------
# 1. The credential route
# ---------------------------------------------------------------------------
class SfxCredentialRoute(SfxTestCase):
    SECRET = "{{SECRET_rqyyeopit}}"

    def test_a_remote_peer_must_present_the_key(self):
        """The protection every other admin write uses: 401 without a key, 403 with a wrong one."""
        self.assertEqual(self.remote.get("/admin/api/sounds/sources").status_code, 401)
        self.assertEqual(self.remote.post("/admin/api/sounds/sources/openverse/credentials",
                                          json={"values": {"access_token": "x"}}).status_code, 401)
        self.assertEqual(self.remote.delete(
            "/admin/api/sounds/sources/openverse/credentials").status_code, 401)
        self.assertEqual(self.remote.get("/admin/api/sounds/sources", headers=AUTH).status_code, 200)
        self.assertEqual(
            self.remote.post("/admin/api/sounds/sources/openverse/credentials",
                             headers={"Authorization": "Bearer not-the-key"},
                             json={"values": {"access_token": "x"}}).status_code, 403)
        self.assertEqual(registry.credentials.stored(self.store_id()), {},
                         "a refused write must store nothing")

    def test_a_signed_in_browser_write_must_come_from_this_origin(self):
        """CSRF, the same rule `require_admin` applies to every other console write."""
        admin_session.set_password("correct horse battery")
        same = {"Origin": "http://testserver"}
        try:
            browser = TestClient(api.app, client=REMOTE)
            login = browser.post("/admin/login", data={"password": "correct horse battery"},
                                 headers=same, follow_redirects=False)
            self.assertEqual(login.status_code, 303)
            # A signed-in browser can save a key — it is the owner's own console.
            self.assertEqual(
                browser.post("/admin/api/sounds/sources/openverse/credentials", headers=same,
                             json={"values": {"access_token": "{{SECRET_csrf}}"}}).status_code, 200)
            # A form on another site carrying that cookie cannot.
            cross = browser.post("/admin/api/sounds/sources/openverse/credentials",
                                 headers={"Origin": "http://evil.example"},
                                 json={"values": {"access_token": "{{SECRET_csrf2}}"}})
            self.assertEqual(cross.status_code, 403)
            self.assertEqual(registry.credentials.stored(self.store_id())["access_token"],
                             "{{SECRET_csrf}}")
        finally:
            admin_session.remove_password()
            admin_session.end_all_sessions()

    def test_saving_a_token_stores_it_and_masks_it(self):
        response = self.remote.post(
            "/admin/api/sounds/sources/openverse/credentials",
            headers=AUTH,
            json={"values": {"access_token": self.SECRET, "client_id": "cid-1234"}},
        )
        self.assertEqual(response.status_code, 200)
        source = response.json()["source"]
        self.assertTrue(source["registered"])
        self.assertTrue(source["configured"])
        fields = {f["key"]: f for f in source["credential_fields"]}
        self.assertTrue(fields["access_token"]["present"])
        self.assertEqual(fields["access_token"]["source"], "stored")
        # **Masked, never echoed.** The response is read over a phone; it may say a key is there
        # and which key it is, and never carry it.
        self.assertNotIn(self.SECRET, response.text)
        self.assertTrue(fields["access_token"]["masked"])
        # A non-secret field is shown as itself, the way a provider's base-url field is.
        self.assertEqual(fields["client_id"]["masked"], "cid-1234")
        # The one field the adapter sends is marked, so the card cannot imply all three matter.
        self.assertEqual([f["key"] for f in source["credential_fields"] if f["used"]],
                         ["access_token"])

        stored_path = Path(registry.credentials.path)
        stored = json.loads(stored_path.read_text())
        self.assertEqual(stored["providers"][self.store_id()]["values"]["access_token"], self.SECRET)
        mode = stat.S_IMODE(os.stat(stored_path).st_mode)
        self.assertEqual(mode, 0o600, "the credential file must stay owner-only")

    def test_the_listing_never_carries_the_secret(self):
        self.remote.post("/admin/api/sounds/sources/openverse/credentials", headers=AUTH,
                         json={"values": {"access_token": self.SECRET}})
        response = self.remote.get("/admin/api/sounds/sources", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.SECRET, response.text)
        sources = response.json()["sources"]
        self.assertEqual([s["id"] for s in sources], ["openverse"])
        source = sources[0]
        # What the card needs to draw itself, all of it declared by the source.
        self.assertEqual(source["display_name"], "Openverse")
        self.assertEqual(source["category"], "sfx")
        self.assertTrue(source["register_url"].startswith("http"))
        # Nothing has been searched yet, so there is no reading — and that is `last_seen_at`
        # null with no scopes, not a zero the card could print as "0 left".
        self.assertEqual(source["quota"]["scopes"], {})
        self.assertIsNone(source["quota"]["last_seen_at"])

    def test_clearing_removes_the_key_and_keeps_the_source_usable(self):
        self.remote.post("/admin/api/sounds/sources/openverse/credentials", headers=AUTH,
                         json={"values": {"access_token": self.SECRET}})
        response = self.remote.delete("/admin/api/sounds/sources/openverse/credentials", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        source = response.json()["source"]
        self.assertFalse(source["registered"])
        # **Anonymous is not unusable.** Clearing the key returns Openverse to its free quota; a
        # card that read "not configured" here would be claiming the lane is dead.
        self.assertTrue(source["configured"])
        self.assertEqual(registry.credentials.stored(self.store_id()), {})

    def test_an_empty_string_clears_one_field(self):
        self.remote.post("/admin/api/sounds/sources/openverse/credentials", headers=AUTH,
                         json={"values": {"access_token": self.SECRET}})
        response = self.remote.post("/admin/api/sounds/sources/openverse/credentials", headers=AUTH,
                                    json={"values": {"access_token": ""}})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["source"]["registered"])

    def test_an_unknown_source_is_a_404_and_a_bad_body_a_422(self):
        self.assertEqual(self.remote.post("/admin/api/sounds/sources/nobody/credentials",
                                          headers=AUTH, json={"values": {}}).status_code, 404)
        self.assertEqual(self.remote.delete("/admin/api/sounds/sources/nobody/credentials",
                                            headers=AUTH).status_code, 404)
        self.assertEqual(self.remote.post("/admin/api/sounds/sources/openverse/credentials",
                                          headers=AUTH, json={}).status_code, 422)

    def test_a_field_the_source_does_not_declare_is_refused_not_dropped(self):
        response = self.remote.post("/admin/api/sounds/sources/openverse/credentials", headers=AUTH,
                                    json={"values": {"api_key": self.SECRET}})
        self.assertEqual(response.status_code, 422)
        # Refused, and **nothing stored**: a typo must not answer "saved" while writing nothing,
        # and the refusal must name the field rather than the value.
        self.assertEqual(registry.credentials.stored(self.store_id()), {})
        self.assertIn("api_key", response.json()["detail"])
        self.assertNotIn(self.SECRET, response.text)

    def test_a_non_string_value_is_refused(self):
        # `str(None)` would store the word "None" and the next search would send `Bearer None`.
        response = self.remote.post("/admin/api/sounds/sources/openverse/credentials", headers=AUTH,
                                    json={"values": {"access_token": None}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(registry.credentials.stored(self.store_id()), {})

    def test_the_secret_appears_in_no_log_line(self):
        with self.no_logs() as captured:
            self.remote.post("/admin/api/sounds/sources/openverse/credentials", headers=AUTH,
                             json={"values": {"access_token": self.SECRET}})
            self.remote.get("/admin/api/sounds/sources", headers=AUTH)
            sound_sources.source_for("openverse", registry.credentials).search(
                query="rain", min_sec=None, max_sec=None, category=None, page_size=5)
            self.remote.delete("/admin/api/sounds/sources/openverse/credentials", headers=AUTH)
        self.assertNotIn(self.SECRET, captured.text)
        # The capture is real, or the assertion above proves nothing.
        self.assertTrue(captured.records, "no log records were captured; the check is vacuous")


# ---------------------------------------------------------------------------
# 2. The adapter reads what the tab saved
# ---------------------------------------------------------------------------
class SfxTokenReachesTheAdapter(SfxTestCase):
    def test_a_saved_token_is_sent_on_the_next_search(self):
        token = "{{SECRET_wwqyyeopit}}"
        source = sound_sources.source_for("openverse", registry.credentials)
        source.search(query="before", min_sec=None, max_sec=None, category=None, page_size=5)
        self.assertEqual(MockOpenverse.authorizations[-1], "",
                         "an unregistered source must search anonymously")

        self.remote.post("/admin/api/sounds/sources/openverse/credentials", headers=AUTH,
                         json={"values": {"access_token": token}})
        # No restart, no reload: the adapter resolves the field on every call.
        source.search(query="after", min_sec=None, max_sec=None, category=None, page_size=5)
        self.assertEqual(MockOpenverse.authorizations[-1], f"Bearer {token}")
        self.assertTrue(source.registered())

        self.remote.delete("/admin/api/sounds/sources/openverse/credentials", headers=AUTH)
        source.search(query="cleared", min_sec=None, max_sec=None, category=None, page_size=5)
        self.assertEqual(MockOpenverse.authorizations[-1], "",
                         "clearing the key must stop the token being sent")

    def test_the_search_route_carries_the_token_end_to_end(self):
        token = "{{SECRET_zzyyeopit}}"
        self.remote.post("/admin/api/sounds/sources/openverse/credentials", headers=AUTH,
                         json={"values": {"access_token": token}})
        response = self.remote.get("/v1/sounds/search?q=rain", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MockOpenverse.authorizations[-1], f"Bearer {token}")
        self.assertEqual([r["source_id"] for r in response.json()["results"]], ["cc0-clip-1"])
        # The upstream's own quota headers are recorded, which is what the SFX card prints.
        quota = self.remote.get("/admin/api/sounds/sources", headers=AUTH).json()["sources"][0]["quota"]
        self.assertEqual(quota["scopes"]["anon_burst"]["available"], "19")
        self.assertIsNotNone(quota["last_seen_at"])

    def test_the_search_response_carries_tags_category_and_genres(self):
        """Openverse's own description reaches Story, not just the title and licence."""
        row = self.remote.get("/v1/sounds/search?q=rain", headers=AUTH).json()["results"][0]
        self.assertEqual(row["tags"], ["rain", "ambient"])
        self.assertEqual(row["category"], "sound_effect")
        self.assertEqual(row["genres"], ["field recording"])

    def test_a_key_from_the_env_file_works_without_the_console(self):
        # `.env` is the documented no-UI key source for every provider; a sound source honours it
        # through the same chain, so a key dropped in a file is not silently ignored here.
        os.environ["OPENVERSE_ACCESS_TOKEN"] = "{{SECRET_envtoken}}"
        try:
            source = sound_sources.source_for("openverse", registry.credentials)
            self.assertTrue(source.registered())
            source.search(query="env", min_sec=None, max_sec=None, category=None, page_size=5)
            self.assertEqual(MockOpenverse.authorizations[-1], "Bearer {{SECRET_envtoken}}")
        finally:
            del os.environ["OPENVERSE_ACCESS_TOKEN"]


# ---------------------------------------------------------------------------
# 3. Serving a library clip's bytes
# ---------------------------------------------------------------------------
class SoundClipBytes(SfxTestCase):
    """`GET /v1/sounds/library/{sha256}/audio` — the only way another Mac gets a clip.

    The library holds the bytes and the sidecar names the licence; this route carries the
    bytes so Story on a second Mac can place a clip the Gateway already downloaded.
    """

    AUDIO = b"\xff\xfb\x90\x00a library clip"

    def setUp(self):
        super().setUp()
        self.sha = hashlib.sha256(self.AUDIO).hexdigest()
        lib = sound_library._library_dir()
        (lib / f"{self.sha}.mp3").write_bytes(self.AUDIO)
        (lib / f"{self.sha}.json").write_text(json.dumps({
            "title": "Clip", "author": "Someone", "licence": "cc0",
            "licence_version": "1.0", "source": "openverse",
        }))
        sound_library._write_index([self.sha])

    @staticmethod
    def url(sha: str) -> str:
        return f"/v1/sounds/library/{sha}/audio"

    def test_serves_the_stored_bytes_with_their_media_type(self):
        response = self.remote.get(self.url(self.sha), headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.AUDIO)
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(int(response.headers["content-length"]), len(self.AUDIO))

    def test_an_unknown_clip_is_404(self):
        response = self.remote.get(self.url("0" * 64), headers=AUTH)
        self.assertEqual(response.status_code, 404)

    def test_a_malformed_id_is_refused_before_the_disk(self):
        for bad in ("not-a-sha", "0" * 63, "0" * 65, "g" * 64):
            self.assertEqual(self.remote.get(self.url(bad), headers=AUTH).status_code, 400,
                             f"{bad!r} must be refused as a clip id")

    def test_a_remote_peer_must_present_the_key(self):
        self.assertEqual(self.remote.get(self.url(self.sha)).status_code, 401)


# ---------------------------------------------------------------------------
# 4. Grouping the Providers page — display only, covering everything
# ---------------------------------------------------------------------------
class ProviderGrouping(SfxTestCase):
    def payload(self, **params):
        response = self.remote.get("/admin/api/providers", headers=AUTH, params=params)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_every_provider_is_in_exactly_one_group(self):
        data = self.payload()
        ids = [p["id"] for p in data["providers"]]
        grouped = [pid for group in data["service_groups"] for pid in group["ids"]]
        self.assertEqual(sorted(grouped), sorted(ids), "a provider is missing from, or repeated in, the page's groups")
        self.assertEqual(len(grouped), len(set(grouped)), "a provider is filed under two headings")

    def test_grouping_covers_the_cloud_scope_the_console_reads(self):
        data = self.payload(scope="cloud")
        ids = [p["id"] for p in data["providers"]]
        grouped = [pid for group in data["service_groups"] for pid in group["ids"]]
        self.assertEqual(sorted(grouped), sorted(ids))
        self.assertTrue(ids, "the console's own read returned nothing")
        self.assertNotIn("studio-hub-local", ids,
                         "a local runner is managed on the Fleet page, not in this list")

    def test_nothing_is_dropped_or_reordered_in_dispatch(self):
        data = self.payload()
        every = {d.id for d in registry.definitions()}
        self.assertEqual({p["id"] for p in data["providers"]}, every,
                         "the console's list must hold every provider the registry knows")
        # The same invariant `test_e2e` asserts: grouping must not hide a provider without an
        # adapter, and nothing about who can run may change because a heading exists.
        self.assertTrue(all(p["generation_supported"] for p in data["providers"]))

    def test_the_headings_are_in_a_fixed_order_with_stable_names(self):
        data = self.payload()
        keys = [group["key"] for group in data["service_groups"]]
        order = list(definitions.service_group_keys())
        self.assertEqual(keys, [k for k in order if k in keys],
                         "headings must follow the catalogue's own modality order")
        labels = {group["key"]: group["label"] for group in data["service_groups"]}
        self.assertEqual(labels.get("tts"), "Voice & TTS")
        self.assertEqual(labels.get("chat"), "Text & chat")
        # Every row's own label agrees with the heading it was filed under.
        for group in data["service_groups"]:
            for provider in data["providers"]:
                if provider["id"] in group["ids"]:
                    self.assertEqual(provider["service_group"], group["key"])
                    self.assertEqual(provider["service_group_label"], group["label"])

    def test_a_provider_is_filed_by_a_lane_it_can_serve(self):
        # Kie.ai declares chat and has no adapter for it (`unserved_modalities` says so), so a
        # card filed under "Text & chat" would be grouped by a lane that never answers.
        data = self.payload()
        kie = next(p for p in data["providers"] if p["id"] == "kie-ai-images")
        self.assertIn("chat", kie["unserved_modalities"])
        self.assertEqual(kie["service_group"], "image")
        # And the file-a-by-declared-order rule is not how it is computed: the same set typed in
        # any order lands under one heading.
        self.assertEqual(definitions.service_group(["video", "image"]), "image")
        self.assertEqual(definitions.service_group(["chat", "music"]), "chat")
        self.assertEqual(definitions.service_group(["music"]), "music")
        self.assertEqual(definitions.service_group([]), definitions.UNGROUPED_KEY)
        self.assertEqual(definitions.service_group(None), definitions.UNGROUPED_KEY)

    def test_a_provider_no_group_names_is_still_on_the_page(self):
        # The console appends rows the grouping did not name rather than dropping them; this is the
        # same rule one layer down, so a row with no group key cannot vanish from the payload.
        rows = [{"id": "alpha", "service_group": "chat"},
                {"id": "beta", "service_group": "image"},
                {"id": "gamma", "service_group": None}]
        groups = definitions.group_by_service(rows)
        grouped = [row for group in groups for row in group["ids"]]
        self.assertEqual(sorted(grouped), ["alpha", "beta", "gamma"])
        self.assertEqual([g["key"] for g in groups], ["chat", "image", definitions.UNGROUPED_KEY])


if __name__ == "__main__":
    unittest.main()
