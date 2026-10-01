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
