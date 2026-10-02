"""Telegram notifications: settings, the secret, incidents, grouping, rate limits, the daily summary.

Nothing here talks to Telegram. Every test uses a fake transport (a list that records what would have
been sent) or patches `urllib.request.urlopen`, and every file lives in a temp dir.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
import struct
import wave
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-notifier-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "notifier-fixture-key",
})

from gateway import notifier as nt  # noqa: E402
from gateway.store import JobStore  # noqa: E402

TOKEN = "123456789:AAH-fake_token_for_tests_0123456789abc"
CHAT = "-1001234567890"


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.results: list[nt.SendResult] = []
        self.calls = 0

    def __call__(self, token: str, chat: str, text: str) -> nt.SendResult:
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        self.sent.append(text)
        return nt.SendResult(True)


class Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def at(hour: int, minute: int = 0, day: int = 2) -> float:
    return datetime(2026, 10, day, hour, minute).timestamp()


def machine(name: str, loc: str = "g1", group: str = "Studio 1", **over) -> dict:
    row = {"location_id": loc, "location": group, "location_reachable": True, "machine": name, "label": name,
           "reachable": True, "node_state": "reachable", "node_reason": None, "disk": None}
    row.update(over)
    return row


def snap(machines=(), locations=None, running=()) -> dict:
    locs = locations if locations is not None else [
        {"id": "g1", "label": "Studio 1", "reachable": True, "url": "http://x", "local": False}]
    return {"locations": locs, "machines": list(machines), "running": list(running)}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir=base))
        self.transport = FakeTransport()
        self.clock = Clock(at(5))  # before the 08:00 summary, which has its own tests
        self.store = JobStore(self.dir / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.snapshot: dict | None = snap()
        self.disk = {"state": "green", "free_gb": 400.0, "refusing": False}
        self.n = nt.Notifier(
            transport=self.transport, clock=self.clock, sleep=lambda s: None,
            settings_path=self.dir / "telegram.json", state_path=self.dir / "state.json", sync=True,
            snapshot=lambda: self.snapshot, store=self.store, disk_status=lambda: self.disk)

    def configure(self, **extra) -> None:
        self.n.save_settings({"bot_token": TOKEN, "chat_id": CHAT, **extra})

    def run_ticks(self, *offsets: float) -> None:
        start = self.clock.now
        for off in offsets:
            self.clock.now = start + off
            self.n.tick()

    def handback(self, job_id: str) -> None:
        """An INSUFFICIENT_MEMORY attempt, stamped with the test clock rather than the real one."""
        self.store.record_attempt(job_id, location_id="g1", code="INSUFFICIENT_MEMORY", detail="no")
        log = json.dumps([{"location": "g1", "code": "INSUFFICIENT_MEMORY", "detail": "no", "at": self.clock.now}])
        with self.store._lock:
            self.store._conn.execute("UPDATE jobs SET attempt_log = ?, updated_at = ? WHERE id = ?",
                                     (log, self.clock.now, job_id))
            self.store._conn.commit()

    def add_job(self, modality="tts", state="succeeded", when=None, **fields) -> str:
        job, _ = self.store.submit(modality=modality, model_id="m", params={}, idempotency_key=None)
        sets = {"state": state, "updated_at": when or self.clock.now, **fields}
        cols = ", ".join(f"{k} = ?" for k in sets)
        with self.store._lock:
            self.store._conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*sets.values(), job["id"]))
            self.store._conn.commit()
        return job["id"]


class SettingsAndSecret(Base):
    def test_defaults_match_the_owner_decision(self) -> None:
        events = {e["key"]: e["enabled"] for e in self.n.public_settings()["events"]}
        self.assertTrue(events["offline"])
        self.assertTrue(events["daily_summary"])
        for key in ("disk_memory", "jobs_trouble", "release", "backup", "holds", "credentials", "gateway_restart"):
            self.assertFalse(events[key], key)
        pub = self.n.public_settings()
        self.assertFalse(pub["quiet_hours"]["enabled"])
        self.assertEqual(pub["summary_time"], "08:00")
        self.assertFalse(pub["configured"])

    def test_token_is_stored_owner_only_and_never_returned(self) -> None:
        pub = self.n.save_settings({"bot_token": TOKEN, "chat_id": CHAT})
        self.assertTrue(pub["token_set"] and pub["configured"])
        self.assertNotIn(TOKEN, json.dumps(pub))
        self.assertNotIn("bot_token", pub)
        mode = stat.S_IMODE((self.dir / "telegram.json").stat().st_mode)
        self.assertEqual(mode, 0o600)
        # the state file (incidents, last error) never holds it either
        self.n.send_test()
        self.assertNotIn(TOKEN, (self.dir / "state.json").read_text())

    def test_partial_update_keeps_the_token_and_clear_removes_it(self) -> None:
        self.configure()
        self.n.save_settings({"events": {"backup": True}})
        self.assertTrue(self.n.public_settings()["token_set"])
        self.assertTrue(self.n.settings()["events"]["backup"])
        self.n.save_settings({"clear_token": True})
        self.assertFalse(self.n.public_settings()["token_set"])
        self.assertFalse(self.n.public_settings()["configured"])

    def test_bad_input_is_refused_with_a_sentence(self) -> None:
        for payload in ({"bot_token": "nope"}, {"chat_id": "my chat"}, {"events": {"nonsense": True}},
                        {"summary_time": "8am"}, {"quiet_hours": {"start": "25:00"}}):
            with self.assertRaises(nt.SettingsRefused, msg=payload):
                self.n.save_settings(payload)
        self.assertFalse(self.n.public_settings()["token_set"])

    def test_nothing_is_sent_until_configured_and_enabled(self) -> None:
        self.assertFalse(self.n.notify("offline", "x"))
        self.configure()
        self.n.save_settings({"enabled": False})
        self.assertFalse(self.n.notify("offline", "x"))
        self.n.save_settings({"enabled": True})
        self.assertTrue(self.n.notify("offline", "x"))
        self.assertEqual(self.transport.sent, ["x"])

    def test_a_switched_off_event_sends_nothing(self) -> None:
        self.configure()
        self.assertFalse(self.n.notify("backup", "Backup done"))
        self.n.save_settings({"events": {"backup": True}})
        self.assertTrue(self.n.notify("backup", "Backup done"))
        with self.assertLogs("gateway.notifier", level="WARNING"):
            self.assertFalse(self.n.notify("not-an-event", "x"))
        self.assertEqual(self.transport.sent, ["Backup done"])


class SendingRules(Base):
    def test_identical_messages_are_dropped_and_come_back_after_the_window(self) -> None:
        self.configure()
        self.assertTrue(self.n.notify("offline", "same"))
        self.assertFalse(self.n.notify("offline", "same"))
        self.clock.now += nt.DEDUPE_S + 1
        self.assertTrue(self.n.notify("offline", "same"))
        self.assertEqual(len(self.transport.sent), 2)

    def test_hourly_cap_holds_back_and_the_next_message_says_so(self) -> None:
        self.configure()
        for i in range(nt.MAX_PER_HOUR + 3):
            self.n.notify("offline", f"m{i}")
        self.assertEqual(len(self.transport.sent), nt.MAX_PER_HOUR)
        self.clock.now += 3601
        self.n.notify("offline", "later")
        self.assertIn("3 more messages held back", self.transport.sent[-1])

    def test_quiet_hours_hold_then_send_one_digest(self) -> None:
        self.configure(quiet_hours={"enabled": True, "start": "22:00", "end": "07:00"})
        self.clock.now = at(23, 30)
        self.n.notify("offline", "one")
        self.n.notify("offline", "two")
        self.assertEqual(self.transport.sent, [])
        self.clock.now = at(6, 59, day=3)
        self.n.tick()
        self.assertEqual(self.transport.sent, [])
        self.clock.now = at(7, 1, day=3)
        self.n.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("one", self.transport.sent[0])
        self.assertIn("two", self.transport.sent[0])

    def test_background_sender_and_lifecycle(self) -> None:
        self.configure()
        self.n.sync = False
        self.assertTrue(self.n.notify("offline", "from the thread"))
        for _ in range(100):
            if self.transport.sent:
                break
            time.sleep(0.02)
        self.assertEqual(self.transport.sent, ["from the thread"])
        self.n.start()
        self.n.stop()
        self.n.stop()

    def test_env_switch_keeps_the_background_pass_from_starting(self) -> None:
        with patch.dict(os.environ, {"GATEWAY_NOTIFIER_DISABLE": "1"}):
            self.n.start()
        self.assertIsNone(self.n._ticker)

    def test_quiet_hours_off_by_default(self) -> None:
        self.configure()
        self.clock.now = at(3)
        self.assertTrue(self.n.notify("offline", "night"))
        self.assertEqual(self.transport.sent, ["night"])

    def test_transient_failure_retries_then_succeeds(self) -> None:
        self.configure()
        self.transport.results = [nt.SendResult(False, "Could not reach api.telegram.org (X).", transient=True)]
        self.n.notify("offline", "hello")
        self.assertEqual(self.transport.sent, ["hello"])
        self.assertEqual(self.transport.calls, 2)

    def test_failure_is_recorded_without_the_token_and_logged_without_it(self) -> None:
        self.configure()

        def boom(token, chat, text):
            raise RuntimeError(f"https://api.telegram.org/bot{token}/sendMessage exploded")

        self.n.transport = boom
        with self.assertLogs("gateway.notifier", level="WARNING") as logs:
            self.n.notify("offline", "hello")
        blob = json.dumps(self.n.public_settings()) + "\n".join(logs.output) + (self.dir / "state.json").read_text()
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn("AAH-fake", blob)

    def test_test_message_reports_telegrams_answer(self) -> None:
        self.configure()
        self.assertEqual(self.n.send_test()["ok"], True)
        self.assertIn("test message", self.transport.sent[0])
        self.transport.results = [nt.SendResult(False, "Telegram refused the bot token.")]
        answer = self.n.send_test()
        self.assertFalse(answer["ok"])
        self.assertIn("refused", answer["error"])

    def test_test_message_can_use_typed_values_without_saving(self) -> None:
        answer = self.n.send_test(TOKEN, CHAT)
        self.assertTrue(answer["ok"])
        self.assertFalse(self.n.public_settings()["token_set"])
        self.assertFalse(self.n.send_test("", "")["ok"])

    def test_real_transport_never_leaks_the_token(self) -> None:
        err = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", 401, "Unauthorized", {},
            BytesIO(json.dumps({"ok": False, "description": f"Unauthorized {TOKEN}"}).encode()))
        with patch("urllib.request.urlopen", side_effect=err):
            result = nt.telegram_transport(TOKEN, CHAT, "hi")
        self.assertFalse(result.ok)
        self.assertNotIn(TOKEN, result.error or "")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(OSError(f"fail {TOKEN}"))):
            result = nt.telegram_transport(TOKEN, CHAT, "hi")
        self.assertTrue(result.transient)
        self.assertNotIn(TOKEN, result.error or "")

    def test_real_transport_posts_only_to_api_telegram_org(self) -> None:
        seen = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'

        def fake(request, timeout=None):
            seen["url"] = request.full_url
            return Resp()

        with patch("urllib.request.urlopen", fake):
            self.assertTrue(nt.telegram_transport(TOKEN, CHAT, "hi").ok)
        self.assertTrue(seen["url"].startswith("https://api.telegram.org/bot"))


class OfflineIncidents(Base):
    def setUp(self) -> None:
        super().setUp()
        self.configure()

    def test_one_message_per_incident_then_one_resolved(self) -> None:
        self.snapshot = snap([machine("Mini A", reachable=False, node_state="unreachable")])
        self.run_ticks(0, 30)
        self.assertEqual(self.transport.sent, [])            # not sustained yet
        self.run_ticks(61, 91, 121, 600)
        self.assertEqual(len(self.transport.sent), 1)        # one message, never repeated
        self.assertIn("Mini A (Studio 1) is offline", self.transport.sent[0])
        self.snapshot = snap([machine("Mini A")])
        self.run_ticks(700, 761)
        self.assertEqual(len(self.transport.sent), 2)
        self.assertIn("back online", self.transport.sent[1])
        self.run_ticks(900, 1200)
        self.assertEqual(len(self.transport.sent), 2)

    def test_a_flap_shorter_than_the_debounce_is_never_announced(self) -> None:
        down = snap([machine("Mini A", reachable=False)])
        up = snap([machine("Mini A")])
        for i, s in enumerate([down, up, down, up, down, up]):
            self.snapshot = s
            self.run_ticks(i * 20)
        self.assertEqual(self.transport.sent, [])

    def test_incident_survives_a_restart_without_repeating(self) -> None:
        self.snapshot = snap([machine("Mini A", reachable=False)])
        self.run_ticks(0, 61)
        self.assertEqual(len(self.transport.sent), 1)
        again = nt.Notifier(
            transport=self.transport, clock=self.clock, sleep=lambda s: None,
            settings_path=self.dir / "telegram.json", state_path=self.dir / "state.json", sync=True,
            snapshot=lambda: self.snapshot, store=self.store, disk_status=lambda: self.disk)
        self.clock.now += 300
        again.tick()
        self.clock.now += 61
        again.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.snapshot = snap([machine("Mini A")])
        for _ in range(2):
            self.clock.now += 61
            again.tick()
        self.assertIn("back online", self.transport.sent[-1])

    def test_many_macs_at_once_are_one_grouped_message(self) -> None:
        macs = [machine(f"Mac {i}", reachable=False, node_state="unreachable") for i in range(6)]
        self.snapshot = snap(macs)
        self.run_ticks(0, 61)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("Studio 1: 6 Macs offline", self.transport.sent[0])
        self.snapshot = snap([machine(f"Mac {i}") for i in range(6)])
        self.run_ticks(100, 161)
        self.assertEqual(len(self.transport.sent), 2)
        self.assertIn("6 Macs back online", self.transport.sent[1])

    def test_a_group_that_is_down_hides_its_macs(self) -> None:
        locs = [{"id": "g1", "label": "Studio 1", "reachable": False, "url": "http://x", "local": False,
                 "error": "Nothing is listening"}]
        macs = [machine("A", location_reachable=False, reachable=None), machine("B", location_reachable=False)]
        self.snapshot = snap(macs, locations=locs)
        self.run_ticks(0, 61)
        self.assertEqual(self.transport.sent, ["🔴 Group Studio 1 is offline"])

    def test_paused_standalone_and_probe_are_not_incidents(self) -> None:
        locs = [{"id": "g1", "label": "Old", "reachable": False, "url": "http://x", "paused_at": 5},
                {"id": "local", "label": "This Mac", "reachable": False, "url": "", "local": True}]
        self.snapshot = snap([machine("probe-1", reachable=False)], locations=locs)
        self.run_ticks(0, 61, 200)
        self.assertEqual(self.transport.sent, [])

    def test_no_snapshot_means_unknown_not_resolved(self) -> None:
        self.snapshot = snap([machine("Mini A", reachable=False)])
        self.run_ticks(0, 61)
        self.snapshot = None
        self.run_ticks(200, 400, 800)
        self.assertEqual(len(self.transport.sent), 1)

    def test_switching_the_event_off_forgets_quietly(self) -> None:
        self.snapshot = snap([machine("Mini A", reachable=False)])
        self.run_ticks(0, 61)
        self.n.save_settings({"events": {"offline": False}})
        self.snapshot = snap([machine("Mini A")])
        self.run_ticks(100, 200)
        self.assertEqual(len(self.transport.sent), 1)

    def test_credential_refusal_is_its_own_event_not_an_offline(self) -> None:
        self.n.save_settings({"events": {"credentials": True}})
        self.snapshot = snap([machine("Mini A", reachable=False, node_state="unauthorized")])
        self.run_ticks(0, 61)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("refused", self.transport.sent[0])
        self.assertNotIn("offline", self.transport.sent[0])


class OtherEvents(Base):
    def setUp(self) -> None:
        super().setUp()
        self.configure(events={"disk_memory": True, "jobs_trouble": True, "holds": True})

    def test_red_disk_then_recovery(self) -> None:
        self.snapshot = snap([machine("Mini A", disk={"state": "red", "free_gb": 9.5})])
        self.run_ticks(0, 61)
        self.assertIn("Mini A (Studio 1): disk almost full, 9.5 GB free", self.transport.sent[0])
        self.snapshot = snap([machine("Mini A", disk={"state": "green", "free_gb": 200})])
        self.run_ticks(100, 161)
        self.assertIn("disk space is back to normal", self.transport.sent[1])

    def test_amber_disk_is_not_an_alert_and_an_unreachable_mac_has_no_disk_reading(self) -> None:
        self.snapshot = snap([machine("A", disk={"state": "amber", "free_gb": 40}),
                              machine("B", reachable=False, disk={"state": "red", "free_gb": 0})])
        self.n.save_settings({"events": {"offline": False}})
        self.run_ticks(0, 61, 200)
        self.assertEqual(self.transport.sent, [])

    def test_gateways_own_disk_counts(self) -> None:
        self.disk = {"state": "red", "free_gb": 8.0, "refusing": False}
        self.run_ticks(0, 61)
        self.assertIn("The Gateway's Mac: disk almost full", self.transport.sent[0])

    def test_repeated_memory_handbacks(self) -> None:
        for _ in range(3):
            jid = self.add_job("tts", "succeeded")
            self.handback(jid)
        self.run_ticks(0, 61, 121)
        self.assertEqual(len([m for m in self.transport.sent if "lack of memory" in m]), 1)
        self.clock.now += nt.MEMORY_WINDOW_S + 100
        self.run_ticks(0, 61, 121)
        self.assertTrue(any("handed back" in m and "No jobs" in m for m in self.transport.sent))

    def test_two_handbacks_are_not_enough(self) -> None:
        for _ in range(2):
            jid = self.add_job("tts", "succeeded")
            self.handback(jid)
        self.run_ticks(0, 61, 121)
        self.assertEqual(self.transport.sent, [])

    def test_failure_burst(self) -> None:
        for _ in range(5):
            self.add_job("image", "failed", error_code="PROVIDER_TIMEOUT")
        self.add_job("image", "succeeded")
        self.run_ticks(0, 61, 121)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("5 jobs failed", self.transport.sent[0])
        self.assertIn("PROVIDER_TIMEOUT ×5", self.transport.sent[0])

    def test_a_few_failures_among_many_successes_is_not_a_burst(self) -> None:
        for _ in range(5):
            self.add_job("image", "failed", error_code="X")
        for _ in range(20):
            self.add_job("image", "succeeded")
        self.run_ticks(0, 61, 121)
        self.assertEqual(self.transport.sent, [])

    def test_stalled_job_and_queue_wait(self) -> None:
        self.snapshot = snap(running=[{"location_id": "g1", "location": "Studio 1", "batch_id": "b1",
                                       "modality": "tts", "stalled": True, "no_progress_s": 900, "label": "Chapter 3"}])
        old, _ = self.store.submit(modality="image", model_id="m", params={}, idempotency_key=None)
        with self.store._lock:
            self.store._conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?",
                                     (self.clock.now - 3000, old["id"]))
            self.store._conn.commit()
        self.run_ticks(0, 61, 121)
        text = "\n".join(self.transport.sent)
        self.assertIn("Voice job stalled at Studio 1", text)
        self.assertIn("waiting in the queue", text)

    def test_unknown_outcome_holds_are_one_grouped_message_without_resolved(self) -> None:
        for _ in range(3):
            jid = self.add_job("tts", "failed")
            self.store.fail(jid, code="UNKNOWN_OUTCOME", detail="?", recovery_state="unknown_outcome")
            with self.store._lock:
                self.store._conn.execute("UPDATE jobs SET state='failed', recovery_state='unknown_outcome' WHERE id=?", (jid,))
                self.store._conn.commit()
        self.run_ticks(0, 61)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("(3)", self.transport.sent[0])
        # hold resolved by the operator: nothing is said
        with self.store._lock:
            self.store._conn.execute("UPDATE jobs SET recovery_state=NULL")
            self.store._conn.commit()
        self.run_ticks(200, 300, 400)
        self.assertEqual(len(self.transport.sent), 1)


class DailySummary(Base):
    def setUp(self) -> None:
        super().setUp()
        self.configure()

    def wav(self, seconds: int) -> str:
        path = self.dir / f"a{seconds}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 8000 * seconds)
        return str(path)

    def seed_yesterday(self) -> None:
        when = at(15, day=1)
        for seconds in (600, 300):
            self.add_job("tts", "succeeded", when, artifacts_json=json.dumps([{"path": self.wav(seconds)}]))
        for _ in range(3):
            self.add_job("image", "succeeded", when, artifacts_json=json.dumps([{"path": "x"}, {"path": "y"}]))
        self.add_job("image", "failed", when, error_code="INSUFFICIENT_MEMORY")
        self.add_job("tts", "failed", when, error_code="PROVIDER_TIMEOUT")
        self.add_job("tts", "failed", when, error_code="PROVIDER_TIMEOUT")
        self.add_job("chat", "succeeded", when)
        self.add_job("tts", "succeeded", at(9, day=2))      # today's work is not yesterday's
        self.add_job("tts", "succeeded", at(23, 59, day=1))

    def test_summary_numbers(self) -> None:
        self.seed_yesterday()
        self.snapshot = snap([machine("Mini B", reachable=False),
                              machine("Mini A", disk={"state": "red", "free_gb": 9.0})])
        text = self.n.build_summary(at(8))
        self.assertIn("Thu 1 Oct", text)
        self.assertIn("10 jobs: 7 done, 3 failed (70% ok)", text)
        self.assertIn("Voice 3 (+2 failed)", text)
        self.assertIn("Image 3 (+1 failed)", text)
        self.assertIn("Chat 1", text)
        self.assertIn("Audio 15 min", text)                # 600 s + 300 s = 15 min (the 23:59 job has no file)
        self.assertIn("Images 6", text)
        self.assertIn("PROVIDER_TIMEOUT ×2", text)
        self.assertIn("Offline now: Mini B", text)
        self.assertIn("Mini A red (9 GB free)", text)

    def m4a(self, seconds: int, name: str = "a.m4a", version: int = 0) -> str:
        def box(kind: bytes, body: bytes) -> bytes:
            return struct.pack(">I4s", 8 + len(body), kind) + body
        times = struct.pack(">QQ" if version else ">II", 0, 0)
        length = struct.pack(">Q" if version else ">I", seconds * 1000)
        mvhd = box(b"mvhd", bytes([version]) + b"\0\0\0" + times + struct.pack(">I", 1000) + length + b"\0" * 80)
        path = self.dir / name
        # mdat before moov: the layout of an un-faststarted file
        path.write_bytes(box(b"ftyp", b"M4A \0\0\0\0") + box(b"mdat", b"x" * 500) + box(b"moov", mvhd))
        return str(path)

    def flac(self, seconds: int) -> str:
        info = struct.pack(">HH", 4096, 4096) + b"\0" * 6 + struct.pack(">Q", (44100 << 44) | (1 << 41) | (15 << 36) | 44100 * seconds) + b"\0" * 16
        path = self.dir / "a.flac"
        path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + info)
        return str(path)

    def test_compressed_audio_is_measured_from_its_header(self) -> None:
        when = at(15, day=1)
        for path in (self.m4a(600), self.m4a(300, "b.m4a", version=1), self.flac(900)):
            self.add_job("music", "succeeded", when, artifacts_json=json.dumps([{"path": path}]))
        text = self.n.build_summary(at(8))
        self.assertIn("Audio 30 min", text)
        self.assertNotIn("not measured", text)

    def test_the_duration_the_hub_recorded_wins_over_the_file(self) -> None:
        when = at(15, day=1)
        self.add_job("tts", "succeeded", when, artifacts_json=json.dumps([{"path": "/gone.m4a"}]),
                     result_json=json.dumps({"text": None, "payload": {"audio_duration_s": 1800.0}}))
        self.add_job("tts", "succeeded", when, artifacts_json=json.dumps([{"path": "/gone.m4a"}]),
                     result_json="not json")
        text = self.n.build_summary(at(8))
        self.assertIn("Audio 30 min (1 files not measured)", text)

    def test_empty_day_and_healthy_fleet(self) -> None:
        text = self.n.build_summary(at(8))
        self.assertIn("No jobs finished", text)
        self.assertIn("All Macs online", text)

    def test_sent_once_a_day_at_the_chosen_time(self) -> None:
        self.seed_yesterday()
        self.clock.now = at(7, 59)
        self.n.tick()
        self.assertEqual(self.transport.sent, [])
        self.clock.now = at(8, 1)
        self.n.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.n.tick()
        self.clock.now = at(20)
        self.n.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.clock.now = at(8, 5, day=3)
        self.n.tick()
        self.assertEqual(len(self.transport.sent), 2)

    def test_custom_time_and_toggle(self) -> None:
        self.n.save_settings({"summary_time": "18:30"})
        self.clock.now = at(18, 0)
        self.n.tick()
        self.assertEqual(self.transport.sent, [])
        self.clock.now = at(18, 31)
        self.n.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.n.save_settings({"events": {"daily_summary": False}})
        self.clock.now = at(18, 31, day=3)
        self.n.tick()
        self.assertEqual(len(self.transport.sent), 1)

    def test_summary_inside_quiet_hours_waits_for_the_end(self) -> None:
        self.n.save_settings({"summary_time": "06:00", "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"}})
        self.clock.now = at(6, 30)
        self.n.tick()
        self.assertEqual(self.transport.sent, [])
        self.clock.now = at(7, 5)
        self.n.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertIn("Gateway summary", self.transport.sent[0])

    def test_offline_events_yesterday_are_mentioned(self) -> None:
        self.snapshot = snap([machine("Mini A", reachable=False)])
        self.clock.now = at(10, day=1)
        self.run_ticks(0, 61)
        self.snapshot = snap([machine("Mini A")])
        self.clock.now = at(8)
        self.assertIn("Went offline yesterday: Mini A", self.n.build_summary(at(8)))


class ReleaseAndApi(Base):
    def test_release_sentences(self) -> None:
        ok = {"release": {"version": "0.6.127"}, "locations": {"a": {"ok": True}, "b": {"ok": True}}}
        self.assertEqual(nt.release_message(ok), "🚀 Release 0.6.127 reached all 2 controllers.")
        bad = {"release": {"version": "0.6.127"}, "locations": {
            "a": {"ok": True}, "b": {"ok": False, "label": "Studio 2", "detail": "holds a newer version"}}}
        text = nt.release_message(bad)
        self.assertIn("1 of 2", text)
        self.assertIn("Studio 2: holds a newer version", text)

    def test_other_modules_use_notify(self) -> None:
        self.configure(events={"backup": True})
        with patch.object(nt, "notifier", self.n):
            self.assertTrue(nt.notify("backup", "Backup failed: disk full"))
        self.assertEqual(self.transport.sent, ["Backup failed: disk full"])

    def test_routes_never_return_the_token(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        with patch.object(nt.notifier, "settings_path", self.dir / "t.json"), \
                patch.object(nt.notifier, "state_path", self.dir / "s.json"), \
                patch.object(nt.notifier, "transport", self.transport):
            client = TestClient(api.app, client=("127.0.0.1", 54321))
            self.addCleanup(client.close)
            r = client.get("/admin/api/settings/telegram")
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["token_set"])
            r = client.put("/admin/api/settings/telegram", json={"bot_token": TOKEN, "chat_id": CHAT})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["token_set"])
            self.assertNotIn(TOKEN, r.text)
            self.assertNotIn(TOKEN, client.get("/admin/api/settings/telegram").text)
            r = client.put("/admin/api/settings/telegram", json={"bot_token": "bad"})
            self.assertEqual(r.status_code, 422)
            self.assertNotIn("bad\"", r.text.replace("bad.", ""))
            r = client.post("/admin/api/settings/telegram/test", json={})
            self.assertTrue(r.json()["ok"])
            self.assertNotIn(TOKEN, r.text)
            self.assertEqual(len(self.transport.sent), 1)


if __name__ == "__main__":
    unittest.main()
