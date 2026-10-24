"""Refuse a voice job whose language the chosen model cannot speak — before anything runs.

Owner, 2026-10-24: Khmer channels were moved onto Breeze TTS (English and Chinese only) by a
"use Breeze everywhere" preset change, and every job babbled, failed its section-quality check
four times and wasted the fleet's time. The model's own published language list decides, read
live from the catalogue (AGENTS rule 1); nothing here names a model.

Two facts are checked, and only against a list the model actually publishes (an empty list is
"unstated" and is never a reason to refuse):

* a `language` the request states;
* the writing system of the text itself. Only scripts that pin a language down are used
  (Khmer, Thai, Lao, Myanmar, Hangul, kana, Han, Arabic, Hebrew, Greek, Devanagari, Cyrillic…).
  Latin is shared by too many languages to judge, so Latin text is never refused on its script.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Unicode ranges → (script name, the language codes written in it).
SCRIPTS: tuple[tuple[tuple[tuple[int, int], ...], str, frozenset[str]], ...] = (
    (((0x1780, 0x17FF), (0x19E0, 0x19FF)), "Khmer", frozenset({"km"})),
    (((0x0E00, 0x0E7F),), "Thai", frozenset({"th"})),
    (((0x0E80, 0x0EFF),), "Lao", frozenset({"lo"})),
    (((0x1000, 0x109F),), "Myanmar", frozenset({"my"})),
    (((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)), "Korean", frozenset({"ko"})),
    (((0x3040, 0x30FF),), "Japanese kana", frozenset({"ja"})),
    (((0x4E00, 0x9FFF), (0x3400, 0x4DBF)), "Chinese characters", frozenset({"zh", "ja", "yue", "cmn"})),
    (((0x0600, 0x06FF), (0x0750, 0x077F)), "Arabic", frozenset({"ar", "fa", "ur", "ps"})),
    (((0x0590, 0x05FF),), "Hebrew", frozenset({"he", "yi"})),
    (((0x0370, 0x03FF),), "Greek", frozenset({"el"})),
    (((0x0900, 0x097F),), "Devanagari", frozenset({"hi", "mr", "ne", "sa"})),
    (((0x0400, 0x04FF),), "Cyrillic", frozenset({"ru", "uk", "bg", "sr", "be", "mk", "kk", "mn", "ky", "tg"})),
    (((0x10A0, 0x10FF),), "Georgian", frozenset({"ka"})),
    (((0x0530, 0x058F),), "Armenian", frozenset({"hy"})),
    (((0x0B80, 0x0BFF),), "Tamil", frozenset({"ta"})),
    (((0x0980, 0x09FF),), "Bengali", frozenset({"bn", "as"})),
)

#: A script must be at least this share of the text's letters to speak for it.
DOMINANT_SHARE = 0.3


def base_code(code: Any) -> str:
    """`en-US` / `zh_Hans` / `EN` → `en` / `zh` / `en`."""
    return str(code or "").strip().lower().replace("_", "-").split("-")[0]


def dominant_script(text: str) -> tuple[str, frozenset[str]] | None:
    """The non-Latin script most of the text's letters are written in, or None."""
    counts: dict[int, int] = {}
    letters = 0
    for char in text:
        if not char.isalpha() and not (0x1780 <= ord(char) <= 0x17FF):  # Khmer vowel signs are not "alpha"
            continue
        letters += 1
        point = ord(char)
        for index, (ranges, _name, _codes) in enumerate(SCRIPTS):
            if any(low <= point <= high for low, high in ranges):
                counts[index] = counts.get(index, 0) + 1
                break
    if not counts or not letters:
        return None
    index, count = max(counts.items(), key=lambda item: item[1])
    if count / letters < DOMINANT_SHARE:
        return None
    _ranges, name, codes = SCRIPTS[index]
    return name, codes


def refusal(text: str, language: Any, model_languages: Iterable[Any], model_name: str,
            speakers: Iterable[str] = ()) -> str | None:
    """Why this model cannot voice this request, or None when it can (or cannot be judged)."""
    published = {base_code(code) for code in model_languages or [] if base_code(code)}
    if not published:
        return None
    listed = ", ".join(sorted(published))
    alternatives = ", ".join(speakers)
    hint = f" Models here that list it: {alternatives}." if alternatives else ""
    stated = base_code(language)
    if stated and stated not in published:
        return (f'{model_name} cannot speak "{stated}": it publishes only {listed}. '
                f"Nothing was queued.{hint}")
    script = dominant_script(text or "")
    if script is not None:
        name, codes = script
        if not published & codes:
            return (f"{model_name} cannot speak this text: it is written in {name} script, and the "
                    f"model publishes only {listed}. Nothing was queued.{hint}")
    return None


def needed_codes(text: str, language: Any) -> frozenset[str]:
    """The language codes a model would have to list to take this request (empty: no constraint)."""
    stated = base_code(language)
    if stated:
        return frozenset({stated})
    script = dominant_script(text or "")
    return script[1] if script else frozenset()
