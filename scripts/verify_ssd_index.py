#!/usr/bin/env python3
"""Verify, or regenerate, an installer folder's SHA256.txt against the DMGs actually present.

The KH Updater reads each app folder's `SHA256.txt` to build `apps.json`, so that list has
to describe the folder as it is. Verification fails when a file named in the list is
missing, a `.dmg` in the folder is not listed, or a listed hash does not match its file —
a list that disagrees with the folder is a broken install kit, not a warning.

`--write` rebuilds the list from a folder scan (newest first, the installer's own choice
rule). An SSD refresh runs this after it has retired older installers, so the list can
never come from a pin somewhere else.

Usage: verify_ssd_index.py [--write] FOLDER [FOLDER ...]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

INDEX = "SHA256.txt"
# A hash line, as the refresh has always written it: 64 lowercase hex, spaces, basename.
LINE = re.compile(r"^([0-9a-f]{64}) +([^/\\]+)$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _folder(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"not a real folder: {path}")
    return path


def _dmgs(folder: Path) -> list[Path]:
    found = []
    for path in folder.glob("*.dmg"):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"not a regular file: {path}")
        found.append(path)
    return found


def _listed(folder: Path) -> tuple[dict[str, str], list[str]]:
    index = folder / INDEX
    if index.is_symlink() or not index.is_file():
        return {}, [f"{INDEX} is missing"]
    listed: dict[str, str] = {}
    problems: list[str] = []
    for number, raw in enumerate(index.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        match = LINE.match(raw)
        if not match:
            problems.append(f"{INDEX}:{number} is not '<sha256>  <name>': {raw[:80]!r}")
            continue
        sha, name = match.groups()
        if name in listed:
            problems.append(f"{INDEX}:{number} names {name} twice")
            continue
        listed[name] = sha
    return listed, problems


def verify(folder: Path) -> list[str]:
    folder = _folder(folder)
    listed, problems = _listed(folder)
    for name, sha in listed.items():
        path = folder / name
        if path.is_symlink() or not path.is_file():
            problems.append(f"listed but missing: {name}")
        elif _sha256(path) != sha:
            problems.append(f"hash does not match: {name}")
    for path in _dmgs(folder):
        if path.name not in listed:
            problems.append(f"present but not listed: {path.name}")
    return problems


def write(folder: Path) -> list[str]:
    """Replace the list with a scan of this folder, newest file first."""
    folder = _folder(folder)
    dmgs = _dmgs(folder)
    if not dmgs:
        raise ValueError(f"no .dmg files in {folder}; refusing to write an empty list")
    # Newest first: build.sh copies the old release first, so the current one is newest.
    dmgs.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    text = "".join(f"{_sha256(path)}  {path.name}\n" for path in dmgs)
    handle, staged = tempfile.mkstemp(dir=folder, prefix=f".{INDEX}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(staged, folder / INDEX)
        staged = None
    finally:
        if staged is not None and os.path.exists(staged):
            os.unlink(staged)
    return [path.name for path in dmgs]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folders", nargs="+", type=Path, help="installer folder(s)")
    parser.add_argument("--write", action="store_true",
                        help="rebuild SHA256.txt from the folder, newest first")
    args = parser.parse_args(argv)

    failed = False
    for folder in args.folders:
        try:
            if args.write:
                names = write(folder)
                print(f"WROTE {folder}: {len(names)} DMGs — {', '.join(names)}")
                problems = verify(folder)
            else:
                problems = verify(folder)
        except (OSError, ValueError) as exc:
            print(f"FAIL {folder}: {exc}")
            failed = True
            continue
        if problems:
            failed = True
            for problem in problems:
                print(f"FAIL {folder}: {problem}")
        else:
            print(f"OK   {folder}: {len(_dmgs(folder))} DMGs, hashes match the list")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
