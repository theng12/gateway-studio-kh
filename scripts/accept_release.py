#!/usr/bin/env python3
"""Promote one accepted Gateway candidate into the canonical project dist.

Call this only after the candidate is installed, launched and accepted, and after
the caller's quiet/active-job/dependency checks. This script never builds, signs,
launches or decides acceptance: it records the caller's evidence and invokes the
shared release-dist.py helper, which validates hashes and retains the latest two
managed DMGs. `menubar/build.sh` stages candidates outside the canonical names.

The exact candidate path and version are required, so a publisher never falls
back to "newest file in dist". The prior accepted release is carried forward from
the helper's own `.release-dist.json` ledger, never invented here.

Example (run by the Gateway release lead after install/launch verification):
    scripts/accept_release.py \
        --candidate "dist/.candidates/Gateway Studio KH-0.2.242-<build>.dmg" \
        --version 0.2.242 \
        --installed-app /Applications/gateway-kh.app \
        --evidence <release-evidence.json|plain-words receipt>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

APP = "Gateway Studio KH"
DEFAULT_PROJECT = "/build/Developer/My Own Apps/gateway-kh/repository"
DEFAULT_HELPER = "/build/Developer/Shared Tools/portfolio/tools/release-dist.py"
MANIFEST = ".release-dist.json"
CHUNK = 1 << 20


class Refused(Exception):
    pass


def _refuse(message: str) -> None:
    raise Refused(message)


def _version_key(value: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(part) for part in value.split("."))
    except ValueError:
        _refuse(f"version must be MAJOR.MINOR.PATCH: {value!r}")
    if len(parts) != 3:
        _refuse(f"version must be MAJOR.MINOR.PATCH: {value!r}")
    return parts


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _managed(dist: Path) -> dict[str, dict]:
    path = dist / MANIFEST
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _refuse(f"cannot read {path}: {exc}")
    files = ((document.get("apps") or {}).get(APP) or {}).get("files")
    if files is None:
        return {}
    if not isinstance(files, dict):
        _refuse(f"{path} has a malformed entry for {APP}")
    return files


def _previous_release(dist: Path, managed: dict[str, dict], version: str) -> list[dict]:
    """The highest managed release other than this one, so the pair stays two."""
    releases = []
    for name, meta in managed.items():
        try:
            version_key = _version_key(str(meta["version"]))
            record = {
                "version": meta["version"],
                "source": str(dist / name),
                "sha256": meta["sha256"],
                "bytes": meta["bytes"],
                "evidence": meta["evidence"],
            }
        except (KeyError, TypeError):
            _refuse(f"{MANIFEST} has a malformed entry for {name}")
        if record["version"] == version or not (dist / name).is_file():
            continue
        releases.append((version_key, record))
    releases.sort(key=lambda item: item[0])
    return [releases[-1][1]] if releases else []


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", required=True, help="exact staged DMG to accept")
    parser.add_argument("--version", required=True, help="exact version in the candidate")
    parser.add_argument("--evidence", required=True, help="acceptance receipt or path")
    parser.add_argument("--installed-app", required=True, help="installed app that was verified")
    parser.add_argument(
        "--project",
        default=os.environ.get("GATEWAY_RELEASE_PROJECT", DEFAULT_PROJECT),
        help="canonical project root whose dist/ is managed",
    )
    parser.add_argument(
        "--helper",
        default=os.environ.get("GATEWAY_RELEASE_DIST_HELPER", DEFAULT_HELPER),
        help="canonical shared release-dist.py",
    )
    args = parser.parse_args(argv)

    try:
        _version_key(args.version)
        candidate = Path(args.candidate)
        if not candidate.is_file():
            _refuse(f"candidate is not a regular file: {candidate}")
        if candidate.suffix != ".dmg":
            _refuse(f"candidate must be a .dmg: {candidate}")
        if not args.evidence.strip():
            _refuse("--evidence must name the real acceptance evidence")
        installed = Path(args.installed_app)
        if not installed.is_dir():
            _refuse(f"installed app is not present: {installed} (acceptance must follow install)")
        project = Path(args.project)
        if not project.is_dir():
            _refuse(f"canonical project is not a directory: {project}")
        helper = Path(args.helper)
        if not helper.is_file():
            _refuse(f"shared release-dist helper is missing: {helper}")

        dist = project / "dist"
        managed = _managed(dist)
        unmanaged = sorted(
            path.name
            for path in (dist.glob(f"{APP}-*.dmg") if dist.is_dir() else [])
            if path.is_file() and path.name not in managed
        )
        if unmanaged:
            _refuse(
                "canonical dist has unmanaged DMGs ("
                + ", ".join(unmanaged)
                + "); adopt them with a trusted pins document before accepting a new release"
            )

        sha, size = _sha256(candidate)
        releases = _previous_release(dist, managed, args.version)
        releases.append(
            {
                "version": args.version,
                "source": str(candidate),
                "sha256": sha,
                "bytes": size,
                "evidence": args.evidence,
            }
        )
        document = {
            "schema": 1,
            "apps": [{"app": APP, "project": str(project), "releases": releases}],
        }
        handle, pins = tempfile.mkstemp(prefix="gateway-accept-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(document, out, indent=2)
                out.write("\n")
            result = subprocess.run(
                [sys.executable, str(helper), pins],
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            os.unlink(pins)
    except Refused as exc:
        print(f"accept-release: refused: {exc}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        return result.returncode
    sys.stdout.write(result.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))