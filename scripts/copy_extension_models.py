#!/usr/bin/env python3
"""Copy Relay's browser-extension catalogues into the Gateway's capability JSON files.

Reproducible by construction: each output embeds the exact **sha256 of the source file** it was
read from, so a copied file can never be mistaken for a different catalogue, and `--check` proves
the on-disk JSON still matches its source.

    python scripts/copy_extension_models.py \
        --relay-image /Volumes/.../story-studio-image-generator/relay-gateway-20261109 \
        --relay-audio /Volumes/.../story-studio-audio-generator/relay-gateway-20261109 \
        [--check]

    --check   verify the JSON on disk matches what would be written; exit 1 on drift (writes nothing)

shortcut: extracts the named JS declarations with targeted regexes, not a JS parser; extraction
fails loudly if Relay's catalogue shape changes, so drift is never silent. Upgrade to a real
parser (or ask Relay to publish JSON) if the declarations move.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent / "gateway"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block(text: str, start: str, end: str = "];") -> str:
    """The text of a `START ... ];` declaration. Raises if the declaration is absent."""
    index = text.find(start)
    if index < 0:
        raise SystemExit(f"source does not declare {start!r}; re-read Relay's catalogue shape")
    stop = text.find(end, index)
    if stop < 0:
        raise SystemExit(f"source declaration {start!r} has no closing {end!r}")
    return text[index:stop]


def _int(pattern: str, text: str, what: str, default=None):
    match = re.search(pattern, text)
    if not match:
        if default is not None:
            return default
        raise SystemExit(f"source states no {what}; re-read Relay's catalogue shape")
    return int(match.group(1))


# --------------------------------------------------------------------------- image
def _image_json(worktree: Path) -> dict:
    source = (worktree / "src/lib/providers.js").read_text()
    block = _block(source, "FLOW_FALLBACK_MODELS = Object.freeze([")
    rows = re.findall(r"\{ id: '([^']+)', internalId: '([^']+)', label: '([^']+)'", block)
    if not rows:
        raise SystemExit("no Flow fallback models found; re-read Relay's catalogue shape")
    # Flow capabilities: maxReferences lives in the flow provider block.
    flow_block = re.search(r"id: 'flow'.*?capabilities:\s*\{(.*?)\}", source, re.S)
    max_refs = _int(r"maxReferences:\s*(\d+)", flow_block.group(1) if flow_block else "",
                    "flow maxReferences")
    return {
        "_note": "Copied by script from Relay's tested Flow catalogue; never hand-edited. "
                 "Re-copy when Relay's catalogue changes.",
        "source": "chrome-extensions/story-studio-image-generator/src/lib/providers.js "
                  "FLOW_FALLBACK_MODELS",
        "source_sha256": _sha256(source),
        "source_date": "2026-11-09",
        # Captured default (Relay image contract: "aspectRatio captured 16:9").
        "aspect_ratios": ["16:9"],
        "max_reference_images": max_refs,
        "models": [
            {"key": internal, "label": label, "provider": "flow"}
            for _id, internal, label in rows
        ],
    }


# --------------------------------------------------------------------------- audio
def _audio_json(worktree: Path) -> dict:
    source = (worktree / "src/lib/providers.js").read_text()
    fish_block = _block(source, "FISH_FALLBACK_MODELS = [")
    eleven_block = _block(source, "ELEVEN_FALLBACK_MODELS = [")

    fish_models = [
        {"key": mid, "label": label}
        for mid, label in re.findall(r"\{ id: '([^']+)', label: '([^']+)' \}", fish_block)
    ]
    # ElevenLabs entries state maxChars either as a literal or as the shared constant.
    const_match = re.search(r"ELEVEN_DEFAULT_MAX_CHARS\s*=\s*(\d+)", source)
    default_chars = const_match.group(1) if const_match else None

    def _chars(value: str) -> int:
        if value.isdigit():
            return int(value)
        if default_chars is None:
            raise SystemExit("ELEVEN_DEFAULT_MAX_CHARS is unset but referenced")
        return int(default_chars)

    eleven_models = []
    for match in re.finditer(
        r"\{ id: '([^']+)', label: '([^']+)', maxChars:\s*([\w]+),\s*style:\s*(true|false), "
        r"speakerBoost:\s*(true|false), speed:\s*(true|false), stability: '([^']+)'", eleven_block
    ):
        mid, label, chars, style, boost, speed, stability = match.groups()
        eleven_models.append({
            "key": mid, "label": label, "max_chars": _chars(chars),
            "style": style == "true", "speaker_boost": boost == "true",
            "speed": speed == "true", "stability": stability,
        })
    if not fish_models or not eleven_models:
        raise SystemExit("no fallback models found; re-read Relay's catalogue shape")

    providers_block = _block(source, "export const PROVIDERS = {", end="\n};")
    fish_block_prov = _provider_slice(providers_block, "fish")
    eleven_block_prov = _provider_slice(providers_block, "elevenlabs_web")
    fish_range = _speed_range(fish_block_prov)
    eleven_range = _speed_range(eleven_block_prov)
    return {
        "_note": "Copied by script from Relay's tested TTS catalogue; never hand-edited. "
                 "Re-copy when Relay's catalogue changes.",
        "source": "chrome-extensions/story-studio-audio-generator/src/lib/providers.js "
                  "FISH_FALLBACK_MODELS + ELEVEN_FALLBACK_MODELS",
        "source_sha256": _sha256(source),
        "source_date": "2026-11-09",
        "providers": {
            "fish": {
                "display_name": "fish.audio",
                "max_chars": _int(r"maxChars:\s*(\d+)", fish_block_prov, "fish maxChars"),
                "speed_range": fish_range,
                # Relay's fish capabilities: autoTag: true, no stability/similarity/speakerBoost.
                "settings": {"auto_tag": "autoTag: true" in fish_block_prov,
                             "provider_settings": []},
                "models": fish_models,
            },
            "elevenlabs_web": {
                "display_name": "ElevenLabs",
                "max_chars": _int(r"maxChars:\s*(\d+)", eleven_block_prov, "ElevenLabs maxChars"),
                "speed_range": eleven_range,
                # Relay's ElevenLabs capabilities: stability/similarity/speakerBoost; no autoTag.
                "settings": {"auto_tag": False,
                             "provider_settings": ["stability", "similarity", "speaker_boost"]},
                "models": eleven_models,
            },
        },
    }


def _provider_slice(providers_block: str, provider_id: str) -> str:
    match = re.search(rf"{re.escape(provider_id)}:\s*\{{(.*?)\n  \}},", providers_block, re.S)
    if not match:
        raise SystemExit(f"source states no {provider_id} provider block")
    return match.group(1)


def _speed_range(block: str) -> dict:
    match = re.search(r"speedRange:\s*\{\s*min:\s*([\d.]+),\s*max:\s*([\d.]+)\s*\}", block)
    if not match:
        raise SystemExit("source states no speedRange; re-read Relay's catalogue shape")
    return {"min": float(match.group(1)), "max": float(match.group(2))}


# --------------------------------------------------------------------------- write / check
def _emit(path: Path, data: dict, check: bool) -> bool:
    """Write `data` to `path`, or in `--check` compare. Returns True when it matches (or wrote)."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if check:
        current = path.read_text() if path.exists() else ""
        if current != text:
            print(f"DRIFT: {path} does not match its Relay source", file=sys.stderr)
            return False
        print(f"ok: {path}")
        return True
    path.write_text(text)
    print(f"wrote {path}")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay-image", type=Path, required=True)
    parser.add_argument("--relay-audio", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    ok = _emit(GATEWAY / "extension_image_models.json", _image_json(args.relay_image), args.check)
    ok &= _emit(GATEWAY / "extension_audio_models.json", _audio_json(args.relay_audio), args.check)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())