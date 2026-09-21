#!/usr/bin/env python3
"""Back up the private Content Pack authority: identity **and** releases.

The signing identity under `DATA_DIR/content-delivery` is deliberately never
regenerated: losing it means future private releases cannot be signed and Story
Studio's trust binding for them breaks. The released `.sspack` bytes live
separately under `DATA_DIR/content-library/<sha256>/`, and the shelf cannot serve
a release without them — so a backup that copies only the identity would look
like a recovery point and not be one.

This copies the three authority files plus every archive the authority
references, verifies each copy by SHA-256, and refuses to overwrite an existing
backup. It prints no secret material.

Usage:
    .venv/bin/python scripts/backup_content_authority.py <destination-dir> [--data-dir DIR]

Keep the destination on local, encrypted storage. Do not put it in a
cloud-synced folder, Git, the transfer bundles, or a chat.

Restore (only after real damage): stop the gateway, copy the three authority
files back into `DATA_DIR/content-delivery/` with mode 0600, copy each archive
back to `DATA_DIR/content-library/<sha256>/archive.sspack`, start the gateway,
and confirm Content Packs reports the shelf configured and lists the releases.
`tests/test_content_authority_backup.py` covers the copy rules.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from datetime import datetime, timezone


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import config  # noqa: E402


FILES = ("authority.json", "signing-key.pem", "initialized")
ARCHIVE_DIR = "content-library"
ARCHIVE_NAME = "archive.sspack"


class BackupError(RuntimeError):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def referenced_archives(source: Path, data_root: Path) -> list[tuple[str, Path]]:
    """Every released archive the authority references, as (sha256, path)."""
    try:
        state = json.loads((source / "authority.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError("Authority state could not be read for its releases.") from exc
    archives: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for release in state.get("releases") or []:
        digest_value = release.get("archive_sha256") if isinstance(release, dict) else None
        if not isinstance(digest_value, str) or digest_value in seen:
            continue
        seen.add(digest_value)
        path = data_root / ARCHIVE_DIR / digest_value / ARCHIVE_NAME
        if path.is_symlink() or not path.is_file():
            raise BackupError(f"Released archive is missing from the library: {digest_value}")
        archives.append((digest_value, path))
    return archives


def backup(source: Path, destination: Path, data_root: Path | None = None) -> list[str]:
    """Copy the authority and every released archive into a fresh directory.

    Returns the list of relative file names copied. Raises `BackupError` on
    anything that would silently produce an unusable or partial backup.
    """
    if not source.is_dir():
        raise BackupError(
            f"No private shelf authority at {source}. Run Content Packs setup first.")
    if destination.exists():
        raise BackupError(f"Refusing to overwrite an existing backup: {destination}")
    data_root = data_root or source.parent

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
    archives = referenced_archives(source, data_root)

    try:
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(destination, 0o700)
    except OSError as exc:
        raise BackupError(f"Could not create the backup directory: {destination}") from exc

    copied: list[str] = []
    try:
        for name, want in expected:
            target = destination / name
            shutil.copy2(source / name, target)
            os.chmod(target, 0o600)
            if digest(target) != want:
                raise BackupError(f"Copy verification failed for {name}.")
            copied.append(name)

        if archives:
            archive_root = destination / "archives"
            archive_root.mkdir(mode=0o700)
            os.chmod(archive_root, 0o700)
            for digest_value, path in archives:
                target = archive_root / f"{digest_value}.sspack"
                shutil.copy2(path, target)
                os.chmod(target, 0o600)
                if digest(target) != digest_value:
                    raise BackupError(f"Copy verification failed for archive {digest_value}.")
                copied.append(f"archives/{digest_value}.sspack")
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
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
        copied = backup(source, destination, data_dir)
    except BackupError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    archives = sum(1 for name in copied if name.startswith("archives/"))
    print(f"Backed up {len(copied) - archives} authority file(s) and {archives} release archive(s) "
          f"to {destination}")
    print("Keep this copy on local, encrypted storage — never a cloud folder or Git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
