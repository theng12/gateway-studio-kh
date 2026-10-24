"""Configuration, paths and secret-file hygiene.

Three sources, in precedence order:

    environment  >  .env  >  defaults

**Where things live.** The app now ships its own Python and its own copy of the
gateway source inside the bundle, so nothing may be written next to the code —
a bundle is read-only in normal use, and replaced wholesale on update. That
splits the paths in two:

    BUNDLED  — shipped read-only defaults: the voice catalogue, the source itself
    DATA     — runtime state owned by the user: credentials, database, artifacts

Runtime state lives in `~/Library/Application Support/gateway-kh/`, which is
where a macOS app is expected to keep it, and is what makes the `.app` movable:
copy the bundle anywhere and the data stays put.

`.env` and the credential store are written and enforced `0600`. This process
holds credentials for every provider the owner has, so a world-readable secret
file would be the worst failure mode in the codebase.
"""

from __future__ import annotations

import os
import ipaddress
import shutil
import stat
import tempfile
from pathlib import Path

from .os_metadata import is_os_metadata

#: The gateway package's parent: `Contents/Resources/gateway` in the bundle, or
#: the repository root in a checkout. Bundled, read-only defaults hang off this.
ROOT = Path(__file__).resolve().parent.parent
BUNDLED_DATA_DIR = ROOT / "data"

#: Where macOS apps keep user data. Everything writable lives below this.
APP_SUPPORT = Path(
    os.environ.get("GATEWAY_APP_SUPPORT")
    or (Path.home() / "Library" / "Application Support" / "gateway-kh")
)

#: Data as it was laid out before the app was self-contained. Migrated on first
#: run so an existing install keeps its credentials and history.
LEGACY_DATA_DIR = ROOT / "data"

DATA_DIR = Path(os.environ.get("GATEWAY_DATA") or (APP_SUPPORT / "data"))
ARTIFACT_DIR = Path(os.environ.get("GATEWAY_ARTIFACTS") or (DATA_DIR / "artifacts"))
CREDENTIALS_PATH = Path(os.environ.get("GATEWAY_CREDENTIALS") or (DATA_DIR / "credentials.json"))
MODEL_CACHE_PATH = Path(os.environ.get("GATEWAY_MODEL_CACHE") or (DATA_DIR / "models_cache.json"))
DB_PATH = Path(os.environ.get("GATEWAY_DB") or (DATA_DIR / "gateway.sqlite3"))
ENV_PATH = Path(os.environ.get("GATEWAY_ENV_FILE") or (APP_SUPPORT / ".env"))

#: Provider definitions a user adds.
CUSTOM_PROVIDERS_PATH = DATA_DIR / "providers.local.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def secret_file_mode(path: Path) -> None:
    """Force owner-only read/write. Safe to call on any existing file."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


#: Files a pre-bundle install kept in `<repo>/data`.
_MIGRATED_FILES = (
    "credentials.json",
    "models_cache.json",
    "gateway.sqlite3",
    "providers.local.json",
    "voices.json",
    # The owner's own curated limit table. It was omitted, so on an upgrade the
    # file stayed behind in the pre-bundle directory: still *read*, because
    # `limits.py` falls back to the legacy path, but no longer the file anything
    # else looked at — and a curated value the owner cannot find is the same as
    # one he never wrote.
    "model-limits.local.json",
    # Per-voice enable/disable, which is his answer to "offer this voice?" — the
    # same class of decision as the model toggles `models_cache.json` carries.
    # Voice *permissions* travel in the voice's own folder metadata; this does not.
    "voices_state.json",
)


def migrate_legacy_data() -> dict[str, object]:
    """Move a pre-bundle install's data into Application Support.

    Idempotent: only runs when the destination has none of the files we would
    migrate. Files are **copied, not moved**, so a failed migration cannot lose
    credentials — the old directory is left intact as a fallback.

    `GATEWAY_NO_MIGRATE=1` disables it, which tests use to guarantee a clean
    slate rather than inheriting whatever the developer has configured.
    """
    result: dict[str, object] = {"migrated": [], "skipped": False}
    if os.environ.get("GATEWAY_NO_MIGRATE", "").strip() in {"1", "true", "yes"}:
        result["skipped"] = True
        return result
    if not LEGACY_DATA_DIR.exists():
        return result
    if LEGACY_DATA_DIR.resolve() == DATA_DIR.resolve():
        result["skipped"] = True
        return result

    # Test for the files themselves rather than "is the directory empty":
    # DATA_DIR and its artifacts subdirectory are created above, so an
    # emptiness check would always see a non-empty directory and never migrate.
    if any((DATA_DIR / name).exists() for name in _MIGRATED_FILES):
        result["skipped"] = True
        return result

    migrated: list[str] = []
    for name in _MIGRATED_FILES:
        source = LEGACY_DATA_DIR / name
        if source.exists():
            target = DATA_DIR / name
            shutil.copy2(source, target)
            secret_file_mode(target)
            migrated.append(name)

    legacy_artifacts = LEGACY_DATA_DIR / "artifacts"
    if legacy_artifacts.is_dir():
        for item in legacy_artifacts.iterdir():
            if not is_os_metadata(item.name) and item.is_file():
                shutil.copy2(item, ARTIFACT_DIR / item.name)
        migrated.append("artifacts/")

    result["migrated"] = migrated
    return result


# Run before any path below is resolved from what is on disk: the voice
# catalogue falls back to the bundled copy only if the data directory has none,
# so migrating afterwards would pin the wrong file for the whole process.
MIGRATION = migrate_legacy_data()


def migrate_legacy_env() -> bool:
    """Bring a pre-bundle `.env` across.

    Handled separately from the data files, and unconditionally: the data
    migration is skipped once its destination has content, so folding `.env`
    into it would mean anyone who migrated data first silently lost their API
    key — leaving the gateway accepting *any* bearer token.
    """
    legacy = ROOT / ".env"
    if os.environ.get("GATEWAY_NO_MIGRATE", "").strip() in {"1", "true", "yes"}:
        return False
    if not legacy.exists() or ENV_PATH.exists():
        return False
    try:
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, ENV_PATH)
        secret_file_mode(ENV_PATH)
        return True
    except OSError:
        return False


ENV_MIGRATED = migrate_legacy_env()

#: The voice catalogue. A user-edited copy in DATA_DIR wins; otherwise the
#: shipped default is used, so a fresh install has voices without any setup.
VOICES_PATH = Path(
    os.environ.get("GATEWAY_VOICES")
    or ((DATA_DIR / "voices.json") if (DATA_DIR / "voices.json").exists()
        else (BUNDLED_DATA_DIR / "voices.json"))
)

#: Where an import script should WRITE voices — never into the read-only bundle.
WRITABLE_VOICES_PATH = Path(os.environ.get("GATEWAY_VOICES_OUT") or (DATA_DIR / "voices.json"))


def load_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """Minimal `.env` reader: KEY=VALUE, `#` comments, optional quotes.

    Deliberately does not support interpolation or multi-line values — those
    are where dotenv implementations quietly diverge, and nothing here needs
    them.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


_ENV_FILE = load_env_file()
#: `(mtime, size)` of `.env` the loaded copy came from. `None` means "never
#: loaded", which the first `get()` treats as a change and reads the file.
#:
#: `.env` is the documented no-UI way to give the gateway a key, so editing it by
#: hand has to work. It did not: `get()` returned a module-level snapshot taken at
#: import, so `GATEWAY_API_KEY` was frozen for the life of the process — and after
#: rotating the key the **old one still worked**, which is the failure mode that
#: matters, because it is a rotation that appears to have happened and has not.
#:
#: `ponytail:` mtime+size is a change *detector*, not a content hash — a same-second
#: edit that leaves the size identical is missed. Cheap (one `stat` per read) and
#: enough for a file a person edits; the upgrade path is a content digest, or the
#: revision counter `providers/registry.py` uses for its own cache.
_ENV_STAMP: tuple[float, int] | None = None


def _refresh_env() -> None:
    """Re-read `.env` when it has changed on disk."""
    global _ENV_FILE, _ENV_STAMP
    try:
        info = ENV_PATH.stat()
        stamp: tuple[float, int] | None = (info.st_mtime, info.st_size)
    except OSError:
        stamp = None
    if stamp == _ENV_STAMP:
        return
    _ENV_FILE = load_env_file()
    _ENV_STAMP = stamp


def get(key: str, default: str = "") -> str:
    """Environment first, then `.env`, then the default.

    `.env` is re-read whenever it changes, so a hand edit is picked up without a
    restart — including a **rotation** of `GATEWAY_API_KEY`, where the old key
    otherwise keeps working until the process is restarted.
    """
    if key in os.environ:
        return os.environ[key]
    _refresh_env()
    return _ENV_FILE.get(key, default)


def write_secret(path: Path, text: str) -> None:
    """Write a file containing secrets, owner-only, atomically and without a readable window.

    A temp file in the same directory is created 0600 (so no world-readable window), flushed to
    disk, then renamed over the target. The old shape truncated the target and wrote into it, so a
    gateway killed mid-write -- it is restarted often -- left `models_cache.json` (every model
    toggle) or `credentials.json` empty or cut off, and the loaders read that as "nothing here".
    """
    target = Path(os.path.realpath(path))  # a symlinked file is written through, as before
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, target)
    except BaseException:
        try:
            os.unlink(scratch)  # our own half-written temp, never the target
        except OSError:
            pass
        raise
    finally:
        secret_file_mode(target)


# ---------------------------------------------------------------------------
# Runtime settings
# ---------------------------------------------------------------------------
def gateway_api_key() -> str:
    """The bearer token Story Studio presents.

    Keyless development accepts any nonempty token on loopback only. Both API
    guards refuse keyless access when the configured bind or actual peer is
    beyond loopback, or the request carries proxy forwarding headers.
    """
    return get("GATEWAY_API_KEY", "").strip()


def bind_host() -> str:
    return get("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1"


def bind_port() -> int:
    try:
        return int(get("GATEWAY_PORT", "8787"))
    except ValueError:
        return 8787


def is_loopback() -> bool:
    return bind_host() in {"127.0.0.1", "localhost", "::1"}


def request_is_loopback(request) -> bool:
    """Trust only a direct loopback peer, never proxy-supplied identity.

    Proxied admin traffic must authenticate even when its last hop is local.
    Supported headless deployments should also pass --no-proxy-headers.
    """
    if request is None or request.client is None:
        return False
    if any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")):
        return False
    try:
        address = ipaddress.ip_address(request.client.host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False
