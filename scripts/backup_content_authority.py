#!/usr/bin/env python3
"""Back up the private Content Pack authority identity and releases.

The signing identity under `DATA_DIR/content-delivery` is deliberately never
regenerated: losing it means future private releases cannot be signed and Story
Studio's trust binding for them breaks. Setup writes three files — a tombstone
marker, the durable state, and the Ed25519 private key — and all three are
needed to restore the authority.

This copies them to a destination directory, verifies the copies by SHA-256,
and refuses to overwrite an existing backup. It prints no secret material.

Usage:
    .venv/bin/python scripts/backup_content_authority.py <destination-dir> [--data-dir DIR]

Keep the destination on local, encrypted storage. Do not put it in a
cloud-synced folder, Git, the transfer bundles, or a chat.

Restore (only after real damage): stop the gateway, copy the three files back
into `DATA_DIR/content-delivery/` with mode 0600, and confirm Content Packs
reports configured. `tests/test_content_authority_backup.py` drills exactly
that, on an isolated profile.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
from datetime import datetime, timezone


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import config  # noqa: E402


FILES = ("authority.json", "signing-key.pem", "initialized")


class BackupError(RuntimeError):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup(source: Path, destination: Path) -> list[str]:
    """Copy the authority files into a fresh destination directory.

    Returns the list of file names copied. Raises `BackupError` on anything
    that would silently produce an unusable or partial backup.
    """
    if not source.is_dir():
        raise BackupError(
            f"No private shelf authority at {source}. Run Content Packs setup first.")
    if destination.exists():
        raise BackupError(f"Refusing to overwrite an existing backup: {destination}")

    expected: list[tuple[str, str]] = []
    for name in FILES:
        path = source / name
        try:
            info = path.lstat()
        except OSError as exc:
            raise BackupError(f"Authority file is missing: {name}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BackupError(f"Authority file is not a regular file: {name}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise BackupError(f"Authority file has unexpected permissions: {name}")
        expected.append((name, digest(path)))

    try:
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(destination, 0o700)
    except OSError as exc:
        raise BackupError(f"Could not create the backup directory: {destination}") from exc

    copied = []
    for name, want in expected:
        target = destination / name
        shutil.copy2(source / name, target)
        os.chmod(target, 0o600)
        if digest(target) != want:
            shutil.rmtree(destination, ignore_errors=True)
            raise BackupError(f"Copy verification failed for {name}; backup removed.")
        copied.append(name)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New directory to hold the backup.")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Override the data directory (defaults to the configured one).")
    args = parser.parse_args()

    data_dir = args.data_dir or Path(config.DATA_DIR)
    source = data_dir / "content-delivery"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = args.destination / f"content-authority-{stamp}"
    try:
        copied = backup(source, destination)
    except BackupError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Backed up {len(copied)} authority file(s) to {destination}")
    print("Keep this copy on local, encrypted storage — never a cloud folder or Git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
