"""Configuration, paths and secret-file hygiene.

There is no config framework here on purpose. Three sources, in precedence order:

    environment  >  gateway-kh/.env  >  defaults

`.env` is the intended home for provider keys during development. Both `.env`
and the credential store are written and enforced `0600`, and both are
gitignored -- this process holds credentials for every provider the owner has,
so a world-readable secret file would be the single worst failure mode in the
codebase.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("GATEWAY_DATA", ROOT / "data"))
ARTIFACT_DIR = Path(os.environ.get("GATEWAY_ARTIFACTS", DATA_DIR / "artifacts"))
CATALOG_SEED = Path(os.environ.get("GATEWAY_CATALOG_SEED", DATA_DIR / "catalog.json"))
CREDENTIALS_PATH = Path(os.environ.get("GATEWAY_CREDENTIALS", DATA_DIR / "credentials.json"))
MODEL_CACHE_PATH = Path(os.environ.get("GATEWAY_MODEL_CACHE", DATA_DIR / "models_cache.json"))
DB_PATH = Path(os.environ.get("GATEWAY_DB", DATA_DIR / "gateway.sqlite3"))
ENV_PATH = Path(os.environ.get("GATEWAY_ENV_FILE", ROOT / ".env"))

ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """Minimal `.env` reader: KEY=VALUE, `#` comments, optional quotes.

    Deliberately does not support interpolation or multi-line values -- those
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


def get(key: str, default: str = "") -> str:
    """Environment first, then `.env`, then the default."""
    if key in os.environ:
        return os.environ[key]
    return _ENV_FILE.get(key, default)


def secret_file_mode(path: Path) -> None:
    """Force owner-only read/write. Safe to call on any existing file."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def write_secret(path: Path, text: str) -> None:
    """Write a file containing secrets, owner-only, without a world-readable window.

    `Path.write_text` creates with the process umask (often 0644), so a secret
    would be briefly readable before any later chmod. Creating with 0600 up
    front removes that window.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    finally:
        secret_file_mode(path)


# ---------------------------------------------------------------------------
# Runtime settings
# ---------------------------------------------------------------------------
def gateway_api_key() -> str:
    """The bearer token Story Studio presents.

    A gateway with no configured key accepts any token, which is fine for a
    loopback-only dev box and dangerous the moment the bind address changes --
    so `bind_host()` below stays loopback unless explicitly overridden.
    """
    return get("GATEWAY_API_KEY", "").strip()


def bind_host() -> str:
    return get("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1"


def bind_port() -> int:
    try:
        return int(get("GATEWAY_PORT", "8000"))
    except ValueError:
        return 8000


def is_loopback() -> bool:
    return bind_host() in {"127.0.0.1", "localhost", "::1"}
