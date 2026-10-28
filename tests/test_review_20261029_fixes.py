#!/usr/bin/env python3
"""The 2026-10-29 re-review: three findings, plus the two voice-library additions.

Each test is written to fail on the code as it was reviewed:

1. `/admin/login` accepted the gym gateway key as the password and minted an **owner**
   session, defeating `require_owner`'s own rule (a browser may be the owner, a key holder
   may not). The form takes the browser password only; the key stays a bearer token.
2. The upload size check ran *after* Starlette had parsed (and spooled) the whole multipart
   body, and a request declaring a huge `Content-Length` but sending little hung. The guard
   is now a dependency — before the parse — that refuses a declared oversize on the header
   and counts a chunked body as it arrives.
3. The same unbounded read existed on `/v1/audio/transcriptions/quotes`.
4. Reserved guest names are compared after NFKC and a lookalike fold, so `Оwner` (Cyrillic
   О) and `0wner` cannot read as the owner.

Plus the owner's two additions: a local voice's `gender` (m/f/n) through
`/api/voices/settings` and into the picker, and annotation that can use any reachable Hub in
the fleet, whose failure never erases good segments.

Body tests go **through the HTTP client** — the whole ASGI stack, including the guard —
never by calling a handler directly.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-review2-")
BASE = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(BASE), "GATEWAY_DATA": str(BASE / "data"),
    "GATEWAY_ARTIFACTS": str(BASE / "artifacts"), "GATEWAY_DB": str(BASE / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(BASE / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(BASE / "models.json"),
    "GATEWAY_ENV_FILE": str(BASE / ".env"), "GATEWAY_FLEET": str(BASE / "fleet.json"),
    "GATEWAY_VOICES": str(BASE / "voices.json"),
    "GATEWAY_VOICES_OUT": str(BASE / "voices.json"),
    "STUDIOHUB_READY_FILE": str(BASE / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "review-key",
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin, admin_session, api, voice_import  # noqa: E402
from gateway.runtime import store  # noqa: E402

FRIEND = ("100.100.1.1", 5555)
ORIGIN = {"Origin": "http://testserver"}
PASSWORD = "correct horse battery"


def fresh() -> None:
    with admin_session._lock:
        admin_session._write(admin_session._guests_path(), {"guests": []})
        admin_session._loaded().clear()
        admin_session._write(admin_session._sessions_path(), {})
    admin_session._failures.clear()
    try:
        admin_session._password_path().unlink()
    except FileNotFoundError:
        pass


def guest(name: str = "Maria", *, paid: bool = False):
    row = admin_session.add_guest(name, PASSWORD, paid_allowed=paid)
    token = admin_session.create_session(role="guest", guest_id=row["id"], name=row["name"])
    return row, {admin_session.COOKIE: token}


# ---------------------------------------------------------------------------
# 1. The login form takes the browser password, never the gateway key.
# ---------------------------------------------------------------------------
class LoginTakesThePasswordOnly(unittest.TestCase):
    def setUp(self) -> None:
        fresh()
        self.addCleanup(fresh)
        self.client = TestClient(api.app, client=FRIEND, follow_redirects=False)
        self.addCleanup(self.client.close)

    def sign_in(self, password: str, name: str = ""):
        body = f"username={name}&password={password}".encode()
        return self.client.post(
            "/admin/login",
            headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
            content=body)

    def test_the_gateway_key_is_not_a_browser_password(self) -> None:
        admin_session.set_password(PASSWORD)
        response = self.sign_in(os.environ["GATEWAY_API_KEY"])
        self.assertEqual(response.status_code, 401, response.text)
        self.assertNotIn(admin_session.COOKIE, response.cookies,
                         "the key must never mint a session")

    def test_the_owner_password_still_signs_in(self) -> None:
        admin_session.set_password(PASSWORD)
        response = self.sign_in(PASSWORD)
        self.assertEqual(response.status_code, 303)
        token = response.cookies.get(admin_session.COOKIE)
        self.assertTrue(token)
        self.assertEqual(admin_session.session(token)["role"], "owner")

    def test_with_no_password_set_the_page_refuses_and_says_what_to_do(self) -> None:
        # The key used to be the fallback here, and that is exactly how it minted an owner
        # session. Now there is nothing to sign in with, and the page says so in a sentence
        # that names the real door.
        response = self.sign_in(os.environ["GATEWAY_API_KEY"])
        self.assertEqual(response.status_code, 403)
        self.assertIn("Browser password", response.text)
        self.assertIn("bearer token", response.text)
        self.assertNotIn(admin_session.COOKIE, response.cookies)

    def test_a_guests_name_and_password_still_work(self) -> None:
        guest("Maria")
        response = self.sign_in(PASSWORD, name="Maria")
        self.assertEqual(response.status_code, 303)
        token = response.cookies.get(admin_session.COOKIE)
        self.assertEqual(admin_session.session(token)["role"], "guest")


# ---------------------------------------------------------------------------
# 2. An oversize upload is refused while it is still a stream.
# ---------------------------------------------------------------------------
class UploadGuard(unittest.TestCase):
    """Through the client: the guard is a dependency, so the form parse never sees the body."""

    def setUp(self) -> None:
        fresh()
        self.addCleanup(fresh)
        self.row, self.cookies = guest("Maria")
        self.client = TestClient(api.app, client=FRIEND)
        self.addCleanup(self.client.close)

    BOUNDARY = "----reviewboundary"

    def chunked(self, chunks: int, size: int = 8192, *, field: str = "model_id"):
        """A real multipart body, generated page by page.

        A sync generator is sent as `Transfer-Encoding: chunked` (no declared length), and
        the envelope matters: without `Content-Type: multipart/form-data` FastAPI never
        parses a form, so the body is never read and the guard has nothing to bound. That
        difference is exactly why this test failed when it was first written.
        """
        head = (f'--{self.BOUNDARY}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n'
                f'whisper-1\r\n--{self.BOUNDARY}\r\nContent-Disposition: form-data; '
                f'name="file"; filename="big.bin"\r\nContent-Type: application/octet-stream'
                f'\r\n\r\n').encode()

        def produce():
            yield head
            for _ in range(chunks):
                yield b"x" * size
            yield f"\r\n--{self.BOUNDARY}--\r\n".encode()
        return produce()

    def multipart_headers(self, extra: dict | None = None) -> dict:
        return {**(extra or {}),
                "Content-Type": f"multipart/form-data; boundary={self.BOUNDARY}"}

    def test_a_chunked_body_is_cut_at_the_ceiling(self) -> None:
        with patch.object(api, "_MAX_UPLOAD_BODY", 16 * 1024):
            response = self.client.post("/admin/api/create/transcription", cookies=self.cookies,
                                        headers={**ORIGIN, **self.multipart_headers()},
                                        content=self.chunked(chunks=64))
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_TOO_LARGE")
        self.assertIn("MB", response.json()["detail"])

    def test_a_declared_oversize_is_refused_on_the_header_alone(self) -> None:
        # Nothing is streamed: the answer comes from `Content-Length`, so a caller that
        # declares 512 MB and sends a byte does not hang the request.
        with patch.object(api, "_MAX_UPLOAD_BODY", 16 * 1024):
            response = self.client.post(
                "/admin/api/create/transcription", cookies=self.cookies,
                headers={**ORIGIN, "Content-Length": str(512 * 1024 * 1024)}, content=b"x")
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_TOO_LARGE")

    def test_a_body_under_the_ceiling_still_reaches_the_route(self) -> None:
        # The guard must not be a wall: a small upload is answered by the route (which then
        # refuses the empty model or the paid lane — anything but 413).
        with patch.object(api, "_MAX_UPLOAD_BODY", 4 * 1024 * 1024):
            response = self.client.post(
                "/admin/api/create/transcription", cookies=self.cookies, headers=ORIGIN,
                files={"file": ("a.wav", b"RIFF" + b"\x00" * 64, "audio/wav")},
                data={"model_id": "no-such-model", "idempotency_key": "small-1"})
        self.assertNotEqual(response.status_code, 413, response.text)
        self.assertNotEqual(response.status_code, 500, response.text)

    def test_a_chunked_body_under_the_ceiling_is_replayed_intact(self) -> None:
        # The guard spools and replays a body with no declared length. A replay that lost or
        # mangled the bytes would break every legitimate chunked upload, so the route must
        # see the same body it would have without the guard — here, a real multipart upload
        # that gets as far as the catalogue and is refused for the model, never for its body.
        with patch.object(api, "_MAX_UPLOAD_BODY", 4 * 1024 * 1024):
            response = self.client.post(
                "/admin/api/create/transcription", cookies=self.cookies,
                headers={**ORIGIN, **self.multipart_headers()},
                content=self.chunked(chunks=1, size=256))
        self.assertNotEqual(response.status_code, 413, response.text)
        self.assertNotEqual(response.status_code, 500, response.text)

    def test_the_v1_quote_door_is_guarded_the_same_way(self) -> None:
        headers = {**ORIGIN, "Authorization": f"Bearer {os.environ['GATEWAY_API_KEY']}"}
        with patch.object(api, "_MAX_UPLOAD_BODY", 16 * 1024):
            chunked = self.client.post("/v1/audio/transcriptions/quotes",
                                       headers={**headers, **self.multipart_headers()},
                                       content=self.chunked(chunks=64, field="model"))
            declared = self.client.post(
                "/v1/audio/transcriptions/quotes",
                headers={**headers, "Content-Length": str(512 * 1024 * 1024)}, content=b"x")
        self.assertEqual(chunked.status_code, 413, chunked.text)
        self.assertEqual(declared.status_code, 413, declared.text)
        self.assertEqual(chunked.json()["code"], "UPLOAD_TOO_LARGE")

    def test_the_v1_quote_door_still_takes_a_small_upload(self) -> None:
        headers = {"Authorization": f"Bearer {os.environ['GATEWAY_API_KEY']}"}
        with patch.object(api, "_MAX_UPLOAD_BODY", 4 * 1024 * 1024):
            response = self.client.post(
                "/v1/audio/transcriptions/quotes", headers=headers,
                files={"file": ("a.mp3", b"ID3" + b"\x00" * 32, "audio/mpeg")},
                data={"model": "whisper-1"}, params={"idempotency_key": "quote-small"})
        self.assertNotEqual(response.status_code, 413, response.text)


# ---------------------------------------------------------------------------
# 4. Reserved names survive a lookalike.
# ---------------------------------------------------------------------------
class ConfusableNames(unittest.TestCase):
    def setUp(self) -> None:
        fresh()
        self.addCleanup(fresh)

    def test_lookalike_spellings_of_a_reserved_name_are_refused(self) -> None:
        for name in ("Owner", "Оwner", "Ownеr", "0wner", "０wner", "Ｇuest",
                     "Ｔhis Mac", "Story Studiо"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    admin_session.add_guest(name, PASSWORD)

    def test_two_guests_cannot_differ_only_by_a_lookalike(self) -> None:
        admin_session.add_guest("Maria", PASSWORD)
        with self.assertRaises(ValueError):
            admin_session.add_guest("Mаria", PASSWORD)  # Cyrillic а

    def test_an_ordinary_name_is_stored_normalised_and_accepted(self) -> None:
        self.assertEqual(admin_session.add_guest("Ｍaria", PASSWORD)["name"], "Maria",
                         "NFKC folds the fullwidth letter, so one name has one spelling")


# ---------------------------------------------------------------------------
# 5. A local voice's gender, through the settings route and into the picker.
# ---------------------------------------------------------------------------
class VoiceGender(unittest.TestCase):
    def setUp(self) -> None:
        fresh()
        self.addCleanup(fresh)
        self.voice = {"id": "voice-0616aec33b41", "display_name": "Christian",
                      "language": "en", "engine_name": "Qwen3 TTS"}
        path = Path(os.environ["GATEWAY_VOICES_OUT"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"voices": [dict(self.voice)]}))
        self.client = TestClient(api.app, client=("127.0.0.1", 1))
        self.addCleanup(self.client.close)

    def sent(self, **payload) -> dict:
        response = self.client.post("/admin/api/voices/settings",
                                    json={"voice_id": self.voice["id"], **payload})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def stored(self) -> dict:
        rows = json.loads(Path(os.environ["GATEWAY_VOICES_OUT"]).read_text())["voices"]
        return next(row for row in rows if row["id"] == self.voice["id"])

    def test_the_settings_route_takes_m_f_or_n_and_stores_it(self) -> None:
        for value, wanted in (("m", "m"), ("female", "f"), ("n", "n")):
            with self.subTest(value=value):
                self.assertEqual(self.sent(gender=value)["gender"], wanted)
                self.assertEqual(self.stored().get("gender"), wanted)

    def test_blank_clears_it_and_anything_else_is_refused(self) -> None:
        self.sent(gender="f")
        self.assertEqual(self.sent(gender="")["gender"], "")
        self.assertNotIn("gender", self.stored(), "not stated is nothing, never 'neutral'")
        response = self.client.post("/admin/api/voices/settings",
                                    json={"voice_id": self.voice["id"], "gender": "robot"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("m, f or n", response.json()["detail"])

    def test_the_picker_gets_it_in_the_row_it_already_reads(self) -> None:
        self.sent(gender="m")
        from gateway import voices as voices_module

        rows = voices_module.voices_for_model("studio-hub::anything")
        row = next((r for r in rows if r["id"] == "studio-hub-local::voice-0616aec33b41"), None)
        self.assertIsNotNone(row, "the voice must be offered for a cloning model")
        self.assertEqual(row["gender"], "m")


# ---------------------------------------------------------------------------
# 6. Annotation reaches any Hub in the fleet, and a failure never erases segments.
# ---------------------------------------------------------------------------
class AnnotationAcrossTheFleet(unittest.TestCase):
    """A Gateway whose own Hub cannot transcribe used to be unable to annotate at all.

    `start_annotation` gated on the *local* probe, and `_run_annotation` posted to the local
    base URL and nothing else — so on 2026-10-28, with every transcription Mac behind this
    Hub paused (NO_AVAILABLE_MACHINE), a voice added here could never get the aligned
    segments that make it shareable. Other Hubs in the fleet had a worker free.
    """

    def setUp(self) -> None:
        fresh()
        self.addCleanup(fresh)
        self.clip = Path(os.environ["GATEWAY_ARTIFACTS"]) / "clip.wav"
        self.clip.parent.mkdir(parents=True, exist_ok=True)
        self.clip.write_bytes(b"RIFF" + b"\x00" * 64)
        self.voice = {
            "id": "voice-0616aec33b41", "display_name": "Christian", "language": "en",
            "engine_name": "Qwen3 TTS", "reference_path": str(self.clip),
            "include_reference_transcript": True,
            "reference_segments": [{"start": 0.0, "end": 1.5, "text": "good words"}],
        }
        Path(os.environ["GATEWAY_VOICES_OUT"]).write_text(json.dumps({"voices": [dict(self.voice)]}))
        self.pending = [dict(self.voice)]

    def stored(self) -> dict:
        rows = json.loads(Path(os.environ["GATEWAY_VOICES_OUT"]).read_text())["voices"]
        return rows[0]

    def targets(self) -> list[dict]:
        return [
            {"label": "this Mac", "base": "http://local", "headers": {"X-Hub-Token": "op"}},
            {"label": "Mac Studio", "base": "http://studio", "headers": {"X-Hub-Token": "tok"}},
        ]

    def test_the_local_hub_being_down_is_not_the_end_of_it(self) -> None:
        fake_location = {"id": "abc123", "label": "Mac Studio", "url": "http://studio",
                         "local": False, "reachable": True}
        with patch.object(voice_import, "hub_probe", lambda **k: {"running": False, "detail": "down"}), \
                patch.object(voice_import, "hub_base_url", lambda: "http://local"), \
                patch("gateway.fleet.locations", lambda: [fake_location]), \
                patch("gateway.fleet.admin_token_for", lambda cid: "tok"):
            targets = voice_import.annotation_targets()
        self.assertEqual([t["label"] for t in targets], ["Mac Studio"])
        self.assertEqual(targets[0]["headers"], {"X-Hub-Token": "tok"},
                         "the location's own Hub token, never this Mac's operator token")

    def test_a_location_without_a_token_is_not_asked(self) -> None:
        fake_location = {"id": "abc123", "label": "Mac Studio", "url": "http://studio",
                         "local": False, "reachable": True}
        with patch.object(voice_import, "hub_probe", lambda **k: {"running": False}), \
                patch.object(voice_import, "hub_base_url", lambda: "http://local"), \
                patch("gateway.fleet.locations", lambda: [fake_location]), \
                patch("gateway.fleet.admin_token_for", lambda cid: ""):
            self.assertEqual(voice_import.annotation_targets(), [])

    def test_a_later_hub_transcribes_and_says_which_one(self) -> None:
        calls: list[str] = []

        def transcribe(client, base, headers, audio, model):
            calls.append(base)
            if base == "http://local":
                raise RuntimeError("NO_AVAILABLE_MACHINE")
            return [{"start": 0.0, "end": 2.0, "text": "hello"}]

        with patch.object(voice_import, "_transcribe", transcribe):
            voice_import._run_annotation(self.pending, "whisper-1", self.targets())
        self.assertEqual(calls, ["http://local", "http://studio"])
        self.assertEqual(self.stored()["reference_segments"],
                         [{"start": 0.0, "end": 2.0, "text": "hello"}])
        result = voice_import.annotation_state()["results"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["location"], "Mac Studio")

    def test_a_forced_re_annotation_that_fails_never_erases_good_segments(self) -> None:
        # The owner re-annotates a voice that already has good segments and every Hub is
        # away. **The segments on disk must survive** — the previous code wrote the voice
        # dict back unconditionally, which only spared them because the dict it read already
        # held them. This pins the guarantee rather than the accident.
        def transcribe(client, base, headers, audio, model):
            raise RuntimeError("NO_AVAILABLE_MACHINE")

        before = self.stored()["reference_segments"]
        with patch.object(voice_import, "_transcribe", transcribe):
            voice_import._run_annotation(self.pending, "whisper-1", self.targets())
        self.assertEqual(self.stored()["reference_segments"], before)
        self.assertFalse(voice_import.annotation_state()["results"][0]["ok"])

    def test_a_clipless_voice_is_reported_and_its_segments_left_alone(self) -> None:
        self.pending[0]["reference_path"] = str(self.clip) + ".gone"
        with patch.object(voice_import, "_transcribe", lambda *a, **k: self.fail("must not call")):
            voice_import._run_annotation(self.pending, "whisper-1", self.targets())
        self.assertEqual(len(self.stored()["reference_segments"]), 1)

    def test_with_nowhere_to_ask_the_refusal_says_so(self) -> None:
        with patch.object(voice_import, "annotation_targets", lambda: []), \
                patch.object(voice_import, "hub_probe",
                             lambda **k: {"running": False, "detail": "no hub-ready.json"}):
            answer = voice_import.start_annotation("whisper-1")
        self.assertIn("No Studio Hub that can transcribe is reachable", answer["error"])
        self.assertIn("no hub-ready.json", answer["error"])



if __name__ == "__main__":
    unittest.main()
