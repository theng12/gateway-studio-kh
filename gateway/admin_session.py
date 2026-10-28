"""Browser sign-in for `/admin`: an owner-set password, server-side sessions, per-IP backoff.

The gateway key keeps working as a bearer token, exactly as before. This is a second
door for a phone browser over Tailscale:

* The owner sets the password in the app's own Settings, which talks to the gateway
  from this Mac with the key (`require_local_app` in admin.py). Only a salted scrypt
  hash is stored, never the password.
* A correct password (or the gateway key) at the login page mints a random session
  token. Only its SHA-256 is kept, server-side, and the browser gets it as an
  HttpOnly, SameSite=Strict cookie. Not `Secure`: the Tailscale URL is plain http,
  where a Secure cookie would never be sent back.
* A session ends after 30 days without use, on Sign out, or when the password is
  changed or removed.
* Failed attempts back off per client address: five free, then 30 s doubling to 15 min.

**Guests (2026-10-28).** The owner can share the Gateway with a friend without handing
over the console. A guest is a *named* account with its own password, the same scrypt
storage, the same cookie, the same session lifetime and the same per-address backoff —
and a session role that the admin routes read. A guest session reaches the Create page
and its own jobs only; `admin.require_admin` refuses it everywhere else, and the app's
`/admin` middleware carries the same allowlist as a backstop, so a route added tomorrow
is owner-only until somebody says otherwise.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from .config import APP_SUPPORT

COOKIE = "gateway_admin_session"
IDLE_SECONDS = 30 * 86400
MIN_LENGTH = 8
FREE_ATTEMPTS = 5
_LOCK_BASE_S = 30
_LOCK_MAX_S = 15 * 60
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}
GUEST_NAME_MAX = 60

#: **Re-entrant on purpose.** Sessions and guest records share one critical section, and a
#: session check asks whether its guest is still enabled — so one function calls another that
#: takes the same lock. A plain `Lock` deadlocks there; an `RLock` does not.
_lock = threading.RLock()
_sessions: dict[str, dict] | None = None  # sha256(token) -> {"seen", "role", "guest_id", "name"}
# ponytail: in-memory per-address counters, reset by a restart; enough behind Tailscale.
_failures: dict[str, tuple[int, float]] = {}  # address -> (failures, locked until)


def _password_path() -> Path:
    return APP_SUPPORT / "admin-password.json"


def _sessions_path() -> Path:
    return APP_SUPPORT / "admin-sessions.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          maxmem=128 * 1024 * 1024, dklen=32)


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------
def password_set() -> bool:
    return bool(_read(_password_path()).get("hash"))


def set_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        raise ValueError(f"Use at least {MIN_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    _write(_password_path(), {
        "algorithm": "scrypt", **_SCRYPT, "salt": salt.hex(),
        "hash": _scrypt(password, salt, **_SCRYPT).hex(), "set_at": time.time(),
    })
    end_owner_sessions()


def remove_password() -> None:
    try:
        _password_path().unlink()
    except FileNotFoundError:
        pass
    end_owner_sessions()


def check_password(password: str) -> bool:
    record = _read(_password_path())
    if not record.get("hash") or not isinstance(password, str) or not password:
        return False
    try:
        got = _scrypt(password, bytes.fromhex(record["salt"]),
                      int(record["n"]), int(record["r"]), int(record["p"]))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), str(record["hash"]))


# ---------------------------------------------------------------------------
# Failed-attempt backoff
# ---------------------------------------------------------------------------
def retry_after(address: str) -> int:
    with _lock:
        _count, until = _failures.get(address, (0, 0.0))
    return max(0, math.ceil(until - time.time()))


def note_failure(address: str) -> None:
    with _lock:
        count, _until = _failures.get(address, (0, 0.0))
        count += 1
        until = 0.0
        if count >= FREE_ATTEMPTS:
            until = time.time() + min(_LOCK_MAX_S, _LOCK_BASE_S * 2 ** (count - FREE_ATTEMPTS))
        _failures[address] = (count, until)


def note_success(address: str) -> None:
    with _lock:
        _failures.pop(address, None)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _loaded() -> dict[str, dict]:
    """Every session, old and new.

    **A bare number is a pre-guest session and it is the owner's.** The file this reads was
    `{digest: last_used}`; rewriting it on upgrade would sign the owner out of every browser
    for a change that did not touch their access, so the old shape is read as what it meant.
    """
    global _sessions
    if _sessions is None:
        loaded: dict[str, dict] = {}
        for key, value in _read(_sessions_path()).items():
            if isinstance(value, dict):
                loaded[key] = {
                    "seen": float(value.get("seen") or 0.0),
                    "role": str(value.get("role") or "owner"),
                    "guest_id": value.get("guest_id"),
                    "name": str(value.get("name") or ""),
                }
            elif isinstance(value, (int, float)):
                loaded[key] = {"seen": float(value), "role": "owner", "guest_id": None, "name": ""}
        _sessions = loaded
    return _sessions


def create_session(*, role: str = "owner", guest_id: str | None = None,
                   name: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        sessions = _loaded()
        for key in [k for k, record in sessions.items() if now - record["seen"] > IDLE_SECONDS]:
            del sessions[key]
        sessions[_digest(token)] = {
            "seen": now, "role": "guest" if role == "guest" else "owner",
            "guest_id": guest_id if role == "guest" else None,
            "name": str(name or ""),
        }
        _write(_sessions_path(), sessions)
    return token


def session(token: str | None) -> dict | None:
    """The identity behind a session token, or `None`.

    **A guest's session is checked against the guest's own record on every request**, not only
    when it was minted: disabling or retiring a guest ends their access at once, without
    anyone having to remember to sweep a session file.
    """
    if not token:
        return None
    key = _digest(token)
    now = time.time()
    with _lock:
        sessions = _loaded()
        record = sessions.get(key)
        if record is None:
            return None
        if now - record["seen"] > IDLE_SECONDS:
            del sessions[key]
            _write(_sessions_path(), sessions)
            return None
        if record["role"] == "guest":
            guest = _guest_record(str(record.get("guest_id") or ""))
            if guest is None or guest.get("removed_at") or guest.get("disabled_at"):
                del sessions[key]
                _write(_sessions_path(), sessions)
                return None
            record["name"] = guest["name"]
        # Written at most hourly: idle expiry is measured in days, not requests.
        if now - record["seen"] > 3600:
            record["seen"] = now
            _write(_sessions_path(), sessions)
        return dict(record)


def valid_session(token: str | None) -> bool:
    return session(token) is not None


def end_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        sessions = _loaded()
        if sessions.pop(_digest(token), None) is not None:
            _write(_sessions_path(), sessions)


def end_all_sessions() -> None:
    with _lock:
        _loaded().clear()
        _write(_sessions_path(), {})


def end_owner_sessions() -> None:
    """Change the owner's password, and only the owner is signed out.

    `set_password` used to clear the file, which was the same thing while the owner was the
    only kind of session. Now it would silently revoke every guest as a side effect of an
    action about the owner's own password — a change nobody asked for.
    """
    with _lock:
        sessions = _loaded()
        doomed = [k for k, record in sessions.items() if record["role"] != "guest"]
        for key in doomed:
            del sessions[key]
        if doomed:
            _write(_sessions_path(), sessions)


def end_guest_sessions(guest_id: str) -> None:
    """Drop every session belonging to one guest. The owner's own sessions are never touched."""
    with _lock:
        sessions = _loaded()
        doomed = [k for k, record in sessions.items()
                  if record["role"] == "guest" and record.get("guest_id") == guest_id]
        for key in doomed:
            del sessions[key]
        if doomed:
            _write(_sessions_path(), sessions)


# ---------------------------------------------------------------------------
# Guest accounts
#
# Stored beside the owner's password, `0600`, scrypt only — the same parameters and the
# same refusal of a short password as the owner's own. **Removal is a soft retire**: the
# record and its history stay, and the account simply can never sign in again.
# ---------------------------------------------------------------------------
_GUEST_NAME = re.compile(r"^[\w .'’-]{1,%d}$" % GUEST_NAME_MAX)


def _guests_path() -> Path:
    return APP_SUPPORT / "admin-guests.json"


def _guest_rows() -> list[dict]:
    raw = _read(_guests_path()).get("guests")
    return [row for row in raw if isinstance(row, dict) and row.get("id")] if isinstance(raw, list) else []


def _save_guest_rows(rows: list[dict]) -> None:
    _write(_guests_path(), {"guests": rows})


def _guest_record(guest_id: str) -> dict | None:
    return next((row for row in _guest_rows() if str(row.get("id")) == str(guest_id)), None)


def _hash(password: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"algorithm": "scrypt", **_SCRYPT, "salt": salt.hex(),
            "hash": _scrypt(password, salt, **_SCRYPT).hex()}


def _matches(record: dict, password: str) -> bool:
    """Does this password match the stored hash? **Every answer costs one scrypt.**

    `not password` used to return immediately, so an empty password on a *real* name
    answered in microseconds while a *fake* name paid for the dummy hash — the login page
    became a way to ask "does this guest exist?", which the dummy hash in `check_guest`
    was added to prevent. An empty or non-string password is now hashed like any other
    (it simply never matches), so the two answers take the same time.
    """
    if not record.get("hash"):
        _scrypt(str(password if isinstance(password, str) else ""), _DUMMY_SALT, **_SCRYPT)
        return False
    try:
        got = _scrypt(str(password if isinstance(password, str) else ""),
                      bytes.fromhex(str(record["salt"])),
                      int(record["n"]), int(record["r"]), int(record["p"]))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), str(record["hash"]))


def public_guest(row: dict) -> dict:
    """One guest, without anything secret and without inventing an answer.

    `spend_recorded` is `false` for every guest because **this gateway keeps no per-job money
    record**; the page says so rather than printing 0, which would read as "this guest cost
    nothing" (the owner's rule: "no data" is never $0).
    """
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "paid_allowed": bool(row.get("paid_allowed")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_login_at": row.get("last_login_at"),
        "disabled": bool(row.get("disabled_at")),
        "removed": bool(row.get("removed_at")),
        "spend_recorded": False,
    }


def guests(*, include_retired: bool = True) -> list[dict]:
    rows = _guest_rows()
    if not include_retired:
        rows = [row for row in rows if not row.get("removed_at")]
    return [public_guest(row) for row in sorted(rows, key=lambda r: str(r.get("created_at") or 0))]


def get_guest(guest_id: str) -> dict | None:
    row = _guest_record(guest_id)
    return public_guest(row) if row else None


#: Names a guest may not take, because they are what an owner-side job says in the
#: "Requested by" column. A guest called "Owner" would be indistinguishable from the owner
#: in exactly the field that exists to tell them apart.
RESERVED_NAMES = {"owner", "this mac", "story studio", "guest"}

#: Lookalikes folded before a name is compared with the reserved list **and with the other
#: guests**. Student-of-an-impostor material: Cyrillic "О"/"е" and Greek "Ο" render as Latin
#: letters, and "0" is how "0wner" reads. `NFKC` has already turned fullwidth "０" into "0".
#: Folding for *comparison* only — the display name keeps the owner's own spelling.
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "г": "r", "е": "e", "ё": "e", "з": "3", "к": "k", "м": "m",
    "н": "h", "о": "o", "п": "n", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x",
    "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x",
    "0": "o",
})
_RESERVED_FOLDED = {name.casefold() for name in RESERVED_NAMES}


#: Named shapes whose last Unicode-name word is not the letter itself. Small, and only where a
#: script names the letter after its sound: Armenian "OH", Greek "OMICRON".
_NAMED_LOOKALIKES = {"oh": "o", "omicron": "o"}


def _fold_char(ch: str) -> str:
    """One character reduced to the Latin letter it reads as, or itself.

    Three steps, in order — the third is what makes this a *skeleton* rather than a list:

    1. the curated table (Cyrillic and Greek letters, `0` for `o`);
    2. `NFKD` minus combining marks, which folds an accented Latin letter to its base (`é`→`e`)
       and a compatibility form to its plain one (`ﬁ`→`fi`);
    3. otherwise the character's own Unicode name: `COPTIC CAPITAL LETTER O` and
       `LATIN LETTER SMALL CAPITAL W` end in the Latin letter, so they fold to it. A handful of
       scripts name the letter after its sound instead (`ARMENIAN … LETTER OH`,
       `GREEK … OMICRON`), and those are the small exception map above.

    Anything else stays itself: unfolding is honest, and a name that is not a reserved one in
    any script is not reserved.
    """
    mapped = ch.translate(_CONFUSABLES)
    if mapped != ch:
        return mapped
    if ch.isascii():
        return ch
    base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                   if not unicodedata.combining(c))
    if base and base.isascii():
        return base
    name = unicodedata.name(ch, "")
    tail = name.rsplit(" ", 1)[-1].casefold() if name else ""
    if len(tail) == 1 and tail.isascii() and tail.isalpha():
        return tail
    return _NAMED_LOOKALIKES.get(tail, ch)


def folded_name(name: str) -> str:
    """A name reduced to what it *looks like*, for the reserved list and for uniqueness.

    `"Ownеr"` with a Cyrillic `е` is not `"Owner"` to `str.lower`, but it is to a reader —
    and the Requested-by column is read by a person. `NFKC` first (compatibility forms,
    fullwidth digits), then casefold, then `_fold_char` on every character.
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    return "".join(_fold_char(ch) for ch in " ".join(text.split()).casefold())


def _clean_name(name: str) -> str:
    # NFKC for what is *stored* too: a fullwidth or decomposed spelling is the same name,
    # and keeping both spellings would make two "identical" guests that are not equal.
    value = " ".join(unicodedata.normalize("NFKC", str(name or "")).split())
    if not _GUEST_NAME.match(value):
        raise ValueError(f"Use a name of 1 to {GUEST_NAME_MAX} characters.")
    if folded_name(value) in _RESERVED_FOLDED:
        raise ValueError(
            f"'{value}' is reserved: it is what this Gateway calls itself or one of its own "
            f"clients. Pick a name that cannot be mistaken for the owner."
        )
    return value


def _name_taken(rows: list[dict], name: str, *, except_id: str | None = None) -> bool:
    # Folded, for the same reason the reserved list is: "Маria" and "Maria" are one name to
    # a reader, and two guests who differ only by a lookalike character are indistinguishable
    # in the Requested-by column.
    needle = folded_name(name)
    return any(folded_name(str(row.get("name") or "")) == needle
               and not row.get("removed_at") and str(row.get("id")) != str(except_id)
               for row in rows)


def add_guest(name: str, password: str, *, paid_allowed: bool = False) -> dict:
    clean = _clean_name(name)
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        raise ValueError(f"Use at least {MIN_LENGTH} characters.")
    with _lock:
        rows = _guest_rows()
        if _name_taken(rows, clean):
            raise ValueError(f"There is already a guest called {clean}.")
        now = time.time()
        row = {"id": "guest-" + secrets.token_hex(6), "name": clean,
               "paid_allowed": bool(paid_allowed), "created_at": now, "updated_at": now,
               "last_login_at": None, "disabled_at": None, "removed_at": None, **_hash(password)}
        rows.append(row)
        _save_guest_rows(rows)
        return public_guest(row)


def update_guest(guest_id: str, *, name: str | None = None, paid_allowed: bool | None = None,
                 disabled: bool | None = None) -> dict:
    """Rename, allow paid models, or disable. Never touches the password or the history."""
    with _lock:
        rows = _guest_rows()
        row = next((r for r in rows if str(r.get("id")) == str(guest_id)), None)
        if row is None:
            raise KeyError(guest_id)
        if row.get("removed_at"):
            raise ValueError("That guest was removed; add a new one instead.")
        if name is not None:
            clean = _clean_name(name)
            if _name_taken(rows, clean, except_id=str(guest_id)):
                raise ValueError(f"There is already a guest called {clean}.")
            row["name"] = clean
        if paid_allowed is not None:
            row["paid_allowed"] = bool(paid_allowed)
        if disabled is not None:
            row["disabled_at"] = time.time() if disabled else None
        row["updated_at"] = time.time()
        _save_guest_rows(rows)
        if disabled:
            end_guest_sessions(str(guest_id))
        return public_guest(row)


def set_guest_password(guest_id: str, password: str) -> dict:
    """A reset signs that guest out everywhere: a new password means the old session is not theirs."""
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        raise ValueError(f"Use at least {MIN_LENGTH} characters.")
    with _lock:
        rows = _guest_rows()
        row = next((r for r in rows if str(r.get("id")) == str(guest_id)), None)
        if row is None:
            raise KeyError(guest_id)
        if row.get("removed_at"):
            raise ValueError("That guest was removed; add a new one instead.")
        row.update(_hash(password))
        row["updated_at"] = time.time()
        _save_guest_rows(rows)
        end_guest_sessions(str(guest_id))
        return public_guest(row)


def retire_guest(guest_id: str) -> dict:
    """Remove: a soft retire. The row and its job count stay; the account can never sign in."""
    with _lock:
        rows = _guest_rows()
        row = next((r for r in rows if str(r.get("id")) == str(guest_id)), None)
        if row is None:
            raise KeyError(guest_id)
        row["removed_at"] = row.get("removed_at") or time.time()
        row["disabled_at"] = row.get("disabled_at") or row["removed_at"]
        row["updated_at"] = time.time()
        _save_guest_rows(rows)
        end_guest_sessions(str(guest_id))
        return public_guest(row)


_DUMMY_SALT = secrets.token_bytes(16)


def check_guest(name: str, password: str) -> dict | None:
    """The guest a name and password belong to, or `None`.

    **An unknown name still pays for a scrypt.** Otherwise "no such guest" answers in
    microseconds and a real name takes ~100 ms, which turns the login page into a way to
    enumerate who the owner has invited.
    """
    with _lock:
        rows = _guest_rows()
        row = next((r for r in rows
                    if str(r.get("name") or "").strip().lower() == str(name or "").strip().lower()
                    and not r.get("removed_at") and not r.get("disabled_at")), None)
        if row is None:
            _scrypt(str(password or "x"), _DUMMY_SALT, **_SCRYPT)
            return None
        if not _matches(row, password):
            return None
        row["last_login_at"] = time.time()
        _save_guest_rows(rows)
        return public_guest(row)


# ---------------------------------------------------------------------------
# CSRF: a cookie-authenticated write must come from this same origin.
# ---------------------------------------------------------------------------
def same_origin(request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    host = request.headers.get("host")
    if not source or not host or source == "null":
        return False
    return urlsplit(source).netloc.lower() == host.lower()


# ---------------------------------------------------------------------------
# Owner console: actions only the owner's own Gateway app window may take.
#
# Loopback is trusted and the gateway key also signs a browser in, so neither tells the owner apart
# from an agent holding the key. A permanent delete (a Mac's Trash Folder) therefore also needs this
# per-launch secret: the app makes it, keeps it in memory, and hands it to the gateway it spawns on a
# stdin pipe (never argv, the environment or a file, which other processes of this user can read).
# Only the app's native client sends it back, as `OWNER_HEADER`. A gateway the app adopted instead of
# spawning never got it, so the action stays refused until the app restarts the gateway.
# ---------------------------------------------------------------------------
OWNER_HEADER = "X-Gateway-Owner-Console"
_owner_secret = ""


def _read_owner_secret() -> None:
    global _owner_secret
    try:
        line = sys.stdin.readline(512).strip()
    except (OSError, ValueError):
        return
    if len(line) >= 32:
        _owner_secret = line


def owner_console(value: str | None) -> bool:
    return bool(_owner_secret) and bool(value) and hmac.compare_digest(str(value).encode(), _owner_secret.encode())


if os.environ.get("GATEWAY_OWNER_CONSOLE_STDIN") == "1":
    threading.Thread(target=_read_owner_secret, name="owner-console-secret", daemon=True).start()
