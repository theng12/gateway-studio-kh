#!/usr/bin/env python3
"""The seven defects an independent review found in the guest work — one test each.

Each test here is written to **fail on the code as it was reviewed** and pass on the fix, and
each names what it is about:

1. `_`-prefixed params from a client reached the adapters, which read those paths off disk and
   send the bytes to a provider, so `{"_reference_path": "~/.ssh/id_rsa"}` was enough.
2. An idempotency key was one flat namespace: a guest replaying the owner's key was handed the
   owner's job (and, once finished, its output text), and a squatted key could sit on a later
   owner or Story Studio submission.
3. An empty password skipped the scrypt, so the login page answered "does this guest exist?"
   in microseconds.
4. `await file.read()` held the whole upload in memory before the size limit was consulted.
5. A guest's job list carried the owner's gateway-wide counters.
6. A guest could take the name "Owner", "This Mac" or "Story Studio" and read as the owner in
   the very column that exists to tell them apart.
7. A guest's own actions carried the name inside an `onclick` attribute, where an apostrophe
   ended the string and broke every button on the row (the browser-side half of 7 is in
   `tests/test_admin_guests.js`).

Temp profile throughout: nothing here touches the owner's guests, sessions, jobs or settings.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-guest-review-")
BASE = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(BASE), "GATEWAY_DATA": str(BASE / "data"),
    "GATEWAY_ARTIFACTS": str(BASE / "artifacts"), "GATEWAY_DB": str(BASE / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(BASE / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(BASE / "models.json"),
    "GATEWAY_ENV_FILE": str(BASE / ".env"), "GATEWAY_FLEET": str(BASE / "fleet.json"),
    "GATEWAY_VOICES": str(BASE / "voices.json"),
    "STUDIOHUB_READY_FILE": str(BASE / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "k" * 32,
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin, admin_session, api  # noqa: E402
from gateway.adapters import base as adapter_base  # noqa: E402
from gateway.runtime import store  # noqa: E402

FRIEND = ("100.100.1.1", 5555)
SAME_ORIGIN = {"Origin": "http://testserver"}
PASSWORD = "correct horse battery"


def fresh() -> None:
    with admin_session._lock:
        admin_session._write(admin_session._guests_path(), {"guests": []})
        admin_session._loaded().clear()
        admin_session._write(admin_session._sessions_path(), {})


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        fresh()
        self.addCleanup(fresh)
        self.made: list[str] = []

    def tearDown(self) -> None:
        if self.made:
            with store._lock:
                store._conn.execute(f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(self.made))})",
                                    self.made)
                store._conn.commit()

    def guest(self, name: str = "Maria", *, paid: bool = False):
        row = admin_session.add_guest(name, PASSWORD, paid_allowed=paid)
        token = admin_session.create_session(role="guest", guest_id=row["id"], name=row["name"])
        return row, {admin_session.COOKIE: token}

    def client(self):
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        return client

    def remember(self, job: dict) -> dict:
        if job and job.get("job"):
            job = job["job"]
        self.made.append(job["id"])
        return job


# ---------------------------------------------------------------------------
# 1. A client may not name an internal (`_`-prefixed) param.
# ---------------------------------------------------------------------------
class InternalParams(_Base):
    def test_a_client_cannot_hand_the_gateway_a_path_to_read(self) -> None:
        secret = BASE / "THE-OWNERS-SECRET.txt"
        secret.write_text("OWNER SECRET FILE CONTENT")
        _row, cookies = self.guest()
        response = self.client().post("/admin/api/create", cookies=cookies, headers=SAME_ORIGIN,
                                      json={"modality": "image", "model_id": "ui-fixture::x",
                                            "params": {"prompt": "draw it",
                                                       "_reference_path": str(secret),
                                                       "_reference_type": "image/png"}})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "INTERNAL_PARAM")
        self.assertIn("_reference_path", response.json()["detail"])
        self.assertIsNone(store.find_by_idempotency_key("never-queued"))

    def test_the_owner_is_refused_the_same_field(self) -> None:
        # Not a guest rule: no client names what only the Gateway writes.
        owner = TestClient(api.app, client=("127.0.0.1", 1))
        self.addCleanup(owner.close)
        response = owner.post("/admin/api/create", json={
            "modality": "image", "model_id": "ui-fixture::x",
            "params": {"prompt": "x", "_upload_path": "/etc/passwd"}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "INTERNAL_PARAM")

    def test_the_adapter_itself_refuses_a_path_outside_the_artifact_store(self) -> None:
        # Defence in depth: even a stored job (or a route written later) cannot make an
        # adapter read a file the Gateway never staged.
        outside = BASE / "elsewhere.txt"
        outside.write_text("not staged")
        request = SimpleNamespace(params={"_reference_path": str(outside), "_reference_type": "image/png"})
        with self.assertRaises(adapter_base.BackendError) as caught:
            adapter_base.staged_bytes(request, "_reference_path", "_reference_type")
        self.assertEqual(caught.exception.code, "INVALID_PARAMS")
        self.assertIn("outside the Gateway's artifact store", str(caught.exception))

    def test_a_real_staged_file_still_reads(self) -> None:
        artifacts = Path(os.environ["GATEWAY_ARTIFACTS"])
        artifacts.mkdir(parents=True, exist_ok=True)
        staged = artifacts / "reference.png"
        staged.write_bytes(b"\x89PNG-staged")
        request = SimpleNamespace(params={"_reference_path": str(staged), "_reference_type": "image/png"})
        content, media = adapter_base.staged_bytes(request, "_reference_path", "_reference_type")
        self.assertEqual(content, b"\x89PNG-staged")
        self.assertEqual(media, "image/png")

    def test_a_list_of_paths_is_confined_too(self) -> None:
        outside = BASE / "elsewhere2.png"
        outside.write_bytes(b"x")
        request = SimpleNamespace(params={"_reference_paths": [str(outside)], "_reference_types": ["image/png"]})
        with self.assertRaises(adapter_base.BackendError) as caught:
            adapter_base.staged_data_uris(request, "_reference_paths", "_reference_types")
        self.assertEqual(caught.exception.code, "INVALID_PARAMS")


# ---------------------------------------------------------------------------
# 2. An idempotency key belongs to the requester who made it.
# ---------------------------------------------------------------------------
class IdempotencyBelongsToItsRequester(_Base):
    def owner_job(self, key: str, text: str = "THE OWNER'S PRIVATE ANSWER") -> dict:
        row, _ = store.submit(modality="chat", model_id="mock", params={"text": "OWNER SCRIPT"},
                              idempotency_key=key, provider_id="ui-fixture",
                              requested_by={"role": "owner", "label": "Owner"})
        self.remember(row)
        store.succeed(row["id"], result={"text": text})
        return store.get(row["id"])

    def test_a_guest_replaying_the_owners_key_gets_a_job_of_their_own(self) -> None:
        theirs = self.owner_job("owner-key-shared")
        _row, cookies = self.guest()
        with patch.object(api.catalog, "resolve",
                          lambda *a, **k: (SimpleNamespace(id="ui-fixture"), {"model_id": "mock"})):
            response = self.client().post(
                "/admin/api/create", cookies=cookies, headers=SAME_ORIGIN,
                json={"modality": "chat", "model_id": "ui-fixture::mock",
                      "idempotency_key": "owner-key-shared"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.remember({"id": body["id"]} if body.get("id") else {})
        self.assertNotEqual(body.get("id"), theirs["id"], "the owner's job was handed over")
        self.assertTrue(body.get("created"), "a replay of somebody else's key must be a new job")
        self.assertIsNone(body.get("output_text"), "the owner's answer must not ride along")
        self.assertEqual(body.get("requested_by", {}).get("role"), "guest")

    def test_a_guest_replaying_their_own_key_still_replays(self) -> None:
        _row, cookies = self.guest()
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture"), {"model_id": "mock"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        first = self.client().post("/admin/api/create", cookies=cookies, headers=SAME_ORIGIN,
                                   json={"modality": "chat", "model_id": "ui-fixture::mock",
                                         "idempotency_key": "mine"}).json()
        self.remember({"id": first["id"]})
        again = self.client().post("/admin/api/create", cookies=cookies, headers=SAME_ORIGIN,
                                   json={"modality": "chat", "model_id": "ui-fixture::mock",
                                         "idempotency_key": "mine"}).json()
        self.assertEqual(again["id"], first["id"], "a caller's own retry must find its job")
        self.assertFalse(again["created"])

    def test_two_guests_with_the_same_key_get_their_own_jobs(self) -> None:
        first_row, first_cookies = self.guest("Maria")
        _second_row, second_cookies = self.guest("Anna")
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture"), {"model_id": "mock"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        client = self.client()
        mine = client.post("/admin/api/create", cookies=first_cookies, headers=SAME_ORIGIN,
                           json={"modality": "chat", "model_id": "ui-fixture::mock",
                                 "idempotency_key": "same"}).json()
        self.remember({"id": mine["id"]})
        theirs = client.post("/admin/api/create", cookies=second_cookies, headers=SAME_ORIGIN,
                             json={"modality": "chat", "model_id": "ui-fixture::mock",
                                   "idempotency_key": "same"}).json()
        self.remember({"id": theirs["id"]})
        self.assertNotEqual(mine["id"], theirs["id"])

    def test_a_guest_cannot_squat_a_key_an_owner_will_use_later(self) -> None:
        _row, cookies = self.guest()
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture"), {"model_id": "mock"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        squatted = self.client().post("/admin/api/create", cookies=cookies, headers=SAME_ORIGIN,
                                      json={"modality": "chat", "model_id": "ui-fixture::mock",
                                            "idempotency_key": "k1"}).json()
        self.remember({"id": squatted["id"]})
        owner = TestClient(api.app, client=("127.0.0.1", 1))
        self.addCleanup(owner.close)
        theirs = owner.post("/admin/api/create",
                            json={"modality": "chat", "model_id": "ui-fixture::mock",
                                  "idempotency_key": "k1"}).json()
        self.remember({"id": theirs["id"]})
        self.assertNotEqual(squatted["id"], theirs["id"],
                            "the guest's key must not answer for the owner's submission")
        self.assertTrue(theirs["created"])

    def test_a_key_from_before_the_change_still_replays_for_the_owner(self) -> None:
        # Rows written before the field existed carry a raw key and no requester. An upgrade
        # must not turn a retry of an in-flight submission into a second generation.
        row, _ = store.submit(modality="chat", model_id="mock", params={},
                              idempotency_key="legacy-key", provider_id="ui-fixture")
        self.remember(row)
        owner = TestClient(api.app, client=("127.0.0.1", 1))
        self.addCleanup(owner.close)
        answer = owner.post("/admin/api/create",
                            json={"modality": "chat", "model_id": "ui-fixture::mock",
                                  "idempotency_key": "legacy-key"}).json()
        self.assertEqual(answer["id"], row["id"])
        self.assertFalse(answer["created"])

    def test_the_stored_key_is_scoped_and_the_raw_one_is_not_shared(self) -> None:
        _row, cookies = self.guest()
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture"), {"model_id": "mock"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        body = self.client().post("/admin/api/create", cookies=cookies, headers=SAME_ORIGIN,
                                  json={"modality": "chat", "model_id": "ui-fixture::mock",
                                        "idempotency_key": "scoped"}).json()
        self.remember({"id": body["id"]})
        stored = store.get(body["id"])
        self.assertIsNone(store.find_by_idempotency_key("scoped"),
                          "the guest's key must not occupy the owner's keyspace")
        self.assertIsNotNone(store.find_by_idempotency_key(stored["idempotency_key"]))
        self.assertTrue(stored["idempotency_key"].startswith("guest:"))


# ---------------------------------------------------------------------------
# 3. An empty password costs the same as a wrong one.
# ---------------------------------------------------------------------------
class LoginTiming(_Base):
    def test_an_empty_password_still_burns_a_scrypt(self) -> None:
        admin_session.add_guest("Maria", PASSWORD)
        burns: list[str] = []
        real = admin_session._scrypt

        def counting(password: str, salt: bytes, *args: int, **kwargs: int) -> bytes:
            burns.append(password)
            return real(password, salt, *args, **kwargs)

        with patch.object(admin_session, "_scrypt", counting):
            self.assertIsNone(admin_session.check_guest("Maria", ""))
            real_name = len(burns)
            burns.clear()
            self.assertIsNone(admin_session.check_guest("Nobody", ""))
            fake_name = len(burns)
        self.assertEqual(real_name, 1, "an empty password on a real name must hash once")
        self.assertEqual(fake_name, 1, "an unknown name must hash once")
        self.assertEqual(real_name, fake_name)

    def test_a_non_string_password_cannot_skip_the_work(self) -> None:
        admin_session.add_guest("Maria", PASSWORD)
        burns: list[str] = []
        real = admin_session._scrypt

        with patch.object(admin_session, "_scrypt",
                          lambda password, salt, *a, **kw: (burns.append(password),
                                                            real(password, salt, *a, **kw))[1]):
            self.assertIsNone(admin_session.check_guest("Maria", None))  # type: ignore[arg-type]
        self.assertEqual(len(burns), 1)


# ---------------------------------------------------------------------------
# 4. An upload is read against the limit, never into memory first.
# ---------------------------------------------------------------------------
class UploadBound(unittest.TestCase):
    def setUp(self) -> None:
        fresh()
        self.addCleanup(fresh)
        row = admin_session.add_guest("Maria", PASSWORD)
        self.identity = {"role": "guest", "guest_id": row["id"], "label": "Maria (guest)",
                         "name": "Maria"}

    def fake_request(self, content_length: str | None):
        headers = {} if content_length is None else {"content-length": content_length}
        return SimpleNamespace(headers=headers, cookies={},
                              client=SimpleNamespace(host="100.100.1.1"))

    def test_a_declared_length_over_the_limit_is_refused_before_a_byte_is_read(self) -> None:
        reads: list[int] = []

        class Upload:
            async def read(self, size: int = -1) -> bytes:
                reads.append(size)
                return b"x" * size

        with patch.object(api, "upload_limit_bytes", lambda model_id: 1024):
            answer = asyncio.run(admin.create_transcription(
                self.fake_request("5000000000"), model_id="ui-fixture::whisper",
                file=Upload(), language="", idempotency_key="upload-1"))
        self.assertEqual(answer.status_code, 413)
        self.assertEqual(reads, [], "the body must not be read at all when it is declared too big")

    def test_an_undeclared_body_is_streamed_and_stopped_at_the_limit(self) -> None:
        reads: list[int] = []

        class Upload:
            async def read(self, size: int = -1) -> bytes:
                reads.append(size)
                return b"x" * (size if size > 0 else 0)

        with patch.object(api, "upload_limit_bytes", lambda model_id: 1024):
            answer = asyncio.run(admin.create_transcription(
                self.fake_request(None), model_id="ui-fixture::whisper",
                file=Upload(), language="", idempotency_key="upload-2"))
        self.assertEqual(answer.status_code, 413)
        # One chunk at a time, and never more than the limit plus that chunk in hand.
        self.assertTrue(reads and all(size == 1 << 20 for size in reads), reads)
        self.assertLessEqual(len(reads), 2, "reading must stop as soon as the limit is passed")

    def test_a_small_upload_still_goes_through_the_size_check(self) -> None:
        class Upload:
            filename = "clip.wav"
            content_type = "audio/wav"

            def __init__(self) -> None:
                self.sent = False

            async def read(self, size: int = -1) -> bytes:
                if self.sent:
                    return b""
                self.sent = True
                return b"tiny"

        with patch.object(api, "upload_limit_bytes", lambda model_id: 25 * 1024 * 1024), \
                patch.object(admin, "_guest_model_refusal", lambda *a, **k: None), \
                patch.object(admin, "requested_by", lambda request: self.identity), \
                patch.object(api, "_submit", side_effect=RuntimeError("reached the queue")):
            with self.assertRaises(RuntimeError):
                asyncio.run(admin.create_transcription(
                    self.fake_request("4"), model_id="ui-fixture::whisper",
                    file=Upload(), language="", idempotency_key="upload-3"))


# ---------------------------------------------------------------------------
# 5. The owner's counters are not a guest's.
# ---------------------------------------------------------------------------
class NoOwnerCounters(_Base):
    def test_a_guests_job_list_carries_neither_the_totals_nor_the_bulk_counts(self) -> None:
        _row, cookies = self.guest()
        body = self.client().get("/admin/api/jobs", params={"limit": 200}, cookies=cookies).json()
        self.assertIsNone(body["stats"], "the gateway-wide tally is not a guest's to read")
        self.assertIsNone(body["bulk"], "nor are everyone's failed and held counts")
        self.assertEqual(body.get("scope"), "guest")

    def test_the_owner_still_gets_them(self) -> None:
        owner = TestClient(api.app, client=("127.0.0.1", 1))
        self.addCleanup(owner.close)
        body = owner.get("/admin/api/jobs", params={"limit": 10}).json()
        self.assertIsNotNone(body["stats"])
        self.assertIn("as_of", body["bulk"])


# ---------------------------------------------------------------------------
# 6. A guest cannot be called "Owner", and every guest says it is one.
# ---------------------------------------------------------------------------
class Names(_Base):
    def test_reserved_names_are_refused(self) -> None:
        for name in ("Owner", "owner", "This Mac", "this mac", "Story Studio", "STORY STUDIO", "Guest"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    admin_session.add_guest(name, PASSWORD)

    def test_a_guest_may_not_be_renamed_to_a_reserved_name_either(self) -> None:
        row, _cookies = self.guest()
        with self.assertRaises(ValueError):
            admin_session.update_guest(row["id"], name="Owner")

    def test_an_ordinary_name_is_still_accepted(self) -> None:
        self.assertEqual(admin_session.add_guest("Maria", PASSWORD)["name"], "Maria")

    def test_a_guests_job_says_it_was_a_guest(self) -> None:
        _row, cookies = self.guest()
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (SimpleNamespace(id="ui-fixture"), {"model_id": "mock"}))
        resolved.start()
        self.addCleanup(resolved.stop)
        body = self.client().post("/admin/api/create", cookies=cookies, headers=SAME_ORIGIN,
                                  json={"modality": "chat", "model_id": "ui-fixture::mock",
                                        "idempotency_key": "labelled"}).json()
        self.remember({"id": body["id"]})
        self.assertEqual(body["requested_by"]["label"], "Maria (guest)")
        rows = self.client().get("/admin/api/jobs", params={"limit": 50}, cookies=cookies).json()["jobs"]
        self.assertEqual(rows[0]["requested_by_label"], "Maria (guest)")


if __name__ == "__main__":
    unittest.main()
