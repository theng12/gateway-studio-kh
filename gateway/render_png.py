"""Bounded validation of the late-label contract's static RGBA PNG inputs."""
from __future__ import annotations

import struct
import zlib


_BLOCK = 64 * 1024


def validate_cue_png(reader, *, max_width: int, max_height: int) -> tuple[int, int]:
    """Read verified asset bytes without allocating a decoded image or trusting its size."""
    def read(size):
        value = reader.read(size)
        if len(value) != size:
            raise ValueError("truncated cue PNG")
        return value

    if read(8) != b'\x89PNG\r\n\x1a\n':
        raise ValueError("cue asset must be PNG")
    width = height = row_bytes = total = 0
    decoder = None
    idat_ended = False

    def pixels(data):
        nonlocal total
        # Validate the decoded scanline framing; pixel reconstruction is the renderer's job.
        first_filter = (-total) % row_bytes
        if any(value > 4 for value in data[first_filter::row_bytes]):
            raise ValueError("invalid PNG scanline filter")
        total += len(data)
        if total > row_bytes * height:
            raise ValueError("cue PNG expands beyond its dimensions")

    while True:
        length, kind = struct.unpack('>I4s', read(8))
        if (length > 2**31 - 1 or kind[2] & 32
                or any(not (65 <= value <= 90 or 97 <= value <= 122) for value in kind)):
            raise ValueError("invalid PNG chunk")
        if not width:
            if kind != b'IHDR' or length != 13:
                raise ValueError("missing PNG header")
            header = read(13)
            if zlib.crc32(header, zlib.crc32(kind)) != struct.unpack('>I', read(4))[0]:
                raise ValueError("invalid PNG header CRC")
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', header)
            if (not 0 < width <= max_width or not 0 < height <= max_height
                    or (depth, color, compression, filtering, interlace) != (8, 6, 0, 0, 0)):
                raise ValueError("cue PNG must be bounded non-interlaced 8-bit RGBA")
            row_bytes = width * 4 + 1
            continue
        if kind in (b'IHDR', b'PLTE', b'tRNS', b'acTL', b'fcTL', b'fdAT'):
            raise ValueError("unsupported cue PNG chunk")
        if kind == b'IDAT':
            if idat_ended:
                raise ValueError("noncontiguous PNG data")
            if decoder is None:
                decoder = zlib.decompressobj()
        elif decoder is not None:
            idat_ended = True
        if not kind[0] & 32 and kind not in (b'IDAT', b'IEND'):
            raise ValueError("unknown critical PNG chunk")
        crc, remaining = zlib.crc32(kind), length
        while remaining:
            piece = read(min(remaining, _BLOCK))
            remaining -= len(piece)
            crc = zlib.crc32(piece, crc)
            if kind == b'IDAT':
                while piece:
                    try:
                        pixels(decoder.decompress(piece, _BLOCK))
                    except zlib.error as exc:
                        raise ValueError("invalid PNG compression") from exc
                    piece = decoder.unconsumed_tail
                    if decoder.unused_data:
                        raise ValueError("trailing compressed PNG data")
        if crc != struct.unpack('>I', read(4))[0]:
            raise ValueError("invalid PNG chunk CRC")
        if kind == b'IEND':
            if (length or decoder is None or not decoder.eof or decoder.unused_data
                    or total != row_bytes * height or reader.read(1)):
                raise ValueError("incomplete or trailing PNG data")
            return width, height
