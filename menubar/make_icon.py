#!/usr/bin/env python3
"""Generate the gateway-kh app icon.

Pure stdlib: writes PNGs by hand (zlib + CRC) and shells out to `iconutil`.
A build step should not need Pillow installed to draw one icon.

The mark is a lightning bolt on a dark rounded square -- "gateway" as in power
distribution, and legible at 16px in the menu bar's neighbouring app lists.

Usage: python menubar/make_icon.py <output.icns>
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

# Lightning bolt, normalised. Scanned left-to-right, so the shape is a simple
# polygon and can be filled with a scanline.
BOLT = [
    (0.585, 0.075),
    (0.300, 0.560),
    (0.462, 0.560),
    (0.383, 0.925),
    (0.700, 0.435),
    (0.530, 0.435),
    (0.648, 0.075),
]

TOP = (34, 48, 78)       # deep navy
BOTTOM = (18, 24, 40)    # near black
BOLT_COLOR = (86, 190, 255)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _bolt_edges(y: float) -> list[float]:
    """X coordinates where the bolt polygon crosses scanline `y`."""
    crossings: list[float] = []
    count = len(BOLT)
    for index in range(count):
        x1, y1 = BOLT[index]
        x2, y2 = BOLT[(index + 1) % count]
        if (y1 <= y < y2) or (y2 <= y < y1):
            crossings.append(x1 + (y - y1) / (y2 - y1) * (x2 - x1))
    return sorted(crossings)


def render(size: int) -> bytes:
    radius = size * 0.22
    rows: list[bytes] = []

    for y in range(size):
        row = bytearray()
        for x in range(size):
            # Vertical gradient.
            t = y / max(size - 1, 1)
            r = int(TOP[0] + (BOTTOM[0] - TOP[0]) * t)
            g = int(TOP[1] + (BOTTOM[1] - TOP[1]) * t)
            b = int(TOP[2] + (BOTTOM[2] - TOP[2]) * t)

            # Rounded-rect alpha: distance outside the corner arcs.
            cx = min(x, size - 1 - x)
            cy = min(y, size - 1 - y)
            alpha = 255
            if cx < radius and cy < radius:
                dx, dy = radius - cx, radius - cy
                distance = (dx * dx + dy * dy) ** 0.5
                if distance > radius:
                    alpha = 0
                else:
                    alpha = int(255 * max(0.0, min(1.0, radius - distance + 0.5)))

            # Composite the bolt over the background.
            nx, ny = x / size, y / size
            edges = _bolt_edges(ny)
            inside = False
            for index in range(0, len(edges) - 1, 2):
                if edges[index] <= nx < edges[index + 1]:
                    inside = True
                    break
            if inside:
                r, g, b = BOLT_COLOR

            # The image is opaque RGB; blend against the gradient using alpha so
            # the corners read as transparent in the .icns.
            row += bytes((r, g, b)) if alpha == 255 else bytes(
                (int(r * alpha / 255), int(g * alpha / 255), int(b * alpha / 255))
            )
        rows.append(bytes(row))
    return _png(size, size, rows)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: make_icon.py <output.icns>")
        return 2
    output = Path(sys.argv[1])

    iconset = Path(tempfile.mkdtemp(prefix="gwicon-")) / "AppIcon.iconset"
    iconset.mkdir(parents=True)

    # The exact set iconutil expects.
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = base * scale
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            (iconset / name).write_bytes(render(pixels))

    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(output)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "iconutil failed")
        return 1
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
