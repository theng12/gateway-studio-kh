"""Owner data that must never reach a shipped file, and the scan that enforces it.

The rules live in one place so the pre-release test (`tests/test_no_private_strings.py`) and the
build's own hard stop (`menubar/build.sh`, right after the files are copied into the app) cannot
drift apart. Names are matched case-blind: 0.2.282 shipped `Terranash-1600` because the scan
of the day was case-sensitive, and the changelog is appended every release, so the check has to run
against the bytes that are actually bundled — not against a list someone remembers to update.

Generic vocabulary stays legal: the `100.64.0.0/10` range, the `100.64.0.9` UI placeholder, the word
"tailnet", and `/Users/…` with no user name after it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    # Stem only, case-blind: `Terranash-1600`, `TERRANASH_1600` and `Terranash 1600` all hit.
    ("machine name", re.compile(r"terranash|macmini|angkor|story-mini", re.I)),
    ("tailnet hostname", re.compile(r"[a-z0-9-]+\.ts\.net", re.I)),
    # A real home directory. The docs' `/Users/YOUR-NAME/…` and `/Users/…` are placeholders.
    (
        "build path",
        re.compile(
            r"/Users/(?!(?:your-name|yourname|you|username|name|example|me)\b)[A-Za-z0-9_.-]", re.I
        ),
    ),
    (
        "tailnet address",
        re.compile(r"100\.(?:6[5-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+"),
    ),
]

# Only text this project writes. A bundled Python runtime's licences, hashes and test fixtures
# mention every kind of string by accident and are not ours to edit.
SKIP_PARTS = {"__pycache__", "site-packages", "Python.framework", "vendor"}


def scan_text(text: str, where: str = "") -> list[str]:
    failures: list[str] = []
    for label, pattern in FORBIDDEN:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            failures.append(f"{where}:{line}: {label}: {match.group(0)}")
    return failures


def scan_paths(paths: list[Path]) -> list[str]:
    failures: list[str] = []
    for path in paths:
        if not path.is_file() or SKIP_PARTS & set(path.parts):
            continue
        raw = path.read_bytes()
        # A binary (Mach-O, dylib, .pyc, image) is not text the build copies for a reader; skipping
        # it keeps the whole-bundle scan under a couple of seconds on an 89 MB runtime.
        if b"\0" in raw[:8192]:
            continue
        failures.extend(scan_text(raw.decode("utf-8", errors="ignore"), path.as_posix()))
    return failures


def scan_tree(root: Path) -> list[str]:
    return scan_paths(sorted(p for p in root.rglob("*") if p.is_file()))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: no_private_strings.py <dir-or-file> [...]", file=sys.stderr)
        return 2
    paths: list[Path] = []
    for argument in argv[1:]:
        target = Path(argument)
        if target.is_dir():
            paths.extend(sorted(p for p in target.rglob("*") if p.is_file()))
        else:
            paths.append(target)
    failures = scan_paths(paths)
    if failures:
        print("PRIVATE DATA WOULD SHIP:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"    no owner machine name, tailnet name/address or build path in {len(paths)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
