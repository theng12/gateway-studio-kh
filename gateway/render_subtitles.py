"""Bounded validation of the subtitle contract's leased ASS and font inputs.

Callers pass the reader from ``assets.open_reader`` plus the bounds they
advertised (manifest ``size_bytes`` / profile ``max_asset_bytes``); this module
adds no ceiling of its own. Every refusal is ``ValueError``.
"""
from __future__ import annotations

import hashlib
import math
import re
import struct

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SECTION = re.compile(r"\[([^\[\]]+)\]\Z")
_TIME = re.compile(r"(\d+):([0-5]\d):([0-5]\d)\.(\d{2})\Z")
_FN = re.compile(r"\\fn([^\\}]*)")
_TABLE_TAG = re.compile(rb"[A-Za-z0-9 /]{4}\Z")
_INFO_SECTIONS = ("Script Info", "V4+ Styles", "Events")
# Known external-file / include-like Script Info keys; everything else is open.
_INFO_REJECTED_KEYS = frozenset(("video file", "audio file", "video files", "audio files",
                                 "import", "include"))
_EVENT_FORMAT = ("Layer", "Start", "End", "Style", "Name",
                 "MarginL", "MarginR", "MarginV", "Effect", "Text")
_STYLE_FORMAT = ("Name", "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
                 "OutlineColour", "BackColour", "Bold", "Italic", "Underline", "StrikeOut",
                 "ScaleX", "ScaleY", "Spacing", "Angle", "BorderStyle", "Outline", "Shadow",
                 "Alignment", "MarginL", "MarginR", "MarginV", "Encoding")
_READ_CHUNK = 64 * 1024


def _refuse(message):
    raise ValueError(message)


def _read_bounded(reader, max_bytes, what):
    if type(max_bytes) is not int or max_bytes < 1:
        _refuse(what + " max_bytes is invalid")
    chunks = []
    remaining = max_bytes + 1
    while remaining > 0:
        piece = reader.read(min(remaining, _READ_CHUNK))
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        _refuse(what + " exceeds max_bytes")
    return data


def _verify(data, expected_sha256, what):
    if _SHA.fullmatch(expected_sha256 or "") is None:
        _refuse(what + " expected_sha256 is invalid")
    if not data:
        _refuse(what + " is empty")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        _refuse(what + " sha256 mismatch")


def _seconds(stamp, what):
    match = _TIME.fullmatch(stamp)
    if match is None:
        _refuse(what + " timestamp is malformed")
    hours, minutes, secs, centis = (int(group) for group in match.groups())
    return hours * 3600 + minutes * 60 + secs + centis / 100


def _script_info(lines, canvas, what):
    play = {}
    seen = set()
    for line in lines:
        key, colon, value = line.partition(":")
        key = key.strip()
        if not colon or not key:
            _refuse(what + " Script Info line is malformed")
        if key.casefold() in seen:
            _refuse(what + " repeats a Script Info key")
        seen.add(key.casefold())
        if key.lower() in _INFO_REJECTED_KEYS:
            _refuse(what + " Script Info references an external file")
        if key in ("PlayResX", "PlayResY"):
            if not value.strip().isdigit() or int(value.strip()) < 1:
                _refuse(what + " PlayRes is invalid")
            play[key] = int(value.strip())
    width, height = canvas.get("width"), canvas.get("height")
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        _refuse(what + " canvas is invalid")
    if play.get("PlayResX") != width or play.get("PlayResY") != height:
        _refuse(what + " PlayRes does not match the recipe canvas")


def _styles(lines, families, what):
    names = set()
    fields = None
    for line in lines:
        key, colon, value = line.partition(":")
        if not colon:
            _refuse(what + " style line is malformed")
        if key == "Comment":
            continue
        if key == "Format":
            if fields is not None:
                _refuse(what + " repeats its style Format")
            fields = tuple(part.strip() for part in value.split(","))
            if fields != _STYLE_FORMAT:
                _refuse(what + " style Format is unsupported")
            continue
        if key != "Style" or fields is None:
            _refuse(what + " style section line is unsupported")
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != len(fields):
            _refuse(what + " style field count is malformed")
        record = dict(zip(fields, parts))
        name, fontname = record["Name"], record["Fontname"]
        if not name or name in names:
            _refuse(what + " style name is invalid or repeated")
        if fontname not in families:
            _refuse(what + " style references an unsupplied font family")
        for field in _STYLE_FORMAT[2:]:
            value = record[field]
            if field.endswith("Colour"):
                if re.fullmatch(r"&H[0-9a-fA-F]{8}", value) is None:
                    _refuse(what + " style colour is malformed")
            else:
                try:
                    number = float(value)
                except ValueError:
                    _refuse(what + " style number is malformed")
                if not math.isfinite(number):
                    _refuse(what + " style number is nonfinite")
        names.add(name)
    if fields is None or not names:
        _refuse(what + " has no usable styles")
    return names


def _events(lines, style_names, families, duration, what):
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        _refuse(what + " duration is invalid")
    fields = None
    for line in lines:
        key, colon, value = line.partition(":")
        if not colon:
            _refuse(what + " event line is malformed")
        if key == "Comment":
            continue
        if key == "Format":
            if fields is not None:
                _refuse(what + " repeats its event Format")
            fields = tuple(part.strip() for part in value.split(","))
            if fields != _EVENT_FORMAT:
                _refuse(what + " event Format is unsupported")
            continue
        if key in ("Video", "Audio"):
            _refuse(what + " references an external media file")
        if key != "Dialogue" or fields is None:
            _refuse(what + " event section line is unsupported")
        parts = value.split(",", len(_EVENT_FORMAT) - 1)
        if len(parts) != len(_EVENT_FORMAT):
            _refuse(what + " dialogue field count is malformed")
        record = dict(zip(_EVENT_FORMAT, (part.strip() for part in parts)))
        start = _seconds(record["Start"], what)
        end = _seconds(record["End"], what)
        if start < 0 or start >= end or end > duration + 1e-6:
            _refuse(what + " dialogue is outside the recipe duration")
        if record["Style"] not in style_names:
            _refuse(what + " dialogue references an unknown style")
        for family in _FN.findall(record["Text"]):
            if family.strip() and family.strip() not in families:
                _refuse(what + " dialogue selects an unsupplied font family")
    if fields is None:
        _refuse(what + " has no event Format")


def validate_subtitle_ass(reader, *, max_bytes, expected_sha256, canvas, duration, families):
    """Refuse unless the leased bytes are Story's closed UTF-8 ASS for this recipe."""
    what = "subtitle ASS"
    data = _read_bounded(reader, max_bytes, what)
    _verify(data, expected_sha256, what)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _refuse(what + " must be UTF-8")
    if "\0" in text:
        _refuse(what + " must be NUL-free")
    if type(families) not in (list, tuple, set, frozenset) or not families:
        _refuse(what + " families are invalid")
    family_set = set()
    for family in families:
        if type(family) is not str or not family:
            _refuse(what + " families are invalid")
        family_set.add(family)

    sections = []
    current = None
    content = {"Script Info": [], "V4+ Styles": [], "Events": []}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if stripped.startswith("["):
            header = _SECTION.fullmatch(stripped)
            if header is None:
                _refuse(what + " section header is malformed")
            name = header.group(1)
            if name not in _INFO_SECTIONS:
                _refuse(what + " section " + name + " is unsupported")
            if name in sections:
                _refuse(what + " repeats section " + name)
            sections.append(name)
            current = name
            continue
        if current is None:
            _refuse(what + " has content before its first section")
        content[current].append(stripped)
    if tuple(sections) != _INFO_SECTIONS:
        _refuse(what + " sections are incomplete or out of order")

    _script_info(content["Script Info"], canvas, what)
    style_names = _styles(content["V4+ Styles"], family_set, what)
    _events(content["Events"], style_names, family_set, duration, what)


def _sfnt_family_names(blob, what):
    if len(blob) < 6:
        _refuse(what + " name table is truncated")
    fmt, count, string_offset = struct.unpack(">HHH", blob[:6])
    if fmt > 1 or not 6 + 12 * count <= string_offset <= len(blob):
        _refuse(what + " name table is malformed")
    names = set()
    for index in range(count):
        base = 6 + 12 * index
        platform, encoding, _language, name_id, length, offset = struct.unpack(
            ">HHHHHH", blob[base:base + 12])
        if name_id not in (1, 16):  # Font Family, Typographic Family
            continue
        start = string_offset + offset
        if start + length > len(blob):
            _refuse(what + " name record is out of bounds")
        raw = blob[start:start + length]
        if platform in (0, 3):  # Unicode / Windows UCS-2
            if length % 2:
                _refuse(what + " name record is malformed")
            try:
                value = raw.decode("utf-16-be")
            except UnicodeDecodeError as exc:
                raise ValueError(what + " name record is malformed") from exc
        elif platform == 1 and encoding == 0:  # Mac Roman
            value = raw.decode("mac_roman")
        else:
            continue
        if value:
            names.add(value)
    return names


def validate_subtitle_font(reader, *, max_bytes, expected_sha256, family):
    """Refuse unless the leased bytes are the sfnt font bound to this family record."""
    what = "subtitle font"
    data = _read_bounded(reader, max_bytes, what)
    _verify(data, expected_sha256, what)
    if type(family) is not str or not family or family.startswith((".", "/", "\\")):
        _refuse(what + " family is invalid")
    if len(data) < 12:
        _refuse(what + " is truncated")
    if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO"):
        _refuse(what + " must be TTF or OTF")
    count = struct.unpack(">H", data[4:6])[0]
    if count < 1 or 12 + 16 * count > len(data):
        _refuse(what + " table directory is invalid")
    name_span = None
    spans = []
    tags = set()
    for index in range(count):
        base = 12 + 16 * index
        tag = data[base:base + 4]
        _checksum, offset, length = struct.unpack(">III", data[base + 4:base + 16])
        if _TABLE_TAG.fullmatch(tag) is None or tag in tags:
            _refuse(what + " table tag is invalid or repeated")
        tags.add(tag)
        if (offset < 12 + 16 * count or offset % 4 or offset + length > len(data)
                or any(offset < end and offset + length > start for start, end in spans)):
            _refuse(what + " table is out of bounds or overlaps")
        spans.append((offset, offset + length))
        if tag == b"name":
            name_span = (offset, length)
    if name_span is None:
        _refuse(what + " has no verifiable family name table")
    names = _sfnt_family_names(data[name_span[0]:name_span[0] + name_span[1]], what)
    if family not in names:
        _refuse(what + " family does not match its name table")
