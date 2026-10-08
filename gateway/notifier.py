"""Telegram notifications: tell the owner what the fleet is doing without opening the app.

Everything the owner can be told is an **event type** with its own switch (`EVENTS`). A message is
sent only when Telegram is configured (bot token + chat id) *and* the event's switch is on, and the
only network call this module ever makes is an HTTPS POST to `api.telegram.org`.

    from gateway import notifier
    notifier.notify("backup", "Backup finished: 14 GB in 6 min")      # for other modules

**The token is a secret** and is stored like the provider credentials: `telegram.json` in the data
folder, written by `config.write_secret` (owner-only 0600, atomic). It is never returned by the
admin API (`token_set` says only that one exists), never logged, and scrubbed from any error text
(`redact`) before that text is stored, logged or shown.

**No spam, by construction.**
* *One message per incident.* A condition (a Mac offline, a disk red, a stalled job) becomes an
  incident only after it has held for `OPEN_AFTER_S`, produces one message, and then one "resolved"
  message once it has been clear for `CLEAR_AFTER_S`. A flap shorter than that is never seen.
* *Grouping.* Everything that opens in one pass is one message; three or more Macs of one group go
  in one line. A group that is down hides its own Macs (they are unknown, not offline).
* *Identical messages* inside `DEDUPE_S` are dropped, and `MAX_PER_HOUR` caps the total; what is held
  back is counted and mentioned in the next message instead of being sent.
* *Quiet hours* (optional, off by default) hold messages and send them together when they end.

Incidents are persisted (`telegram_state.json`), so a restart neither repeats an open incident nor
forgets to say it resolved. The daily summary is sent once per local day at the owner's time, for
the previous local day.

Detection runs on the notifier's own thread (`start()`, from the app's lifespan) over the same data
the pages read: the fleet snapshot the refresher keeps warm, the job store and the queue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import config

log = logging.getLogger("gateway.notifier")

# --------------------------------------------------------------------------- events


@dataclass(frozen=True)
class Event:
    key: str
    title: str
    description: str
    default: bool


EVENTS: tuple[Event, ...] = (
    Event("offline", "Mac or group goes offline",
          "A Mac or a whole group stops answering, and a message when it is back.", True),
    Event("daily_summary", "Daily summary",
          "Yesterday's jobs, audio and images, top failures, and which Macs are offline.", True),
    Event("disk_memory", "Disk or memory trouble",
          "A Mac's disk is red or stopped, or jobs keep being handed back for lack of memory.", False),
    Event("jobs_trouble", "Jobs failing or stuck",
          "A burst of failures, a stalled job, or the queue waiting too long.", False),
    Event("release", "Release rollout",
          "A Studio Hub release finished pushing to the controllers, or was blocked.", False),
    Event("backup", "Backup",
          "A nightly backup finished or failed.", False),
    Event("holds", "Jobs held for review",
          "A job's outcome is unknown and needs a decision before it can run again.", False),
    Event("credentials", "Credential refused",
          "A Studio Hub group or Mac refused the Gateway's access token.", False),
    Event("gateway_restart", "Gateway restarted",
          "The Gateway app started, after an update or a crash.", False),
)
EVENT_KEYS = frozenset(event.key for event in EVENTS)

# --------------------------------------------------------------------------- tuning

OPEN_AFTER_S = 60.0       # a condition must hold this long before it is an incident
CLEAR_AFTER_S = 60.0      # ...and be gone this long before it is resolved
DEDUPE_S = 600.0          # an identical message inside this window is dropped
MAX_PER_HOUR = 20         # hard cap on messages an hour (the summary and the test are exempt)
MIN_SPACING_S = 1.1       # Telegram allows about one message a second to one chat
TICK_S = 30.0
JOBS_EVERY_S = 60.0       # the job-table checks run this often, the fleet ones every tick
FAILURE_WINDOW_S = 900.0
FAILURE_BURST = 5         # failed jobs in the window (and at least half of what finished)
MEMORY_HANDBACKS = 3      # INSUFFICIENT_MEMORY hand-backs in an hour
MEMORY_WINDOW_S = 3600.0
QUEUE_WAIT_S = 1800.0     # the oldest waiting job is this old
MAX_BULLETS = 8
MESSAGE_LIMIT = 3900      # Telegram's is 4096 characters

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
_CHAT_RE = re.compile(r"^(-?\d{3,}|@[A-Za-z][A-Za-z0-9_]{3,})$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_BOT_URL_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

#: Incident keys that come from the fleet snapshot (the others come from the job table).
_FLEET_PREFIXES = frozenset({"loc", "mac", "disk", "cred", "stalled"})

KIND_LABEL = {"tts": "Voice", "image": "Image", "music": "Music", "video": "Video",
              "transcription": "Subtitles", "chat": "Chat"}


class SettingsRefused(ValueError):
    """The owner's input was not usable. The message is a sentence for the settings page."""


def redact(text: object, token: str = "") -> str:
    """`text` with the bot token, and anything shaped like a bot URL, removed."""
    out = str(text)
    if token:
        out = out.replace(token, "[token]")
    return _BOT_URL_RE.sub("bot[token]", out)


@dataclass
class SendResult:
    ok: bool
    error: str | None = None
    transient: bool = False
    retry_after: float | None = None


def _friendly(status: int | None, description: str) -> str:
    text = (description or "").strip()
    if status == 401:
        return "Telegram refused the bot token. Copy it again from @BotFather."
    if status == 403:
        return ("The bot is not allowed to write to that chat. Open the chat and press Start "
                "(or add the bot to the group) and try again.")
    if status == 400 and "chat not found" in text.lower():
        return ("Telegram does not know that chat id. Send the bot a message first, then check "
                "the number.")
    return text or (f"Telegram answered HTTP {status}." if status else "Telegram did not answer.")


def telegram_transport(token: str, chat_id: str, text: str) -> SendResult:
    """POST one message to the Bot API. The only code that talks to Telegram."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "true",
    }).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed https host
            data = json.loads(response.read() or b"{}")
        if data.get("ok"):
            return SendResult(True)
        return SendResult(False, _friendly(None, str(data.get("description") or "")))
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except (ValueError, OSError):
            data = {}
        description = str(data.get("description") or "")
        retry = (data.get("parameters") or {}).get("retry_after")
        return SendResult(
            False, redact(_friendly(exc.code, description), token),
            transient=exc.code == 429 or exc.code >= 500,
            retry_after=float(retry) if isinstance(retry, (int, float)) else None,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return SendResult(
            False, redact(f"Could not reach api.telegram.org ({type(reason).__name__}).", token),
            transient=True,
        )


# --------------------------------------------------------------------------- incidents


@dataclass
class Condition:
    """One thing that is wrong right now. `back` is the sentence for when it clears (None: say nothing)."""

    key: str
    event: str
    text: str
    back: str | None = None
    icon: str = "⚠️"
    group: str | None = None     # the group a Mac belongs to, for collapsing a mass outage
    name: str | None = None      # a Mac's own name
    open_after: float = OPEN_AFTER_S


def _clip(text: str, limit: int = MESSAGE_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _bullets(conds: list[Condition], *, back: bool) -> list[str]:
    """One line per condition; three or more Macs of one group share a line."""
    lines: list[str] = []
    by_group: dict[str, list[Condition]] = {}
    singles: list[Condition] = []
    for cond in conds:
        if cond.group and cond.name:
            by_group.setdefault(cond.group, []).append(cond)
        else:
            singles.append(cond)
    for group, members in by_group.items():
        if len(members) >= 3:
            names = ", ".join(m.name or "" for m in members[:4])
            more = f", +{len(members) - 4}" if len(members) > 4 else ""
            verb = "back online" if back else "offline"
            lines.append(f"{group}: {len(members)} Macs {verb} ({names}{more})")
        else:
            singles.extend(members)
    for cond in singles:
        lines.append((cond.back if back else cond.text) or cond.text)
    return lines


def format_incidents(conds: list[Condition], *, back: bool) -> str:
    """The message for everything that opened (or cleared) in one pass."""
    if not conds:
        return ""
    icon = "🟢" if back else conds[0].icon
    lines = _bullets(conds, back=back)
    if len(lines) == 1 and len(conds) == 1:
        return f"{icon} {lines[0]}"
    shown = lines[:MAX_BULLETS]
    head = f"{icon} {'Resolved' if back else 'Needs a look'} ({len(conds)})"
    out = [head, *[f"• {line}" for line in shown]]
    if len(lines) > len(shown):
        out.append(f"…and {len(lines) - len(shown)} more")
    return "\n".join(out)


# --------------------------------------------------------------------------- settings


def _default_paths() -> tuple[Path, Path]:
    data = Path(str(config.DATA_DIR))
    return data / "telegram.json", data / "telegram_state.json"


def _hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


class Notifier:
    def __init__(
        self,
        *,
        transport: Callable[[str, str, str], SendResult] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        settings_path: Path | None = None,
        state_path: Path | None = None,
        sync: bool = False,
        snapshot: Callable[[], dict | None] | None = None,
        store: Any = None,
        disk_status: Callable[[], dict] | None = None,
    ) -> None:
        settings_default, state_default = _default_paths()
        self.settings_path = Path(settings_path or settings_default)
        self.state_path = Path(state_path or state_default)
        self.transport = transport or telegram_transport
        self._clock = clock
        self._sleep = sleep
        self.sync = sync
        self._snapshot = snapshot
        self._store = store
        self._disk_status = disk_status
        self._lock = threading.RLock()
        self._recent: dict[str, float] = {}
        self._sent: deque[float] = deque()
        self._suppressed = 0
        self._last_send_at = 0.0
        self._pending_open: dict[str, float] = {}
        self._clearing: dict[str, float] = {}
        self._last_jobs_check = 0.0
        self._job_conditions: list[Condition] = []
        self._outbox: queue.Queue[tuple[str, bool] | None] = queue.Queue()
        self._sender: threading.Thread | None = None
        self._ticker: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_logged: dict[str, float] = {}

    # ----- settings file --------------------------------------------------

    def _read_json(self, path: Path) -> dict:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def settings(self) -> dict[str, Any]:
        """Settings with every default filled in. Includes the token: never hand this to a route."""
        raw = self._read_json(self.settings_path)
        events = raw.get("events") if isinstance(raw.get("events"), dict) else {}
        quiet = raw.get("quiet_hours") if isinstance(raw.get("quiet_hours"), dict) else {}
        summary_time = str(raw.get("summary_time") or "08:00")
        return {
            "enabled": bool(raw.get("enabled", True)),
            "bot_token": str(raw.get("bot_token") or ""),
            "chat_id": str(raw.get("chat_id") or ""),
            "events": {e.key: bool(events.get(e.key, e.default)) for e in EVENTS},
            "quiet_hours": {
                "enabled": bool(quiet.get("enabled", False)),
                "start": str(quiet.get("start") or "22:00") if _HHMM_RE.match(str(quiet.get("start") or "22:00")) else "22:00",
                "end": str(quiet.get("end") or "07:00") if _HHMM_RE.match(str(quiet.get("end") or "07:00")) else "07:00",
            },
            "summary_time": summary_time if _HHMM_RE.match(summary_time) else "08:00",
        }

    @staticmethod
    def configured(cfg: dict) -> bool:
        return bool(cfg["bot_token"] and cfg["chat_id"])

    def ready(self, cfg: dict | None = None) -> bool:
        cfg = cfg or self.settings()
        return bool(cfg["enabled"] and self.configured(cfg))

    def public_settings(self) -> dict[str, Any]:
        """What the settings page may see: everything except the token itself."""
        cfg = self.settings()
        state = self._state()
        return {
            "enabled": cfg["enabled"],
            "configured": self.configured(cfg),
            "token_set": bool(cfg["bot_token"]),
            "chat_id": cfg["chat_id"],
            "events": [
                {"key": e.key, "title": e.title, "description": e.description,
                 "default": e.default, "enabled": cfg["events"][e.key]}
                for e in EVENTS
            ],
            "quiet_hours": cfg["quiet_hours"],
            "summary_time": cfg["summary_time"],
            "status": {
                "last_sent_at": state.get("last_sent_at"),
                "last_error": state.get("last_error"),
                "last_error_at": state.get("last_error_at"),
            },
        }

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial update. A key that is absent is left alone; the token is never echoed."""
        raw = self._read_json(self.settings_path)
        if "enabled" in payload:
            raw["enabled"] = bool(payload["enabled"])
        if payload.get("clear_token"):
            raw.pop("bot_token", None)
        token = payload.get("bot_token")
        if token is not None and str(token).strip():
            token = str(token).strip()
            if not _TOKEN_RE.match(token):
                raise SettingsRefused(
                    "That does not look like a bot token. It is a number, a colon, then a long "
                    "string, as @BotFather sends it.")
            raw["bot_token"] = token
        if "chat_id" in payload:
            chat = str(payload["chat_id"] or "").strip()
            if chat and not _CHAT_RE.match(chat):
                raise SettingsRefused("The chat id is a number (a group's starts with a minus sign) or @channelname.")
            raw["chat_id"] = chat
        if isinstance(payload.get("events"), dict):
            unknown = sorted(set(payload["events"]) - EVENT_KEYS)
            if unknown:
                raise SettingsRefused(f"Unknown notification type: {', '.join(unknown)}.")
            events = dict(raw.get("events") or {})
            events.update({k: bool(v) for k, v in payload["events"].items()})
            raw["events"] = events
        if isinstance(payload.get("quiet_hours"), dict):
            quiet = dict(raw.get("quiet_hours") or {})
            for key in ("start", "end"):
                if key in payload["quiet_hours"]:
                    value = str(payload["quiet_hours"][key])
                    if not _HHMM_RE.match(value):
                        raise SettingsRefused("Quiet hours are times such as 22:00 and 07:00.")
                    quiet[key] = value
            if "enabled" in payload["quiet_hours"]:
                quiet["enabled"] = bool(payload["quiet_hours"]["enabled"])
            raw["quiet_hours"] = quiet
        if "summary_time" in payload:
            value = str(payload["summary_time"])
            if not _HHMM_RE.match(value):
                raise SettingsRefused("The summary time is a time such as 08:00.")
            raw["summary_time"] = value
        config.write_secret(self.settings_path, json.dumps(raw, indent=2, sort_keys=True) + "\n")
        return self.public_settings()

    # ----- state file -----------------------------------------------------

    def _state(self) -> dict:
        return self._read_json(self.state_path)

    def _save_state(self, state: dict) -> None:
        try:
            config.write_secret(self.state_path, json.dumps(state, indent=1, sort_keys=True) + "\n")
        except OSError as exc:  # noqa: BLE001 - state loss must never stop a notification
            log.warning("could not save notifier state: %s", type(exc).__name__)

    # ----- the public API -------------------------------------------------

    def notify(self, event_type: str, text: str, *, dedupe_key: str | None = None) -> bool:
        """Queue one message for `event_type`. True when it was sent or held for quiet hours.

        False when Telegram is not set up, the event is switched off, or the message is a repeat.
        Never raises and never blocks on the network.
        """
        try:
            if event_type not in EVENT_KEYS:
                log.warning("notify: unknown event type %r", event_type)
                return False
            cfg = self.settings()
            if not self.ready(cfg) or not cfg["events"][event_type]:
                return False
            text = _clip(str(text).strip())
            if not text:
                return False
            now = self._clock()
            key = dedupe_key or hashlib.sha1(f"{event_type}\0{text}".encode()).hexdigest()
            with self._lock:
                self._recent = {k: t for k, t in self._recent.items() if now - t < DEDUPE_S}
                if key in self._recent:
                    return False
                self._recent[key] = now
                if self._in_quiet(cfg, now):
                    self._hold(event_type, text, now)
                    return True
            self._enqueue(text)
            return True
        except Exception as exc:  # noqa: BLE001 - a notification must never break its caller
            log.warning("notify failed: %s", redact(f"{type(exc).__name__}: {exc}"))
            return False

    def send_test(self, token: str | None = None, chat_id: str | None = None) -> dict[str, Any]:
        """Send a test message now, with the typed values or the saved ones. Not rate limited."""
        cfg = self.settings()
        token = (token or "").strip() or cfg["bot_token"]
        chat = (chat_id or "").strip() or cfg["chat_id"]
        if not token or not chat:
            return {"ok": False, "error": "Enter the bot token and the chat id first."}
        if not _TOKEN_RE.match(token):
            return {"ok": False, "error": "That does not look like a bot token."}
        if not _CHAT_RE.match(chat):
            return {"ok": False, "error": "The chat id is a number (a group's starts with a minus sign)."}
        try:
            host = socket.gethostname().split(".")[0]
        except OSError:
            host = "this Mac"
        text = f"✅ Gateway test message from {host}. Telegram notifications are working."
        result = self._call_transport(token, chat, text)
        if result.ok:
            self._record_sent()
            return {"ok": True, "error": None}
        return {"ok": False, "error": redact(result.error or "Telegram did not accept the message.", token)}

    # ----- sending --------------------------------------------------------

    def _call_transport(self, token: str, chat: str, text: str) -> SendResult:
        try:
            return self.transport(token, chat, text)
        except Exception as exc:  # noqa: BLE001 - a broken transport is a failed send
            return SendResult(False, redact(f"{type(exc).__name__}", token), transient=True)

    def _record_sent(self) -> None:
        state = self._state()
        state["last_sent_at"] = self._clock()
        state.pop("last_error", None)
        state.pop("last_error_at", None)
        self._save_state(state)

    def _record_error(self, error: str, token: str) -> None:
        error = redact(error, token)
        state = self._state()
        state["last_error"] = error
        state["last_error_at"] = self._clock()
        self._save_state(state)
        now = self._clock()
        if now - self._last_logged.get(error, 0.0) > 600:
            self._last_logged[error] = now
            log.warning("telegram send failed: %s", error)

    def _enqueue(self, text: str, *, exempt: bool = False) -> None:
        if self.sync:
            self._send_now(text, exempt=exempt)
            return
        with self._lock:
            if self._sender is None or not self._sender.is_alive():
                self._sender = threading.Thread(target=self._send_loop, name="telegram-send", daemon=True)
                self._sender.start()
        self._outbox.put((text, exempt))

    def _send_loop(self) -> None:
        while True:
            item = self._outbox.get()
            if item is None:
                return
            try:
                self._send_now(item[0], exempt=item[1])
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram send crashed: %s", redact(type(exc).__name__))

    def _send_now(self, text: str, *, exempt: bool = False) -> bool:
        cfg = self.settings()
        if not self.ready(cfg):
            return False
        now = self._clock()
        with self._lock:
            while self._sent and now - self._sent[0] > 3600:
                self._sent.popleft()
            if not exempt and len(self._sent) >= MAX_PER_HOUR:
                self._suppressed += 1
                return False
            if self._suppressed:
                text = _clip(f"{text}\n\n({self._suppressed} more message"
                             f"{'s' if self._suppressed != 1 else ''} held back to avoid spam)")
                self._suppressed = 0
            wait = self._last_send_at + MIN_SPACING_S - now
        if wait > 0:
            self._sleep(wait)
        token, chat = cfg["bot_token"], cfg["chat_id"]
        result = self._call_transport(token, chat, text)
        for delay in (3.0, 10.0):
            if result.ok or not result.transient:
                break
            self._sleep(min(result.retry_after or delay, 30.0))
            result = self._call_transport(token, chat, text)
        with self._lock:
            self._last_send_at = self._clock()
            if result.ok:
                self._sent.append(self._last_send_at)
        if result.ok:
            self._record_sent()
            return True
        self._record_error(result.error or "Telegram did not accept the message.", token)
        return False

    # ----- quiet hours ----------------------------------------------------

    def _in_quiet(self, cfg: dict, now: float) -> bool:
        quiet = cfg["quiet_hours"]
        if not quiet["enabled"] or quiet["start"] == quiet["end"]:
            return False
        local = datetime.fromtimestamp(now)
        minute = local.hour * 60 + local.minute
        start, end = _hhmm_to_minutes(quiet["start"]), _hhmm_to_minutes(quiet["end"])
        return start <= minute < end if start < end else (minute >= start or minute < end)

    def _hold(self, event: str, text: str, now: float) -> None:
        state = self._state()
        held = list(state.get("held") or [])
        held.append({"event": event, "text": text, "at": now})
        state["held"] = held[-50:]
        self._save_state(state)

    def _flush_held(self, cfg: dict, now: float) -> None:
        state = self._state()
        held = state.get("held") or []
        if not held or self._in_quiet(cfg, now):
            return
        state["held"] = []
        self._save_state(state)
        summaries = [h["text"] for h in held if h.get("event") == "daily_summary"]
        others = [h for h in held if h.get("event") != "daily_summary"]
        if len(others) == 1:
            self._enqueue(others[0]["text"])
        elif others:
            body = "\n\n".join(h["text"] for h in others[-10:])
            skipped = len(others) - 10
            extra = f"\n\n…and {skipped} earlier" if skipped > 0 else ""
            self._enqueue(_clip(f"🌙 While quiet hours were on ({len(others)})\n\n{body}{extra}"))
        for text in summaries:
            self._enqueue(text, exempt=True)

    # ----- detection ------------------------------------------------------

    def _get_snapshot(self) -> dict | None:
        if self._snapshot is not None:
            return self._snapshot()
        from . import fleet

        return fleet.snapshot(allow_stale=True)

    def _get_store(self) -> Any:
        if self._store is not None:
            return self._store
        from .runtime import store

        return store

    def _get_disk(self) -> dict:
        if self._disk_status is not None:
            return self._disk_status()
        from . import diskguard

        return diskguard.status()

    def offline_conditions(self, snap: dict) -> list[Condition]:
        from .fleet import workstation_resting

        out: list[Condition] = []
        down_locations: set[str] = set()
        for row in snap.get("locations") or []:
            if row.get("reachable") is not False or row.get("paused_at"):
                continue
            if row.get("local") and not row.get("url"):
                continue  # no Studio Hub is configured on this Mac: nothing to be offline
            if str(row.get("error") or "").startswith("Rejected the Hub token"):
                continue  # reachable, but refusing us: that is the credentials event
            label = str(row.get("label") or row.get("id") or "a group")
            down_locations.add(str(row.get("id")))
            out.append(Condition(
                f"loc:{row.get('id')}", "offline", f"Group {label} is offline",
                back=f"Group {label} is back online", icon="🔴"))
        for machine in snap.get("machines") or []:
            if str(machine.get("machine") or "").startswith("probe-"):
                continue
            if str(machine.get("location_id")) in down_locations or machine.get("location_reachable") is False:
                continue
            state = str(machine.get("node_state") or "").lower()
            if state == "unauthorized":
                continue
            if machine.get("reachable") is not False and state not in ("unreachable", "error"):
                continue
            if workstation_resting(machine) or machine.get("lite_off"):
                continue  # a shop's Lite Mode Mac in use, or switched off: expected, not an alarm
            name = str(machine.get("label") or machine.get("machine") or "a Mac")
            group = str(machine.get("location") or "")
            reason = {"no_hub": "Studio Hub is not running on it",
                      "unreachable": "not answering, asleep or off?"}.get(
                          str(machine.get("node_reason") or "").lower(), "")
            where = f" ({group})" if group else ""
            out.append(Condition(
                f"mac:{machine.get('location_id')}:{machine.get('machine')}", "offline",
                f"{name}{where} is offline" + (f": {reason}" if reason else ""),
                back=f"{name}{where} is back online", icon="🔴", group=group or None, name=name))
        return out

    def disk_rows(self, snap: dict) -> list[dict]:
        """Every reachable Mac (and this one) whose disk is not green."""
        rows: list[dict] = []
        for machine in snap.get("machines") or []:
            disk = machine.get("disk") or {}
            if not machine.get("reachable") or disk.get("state") not in ("amber", "red", "stop"):
                continue
            rows.append({"key": f"disk:{machine.get('location_id')}:{machine.get('machine')}",
                         "name": str(machine.get("label") or machine.get("machine")),
                         "group": str(machine.get("location") or ""), "state": disk["state"],
                         "free_gb": disk.get("free_gb")})
        own = self._get_disk()
        if own.get("state") in ("amber", "red") or own.get("refusing"):
            rows.append({"key": "disk:gateway", "name": "The Gateway's Mac", "group": "",
                         "state": "stop" if own.get("refusing") else own["state"],
                         "free_gb": own.get("free_gb")})
        return rows

    def disk_conditions(self, snap: dict) -> list[Condition]:
        out = []
        for row in self.disk_rows(snap):
            if row["state"] not in ("red", "stop"):
                continue
            free = f", {row['free_gb']:g} GB free" if row["free_gb"] is not None else ""
            where = f" ({row['group']})" if row["group"] else ""
            word = "full" if row["state"] == "stop" else "almost full"
            out.append(Condition(
                row["key"], "disk_memory", f"{row['name']}{where}: disk {word}{free}",
                back=f"{row['name']}{where}: disk space is back to normal", icon="💾"))
        return out

    def credential_conditions(self, snap: dict) -> list[Condition]:
        out = []
        for row in snap.get("locations") or []:
            if str(row.get("error") or "").startswith("Rejected the Hub token") and not row.get("paused_at"):
                label = str(row.get("label") or row.get("id"))
                out.append(Condition(
                    f"cred:{row.get('id')}", "credentials",
                    f"Group {label} refused the Gateway's token",
                    back=f"Group {label} accepts the Gateway's token again", icon="🔑"))
        for machine in snap.get("machines") or []:
            if str(machine.get("node_state") or "").lower() == "unauthorized":
                name = str(machine.get("label") or machine.get("machine"))
                group = str(machine.get("location") or "")
                where = f" ({group})" if group else ""
                out.append(Condition(
                    f"cred:{machine.get('location_id')}:{machine.get('machine')}", "credentials",
                    f"{name}{where} refused its controller's credential",
                    back=f"{name}{where} accepts its credential again", icon="🔑"))
        return out

    def stalled_conditions(self, snap: dict) -> list[Condition]:
        out = []
        for job in snap.get("running") or []:
            if not job.get("stalled"):
                continue
            what = KIND_LABEL.get(str(job.get("modality")), str(job.get("modality") or "A"))
            title = job.get("label") or job.get("model") or ""
            mins = job.get("no_progress_s")
            for_ = f" for {int(mins // 60)} min" if isinstance(mins, (int, float)) and mins >= 60 else ""
            out.append(Condition(
                f"stalled:{job.get('location_id')}:{job.get('batch_id')}", "jobs_trouble",
                f"{what} job stalled at {job.get('location')}{(': ' + str(title)) if title else ''}"
                f", no progress{for_}",
                back=f"The {what.lower()} job at {job.get('location')} is moving again", icon="⏳"))
        return out

    def job_conditions(self, now: float) -> list[Condition]:
        """Checks that read the job table. Cheap, but not worth doing every 30 s."""
        store = self._get_store()
        out: list[Condition] = []
        cfg_events = self.settings()["events"]
        if cfg_events["jobs_trouble"]:
            rows = store.settled_between(now - FAILURE_WINDOW_S, now + 1)
            # A workstation yield that found nowhere else to go is the shop taking its Mac back, not a failure.
            failed = [r for r in rows if r["state"] == "failed" and r.get("error_code") != "WORKSTATION_YIELDED"]
            if len(failed) >= FAILURE_BURST and len(failed) * 2 >= len(rows):
                reasons = _top_reasons(failed, 2)
                out.append(Condition(
                    "jobs:failures", "jobs_trouble",
                    f"{len(failed)} jobs failed in the last {int(FAILURE_WINDOW_S // 60)} minutes"
                    + (f" ({reasons})" if reasons else ""),
                    back="Jobs are no longer failing in bursts", icon="❌"))
            waiting = store.waiting_jobs(limit=500)
            if waiting:
                oldest = max(now - float(w.get("created_at") or now) for w in waiting)
                if oldest >= QUEUE_WAIT_S:
                    out.append(Condition(
                        "queue:wait", "jobs_trouble",
                        f"{len(waiting)} job{'s' if len(waiting) != 1 else ''} waiting in the queue; "
                        f"the oldest for {int(oldest // 60)} min",
                        back="The queue is moving again", icon="⏳"))
        if cfg_events["disk_memory"]:
            hits = store.attempts_with_code("INSUFFICIENT_MEMORY", now - MEMORY_WINDOW_S)
            if len(hits) >= MEMORY_HANDBACKS:
                sites = sorted({str(h.get("location") or "?") for h in hits})
                out.append(Condition(
                    "memory:handbacks", "disk_memory",
                    f"{len(hits)} jobs were handed back for lack of memory in the last hour "
                    f"({', '.join(sites[:3])})",
                    back="No jobs have been handed back for memory for an hour", icon="🧠"))
        if cfg_events["holds"]:
            for job in store.holds(limit=50):
                if job.get("recovery_state") != "unknown_outcome":
                    continue
                kind = KIND_LABEL.get(str(job.get("modality")), str(job.get("modality") or "A"))
                out.append(Condition(
                    f"hold:{job['id']}", "holds",
                    f"A {kind.lower()} job needs review: its outcome is unknown ({str(job['id'])[:8]})",
                    back=None, icon="🔎"))
        return out

    def conditions(self, snap: dict | None, now: float, cfg: dict) -> list[Condition]:
        events = cfg["events"]
        out: list[Condition] = []
        if snap:
            if events["offline"]:
                out += self.offline_conditions(snap)
            if events["disk_memory"]:
                out += self.disk_conditions(snap)
            if events["credentials"]:
                out += self.credential_conditions(snap)
            if events["jobs_trouble"]:
                out += self.stalled_conditions(snap)
        if now - self._last_jobs_check >= JOBS_EVERY_S:
            self._last_jobs_check = now
            try:
                self._job_conditions = self.job_conditions(now)
            except Exception as exc:  # noqa: BLE001 - one bad query must not stop the fleet checks
                log.warning("job checks failed: %s", type(exc).__name__)
        out += [c for c in self._job_conditions if events.get(c.event)]
        return out

    def _advance(self, conds: list[Condition], now: float, cfg: dict,
                 fleet_known: bool = True) -> tuple[list[Condition], list[Condition]]:
        """Move incidents along. Returns (newly opened, newly resolved), both already debounced.

        When the fleet snapshot is unavailable (`fleet_known` false) the fleet incidents are left
        exactly as they are: not knowing is not the same as the problem being gone.
        """
        state = self._state()
        incidents: dict[str, dict] = dict(state.get("incidents") or {})
        seen = {c.key: c for c in conds}
        opened: list[Condition] = []
        closed: list[Condition] = []
        for key, cond in seen.items():
            self._clearing.pop(key, None)
            if key in incidents:
                continue
            first = self._pending_open.setdefault(key, now)
            if now - first >= cond.open_after:
                self._pending_open.pop(key, None)
                incidents[key] = {"event": cond.event, "since": now, "text": cond.text,
                                  "back": cond.back, "icon": cond.icon,
                                  "group": cond.group, "name": cond.name}
                opened.append(cond)
                if cond.event == "offline":
                    log_ = list(state.get("log") or [])
                    log_.append({"at": now, "key": key, "name": cond.name or cond.text,
                                 "group": cond.group})
                    state["log"] = log_[-300:]
        for key in [k for k in self._pending_open if k not in seen]:
            self._pending_open.pop(key, None)
        for key, info in list(incidents.items()):
            if key in seen:
                continue
            if not fleet_known and key.split(":", 1)[0] in _FLEET_PREFIXES:
                continue
            if not cfg["events"].get(info.get("event"), False):
                incidents.pop(key)  # the owner switched this type off: forget it quietly
                continue
            first = self._clearing.setdefault(key, now)
            if now - first >= CLEAR_AFTER_S:
                self._clearing.pop(key, None)
                incidents.pop(key)
                if info.get("back"):
                    closed.append(Condition(key, info["event"], info.get("text") or "", back=info["back"],
                                            icon=info.get("icon") or "⚠️",
                                            group=info.get("group"), name=info.get("name")))
        state["incidents"] = incidents
        self._save_state(state)
        return opened, closed

    def tick(self, now: float | None = None) -> None:
        """One pass: incidents, held messages, and the daily summary. Safe to call at any time."""
        now = self._clock() if now is None else now
        cfg = self.settings()
        if not self.ready(cfg):
            return
        try:
            snap = self._get_snapshot()
        except Exception as exc:  # noqa: BLE001
            log.warning("fleet read failed: %s", type(exc).__name__)
            snap = None
        conds = self.conditions(snap, now, cfg)
        opened, closed = self._advance(conds, now, cfg, fleet_known=snap is not None)
        for group, back in ((opened, False), (closed, True)):
            by_event: dict[str, list[Condition]] = {}
            for cond in group:
                by_event.setdefault(cond.event, []).append(cond)
            for event, items in by_event.items():
                self.notify(event, format_incidents(items, back=back),
                            dedupe_key=f"incident:{back}:{'|'.join(sorted(c.key for c in items))}:{now}")
        self._flush_held(cfg, now)
        self._maybe_summary(cfg, now, snap)

    # ----- the daily summary ---------------------------------------------

    def _maybe_summary(self, cfg: dict, now: float, snap: dict | None) -> None:
        if not cfg["events"]["daily_summary"]:
            return
        local = datetime.fromtimestamp(now)
        today = local.date().isoformat()
        state = self._state()
        if state.get("last_summary_date") == today:
            return
        if local.hour * 60 + local.minute < _hhmm_to_minutes(cfg["summary_time"]):
            return
        state["last_summary_date"] = today
        self._save_state(state)
        text = self.build_summary(now, snap)
        if self._in_quiet(cfg, now):
            self._hold("daily_summary", text, now)
        else:
            self._enqueue(text, exempt=True)

    def build_summary(self, now: float, snap: dict | None = None) -> str:
        """Yesterday's work as a short message. The window is the previous local day."""
        today = datetime.fromtimestamp(now).date()
        day = today - timedelta(days=1)
        start = datetime.combine(day, dtime.min).timestamp()
        end = datetime.combine(today, dtime.min).timestamp()
        rows = self._get_store().settled_between(start, end)
        done = [r for r in rows if r["state"] == "succeeded"]
        failed = [r for r in rows if r["state"] == "failed"]
        lines = [f"📊 Gateway summary for {day.strftime('%a %-d %b')}"]
        if not rows:
            lines.append("No jobs finished.")
        else:
            rate = round(100 * len(done) / max(len(done) + len(failed), 1))
            lines.append(f"{len(rows)} jobs: {len(done)} done, {len(failed)} failed ({rate}% ok)")
            kinds: dict[str, list[int]] = {}
            for r in rows:
                entry = kinds.setdefault(KIND_LABEL.get(r["modality"], str(r["modality"]).title()), [0, 0])
                entry[0] += r["state"] == "succeeded"
                entry[1] += r["state"] == "failed"
            lines.append(" · ".join(
                f"{name} {ok}" + (f" (+{bad} failed)" if bad else "")
                for name, (ok, bad) in sorted(kinds.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))))
            minutes, unmeasured = _audio_minutes(done)
            images = sum(max(len(_artifact_list(r)), 1) for r in done if r["modality"] == "image")
            facts = []
            if minutes or any(r["modality"] in ("tts", "music") for r in done):
                facts.append(f"Audio {_hours(minutes)}" + (f" ({unmeasured} files not measured)" if unmeasured else ""))
            if images:
                facts.append(f"Images {images}")
            if facts:
                lines.append(" · ".join(facts))
            if failed:
                reasons = _top_reasons(failed, 3)
                if reasons:
                    lines.append(f"Top failures: {reasons}")
        state = self._state()
        went = [e for e in (state.get("log") or []) if start <= e.get("at", 0) < end]
        if went:
            names = sorted({str(e.get("name")) for e in went})
            lines.append(f"Went offline yesterday: {', '.join(names[:5])}" + (f" +{len(names) - 5}" if len(names) > 5 else ""))
        if snap is None:
            try:
                snap = self._get_snapshot()
            except Exception:  # noqa: BLE001
                snap = None
        trouble = bool(went)
        if snap is not None:
            offline = self.offline_conditions(snap)
            if offline:
                names = [c.name or c.text.replace("", "") for c in offline]
                lines.append(f"Offline now: {', '.join(names[:5])}" + (f" +{len(names) - 5}" if len(names) > 5 else ""))
            disks = self.disk_rows(snap)
            trouble = trouble or bool(offline) or bool(disks)
            if disks:
                lines.append("Disk: " + ", ".join(
                    f"{d['name']} {d['state']}" + (f" ({d['free_gb']:g} GB free)" if d["free_gb"] is not None else "")
                    for d in disks[:4]))
        if snap is not None and not trouble:
            lines.append("All Macs online, disks fine.")
        return _clip("\n".join(lines))

    # ----- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Begin the background pass. Called from the app's lifespan; harmless when unconfigured.

        `GATEWAY_NOTIFIER_DISABLE=1` keeps it from starting at all (the render harness seeds a
        fake token to draw the Settings page and must never be able to reach Telegram).
        """
        if os.environ.get("GATEWAY_NOTIFIER_DISABLE", "").strip() in {"1", "true", "yes"}:
            return
        with self._lock:
            if self._ticker is not None and self._ticker.is_alive():
                return
            self._stop.clear()
            self._ticker = threading.Thread(target=self._loop, name="telegram-notifier", daemon=True)
            self._ticker.start()
        try:
            from . import __version__

            self.notify("gateway_restart", f"🔄 Gateway {__version__} started on {socket.gethostname().split('.')[0]}",
                        dedupe_key="gateway:start")
        except Exception:  # noqa: BLE001
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any one pass
                log.warning("notifier pass failed: %s", redact(f"{type(exc).__name__}: {exc}"))
            self._stop.wait(TICK_S)

    def stop(self) -> None:
        self._stop.set()
        if self._sender is not None and self._sender.is_alive():
            self._outbox.put(None)


# --------------------------------------------------------------------------- helpers


def _artifact_list(row: dict) -> list:
    try:
        parsed = json.loads(row.get("artifacts_json") or "[]")
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _mp4_seconds(f) -> float | None:
    """Duration from the `moov/mvhd` box of an m4a/mp4 (the default narration and music format)."""
    def find(kind: bytes, pos: int, end: int | None):
        while end is None or pos + 8 <= end:
            f.seek(pos)
            head = f.read(8)
            if len(head) < 8:
                return None
            size, name = struct.unpack(">I4s", head)
            body = pos + 8
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                body += 8
            if size < body - pos:
                return None  # size 0 (to EOF) or garbage: not worth guessing
            if name == kind:
                return body, pos + size
            pos += size
        return None

    moov = find(b"moov", 0, None)
    mvhd = moov and find(b"mvhd", moov[0], moov[1])
    if not mvhd:
        return None
    f.seek(mvhd[0])
    version = f.read(1)[0:1]
    f.seek(3 + (16 if version == b"\x01" else 8), 1)
    scale, = struct.unpack(">I", f.read(4))
    length, = struct.unpack(">Q", f.read(8)) if version == b"\x01" else struct.unpack(">I", f.read(4))
    return length / scale if scale else None


def _flac_seconds(f) -> float | None:
    """Duration from the STREAMINFO block (the first metadata block) of a FLAC file."""
    f.seek(18)  # "fLaC" + block header + 10 bytes of block/frame sizes
    packed, = struct.unpack(">Q", f.read(8))
    rate, total = packed >> 44, packed & (2**36 - 1)
    return total / rate if rate and total else None


def _file_seconds(path: str) -> float | None:
    """Audio length from the file's own header: WAV, m4a/mp4 or FLAC. Never decodes, never guesses."""
    with open(path, "rb") as f:
        magic = f.read(12)
        if magic[:4] == b"fLaC":
            return _flac_seconds(f)
        if magic[4:8] == b"ftyp":
            return _mp4_seconds(f)
    with wave.open(path, "rb") as handle:
        return handle.getnframes() / float(handle.getframerate() or 1)


def _audio_minutes(rows: list[dict]) -> tuple[float, int]:
    """Minutes of audio in the succeeded voice and music jobs.

    The length the Hub measured and the job recorded (`audio_duration_s`) wins; otherwise the file's
    own header is read (WAV, m4a, FLAC). A file that is gone or unreadable is counted as unmeasured
    rather than guessed from its size.
    """
    seconds, unmeasured = 0.0, 0
    for row in rows:
        if row["modality"] not in ("tts", "music"):
            continue
        recorded = row.get("audio_duration_s")
        if isinstance(recorded, (int, float)) and recorded > 0:
            seconds += recorded
            continue
        paths = [a.get("path") for a in _artifact_list(row) if isinstance(a, dict)] or [row.get("asset_path")]
        for path in [p for p in paths if p]:
            try:
                length = _file_seconds(str(path))
            except (OSError, EOFError, wave.Error, struct.error, IndexError):
                length = None
            if length is None:
                unmeasured += 1
            else:
                seconds += length
    return seconds / 60.0, unmeasured


def _hours(minutes: float) -> str:
    whole = int(round(minutes))
    return f"{whole // 60} h {whole % 60:02d} min" if whole >= 60 else f"{whole} min"


def _top_reasons(rows: list[dict], count: int) -> str:
    tally: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("error_code") or "UNKNOWN")
        tally[reason] = tally.get(reason, 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:count]
    return ", ".join(f"{reason} ×{n}" for reason, n in ranked)


def release_message(state: dict) -> str:
    """One sentence-and-a-bit about a finished release push (`release_publisher.publish`'s result)."""
    version = (state.get("release") or {}).get("version") or "?"
    results = [r for r in (state.get("locations") or {}).values() if isinstance(r, dict)]
    ok = [r for r in results if r.get("ok")]
    blocked = [r for r in results if not r.get("ok")]
    if not results:
        return f"⚠️ Release {version}: there are no controllers to push to."
    if not blocked:
        return f"🚀 Release {version} reached all {len(ok)} controller{'s' if len(ok) != 1 else ''}."
    lines = [f"⚠️ Release {version}: {len(ok)} of {len(results)} controllers took it."]
    for r in blocked[:5]:
        why = str(r.get("detail") or r.get("code") or "refused")
        lines.append(f"• {r.get('label') or 'A controller'}: {why[:140]}")
    if len(blocked) > 5:
        lines.append(f"…and {len(blocked) - 5} more")
    return "\n".join(lines)


#: The process-wide notifier. Other modules call `notifier.notify(...)` (below) and never build one.
notifier = Notifier()


def notify(event_type: str, text: str, *, dedupe_key: str | None = None) -> bool:
    """Send `text` as an `event_type` message, if Telegram is set up and that type is switched on."""
    return notifier.notify(event_type, text, dedupe_key=dedupe_key)
