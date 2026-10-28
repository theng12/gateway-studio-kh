"""Stocking the voice catalogue from a folder the owner picks.

A voice is a reference clip plus a transcript, and the transcript is not
decoration: Qwen3 **Base** is a cloning model and refuses a reference without
aligned timestamps (`REFERENCE_TRANSCRIPT_ALIGNMENT_REQUIRED`). So the segments are
produced by transcribing each clip, and that means **a working transcription model
is a prerequisite for stocking voices at all**.

Two things this does that `scripts/import_voices.py` did not:

  * **It accepts what people actually have.** The old importer read only
    subdirectories containing `reference.<ext>`, so a loose `Morgan.wav` dropped
    into the folder was silently skipped. Here a bare audio file is a voice too.
  * **It owns the clip.** The catalogue used to point at wherever the clip came
    from, so moving or deleting that folder broke the voice. Imported clips are
    copied into this gateway's own data directory, which is a place that exists
    for the life of the install — and is what gets staged to a location later.

The clips live in `DATA_DIR/voice-library/<voice-id>/`, one folder per voice, next
to the catalogue (`voices.json`) that this writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from . import voice_audio
from .os_metadata import is_os_metadata
from .config import DATA_DIR, VOICES_PATH, WRITABLE_VOICES_PATH, write_secret
from .hub import hub_base_url, hub_probe, local_hub_headers

log = logging.getLogger("gateway.voice_import")

#: Where imported clips are copied. A subdirectory per voice, so a voice is one
#: thing on disk and deleting it removes exactly that voice's audio.
LIBRARY_DIR = Path(str(DATA_DIR)) / "voice-library"

AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aif", ".aiff")

#: The cloning-capable local model. Qwen3 Base takes a reference clip; the
#: CustomVoice/VoiceDesign variants take a preset speaker instead.
DEFAULT_TTS_MODEL = "Qwen3-TTS-12Hz-0.6B-Base-4bit"

#: Whisper, for the segments. Same default the standalone script used.
DEFAULT_TRANSCRIPTION_MODEL = "mlx-community/whisper-base-mlx"

# --- What a *shared* voice has to satisfy -------------------------------
#
# These are the Hub's own limits, read from its `shared_voices._validate`: a push
# that breaks one is answered with a 400, so the gateway checks them first and says
# which voice is the problem. Kept here rather than discovered per push because a
# refusal that arrives from the far end names no voice.

#: The Hub's `ALLOWED_GENDERS`. `n` is "not stated", which is what an unknown
#: gender has to become — the Hub will not accept an empty one.
GENDERS = {"m", "f", "n"}

#: The Hub's `ALLOWED_LICENSES`. `self-owned` is the honest default for a clip the
#: owner supplied; it is an attestation, so a wrong value is worse than a question.
LICENSES = {"self-owned", "public-domain", "permission", "unknown"}
DEFAULT_LICENSE = "self-owned"

#: The Hub's `ALLOWED_EXTENSIONS` and `MAX_BYTES` (25 MB).
SHARED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
SHARED_MAX_BYTES = 25_000_000
SHARED_MAX_TRANSCRIPT_CHARS = 200_000

POLL_S = 2.0
TRANSCRIBE_DEADLINE_S = 900.0

_TIMING = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})"
)


# ---------------------------------------------------------------------------
# Catalogue


def catalog_path() -> Path:
    """Where the catalogue is written.

    Prefers the writable copy and falls back to wherever the effective one lives,
    so the first import on a fresh install writes a file the gateway will then
    read rather than one it will keep ignoring until a restart.
    """
    return WRITABLE_VOICES_PATH


def _read_catalog() -> dict:
    path = catalog_path()
    if not path.exists() and VOICES_PATH.exists():
        path = VOICES_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"voices": []}
    if isinstance(payload, list):
        return {"voices": payload}
    payload.setdefault("voices", [])
    return payload


def _write_catalog(payload: dict) -> None:
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["_comment"] = (
        "Reference clips and transcripts drive Qwen3 Base cloning and are never "
        "exposed to Story Studio."
    )
    write_secret(path, json.dumps(payload, indent=2) + "\n")


def voices() -> list[dict]:
    return [v for v in _read_catalog().get("voices") or [] if isinstance(v, dict)]


def voice_dir(voice_id: str) -> Path:
    """One folder per voice: the clip *and* everything derived from it."""
    return LIBRARY_DIR / voice_id


def write_voice_files(entry: dict) -> None:
    """Write a voice's derived files beside its clip.

    A voice folder is meant to be self-contained — clip, transcript, timestamped
    segments, metadata — rather than a clip here and its meaning in a catalogue
    somewhere else. Two reasons, and the second is the point:

      * the catalogue can be rebuilt from the folders, so a lost `voices.json` is
        recoverable rather than a re-import of clips nobody kept;
      * a voice folder is exactly the unit that would be copied to a node, so
        making one portable now is what makes that possible later.

    Never raises: a catalogue entry that cannot write its files is still usable,
    and losing a whole import over an unwritable folder would be worse.
    """
    voice_id = str(entry.get("id") or "")
    if not voice_id:
        return
    directory = voice_dir(voice_id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        transcript = str(entry.get("reference_transcript") or "")
        if transcript:
            (directory / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")
        segments = entry.get("reference_segments")
        if segments:
            (directory / "segments.json").write_text(
                json.dumps(segments, indent=2) + "\n", encoding="utf-8"
            )
        (directory / "metadata.json").write_text(
            json.dumps({
                "id": voice_id,
                "display_name": entry.get("display_name"),
                "full_name": entry.get("full_name"),
                "language": entry.get("language"),
                "gender": entry.get("gender"),
                "audio_sha256": entry.get("audio_sha256"),
                # The reference file this folder serves (a proper WAV after
                # normalisation) and its own digest; `audio_sha256` names the original.
                "reference_file": Path(str(entry["reference_path"])).name if entry.get("reference_path") else None,
                "reference_sha256": entry.get("reference_sha256"),
                # Whether this voice can clone at all: no segments, no reference.
                "has_transcript": bool(transcript),
                "segment_count": len(segments or []),
                # Off: the clip clones from its audio alone (`transcript_included`).
                "include_reference_transcript": transcript_included(entry),
                # Permission to clone and distribute, granted per voice and never
                # assumed. `license` exists only once it has been granted.
                "permission_acknowledged": bool(entry.get("permission_acknowledged")),
                "license": entry.get("license"),
            }, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def catalogue_id(value: Any) -> str:
    """The id a voice has **in this catalogue**, from any spelling of it.

    A caller may name a voice the way the registry publishes it
    (`studio-hub-local::voice-a9aedc5c6bd3`) or the way the catalogue stores it
    (`voice-a9aedc5c6bd3`). These routes matched the stored form exactly, so the
    menu bar app — which sends the published form, because that is what
    `/admin/api/voices` returns — got `changed: 0` and reported success, pushed
    nothing, and **bypassed the 403 that refuses to share a voice whose owner
    never granted permission**. Both spellings are really sent, so both are
    accepted.

    **Only this Mac's own namespace is stripped.** The catalogue holds this Mac's
    clips and nothing else, so `studio-hub-local` is the only prefix that can be
    on an id it knows; stripping whatever precedes `::` would make
    `elevenlabs-direct::voice-a` and `volc::voice-a` the same voice, which is
    exactly the "identity derived from a label" mistake the fleet id rules exist
    to prevent.
    """
    from .fleet import LOCAL_ID, provider_id_for  # local import: avoids a cycle
    from .voices import ID_SEPARATOR

    raw = str(value or "")
    local_prefix = provider_id_for(LOCAL_ID) + ID_SEPARATOR
    return raw[len(local_prefix):] if raw.startswith(local_prefix) else raw


def set_permission(
    voice_ids: list[str], acknowledged: bool, *, license: str = ""
) -> dict:
    """Record, or withdraw, permission to clone and distribute these voices.

    **This is an attestation, so it is never assumed.** A voice is private until
    someone says otherwise: the owner has private voices he does not want cloned or
    distributed, and a default of "allowed" would distribute them by accident. That
    is also why import leaves it false — the tick has to be a deliberate act, not a
    side effect of adding a clip.

    Withdrawing is as important as granting: `acknowledged=False` puts a voice back
    to private, including one that was already pushed.
    """
    wanted = {catalogue_id(v) for v in voice_ids if v}
    if not wanted:
        return {"changed": 0, "acknowledged": acknowledged}

    payload = _read_catalog()
    changed = 0
    for entry in payload.get("voices") or []:
        if str(entry.get("id")) not in wanted:
            continue
        entry["permission_acknowledged"] = bool(acknowledged)
        if acknowledged:
            chosen = license or str(entry.get("license") or "") or DEFAULT_LICENSE
            entry["license"] = chosen if chosen in LICENSES else DEFAULT_LICENSE
        else:
            # Withdrawn: the license was part of the permission, so it goes too.
            # Leaving it behind would let a later push look authorised.
            entry.pop("license", None)
        entry["shared_at"] = time.time() if acknowledged else None
        write_voice_files(entry)
        changed += 1

    _write_catalog(payload)
    return {"changed": changed, "acknowledged": bool(acknowledged)}


def transcript_included(entry: dict) -> bool:
    """Whether this voice's words travel with its clip. Absent means yes (every older voice).

    Off is the owner's choice for a recording the transcriber cannot understand (Khmer,
    2026-10-24): its Whisper text is wrong, and a model like VoxCPM2 conditions on whatever
    words it is given. Off means audio only — no transcript, no segments, no transcription.
    """
    return entry.get("include_reference_transcript") is not False


def reference_words(entry: dict) -> tuple[str, list]:
    """The (transcript, segments) a clone may use: both empty when the owner turned them off."""
    if not transcript_included(entry):
        return "", []
    return str(entry.get("reference_transcript") or "").strip(), list(entry.get("reference_segments") or [])


#: A language label: a code like `km` or `en`, or a short name. Story shows it and searches on it;
#: the fleet share route caps it at 40 characters.
LANGUAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9 _()-]{0,39}")


#: The three the owner asked for (`m`/`f`/`n`), plus the words a person types. Stored as the
#: letter so one field cannot hold "male" for one voice and "m" for another.
GENDERS = {"m": "m", "male": "m", "f": "f", "female": "f", "n": "n", "neutral": "n", "": ""}


def clean_gender(value: str) -> str:
    """`m`, `f`, `n` or empty — anything else is refused rather than silently dropped."""
    text = str(value or "").strip().lower()
    if text not in GENDERS:
        raise ValueError("A voice's gender is m, f or n (or blank to clear it).")
    return GENDERS[text]


def update_voice(voice_id: str, *, language: str | None = None, include: bool | None = None,
                 transcript: str | None = None, gender: str | None = None) -> dict:
    """Change a voice's language label and/or its reference transcript. `None` leaves a field alone.

    Transcript on needs words to send: the text given here, a transcript already saved, or
    timed segments — on with none of those would be off while saying otherwise. Off keeps
    whatever is saved, so turning it back on restores it. An empty language clears the label.
    """
    wanted = catalogue_id(voice_id)
    label = None if language is None else str(language).strip()
    if label and not LANGUAGE_RE.fullmatch(label):
        raise ValueError("A language is a code like km or en, or a short name, up to 40 letters.")
    text = None if transcript is None else str(transcript).strip()
    payload = _read_catalog()
    for entry in payload.get("voices") or []:
        if str(entry.get("id")) != wanted:
            continue
        if include:
            words = text if text is not None else str(entry.get("reference_transcript") or "").strip()
            if not words and not entry.get("reference_segments"):
                raise ValueError("Type the words spoken in the clip to include a transcript.")
        if include is not None:
            entry["include_reference_transcript"] = bool(include)
        if include and text is not None:
            entry["reference_transcript"] = text
        if label is not None:
            entry["language"] = label
        if gender is not None:
            # **Blank clears it.** "Not stated" and "neutral" are different answers, and the
            # catalogue stores nothing for the first (the rule every other field follows).
            entry["gender"] = clean_gender(gender)
            if not entry["gender"]:
                entry.pop("gender", None)
        write_voice_files(entry)
        _write_catalog(payload)
        return {"id": wanted, "language": str(entry.get("language") or ""),
                "gender": str(entry.get("gender") or ""),
                "include_reference_transcript": transcript_included(entry),
                "reference_transcript": str(entry.get("reference_transcript") or "")}
    raise KeyError(voice_id)


def shareable() -> list[dict]:
    """Voices that carry permission, and could actually be pushed."""
    return [v for v in voices() if v.get("permission_acknowledged")]


def share(voice_ids: list[str], *, wait: bool = True) -> dict:
    """Push the named voices to every location. Never raises.

    Pushed to **controllers only** — the gateway does not talk to nodes, and the
    controller fans out to its own. That is why this is six requests and not a
    hundred, and why a node being off is `pending` rather than a failure.

    Each location needs the controller's **Hub token**, not its fleet token: the Hub
    refuses these routes to a fleet token deliberately, so a sold node can never
    administer a controller. A location without one is skipped and says so rather
    than failing silently.
    """
    from . import fleet

    wanted = {catalogue_id(v) for v in voice_ids if v}
    entries = [v for v in voices() if catalogue_id(v.get("id")) in wanted]
    missing = sorted(wanted - {catalogue_id(v.get("id")) for v in entries})

    results: list[dict] = []
    for entry in entries:
        for location in fleet.locations():
            label = str(location.get("label") or location.get("id"))
            if not location.get("reachable"):
                results.append({
                    "voice": entry.get("display_name"), "location": label,
                    "ok": False, "detail": location.get("error") or "not reachable",
                })
                continue
            token = fleet.admin_token_for(str(location.get("id") or ""))
            if not token:
                results.append({
                    "voice": entry.get("display_name"), "location": label, "ok": False,
                    "detail": "no Hub token stored for this location, and these routes are "
                              "refused to a fleet token on purpose",
                })
                continue
            outcome = push_to_controller(
                entry, str(location.get("url") or ""), token, wait=wait
            )
            results.append({
                "voice": entry.get("display_name"),
                "location": label,
                "ok": outcome.get("ok", False),
                "created": outcome.get("created"),
                "voice_id": outcome.get("voice_id"),
                "segments": outcome.get("segments"),
                "detail": outcome.get("detail") or summarise_targets(outcome.get("targets") or []),
                "sync": outcome.get("sync"),
            })
    return {"pushed": len(entries), "unknown": missing, "results": results}


def materialise() -> int:
    """Write the derived files for every voice that is missing them.

    Idempotent, and used as a backfill for voices imported before the folders were
    self-contained. Returns how many folders were completed.
    """
    completed = 0
    for entry in voices():
        directory = voice_dir(str(entry.get("id") or ""))
        wanted = [directory / "metadata.json"]
        if entry.get("reference_transcript"):
            wanted.append(directory / "transcript.txt")
        if entry.get("reference_segments"):
            wanted.append(directory / "segments.json")
        if not all(path.exists() for path in wanted):
            write_voice_files(entry)
            completed += 1
    return completed


def shared_voice_id(entry: dict) -> str:
    """The id this voice takes on the wire for fleet sharing.

    The Hub's receiver enforces *"voice_id must be exactly 12 lowercase hex
    characters"* (measured, 400 otherwise), so a gateway id like
    `voice-christian` cannot be pushed at all. Mine are derived from the clip's own
    `audio_sha256`, which makes them valid, **stable across re-imports**, and — the
    useful part — identical for identical audio. Pushing the same clip twice is
    therefore the same id twice, which is what lets the receiver's install be
    genuinely idempotent instead of accidentally creating a duplicate.
    """
    digest = str(entry.get("audio_sha256") or "")
    if len(digest) >= 12 and all(c in "0123456789abcdef" for c in digest[:12]):
        return digest[:12]
    # No hash stored yet: fall back to the gateway id only when it already fits,
    # otherwise this is not pushable and the caller is told so.
    existing = str(entry.get("id") or "")
    return existing if re.fullmatch(r"[0-9a-f]{12}", existing) else ""


def shared_readiness(entry: dict) -> dict:
    """Whether a voice could be pushed to the fleet, and why not if it could not.

    Checked here rather than learned from a 400: the Hub's refusal names a field,
    not the voice, so on a hundred-node push the useful question — *which* voice is
    wrong — would have no answer.
    """
    problems: list[str] = []
    name = str(entry.get("display_name") or "").strip()
    language = str(entry.get("language") or "").strip()
    clip = Path(str(entry.get("reference_path") or ""))

    if not shared_voice_id(entry):
        problems.append("it has no clip hash, so it has no valid shared id")
    if not name:
        problems.append("it has no name")
    elif len(name) > 200:
        problems.append("its name is over 200 characters")
    if not language:
        problems.append("it has no language")
    elif len(language) > 40:
        problems.append("its language is over 40 characters")
    if str(entry.get("gender") or "n") not in GENDERS:
        problems.append(f"its gender is not one of {sorted(GENDERS)}")
    if str(entry.get("license") or DEFAULT_LICENSE) not in LICENSES:
        problems.append(f"its license is not one of {sorted(LICENSES)}")
    if not entry.get("permission_acknowledged"):
        problems.append("permission to clone and distribute it was never acknowledged")
    if len(str(entry.get("reference_transcript") or "")) > SHARED_MAX_TRANSCRIPT_CHARS:
        problems.append("its transcript is too long")
    if not clip.exists():
        problems.append("its clip is missing")
    elif clip.suffix.lower() not in SHARED_EXTENSIONS:
        problems.append(f"its audio type {clip.suffix or '(none)'} cannot be shared")
    elif clip.stat().st_size > SHARED_MAX_BYTES:
        problems.append(
            f"its clip is {clip.stat().st_size // 1_000_000} MB, over the 25 MB limit"
        )
    if transcript_included(entry) and not entry.get("reference_segments"):
        # Not fatal for the push, and fatal for the clone: without aligned
        # timestamps Qwen3 Base refuses the reference.
        problems.append("it has no aligned segments, so it could be installed but not cloned from")
    return {"ready": not problems, "problems": problems}


def shared_segments(entry: dict) -> list[dict]:
    """The segments in the shape the Hub's shared-voice route requires.

    **Not the shape this module stores.** Mine are `{start, end, text}` because the
    Hub's *receiver* uses that; the shared-voice *route* wants
    `{start_seconds, end_seconds, text}`. Converting here rather than storing both
    keeps one source of truth on disk and one place where the difference is
    explained — and getting it wrong would be silent, because the field would be
    present and simply ignored.
    """
    out: list[dict] = []
    for segment in entry.get("reference_segments") or []:
        if not isinstance(segment, dict):
            continue
        start = segment.get("start_seconds", segment.get("start"))
        end = segment.get("end_seconds", segment.get("end"))
        text = str(segment.get("text") or "").strip()
        if start is None or end is None or not text:
            continue
        out.append({"start_seconds": float(start), "end_seconds": float(end), "text": text})
    return out


def push_fields(entry: dict) -> dict:
    """The exact multipart fields the Hub's shared-voice route expects.

    Mirrors the route's table field for field. `transcript_segments` is sent as JSON
    in the Hub's own `start_seconds`/`end_seconds` shape, and a transcript without
    it is refused — deliberately, because the alternative is a hundred nodes
    installing a voice that then refuses to clone with it.
    """
    clip = Path(str(entry.get("reference_path") or ""))
    return {
        # 12 lowercase hex, derived from the bytes. The Hub keeps it, so one clip
        # carries one id across every controller instead of one id per controller,
        # and the gateway's mapping stays one row per voice.
        "voice_id": shared_voice_id(entry),
        # The Hub checks this against the bytes uploaded, so it is the digest of the
        # reference file itself. For a voice whose reference was normalised to WAV that is
        # `reference_sha256`; `audio_sha256` keeps naming the *original* recording (the
        # identity re-imports match on, and the shared id above).
        "audio_sha256": str(entry.get("reference_sha256") or entry.get("audio_sha256") or ""),
        "name": str(entry.get("display_name") or "")[:200],
        # Verbatim. `shared_readiness` refuses a voice with no language before this
        # is reached, so a fallback here would only ever hide a claim — and "en" was
        # exactly that claim reaching the fleet.
        "language": str(entry.get("language") or "")[:40],
        "gender": str(entry.get("gender") or "n"),
        "license": str(entry.get("license") or DEFAULT_LICENSE),
        "notes": str(entry.get("description") or ""),
        "source_url": "",
        "transcript": reference_words(entry)[0],
        "transcript_segments": json.dumps(shared_segments(entry) if transcript_included(entry) else []),
        "permission_acknowledged": bool(entry.get("permission_acknowledged")),
        "filename": clip.name or "reference.wav",
        # Sent separately as the file part; kept here so a caller has one place to
        # read the whole request from.
        "audio_path": str(clip),
    }


def push_to_controller(
    entry: dict, base: str, hub_token: str, *, wait: bool = True, timeout: float = 900.0
) -> dict:
    """Create or refresh one shared voice on one controller. Never raises.

    The controller fans out to its own joined nodes; `wait=true` makes it run one
    pass before answering, so this can report what happened per node instead of
    only that the request was accepted.
    """
    if not base or not hub_token:
        return {"ok": False, "detail": "No address or Hub token for that controller."}
    readiness = shared_readiness(entry)
    if readiness["problems"]:
        # Refused here, naming the voice, rather than as a 400 from the far end that
        # names only a field and leaves you guessing across a fleet.
        return {"ok": False, "detail": "; ".join(readiness["problems"])}

    fields = push_fields(entry)
    clip = Path(fields["audio_path"])
    form = {
        key: str(value) for key, value in fields.items()
        if key not in {"audio_path", "filename"}
    }
    try:
        with clip.open("rb") as handle:
            response = httpx.post(
                f"{base.rstrip('/')}/api/hub/shared-voices",
                params={"wait": "true"} if wait else None,
                headers={"X-Hub-Token": hub_token},
                files={"audio": (fields["filename"], handle, "application/octet-stream")},
                data=form,
                timeout=timeout,
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}

    if response.status_code >= 400:
        return {"ok": False, "status": response.status_code, "detail": response.text[:300]}
    body = response.json() or {}
    voice = body.get("voice") or {}
    return {
        "ok": True,
        "created": bool(body.get("created")),
        "voice_id": voice.get("id"),
        "segments": voice.get("segment_count"),
        "sync": voice.get("sync") or {},
        "targets": voice.get("targets") or [],
    }


def summarise_targets(targets: list[dict]) -> str:
    """A sentence a person can act on, from the route's per-node statuses.

    `pending` is not a failure and must not read as one: it means that Mac was off
    and the controller's reconciler will retry. Calling it an error would send
    someone to a site that was never broken.
    """
    if not targets:
        return "no nodes reported"
    counts: dict[str, int] = {}
    for target in targets:
        key = str(target.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    parts: list[str] = []
    if counts.get("synced"):
        parts.append(f"{counts['synced']} installed")
    if counts.get("pending"):
        parts.append(f"{counts['pending']} will install when that Mac is next on")
    for key in ("failed", "conflict", "unsupported"):
        if counts.get(key):
            parts.append(f"{counts[key]} {key}")
    return ", ".join(parts) if parts else "no nodes reported"


# ---------------------------------------------------------------------------
# Finding what the owner picked


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return cleaned or "voice"


def clip_digest(path: Path) -> str:
    """The sha256 of a clip's bytes — the one thing that identifies it.

    Shared with `shared_voice_id`, which sends the first 12 hex to a controller,
    so "the same recording" means the same digest on both sides of the wire.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def voice_id_for_digest(digest: str) -> str:
    """The id a clip is stored under: its own bytes.

    `voice-<sha256[:12]>` — twelve lowercase hex, the same shape as the ids the
    VoiceStudio migration brought in, so nothing downstream can tell the two
    schemes apart.

    **A name is not an identity.** Deriving the id from the folder or file name
    meant the same recording re-imported under another name became a second
    voice, and two clips whose names slugged alike (`Morgan`, `morgan!`) silently
    replaced one another. Bytes decide: the same clip is one voice, and a
    different clip claiming a used name is paired with the existing row by
    `_existing_id_for` rather than overwriting it.
    """
    return f"voice-{digest[:12]}"


def _metadata_for_file(audio: Path) -> dict:
    """A loose clip's own facts, from a sibling `<name>.json` when it has one.

    Loose files used to have no way to state anything, so their language was
    *assumed* to be English. Reading a sidecar is the same treatment the transcript
    already gets (`<name>.txt`), and it costs nothing when the file is absent.
    """
    sidecar = audio.with_suffix(".json")
    if not sidecar.exists():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _audio_in(directory: Path) -> Path | None:
    """The reference clip inside a voice folder.

    `reference.<ext>` first because that is the convention the existing library
    uses, then any single audio file, so a folder the owner made by hand works.
    """
    for extension in AUDIO_EXTENSIONS:
        candidate = directory / f"reference{extension}"
        if candidate.is_file():
            return candidate
    others = sorted(
        p for p in directory.iterdir()
        if not is_os_metadata(p.name) and p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )
    return others[0] if len(others) == 1 else None


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _metadata(directory: Path) -> dict:
    path = directory / "metadata.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _short_name(full: str) -> str:
    for separator in (" - ", " – ", " — "):
        if separator in full:
            return full.split(separator, 1)[0].strip()
    return full.strip()


def _gender(value: object) -> str:
    """Normalise a gender onto the Hub's {m, f, n}.

    The Hub rejects anything else outright, and "not stated" has to be `n` rather
    than an empty string — which is what a voice imported from a loose audio file
    has, since a bare file carries no metadata at all.
    """
    text = str(value or "").strip().lower()
    if text in GENDERS:
        return text
    return text[:1] if text[:1] in {"m", "f"} else "n"


def discover(folder: Path) -> tuple[list[dict], list[dict]]:
    """(voices found, entries skipped with a reason).

    Both shapes are accepted, because both are things people have: a folder of
    per-voice folders (the existing library), and a folder of loose audio files.
    """
    found: list[dict] = []
    skipped: list[dict] = []
    if not folder.is_dir():
        return [], [{"name": str(folder), "reason": "That is not a folder."}]

    for entry in sorted(folder.iterdir()):
        if not entry.name.startswith("."):
            _discover_entry(entry, found, skipped)
    return found, skipped


def discover_paths(paths: list[Path]) -> tuple[list[dict], list[dict]]:
    """What the owner picked one by one: each file is a clip, each folder is read as `discover` reads one.

    The picker used to accept folders only, so adding one voice meant adding a whole folder.
    """
    found: list[dict] = []
    skipped: list[dict] = []
    for path in paths:
        if is_os_metadata(path.name):
            continue
        if path.is_dir():
            more, missed = discover(path)
            found += more
            skipped += missed
        elif path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            _discover_entry(path, found, skipped)
        else:
            skipped.append({"name": path.name or str(path), "source": str(path),
                            "reason": "Not an audio file this can read." if path.exists() else "Not found."})
    return found, skipped


def _discover_entry(entry: Path, found: list[dict], skipped: list[dict]) -> None:
    """One picked thing — a voice folder or a loose clip — into `found`, or `skipped` with a reason."""
    if is_os_metadata(entry.name):
        return
    if entry.is_dir():
        audio = _audio_in(entry)
        if audio is None:
            skipped.append({"name": entry.name, "source": str(entry),
                            "reason": "No audio file inside it."})
            return
        metadata = _metadata(entry)
        full_name = str(metadata.get("name") or entry.name)
        item = {
            # The clip's bytes, not the folder's name — see `voice_id_for_digest`.
            "slug_id": f"voice-{_slug(entry.name)}",
            "display_name": _short_name(full_name) or entry.name,
            "full_name": full_name,
            # **Not `or "en"`.** A clip whose language nobody stated was stored
            # as English and republished as this voice's own, which also made the
            # readiness gate's "it has no language" unreachable. The Hub really
            # does require a language ("language is required"), so the honest
            # sequence is: store nothing, refuse the push locally in one sentence,
            # and let the owner state it.
            "language": str(metadata.get("language") or ""),
            "gender": metadata.get("gender"),
            "audio": audio,
            "transcript": _text(entry / "transcript.txt"),
        }
    elif entry.suffix.lower() in AUDIO_EXTENSIONS:
        # A loose file is a voice: one clip, one entry. Any sidecar transcript
        # beside it is used if it is there.
        full_name = entry.stem
        # A loose clip is a voice with no folder to carry its facts, so a sibling
        # `<name>.json` is read if it is there — the same way the transcript
        # sidecar already is. **Without it the language is empty, not "en":** a
        # bare audio file states nothing, and inventing English is what made the
        # fleet receive a voice labelled in a language nobody chose. A voice with
        # no language cannot be pushed, and the readiness gate says so in one
        # sentence before the Hub would answer 400 naming only a field.
        sidecar = _metadata_for_file(entry)
        item = {
            "slug_id": f"voice-{_slug(entry.stem)}",
            "display_name": _short_name(str(sidecar.get("name") or full_name)) or entry.stem,
            "full_name": full_name,
            "language": str(sidecar.get("language") or ""),
            "gender": sidecar.get("gender"),
            "audio": entry,
            "transcript": _text(entry.with_suffix(".txt")),
        }
    else:
        # Other files are just other files. Reporting them would bury the one
        # skip that matters — a folder with no audio in it — under a list of
        # transcripts and `.DS_Store`.
        return

    try:
        item["digest"] = clip_digest(item["audio"])
    except OSError as exc:
        # An unreadable clip has no identity, so it cannot be stored as one.
        skipped.append({"name": item["display_name"], "source": str(entry),
                        "reason": f"Could not read it: {exc}"})
        return
    item["id"] = voice_id_for_digest(item["digest"])
    item["source"] = str(entry)
    found.append(item)


def _legacy_id_for(by_id: dict[str, dict], item: dict) -> str | None:
    """A row this gateway minted from a *name* before it recorded bytes, if this clip is it.

    Such a row has no digest, so the name is the only evidence it has, and a re-import of
    it updates in place. Every other match is by bytes (a skip) or by name (a question).
    """
    slug_id = str(item.get("slug_id") or "")
    legacy = by_id.get(slug_id)
    return slug_id if legacy is not None and not legacy.get("audio_sha256") else None


def _free_id(by_id: dict[str, dict], digest: str) -> str:
    """`voice-<sha[:12]>`, or a later 12-hex window of the same digest when that id is taken.

    Taken only after a **Replace**, which keeps a voice's id while its bytes change, so the
    id of the old recording can still be held by the voice that replaced it.
    """
    for start in range(0, len(digest) - 11):
        candidate = f"voice-{digest[start:start + 12]}"
        if candidate not in by_id:
            return candidate
    return f"voice-{digest}"


def _name_key(name: object) -> str:
    return str(name or "").strip().casefold()


def _free_name(by_id: dict[str, dict], name: str) -> str:
    """`Chris 2`, `Chris 3`… — the first that no voice uses."""
    used = {_name_key(v.get("display_name")) for v in by_id.values()}
    n = 2
    while _name_key(f"{name} {n}") in used:
        n += 1
    return f"{name} {n}"


def imported_at(entry: dict) -> float | None:
    """When this voice's clip came in: stamped at import, else its folder's creation time.

    Voices imported before the stamp existed still have the folder they were created with,
    and re-imports never re-created it, so its birth time is the first import.
    """
    if entry.get("imported_at"):
        return float(entry["imported_at"])
    if not entry.get("reference_path"):
        return None
    try:
        info = Path(str(entry["reference_path"])).parent.stat()
    except OSError:
        return None
    return float(getattr(info, "st_birthtime", 0) or info.st_mtime)


def clip_seconds(entry: dict) -> float | None:
    """The clip's length: as recorded, else read from the WAV header (never guessed)."""
    if entry.get("duration_seconds"):
        return float(entry["duration_seconds"])
    path = entry.get("reference_path")
    return voice_audio.seconds(Path(str(path))) if path else None


def _summary(entry: dict) -> dict:
    """How the owner tells two same-named voices apart: id, length, import date."""
    return {
        "id": entry.get("id"),
        "display_name": entry.get("display_name"),
        "duration_seconds": clip_seconds(entry),
        "imported_at": imported_at(entry),
    }


def backup_dir() -> Path:
    """Where a removed voice, or a replaced clip, is moved. Never unlinked; older backups retire into
    the Gateway Trash after the artifact retention (`sweep_backups`)."""
    return LIBRARY_DIR.parent / "voice-backups"


def sweep_backups(now: float | None = None, *, days: float | None = None) -> dict:
    """Retire backup folders older than the artifact retention into the Gateway Trash.

    One folder per removed or replaced voice, written once, so its modification time is when the backup
    was taken. A failed move leaves it where it is; nothing here is unlinked. The live voice library is
    never touched.
    """
    from . import retention

    moment = time.time() if now is None else now
    keep_days = retention.settings()["max_age_days"] if days is None else float(days)
    cutoff = moment - keep_days * 86400.0
    root = backup_dir()
    retired = failed = 0
    if not root.is_dir() or root.is_symlink():
        return {"retired": 0, "failed": 0, "days": keep_days}
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return {"retired": 0, "failed": 0, "days": keep_days}
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir() or entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if retention.retire(entry, root=root, namespace="voice-backups", now=moment, tree=True):
            retired += 1
        else:
            failed += 1
    return {"retired": retired, "failed": failed, "days": keep_days}


def _back_up(entry: dict, reason: str) -> Path:
    """Move a voice's folder into `voice-backups/`, with its catalogue row beside it.

    Raises OSError rather than carrying on: a replace or remove that could not keep the
    old copy must not go ahead and lose it.
    """
    voice_id = str(entry.get("id") or "")
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    target = backup_dir() / f"{voice_id}--{stamp}--{reason}"
    n = 2
    while target.exists():
        target = backup_dir() / f"{voice_id}--{stamp}-{n}--{reason}"
        n += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    source = voice_dir(voice_id)
    if source.is_dir():
        shutil.move(str(source), str(target))
    else:
        target.mkdir()
    (target / "entry.json").write_text(
        json.dumps({**entry, "backed_up_at": time.time(), "reason": reason}, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def place_reference(source: Path, directory: Path) -> tuple[Path, Path | None]:
    """Copy a clip into a voice folder as a proper `reference.wav`.

    Returns `(reference, original)`. A real WAV is copied as it is. Anything else is
    decoded to WAV and the source is kept beside it as `original.<ext>`, so a voice never
    again has a container the app's player cannot open (a QuickTime file named `.mp3`
    previewed as nothing). **If it cannot be converted** the clip is copied unchanged, as it
    was before, rather than losing the import; `original` is then None.
    """
    directory.mkdir(parents=True, exist_ok=True)
    wav = directory / "reference.wav"
    if voice_audio.sniff(source) == "wav":
        if source.resolve() != wav.resolve():
            shutil.copy2(source, wav)
        return wav, None
    try:
        voice_audio.to_wav(source, wav)
    except RuntimeError as exc:
        log.warning("could not convert %s to WAV: %s", source, exc)
        fallback = directory / f"reference{source.suffix.lower()}"
        shutil.copy2(source, fallback)
        return fallback, None
    original = directory / f"original{source.suffix.lower()}"
    shutil.copy2(source, original)
    return wav, original


def migrate_references() -> dict:
    """Give every voice whose reference is not what its name says a proper `reference.wav`.

    The one-off repair for clips that are QuickTime/ISO-media containers named `.mp3` (the
    app's player cannot open them). Per voice: decode to `reference.wav`, move the old file
    to `original.<ext>` (moved, never deleted), point `reference_path` at the WAV and record
    `reference_sha256` -- the Hub-staging path hashes whatever `reference_path` names, so the
    staged sha is of the new file. `audio_sha256` is left alone: it names the original
    recording, which is what re-imports and the shared voice id are keyed on.

    Idempotent (a voice already on a WAV is skipped) and recoverable (a run that stopped after
    moving the old file finds `reference.wav` beside the missing path and repoints). One
    voice failing does not stop the rest. Logged.
    """
    payload = _read_catalog()
    migrated: list[str] = []
    failed: list[dict] = []
    for entry in payload.get("voices") or []:
        if not isinstance(entry, dict) or not entry.get("reference_path"):
            continue
        clip = Path(str(entry["reference_path"]))
        voice_id = str(entry.get("id") or "")
        wav = clip.with_name("reference.wav")
        try:
            if clip.is_file() and voice_audio.plain_media_type(clip):
                continue
            if clip.is_file():
                original = clip.with_name(f"original{clip.suffix.lower() or '.bin'}")
                if original.exists():
                    original = clip.with_name(f"original-{clip_digest(clip)[:8]}{clip.suffix.lower()}")
                if clip == wav:
                    # A MOV named `.wav`: the old bytes move aside first, the WAV is made from them.
                    os.replace(clip, original)
                    try:
                        voice_audio.to_wav(original, wav)
                    except RuntimeError:
                        os.replace(original, clip)
                        raise
                else:
                    if not (wav.is_file() and voice_audio.sniff(wav) == "wav"):
                        voice_audio.to_wav(clip, wav)
                    os.replace(clip, original)
                entry["original_path"] = str(original)
            elif wav.is_file() and voice_audio.sniff(wav) == "wav":
                pass  # the old file was already moved aside; repoint
            else:
                continue  # nothing there to repair
            entry["reference_path"] = str(wav)
            entry["reference_sha256"] = clip_digest(wav)
            migrated.append(voice_id)
            log.info("voice %s: reference %s -> %s (sha256 %s)", voice_id, clip.name, wav.name,
                     entry["reference_sha256"][:12])
        except (OSError, RuntimeError) as exc:
            failed.append({"id": voice_id, "detail": f"{type(exc).__name__}: {exc}"[:200]})
            log.warning("voice %s: reference migration failed: %s", voice_id, exc)
    if migrated:
        _write_catalog(payload)
        for entry in payload["voices"]:
            if entry.get("id") in migrated:
                write_voice_files(entry)
    return {"migrated": migrated, "failed": failed}


def import_folder(folder: Path, resolutions: dict | None = None, *, transcript: bool = True) -> dict:
    """Every voice in a folder (`discover`), imported by `import_found`."""
    return import_found(*discover(folder), resolutions, transcript=transcript)


def import_paths(paths: list[Path], resolutions: dict | None = None, *, transcript: bool = True) -> dict:
    """The files and folders the owner picked one by one, imported by `import_found`."""
    return import_found(*discover_paths(paths), resolutions, transcript=transcript)


#: What the owner may answer for a clip whose name is taken (`resolutions[source]`).
KEEP_BOTH, SKIP, REPLACE = "keep_both", "skip", "replace:"


def import_found(
    found: list[dict], skipped: list[dict], resolutions: dict | None = None, *, transcript: bool = True
) -> dict:
    """Copy each discovered clip into the library and the catalogue — or say why not.

    **The clip's bytes are the identity**, and nothing already here is changed by accident:

    * **the same recording** (same sha256, under any name) is *skipped* and named as such.
      It used to be re-imported over the voice it already was, which reset that voice's
      language, gender and fleet permission to whatever the new file said — usually nothing;
    * **a different recording under a name that is taken** is *not added*. It comes back in
      `conflicts`, and the caller asks the owner and sends the answer back in `resolutions`,
      keyed by the conflict's `source`: `keep_both` (added as "Name 2"), `skip`, or
      `replace:<voice-id>` — that voice keeps its id (channels using it keep working) and
      gets the new clip, the old folder moved to `voice-backups/` and the clip re-transcribed.
      Adding silently produced second Chris/Christian/Leo rows indistinguishable in the list;
    * a row minted from a name before bytes were recorded (no digest) updates in place.
    """
    resolutions = resolutions or {}
    payload = _read_catalog()
    by_id = {str(v.get("id")): v for v in payload.get("voices") or [] if v.get("id")}

    imported: list[dict] = []
    notes: list[str] = []
    conflicts: list[dict] = []
    for item in found:
        digest = str(item["digest"])
        source = str(item["source"])
        name = str(item["display_name"])
        same = next((v for v in by_id.values() if v.get("audio_sha256") == digest), None)
        if same is not None:
            skipped.append({
                "name": name, "source": source, "duplicate": True, "id": same.get("id"),
                "reason": f"The same recording is already here as “{same.get('display_name')}”.",
            })
            continue

        previous: dict = {}
        voice_id = _legacy_id_for(by_id, item)
        clashes = [] if voice_id else [
            v for v in by_id.values() if _name_key(v.get("display_name")) == _name_key(name)
        ]
        choice = str(resolutions.get(source) or "")
        if clashes and not choice:
            conflicts.append({
                "name": name,
                "source": source,
                "message": f"A voice named “{name}” already exists.",
                "duration_seconds": voice_audio.seconds(Path(item["audio"])),
                "suggested_name": _free_name(by_id, name),
                "existing": [_summary(v) for v in clashes],
            })
            continue
        if clashes and choice.startswith(REPLACE):
            target = choice[len(REPLACE):]
            previous = next((v for v in clashes if v.get("id") == target), {})
            if not previous:
                skipped.append({"name": name, "source": source,
                                "reason": f"There is no voice {target} named “{name}” to replace."})
                continue
            voice_id = target
        elif clashes and choice == KEEP_BOTH:
            name = _free_name(by_id, name)
        elif clashes:
            skipped.append({"name": name, "source": source,
                            "reason": f"A voice named “{name}” already exists; you chose Skip."})
            continue
        voice_id = voice_id or _free_id(by_id, digest)

        target_dir = voice_dir(voice_id)
        try:
            if previous:
                # The new clip is staged first and the old folder backed up only then, so a
                # failed copy leaves the voice as it was. Kept, not overwritten: the old clip
                # is the only copy of that recording.
                staging = LIBRARY_DIR / f".incoming-{voice_id}"
                target, original = place_reference(item["audio"], staging)
                _back_up(previous, "replaced")
                os.replace(staging, target_dir)
                target = target_dir / target.name
                original = target_dir / original.name if original is not None else None
            else:
                target_dir.mkdir(parents=True, exist_ok=True)
                target, original = place_reference(item["audio"], target_dir)
        except OSError as exc:
            skipped.append({"name": name, "source": source, "reason": f"Could not copy: {exc}"})
            continue
        if target.suffix.lower() != ".wav" and voice_audio.plain_media_type(target) is None:
            notes.append(
                f"“{name}” could not be converted to WAV, so its "
                f"reference is kept as it came; it may not preview in the app."
            )

        if previous:
            entry = dict(previous)
            # A fact the new clip states wins; nothing stated keeps the voice's own.
            entry["language"] = item["language"] or entry.get("language") or ""
            if item["gender"]:
                entry["gender"] = _gender(item["gender"])
            if entry.get("permission_acknowledged"):
                notes.append(f"“{name}” has a new recording, so it is private again: "
                             "fleet permission was given for the old one.")
            entry.pop("license", None)
            entry.pop("original_path", None)
        else:
            entry = {
                "id": voice_id,
                "display_name": name,
                "full_name": item["full_name"],
                "language": item["language"],
                "gender": _gender(item["gender"]),
                "description": item["full_name"],
                "provider": "Studio Hub (this Mac)",
                "engine_name": "Qwen3 TTS",
                "engine_id": "qwen3-tts",
                "model_family": "qwen3-tts",
                "tts_model_id": DEFAULT_TTS_MODEL,
                "voice_tier": "custom",
                "output_format": "wav",
            }
        entry.update({
            # Read by the Hub's shared-voice validation, which refuses a push without a
            # license and an explicit acknowledgement. **Both start unset**: permission to
            # clone and distribute is the owner's to grant per voice (and per recording),
            # deliberately, because some of his voices are private.
            "permission_acknowledged": False,
            "shared_at": None,
            "preview_available": False,
            "reference_path": str(target),
            "reference_transcript": item["transcript"],
            # Segments are measured against a clip; a new clip has none until transcribed.
            "reference_segments": None,
            # The *original* recording, compared without reading the file;
            # `reference_sha256` is the reference file's.
            "audio_sha256": digest,
            "reference_sha256": clip_digest(target),
            # What tells two same-named voices apart in the list.
            "duration_seconds": voice_audio.seconds(target),
            "imported_at": time.time(),
            # `transcript=False`: audio only, for a language the transcriber cannot understand.
            "include_reference_transcript": bool(transcript),
        })
        if original is not None:
            entry["original_path"] = str(original)

        by_id[voice_id] = entry
        # The folder, not just the catalogue row: clip plus transcript plus
        # metadata, so the voice is one self-contained thing on disk.
        write_voice_files(entry)
        imported.append({
            "id": voice_id,
            "display_name": name,
            "source": source,
            "replaced": bool(previous),
            "has_transcript": bool(item["transcript"]),
            "needs_segments": True,
        })

    payload["voices"] = list(by_id.values())
    _write_catalog(payload)
    return {
        "imported": imported,
        "skipped": skipped,
        # Not added until the owner says what to do with a name that is taken.
        "conflicts": conflicts,
        # Things that happened and are not a failure: said out loud rather than
        # left for the owner to notice in the table.
        "notes": notes,
        "catalog": str(catalog_path()),
        "library": str(LIBRARY_DIR),
    }


def remove(voice_id: str) -> dict | None:
    """Take a voice out of the catalogue; its folder moves to `voice-backups/`. None if unknown.

    Never a delete: `restore` puts it back under the same id, so a channel that used it
    works again.
    """
    wanted = catalogue_id(voice_id)
    payload = _read_catalog()
    entry = next((v for v in payload.get("voices") or [] if str(v.get("id")) == wanted), None)
    if entry is None:
        return None
    entry = {**entry, "duration_seconds": clip_seconds(entry), "imported_at": imported_at(entry)}
    backup = _back_up(entry, "removed")
    payload["voices"] = [v for v in payload["voices"] if str(v.get("id")) != wanted]
    _write_catalog(payload)
    return {"removed": wanted, "display_name": entry.get("display_name"), "backup": backup.name}


def removed() -> list[dict]:
    """Removed voices that can be restored, newest first."""
    out: list[dict] = []
    root = backup_dir()
    for folder in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        if not folder.name.endswith("--removed"):
            continue
        try:
            entry = json.loads((folder / "entry.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({**_summary(entry), "backup": folder.name, "removed_at": entry.get("backed_up_at")})
    return out


def restore(backup: str) -> dict:
    """Put a removed voice back under its own id. Raises ValueError naming why it cannot."""
    folder = backup_dir() / Path(backup).name
    if not backup.endswith("--removed") or not (folder / "entry.json").is_file():
        raise ValueError("There is no removed voice by that name.")
    entry = json.loads((folder / "entry.json").read_text(encoding="utf-8"))
    entry.pop("backed_up_at", None)
    entry.pop("reason", None)
    voice_id = str(entry.get("id") or "")
    payload = _read_catalog()
    clash = next((v for v in payload.get("voices") or []
                  if v.get("id") == voice_id or v.get("audio_sha256") == entry.get("audio_sha256")), None)
    if clash is not None or voice_dir(voice_id).exists():
        holder = f" by “{clash.get('display_name')}”" if clash else ""
        raise ValueError(f"“{entry.get('display_name')}” cannot come back: this recording "
                         f"or its id is already in use{holder}.")
    shutil.move(str(folder), str(voice_dir(voice_id)))
    payload["voices"].append(entry)
    _write_catalog(payload)
    write_voice_files(entry)
    return {"restored": voice_id, "display_name": entry.get("display_name")}


# ---------------------------------------------------------------------------
# Making them usable: the segments


def parse_srt(text: str) -> list[dict]:
    segments: list[dict] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        index = next((i for i, line in enumerate(lines) if _TIMING.search(line)), None)
        if index is None:
            continue
        match = _TIMING.search(lines[index])
        if match is None:
            continue
        groups = match.groups()
        to_seconds = lambda h, m, s, ms: int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
        segments.append({
            "start": to_seconds(*groups[:4]),
            "end": to_seconds(*groups[4:]),
            "text": " ".join(lines[index + 1:]).strip(),
        })
    return segments


def _transcribe(client: httpx.Client, base: str, headers: dict, audio: Path, model: str) -> list[dict]:
    with audio.open("rb") as handle:
        response = client.post(
            f"{base}/api/hub/transcription/jobs",
            headers=headers,
            files={"files": (audio.name, handle, "application/octet-stream")},
            data={"model": model, "item_ids": "0", "word_timestamps": "true"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"the Hub refused it (HTTP {response.status_code}): {response.text[:160]}")
    batch_id = str((response.json() or {}).get("batch_id") or "")
    if not batch_id:
        raise RuntimeError("the Hub returned no batch id")

    deadline = time.time() + TRANSCRIBE_DEADLINE_S
    item: dict = {}
    while time.time() < deadline:
        summary = client.get(
            f"{base}/api/hub/transcription/jobs/{batch_id}", headers=headers
        ).json() or {}
        items = summary.get("items") or []
        item = items[0] if items else {}
        state = str(item.get("state") or "").lower()
        if state in {"done", "succeeded"}:
            break
        if state in {"error", "failed", "cancelled"}:
            raise RuntimeError(f"transcription {state}: {item.get('error')}")
        time.sleep(POLL_S)
    else:
        raise RuntimeError("transcription timed out")

    # The job's own metadata first: `GET .../jobs/{id}` already returns each
    # item's worker payload, so this is both richer and one round trip cheaper
    # than the artifact.
    segments = _segments_from_metadata(item)
    if segments:
        return segments

    artifact = client.get(
        f"{base}/api/hub/transcription/jobs/{batch_id}/items/0/artifact",
        headers=headers,
        params={"format": "srt"},
    )
    if artifact.status_code >= 400:
        raise RuntimeError(f"the Hub returned no transcript (HTTP {artifact.status_code})")
    return parse_srt(artifact.text)


def _segments_from_metadata(item: dict) -> list[dict]:
    """The worker's own segments, from the job metadata rather than its SRT.

    The artifact route serves **only** SRT — it takes no `format` parameter and
    is a `FileResponse` on the `.srt` file — while `GET /api/hub/transcription/jobs/{id}`
    hands back the worker payload as `metadata`. The segments are the same ones
    the SRT was rendered from, so reading them loses nothing and saves a request.

    **Word timings are used where the engine produces them.** The Hub's audits
    describe reference-window selection as "speech-aware and word-boundary-aligned",
    and its `_best_segment_window` cuts on whole segment boundaries — so finer
    segments let it fit closer to a model's cap instead of stopping a cue short.
    Measured on this Mac: `whisper-base-mlx` returns an **empty** `words` list
    even when `word_timestamps: "true"` is requested, so today this is a no-op and
    windows land on cue boundaries. A clip of 13.45s in three cues therefore
    yields an ~8.7s window under a 10s cap rather than a 10s one.

    ponytail: cue-level is the ceiling while the installed subtitle engine emits
    no word timings. Upgrade path — an engine that does (the Hub's `nemotron`
    path builds words itself), or mlx-whisper returning `words`; this function
    then starts returning word-level entries with no change at the call site.
    """
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("segments")
    if not isinstance(raw, list):
        return []

    cues: list[dict] = []
    words: list[dict] = []
    for segment in raw:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        cue = _span(segment.get("start"), segment.get("end"), text)
        if cue:
            cues.append(cue)
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            spoken = _span(word.get("start"), word.get("end"), str(word.get("word") or "").strip())
            if spoken:
                words.append(spoken)
    # A partial word list would describe audio the cues do not agree with, so it
    # is all-or-nothing: either every cue offered words and they cover the clip,
    # or the cues stand.
    return words if words and len(words) >= len(cues) else cues


def _span(start: Any, end: Any, text: str) -> dict | None:
    """One `{start, end, text}` entry, or None when it cannot be trusted.

    Ordered and non-empty is the whole contract the Hub's `_validated_segments`
    asks for; anything else is dropped here rather than sent to be refused.
    """
    try:
        begin = float(start)
        finish = float(end)
    except (TypeError, ValueError):
        return None
    text = str(text or "").strip()
    if not text or finish <= begin or begin < 0:
        return None
    return {"start": begin, "end": finish, "text": text}


#: One annotation pass at a time, with its progress readable while it runs. A
#: person who just picked a folder wants to watch it, and transcribing a library
#: takes long enough that a request that blocked on it would simply time out.
_state: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": "",
    "results": [],
    "error": None,
    "transcription_model": "",
    "started_at": None,
    "finished_at": None,
}
_state_lock = threading.Lock()


def annotation_state() -> dict:
    with _state_lock:
        return json.loads(json.dumps(_state))


def annotation_targets() -> list[dict]:
    """Every Hub that could transcribe a clip, **this Mac's first**.

    Segments come from a Hub's own transcription worker, and that worker is per-Mac. On
    2026-10-28 every transcription Mac behind this Gateway's own Hub was paused, so
    annotation answered `NO_AVAILABLE_MACHINE` — and a voice added here could never get the
    aligned segments that make it shareable, even though other Hubs in the fleet have a
    transcription worker free. The fleet is already how the Gateway reaches those Macs, so
    this is a list of the same places work is dispatched to, tried in order.

    Each target carries the credential **belonging to that Hub**: the local one uses the
    operator token through `local_hub_headers()`, a controller uses its own stored Hub token
    (`fleet.admin_token_for`, the same one the shared-voice routes need). A controller with
    no token is skipped rather than sent this Mac's operator token — that fallback is the
    mistake `hub_headers` was fixed to refuse.
    """
    from . import fleet
    from .hub import hub_headers

    targets: list[dict] = []
    base = hub_base_url()
    try:
        probe = hub_probe(base=base)
    except Exception:  # noqa: BLE001 - an unreadable Hub is "no local target", not a crash
        probe = {"running": False}
    if probe.get("running") and base:
        targets.append({"label": "this Mac", "base": base, "headers": local_hub_headers()})
    try:
        for location in fleet.locations():
            if location.get("local") or not location.get("reachable"):
                continue
            url = str(location.get("url") or "").rstrip("/")
            token = fleet.admin_token_for(str(location.get("id") or ""))
            if not url or not token:
                continue
            targets.append({"label": str(location.get("label") or location.get("id") or url),
                            "base": url, "headers": hub_headers(token, local=False)})
    except Exception:  # noqa: BLE001 - the fleet is a convenience here, never a gate
        pass
    return targets


def _run_annotation(pending: list[dict], model: str, targets: list[dict] | None = None) -> None:
    targets = targets if targets is not None else annotation_targets()
    results: list[dict] = []
    try:
        with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            for voice in pending:
                name = str(voice.get("display_name") or voice.get("id"))
                with _state_lock:
                    _state["current"] = name
                audio = Path(str(voice.get("reference_path") or ""))
                segments: list[dict] | None = None
                where = ""
                failure = ""
                if not audio.exists():
                    failure = "the reference clip is missing"
                else:
                    for target in targets:
                        try:
                            segments = _transcribe(client, target["base"], target["headers"],
                                                   audio, model)
                            where = str(target.get("label") or target["base"])
                            break
                        except Exception as exc:  # noqa: BLE001 - try the next Hub, report the last
                            failure = f"{type(exc).__name__}: {exc}"[:200]
                    if segments is not None:
                        results.append({"id": voice.get("id"), "name": name, "ok": True,
                                        "location": where,
                                        "detail": f"{len(segments)} segments from {where}"})
                    else:
                        results.append({"id": voice.get("id"), "name": name, "ok": False,
                                        "detail": failure or "no Hub could transcribe it"})
                payload = _read_catalog()
                for entry in payload.get("voices") or []:
                    if entry.get("id") == voice.get("id"):
                        # **Only a success writes segments.** A forced re-annotation whose
                        # transcription fails must leave the good segments that are already
                        # on disk exactly as they are — writing this back unconditionally
                        # would have erased them the first time a fleet went quiet.
                        if segments is not None:
                            entry["reference_segments"] = segments
                            # Written here too: the segments are what makes the voice
                            # clonable, so they belong beside the clip and not only in a
                            # catalogue that can be lost.
                            write_voice_files(entry)
                _write_catalog(payload)
                with _state_lock:
                    _state["done"] += 1
                    _state["results"] = results
    except Exception as exc:  # noqa: BLE001 - reported, not raised into a thread
        with _state_lock:
            _state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current"] = ""
            _state["finished_at"] = time.time()


def start_annotation(model: str = "", *, force: bool = False) -> dict:
    """Transcribe every voice that has no segments. Returns the state to poll.

    Refuses without a reachable Hub and says why: the segments come from Studio
    Hub's own transcription, so with no Hub there is nothing to transcribe with,
    and "silently did nothing" is the worst possible answer here.
    """
    with _state_lock:
        if _state["running"]:
            return annotation_state()

    # **Asked whether it answers, not whether a file exists.** This gated on
    # `hub_base_url()` — a URL in `hub-ready.json` — while the docstring promised
    # "refuses without a reachable Hub". With a stale ready-file the pass started
    # and every voice failed one at a time with a network error, burying the one
    # actionable sentence under N of them.
    targets = annotation_targets()
    if not targets:
        # **"Nothing can transcribe" is a different sentence from "this Mac's Hub is down".**
        # The old gate refused on the local probe alone, so a Gateway whose own Hub had every
        # transcription Mac paused could never annotate, even with a free worker elsewhere in
        # the fleet.
        why = ""
        try:
            why = str(hub_probe(base=hub_base_url()).get("detail") or "")
        except Exception:  # noqa: BLE001 - the sentence below stands without it
            pass
        return {
            **annotation_state(),
            "error": ("No Studio Hub that can transcribe is reachable: this Mac's Hub is not "
                      "answering and no joined location offers a Hub token to ask. "
                      + (why or "")).strip(),
        }

    pending = [
        v for v in voices()
        if (force or not v.get("reference_segments")) and v.get("reference_path")
        # Audio-only voices are never transcribed: the owner turned the words off.
        and transcript_included(v)
    ]
    if not pending:
        return {**annotation_state(), "total": 0, "done": 0,
                "error": None, "results": [], "current": ""}

    chosen = model or DEFAULT_TRANSCRIPTION_MODEL
    with _state_lock:
        _state.update({
            "running": True,
            "total": len(pending),
            "done": 0,
            "current": "",
            "results": [],
            "error": None,
            "transcription_model": chosen,
            "started_at": time.time(),
            "finished_at": None,
        })
    threading.Thread(target=_run_annotation, args=(pending, chosen, targets), daemon=True).start()
    return annotation_state()
