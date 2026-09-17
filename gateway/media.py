"""Placeholder artifact generation for the Phase 0 stub.

The stub's whole job is to prove the wiring: that Story Studio can list models,
submit work, poll it, and download a byte-exact artifact that passes its own
validation. So the bytes here must be *real* formats, not dummy data.

The PNG matters most. Story Studio validates every image artifact with

    bytes.length >= 24 && bytes.subarray(1, 4) === 'PNG'

so a stub returning arbitrary bytes fails at the last step of an otherwise
working pipeline. Producing a genuine PNG with the stdlib costs ~15 lines.
"""

from __future__ import annotations

import io
import struct
import wave
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def placeholder_png(width: int = 1024, height: int = 576) -> bytes:
    """A valid, obviously-synthetic PNG (vertical gradient + border)."""
    rows: list[bytes] = []
    for y in range(height):
        t = y / max(height - 1, 1)
        base = bytes(
            (
                int(18 + t * 26),
                int(20 + t * 34),
                int(30 + t * 62),
            )
        )
        row = bytearray(base * width)
        # 4px accent rule down the left edge, so the frame is visibly a stub.
        for x in range(min(4, width)):
            row[x * 3 : x * 3 + 3] = bytes((90, 160, 255))
        rows.append(b"\x00" + bytes(row))

    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )


def placeholder_wav(duration_s: float = 1.0, sample_rate: int = 24000) -> bytes:
    """A valid 16-bit mono PCM WAV of silence.

    Story Studio downloads TTS artifacts as WAV and transcodes locally, so the
    container must be well-formed even though the payload is silent.
    """
    frames = int(max(duration_s, 0.05) * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def placeholder_srt(text: str = "gateway-kh placeholder transcript") -> bytes:
    return (
        "1\n"
        "00:00:00,000 --> 00:00:02,000\n"
        f"{text}\n"
        "\n"
    ).encode("utf-8")
