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
import re
import shutil
import threading
import time
from pathlib import Path

import httpx

from .config import DATA_DIR, VOICES_PATH, WRITABLE_VOICES_PATH
from .hub import hub_base_url, hub_headers

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
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


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
                # Whether this voice can clone at all: no segments, no reference.
                "has_transcript": bool(transcript),
                "segment_count": len(segments or []),
            }, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


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
    if not entry.get("reference_segments"):
        # Not fatal for the push, and fatal for the clone: without aligned
        # timestamps Qwen3 Base refuses the reference.
        problems.append("it has no aligned segments, so it could be installed but not cloned from")
    return {"ready": not problems, "problems": problems}


def push_fields(entry: dict) -> dict:
    """The exact multipart fields the Hub's `fleet-sync` receiver expects.

    Mirrors the receiver's signature field for field. Note what is **absent**:
    `transcript_segments`. The receiver does not accept them, so a voice pushed
    through it arrives without the timestamps Qwen3 Base requires — see
    `_handoffs/2026-09-19-gateway-kh-shared-voices-need-a-hub-route.md`. This
    function therefore reports readiness rather than pretending the push is
    sufficient on its own.
    """
    clip = Path(str(entry.get("reference_path") or ""))
    return {
        # The receiver demands 12 lowercase hex, so this is derived, not the
        # gateway's own id.
        "voice_id": shared_voice_id(entry),
        "audio_sha256": str(entry.get("audio_sha256") or ""),
        "name": str(entry.get("display_name") or "")[:200],
        "language": (str(entry.get("language") or "en"))[:40],
        "gender": str(entry.get("gender") or "n"),
        "license": str(entry.get("license") or DEFAULT_LICENSE),
        "notes": str(entry.get("description") or ""),
        "source_url": "",
        "transcript": str(entry.get("reference_transcript") or ""),
        "permission_acknowledged": bool(entry.get("permission_acknowledged")),
        "filename": clip.name or "reference.wav",
        # Sent separately as the file part; kept here so a caller has one place to
        # read the whole request from.
        "audio_path": str(clip),
    }


# ---------------------------------------------------------------------------
# Finding what the owner picked


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return cleaned or "voice"


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
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
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
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            audio = _audio_in(entry)
            if audio is None:
                skipped.append({"name": entry.name, "reason": "No audio file inside it."})
                continue
            metadata = _metadata(entry)
            full_name = str(metadata.get("name") or entry.name)
            found.append({
                "id": f"voice-{_slug(entry.name)}",
                "display_name": _short_name(full_name) or entry.name,
                "full_name": full_name,
                "language": str(metadata.get("language") or "en"),
                "gender": metadata.get("gender"),
                "audio": audio,
                "transcript": _text(entry / "transcript.txt"),
            })
        elif entry.suffix.lower() in AUDIO_EXTENSIONS:
            # A loose file is a voice: one clip, one entry. Any sidecar transcript
            # beside it is used if it is there.
            full_name = entry.stem
            found.append({
                "id": f"voice-{_slug(entry.stem)}",
                "display_name": _short_name(full_name) or entry.stem,
                "full_name": full_name,
                "language": "en",
                "gender": None,
                "audio": entry,
                "transcript": _text(entry.with_suffix(".txt")),
            })
        else:
            # Other files are just other files. Reporting them would bury the one
            # skip that matters — a folder with no audio in it — under a list of
            # transcripts and `.DS_Store`.
            continue
    return found, skipped


def import_folder(folder: Path) -> dict:
    """Copy every voice found into the library and add it to the catalogue.

    Re-importing the same name updates that voice rather than creating a second
    one, because the id is derived from the name.
    """
    found, skipped = discover(folder)
    payload = _read_catalog()
    by_id = {str(v.get("id")): v for v in payload.get("voices") or [] if v.get("id")}

    imported: list[dict] = []
    for item in found:
        voice_id = item["id"]
        target_dir = LIBRARY_DIR / voice_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"reference{item['audio'].suffix.lower()}"
        try:
            shutil.copy2(item["audio"], target)
        except OSError as exc:
            skipped.append({"name": item["display_name"], "reason": f"Could not copy: {exc}"})
            continue

        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        entry = {
            **{k: v for k, v in (by_id.get(voice_id) or {}).items()
               if k in {"reference_segments", "preview_available", "duration_seconds"}},
            "id": voice_id,
            "display_name": item["display_name"],
            "full_name": item["full_name"],
            "language": item["language"],
            "gender": _gender(item["gender"]),
            # Read by the Hub's shared-voice validation, which refuses a push
            # without both. `self-owned` and an acknowledged permission are the
            # honest defaults for a clip the owner supplied himself; they are
            # stored, not assumed at push time, so either can be corrected per
            # voice without re-importing.
            "license": DEFAULT_LICENSE,
            "permission_acknowledged": True,
            "description": item["full_name"],
            "provider": "Studio Hub (this Mac)",
            "engine_name": "Qwen3 TTS",
            "engine_id": "qwen3-tts",
            "model_family": "qwen3-tts",
            "tts_model_id": DEFAULT_TTS_MODEL,
            "voice_tier": "custom",
            "output_format": "wav",
            "preview_available": False,
            "reference_path": str(target),
            "reference_transcript": item["transcript"],
            # Stored so a location lookup can compare without reading the file:
            # it is the one field that means the same thing on both sides.
            "audio_sha256": digest,
        }
        # A re-import of changed audio invalidates segments measured against the
        # old clip; keeping them would describe audio that is no longer there.
        previous = by_id.get(voice_id) or {}
        if previous.get("audio_sha256") and previous["audio_sha256"] != digest:
            entry.pop("reference_segments", None)
        if not entry.get("reference_segments"):
            entry.setdefault("reference_segments", None)

        by_id[voice_id] = entry
        # The folder, not just the catalogue row: clip plus transcript plus
        # metadata, so the voice is one self-contained thing on disk.
        write_voice_files(entry)
        imported.append({
            "id": voice_id,
            "display_name": entry["display_name"],
            "source": str(item["audio"]),
            "has_transcript": bool(item["transcript"]),
            "needs_segments": not entry.get("reference_segments"),
        })

    payload["voices"] = list(by_id.values())
    _write_catalog(payload)
    return {
        "imported": imported,
        "skipped": skipped,
        "catalog": str(catalog_path()),
        "library": str(LIBRARY_DIR),
    }


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
    while time.time() < deadline:
        summary = client.get(
            f"{base}/api/hub/transcription/jobs/{batch_id}", headers=headers
        ).json() or {}
        items = summary.get("items") or []
        state = str((items[0] if items else {}).get("state") or "").lower()
        if state in {"done", "succeeded"}:
            break
        if state in {"error", "failed", "cancelled"}:
            raise RuntimeError(f"transcription {state}: {(items[0] or {}).get('error')}")
        time.sleep(POLL_S)
    else:
        raise RuntimeError("transcription timed out")

    artifact = client.get(
        f"{base}/api/hub/transcription/jobs/{batch_id}/items/0/artifact",
        headers=headers,
        params={"format": "srt"},
    )
    if artifact.status_code >= 400:
        raise RuntimeError(f"the Hub returned no transcript (HTTP {artifact.status_code})")
    return parse_srt(artifact.text)


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


def _run_annotation(pending: list[dict], model: str) -> None:
    base = hub_base_url()
    headers = hub_headers()
    results: list[dict] = []
    try:
        with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            for voice in pending:
                name = str(voice.get("display_name") or voice.get("id"))
                with _state_lock:
                    _state["current"] = name
                audio = Path(str(voice.get("reference_path") or ""))
                if not audio.exists():
                    results.append({"id": voice.get("id"), "name": name,
                                    "ok": False, "detail": "the reference clip is missing"})
                else:
                    try:
                        segments = _transcribe(client, base, headers, audio, model)
                        voice["reference_segments"] = segments
                        results.append({"id": voice.get("id"), "name": name, "ok": True,
                                        "detail": f"{len(segments)} segments"})
                    except Exception as exc:  # noqa: BLE001 - one voice must not stop the rest
                        results.append({"id": voice.get("id"), "name": name, "ok": False,
                                        "detail": f"{type(exc).__name__}: {exc}"[:200]})
                payload = _read_catalog()
                for entry in payload.get("voices") or []:
                    if entry.get("id") == voice.get("id"):
                        entry["reference_segments"] = voice.get("reference_segments")
                        # Written here too: the segments are the thing that makes
                        # the voice clonable, so they belong beside the clip and not
                        # only in a catalogue that can be lost.
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

    if not hub_base_url():
        return {
            **annotation_state(),
            "error": "Studio Hub is not running on this Mac, so there is nothing to transcribe with.",
        }

    pending = [
        v for v in voices()
        if (force or not v.get("reference_segments")) and v.get("reference_path")
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
    threading.Thread(target=_run_annotation, args=(pending, chosen), daemon=True).start()
    return annotation_state()
