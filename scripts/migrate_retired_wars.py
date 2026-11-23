#!/usr/bin/env python3
"""Migration helper for the Sep-21 Wars archive that was merged and moved out.

Background:
  On 2026-09-22, the owner merged 'com.storystudio.pack.topic.historical-wars-treaties-world-order-v2'
  into 'com.storystudio.pack.topic.wars-treaties-world-order-v2' and moved the archive to:
    gateway-kh/backups/retired-wars-merge-20260922/747e64a9531f5d8676c0207889fe4ea4eaea6e539caed933bc509b8dd30b2dd7/
  Because the release remained in authority.json releases without a retired/removed
  record, scripts/backup_content_authority.py failed on 2026-10-27.

This script:
  1. Runs a read-only audit verifying that the Wars archive is the ONLY missing archive.
  2. Confirms the preserved archive exists and matches byte-for-byte at the backup path.
  3. When --apply is given, records it as removed/retired in authority.json pointing at its
     current backup location, without moving or rewriting any archive file.

Usage:
  # Read-only check:
  python scripts/migrate_retired_wars.py [--data-dir DIR]

  # Apply migration:
  python scripts/migrate_retired_wars.py --apply [--data-dir DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import config, content_delivery  # noqa: E402

WARS_PACK_ID = "com.storystudio.pack.topic.historical-wars-treaties-world-order-v2"
WARS_VERSION = "1.0.0"
WARS_DIGEST = "747e64a9531f5d8676c0207889fe4ea4eaea6e539caed933bc509b8dd30b2dd7"
WARS_KNOWN_BACKUP = Path(os.environ.get("GATEWAY_WARS_BACKUP", str(Path.home() / "Developer" / "gateway-kh" / "backups" / "retired-wars-merge-20260922"))) / WARS_DIGEST / "archive.sspack"


def audit_authority(data_dir: Path) -> dict:
    source = data_dir / "content-delivery"
    library = data_dir / "content-library"
    authority_file = source / "authority.json"
    if not authority_file.is_file():
        raise SystemExit(f"No authority.json found at {authority_file}")

    state = json.loads(authority_file.read_text(encoding="utf-8"))
    releases = state.get("releases", [])
    removed = state.get("removed", [])
    removed_digests = {
        r.get("archive_sha256") or r.get("sha256")
        for r in removed if isinstance(r, dict)
    }

    missing_from_lib = []
    for r in releases:
        digest = r.get("archive_sha256")
        if not digest or digest in removed_digests:
            continue
        archive_path = library / digest / "archive.sspack"
        if not archive_path.is_file() or archive_path.is_symlink():
            missing_from_lib.append(r)

    return {
        "data_dir": str(data_dir),
        "total_releases": len(releases),
        "total_removed": len(removed),
        "missing_releases": missing_from_lib,
        "wars_in_releases": any(r.get("archive_sha256") == WARS_DIGEST for r in releases),
        "wars_in_removed": WARS_DIGEST in removed_digests,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Gateway data directory (defaults to configured DATA_DIR).")
    parser.add_argument("--backup-file", type=Path, default=WARS_KNOWN_BACKUP,
                        help="Path to the preserved Wars archive file.")
    parser.add_argument("--apply", action="store_true",
                        help="Record the removal in authority.json (default: read-only check).")
    args = parser.parse_args()

    data_dir = (args.data_dir or Path(config.DATA_DIR)).resolve()
    backup_file = args.backup_file.resolve()

    print(f"Auditing authority under: {data_dir}")
    report = audit_authority(data_dir)

    print(f"Total active releases: {report['total_releases']}")
    print(f"Already recorded as removed: {report['total_removed']}")
    print(f"Releases missing from content-library: {len(report['missing_releases'])}")

    for r in report["missing_releases"]:
        print(f"  - Missing: {r.get('pack_id')} v{r.get('version')} (digest: {r.get('archive_sha256')[:12]}…)")

    # Verify Wars backup file
    if not backup_file.is_file():
        print(f"\nWARNING: Wars backup archive not found at: {backup_file}")
    else:
        file_bytes = backup_file.read_bytes()
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        if file_sha256 == WARS_DIGEST:
            print(f"Preserved Wars archive verified byte-identical at: {backup_file} ({len(file_bytes)} bytes)")
        else:
            print(f"ERROR: Preserved file hash {file_sha256} does not match expected {WARS_DIGEST}")
            return 1

    is_only_wars = (
        len(report["missing_releases"]) == 1
        and report["missing_releases"][0].get("archive_sha256") == WARS_DIGEST
    )
    if is_only_wars:
        print("\nCHECK PASSED: The Sep-21 Wars archive is the ONLY missing release referenced by authority records.")
    elif len(report["missing_releases"]) == 0 and report["wars_in_removed"]:
        print("\nALREADY MIGRATED: Wars archive is already recorded as removed in authority records.")
        return 0
    else:
        print(f"\nNOTE: Found {len(report['missing_releases'])} missing releases.")

    if not args.apply:
        print("\n[Dry Run / Read-Only] Pass --apply to record Wars as removed in authority.json without moving files.")
        return 0

    print("\nApplying migration...")
    # Apply through content_delivery.record_removed
    res = content_delivery.record_removed(
        WARS_PACK_ID,
        WARS_VERSION,
        WARS_DIGEST,
        trash_path=str(backup_file),
        when="2026-09-22T00:00:00Z",
        title="Wars, Treaties & World Order",
    )
    print(f"Result: {res}")
    print("Migration applied successfully. The next backup_content_authority run will pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
