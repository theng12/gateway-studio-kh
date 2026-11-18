"""Rewrite bundled console scripts onto a script-relative interpreter path.

pip writes some entry-point scripts with an absolute interpreter shebang pointing into the build
directory. Built into the app that ships the builder's home path; the app then cannot be mirrored.

Ported from Hub Studio KH's `_strip_absolute_python_header` (packaging/voice-runtime), including
uv's three-line `#!/bin/sh` polyglot wrapper, which hardcodes the absolute path on its second line.

Usage: python3 strip_python_headers.py <python-runtime-root>   (expects <root>/bin)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Same shape the relocatable runtime already ships for pip's own scripts: the interpreter is the
# `python3` beside the script, so the bundle carries no build path.
HEADER = b"""#!/bin/sh
'''exec' "$(dirname "$0")/python3" "$0" "$@"
' '''
"""


def strip_absolute_python_header(contents: bytes) -> bytes | None:
    """Return the script body after an absolute Python shebang or uv polyglot header."""
    first_line, separator, body = contents.partition(b"\n")
    if not separator:
        return None
    if first_line.startswith(b"#!") and b"python" in first_line.lower():
        return body
    lines = contents.splitlines(keepends=True)
    if len(lines) < 3:
        return None
    line0 = lines[0].rstrip(b"\r\n")
    line1 = lines[1].rstrip(b"\r\n")
    line2 = lines[2].rstrip(b"\r\n")
    prefix = b"'''exec' "
    suffix = b' "$0" "$@"'
    if line0 != b"#!/bin/sh" or line2 != b"' '''":
        return None
    if not (line1.startswith(prefix) and line1.endswith(suffix)):
        return None
    target = line1[len(prefix) : len(line1) - len(suffix)]
    if (target.startswith(b"'/") or target.startswith(b'"/')) and b"python" in target.lower():
        return b"".join(lines[3:])
    return None


def normalize(root: Path) -> list[str]:
    """Rewrite every absolute-header console script under root/bin; return the names changed."""
    bin_dir = root / "bin"
    if not bin_dir.is_dir():
        return []
    changed: list[str] = []
    for path in sorted(bin_dir.iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        body = strip_absolute_python_header(path.read_bytes())
        if body is None:
            continue
        path.write_bytes(HEADER + body)
        changed.append(path.name)
    leftovers = []
    for path in sorted(bin_dir.iterdir()):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                head = handle.read(200)
            if b"/Users/" in head or b"/home/" in head:
                leftovers.append(path.name)
    if leftovers:
        raise SystemExit(f"ERROR: console scripts still carry a build path: {', '.join(leftovers)}")
    return changed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: strip_python_headers.py <python-runtime-root>", file=sys.stderr)
        return 2
    changed = normalize(Path(argv[1]))
    print(f"    console scripts on a relative interpreter: {len(changed)} rewritten"
          + (f" ({', '.join(changed)})" if changed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
