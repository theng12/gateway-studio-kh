"""Curated model limits — the owner's own knowledge, where providers are silent.

Some lanes publish nothing. Anthropic's `/v1/models` returns only
`id/display_name/created_at/type`; DeepSeek's returns `id/object/owned_by`; OpenAI's
and NVIDIA's return non-JSON. Story Studio then has no context window and no output
allowance for those models, and — correctly — invents none.

Publishing `null` is honest but not the same as *knowing*. The owner does know
these models, so this is where that knowledge goes: a small file, outside the
code, that the catalogue reads.

**It fills gaps; it never overrides.** A value a provider actually publishes always
wins, because live data beats a file that goes stale. This file's job is only to
answer where nothing else does.

Absent file, absent entry, or unreadable file all mean "no opinion" — which is the
same `null` as before, so nothing here can make the catalogue *less* honest.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .fleet import publish_id

#: Where the owner's curated values live. Matches the `*.local.json` convention of
#: the provider file: user-owned, never shipped, and outside the bundle so it
#: survives an app reinstall.
LIMITS_PATH = Path(os.environ.get("GATEWAY_MODEL_LIMITS") or (DATA_DIR / "model-limits.local.json"))
LEGACY_LIMITS_PATH = Path(__file__).resolve().parent.parent / "data" / "model-limits.local.json"

#: Fields the overlay may supply. Deliberately a short list of the things a
#: provider can fail to publish and a person can know — not a general escape hatch
#: for rewriting the catalogue.
_KNOWN_FIELDS = (
    "context_window_tokens",
    "max_output_tokens",
    # **The owner's own request ceiling** (2026-11-02): how large a request this
    # gateway will accept for a model, measured in tokens. Nothing publishes it —
    # no provider or Hub states a *request* limit in a form a caller can size
    # against — so it is curated knowledge like the rest of this file, and the
    # admission check in `api._submit` reads it.
    "max_request_tokens",
    "max_prompt_characters",
    "maximum_input_characters",
)

_cache: dict = {"key": None, "limits": {}}


def _read() -> dict[str, dict[str, Any]]:
    path = LIMITS_PATH if LIMITS_PATH.exists() else LEGACY_LIMITS_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    clean: dict[str, dict[str, Any]] = {}
    for key, value in entries.items():
        if not isinstance(value, dict):
            continue
        clean[str(key)] = {
            field: value[field]
            for field in _KNOWN_FIELDS
            if isinstance(value.get(field), (int, float)) and value[field] > 0
        }
    return clean


def _all() -> dict[str, dict[str, Any]]:
    """Loaded once per file change. One `stat` on the common path.

    The key carries **mtime and size**, the same pair `providers/registry.py`
    documents for this exact hazard: an edit that preserves the mtime (a
    restore-from-backup, a copy that sets times) changes the size, and keying on
    mtime alone would keep serving the old numbers as though the file had not
    moved. `registry.py` guards this with a revision counter; here the file is
    read directly, so its own size is the second half of the key.
    """
    path = LIMITS_PATH if LIMITS_PATH.exists() else LEGACY_LIMITS_PATH
    try:
        stat_result = os.stat(path)
        key = (str(path), stat_result.st_mtime, stat_result.st_size)
    except OSError:
        key = (str(path), None, None)
    if _cache["key"] != key:
        _cache["key"] = key
        _cache["limits"] = _read()
    return _cache["limits"]


def for_model(provider_id: str, model_id: str) -> dict[str, Any]:
    """Curated values for one model, or `{}` when the owner has said nothing.

    **Both namespaces are tried.** The README documents the key as the *visible*
    `studio-hub::<model>` — the id Story Studio reads — while a location's models
    are stored under their concrete provider (`studio-hub-<controller>`), so an
    entry written the documented way was silently ignored for every location.
    The concrete id is tried first because it is the more specific fact; the
    published maker id is the fallback the owner can actually see.
    """
    limits = _all()
    for key_provider in (provider_id, publish_id(provider_id)):
        exact = limits.get(f"{key_provider}::{model_id}")
        if exact:
            return exact
    return limits.get(model_id, {})


def gap(field: str, published: Any, provider_id: str, model_id: str) -> Any:
    """`published` if the provider stated it, else the curated value, else None.

    The order matters and is not a preference: a live value from the provider is
    current, and this file is not.

    **`0` is a stated value, not a silence.** This used to test `published > 0`,
    so a provider that published `context_length: 0` — a real claim that the
    model accepts nothing — was treated as if it had said nothing and the curated
    file's number was published *as though the provider had stated it*. That is
    the one thing this module exists to prevent: "none" and "could not work it
    out" sharing a value, and then the wrong one being attributed to the
    provider. A caller that has no value to state passes `None`.
    """
    if isinstance(published, (int, float)) and not isinstance(published, bool):
        return int(published)
    curated = for_model(provider_id, model_id).get(field)
    if isinstance(curated, (int, float)) and not isinstance(curated, bool):
        return int(curated)
    return None


def entries() -> dict[str, dict[str, Any]]:
    """Every curated entry as written (model key -> fields), for the console's table."""
    return {key: dict(value) for key, value in _read().items()}


def set_entry(model_key: str, fields: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Write one model's curated limits (`None` or no fields removes it), keeping the rest and `_comment`.

    Values must be positive whole numbers; only the known fields are accepted. Written atomically
    to the user's file (never the legacy bundled one), which the catalogue re-reads on change.
    """
    import tempfile

    key = str(model_key or "").strip()
    if not key or len(key) > 300:
        raise ValueError("name the model, e.g. anthropic-direct::claude-haiku-5-5")
    clean: dict[str, int] = {}
    for field, value in (fields or {}).items():
        if field not in _KNOWN_FIELDS:
            raise ValueError(f"{field} is not a model limit this file can hold")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value <= 0:
            raise ValueError(f"{field} must be a positive whole number")
        clean[field] = int(value)
    path = LIMITS_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.name} is unreadable, so nothing was changed: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} is unreadable, so nothing was changed")
    models = dict(raw.get("models") or {}) if isinstance(raw.get("models"), dict) else {}
    if clean:
        models[key] = clean
    else:
        models.pop(key, None)
    raw["models"] = dict(sorted(models.items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    Path(name).replace(path)
    return entries()


def purge_provider(provider_id: str) -> int:
    """Remove curated limit overrides keyed to one provider id (`provider_id::*`)."""
    import tempfile

    if not provider_id:
        return 0
    prefix = f"{provider_id}::"
    path = LIMITS_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return 0
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
        return 0
    models = dict(raw["models"])
    gone = [k for k in models if isinstance(k, str) and k.startswith(prefix)]
    if not gone:
        return 0
    for k in gone:
        models.pop(k, None)
    raw["models"] = dict(sorted(models.items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    Path(name).replace(path)
    return len(gone)

