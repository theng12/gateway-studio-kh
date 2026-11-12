"""Process-wide state the gateway freezes at first import, repointed per test.

`unittest discover` imports `gateway.*` once for the whole run, so the first test file's temp dirs
(and the last file's env) apply to every test. Anything that reads or writes the content authority
or content library must call `isolate_content` instead of trusting its own module-level env.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


def isolate_content(case, data_dir: Path, api_key: str | None = None) -> None:
    """Point content authority/library at `data_dir` (and optionally pin the API key) for `case`."""
    from gateway import config, content_packs
    data_dir = Path(data_dir)
    patches = [patch.object(config, "DATA_DIR", data_dir),
               patch.object(content_packs, "DATA_DIR", data_dir),
               patch.object(content_packs, "LIBRARY", data_dir / "content-library")]
    # `root: Path = LIBRARY` is bound when the function is defined, so patching LIBRARY alone is not enough.
    for name in ("import_archive", "inventory", "read_archive"):
        patches.append(patch.dict(getattr(content_packs, name).__kwdefaults__, {"root": data_dir / "content-library"}))
    if api_key is not None:
        patches.append(patch.dict(os.environ, {"GATEWAY_API_KEY": api_key}))
    for p in patches:
        p.start()
        case.addCleanup(p.stop)


def sandbox_env(prefix: str = "gateway-test-"):
    """Point every gateway path at a fresh temp dir. Call BEFORE the first `import gateway...`.

    For test files that never set an environment of their own: without it the first import binds
    the process to the owner's real Application Support. Returns the TemporaryDirectory so the
    caller keeps it alive (module global). Paths only; no API key, so it cannot decide auth for
    other files under `discover`.
    """
    import tempfile
    tmp = tempfile.TemporaryDirectory(prefix=prefix)
    root = Path(tmp.name)
    os.environ.update({
        "GATEWAY_APP_SUPPORT": str(root),
        "GATEWAY_DATA": str(root / "data"),
        "GATEWAY_ARTIFACTS": str(root / "data" / "artifacts"),
        "GATEWAY_DB": str(root / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(root / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(root / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(root / ".env"),
        "GATEWAY_ENDPOINT_FILE": str(root / "gateway-endpoint.json"),
        "GATEWAY_FLEET": str(root / "fleet.json"),
        "GATEWAY_VOICES": str(root / "voices.json"),
        "GATEWAY_VOICES_OUT": str(root / "voices.json"),
        "STUDIOHUB_READY_FILE": str(root / "no-hub.json"),
        "GATEWAY_NO_MIGRATE": "1",
        # Never the owner's real iCloud Drive or drives: see scripts/run_gate.py.
        "GATEWAY_BACKUP_ICLOUD": str(root / "backup-icloud"),
        "GATEWAY_BACKUP_LOCAL": str(root / "backup-local"),
        "GATEWAY_BACKUP_VOLUMES": str(root / "backup-volumes"),
        "GATEWAY_BACKUP_TRASH": str(root / "backup-trash"),
    })
    return tmp
