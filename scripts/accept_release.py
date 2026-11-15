#!/usr/bin/env python3
"""Promote one accepted Gateway candidate into the canonical project dist.

Call this only after the candidate is installed, launched and accepted, and after
the caller's quiet/active-job/dependency checks. This script never builds, signs,
launches or decides acceptance: it records the caller's evidence and invokes the
shared release-dist.py helper, which validates hashes and retains the latest three
managed DMGs. `menubar/build.sh` stages candidates outside the canonical names.

The candidate must be build.sh's exact staged file for the build actually
installed: `Gateway Studio KH-<version>-<GatewayBuild>.dmg`. The version and
build are read from the installed app's Info.plist to check the candidate name.
The trusted release caller must establish that these bytes produced the verified
install. Evidence must be an existing, non-empty acceptance receipt.

The prior accepted release is carried forward from the helper's own
`.release-dist.json` ledger, never invented here. A requested version lower than
the highest managed release is refused; the same version is allowed only when
the candidate is byte-identical to the managed file (an idempotent re-run).

Example (run by the Gateway release lead after install/launch verification):
    scripts/accept_release.py \
        --candidate "dist/.candidates/<candidate>/Gateway Studio KH-0.2.242-<build>.dmg" \
        --version 0.2.242 \
        --installed-app /Applications/gateway-kh.app \
        --evidence <release-acceptance-receipt.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

APP = "Gateway Studio KH"
DEFAULT_PROJECT = "/build/Developer/My Own Apps/gateway-kh/repository"
DEFAULT_HELPER = "/build/Developer/Shared Tools/portfolio/tools/release-dist.py"
MANIFEST = ".release-dist.json"
CHUNK = 1 << 20
VERSION_RE = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class Refused(Exception):
    pass


def _refuse(message: str) -> None:
    raise Refused(message)


def _version(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not VERSION_RE.match(value):
        _refuse(f"{field} must be MAJOR.MINOR.PATCH: {value!r}")
    return tuple(int(part) for part in value.split("."))


def _installed_identity(installed: Path) -> tuple[str, str]:
    plist = installed / "Contents" / "Info.plist"
    try:
        with plist.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        _refuse(f"cannot read installed Info.plist {plist}: {exc}")
    version = info.get("CFBundleShortVersionString")
    build = info.get("GatewayBuild")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        _refuse(f"{plist} has no valid CFBundleShortVersionString")
    if not isinstance(build, str) or not build.strip() or "/" in build or "\\" in build:
        _refuse(f"{plist} has no usable GatewayBuild")
    return version, build


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _managed(dist: Path) -> dict[str, dict]:
    """The helper's ledger, validated enough to read before handing it back."""
    path = dist / MANIFEST
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        _refuse(f"{path} must be a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _refuse(f"cannot read {path}: {exc}")
    if (
        not isinstance(document, dict)
        or document.get("schema") != 1
        or not isinstance(document.get("apps"), dict)
    ):
        _refuse(f"{path} is not a schema 1 record")
    entry = document["apps"].get(APP)
    if entry is None:
        return {}
    if not isinstance(entry, dict) or not isinstance(entry.get("files"), dict):
        _refuse(f"{path} has a malformed entry for {APP}")
    return entry["files"]


def _managed_release(dist: Path, name: object, meta: object) -> dict:
    if (
        not isinstance(name, str)
        or os.path.basename(name) != name
        or "/" in name
        or "\\" in name
    ):
        _refuse(f"{MANIFEST} has an unsafe file name: {name!r}")
    if not isinstance(meta, dict):
        _refuse(f"{MANIFEST} has a malformed entry for {name}")
    version, sha = meta.get("version"), meta.get("sha256")
    size, evidence = meta.get("bytes"), meta.get("evidence")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        _refuse(f"{MANIFEST}:{name} has an invalid version")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        _refuse(f"{MANIFEST}:{name} has an invalid sha256")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        _refuse(f"{MANIFEST}:{name} has an invalid byte count")
    if not isinstance(evidence, str) or not evidence.strip():
        _refuse(f"{MANIFEST}:{name} has no evidence")
    return {
        "version": version,
        "source": str(dist / name),
        "sha256": sha,
        "bytes": size,
        "evidence": evidence,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", required=True, help="exact staged DMG to accept")
    parser.add_argument("--version", required=True, help="exact version in the candidate")
    parser.add_argument(
        "--evidence", required=True, help="existing non-empty acceptance receipt"
    )
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
        requested_key = _version(args.version, "--version")

        # Keep symlinks visible to the shared helper's refusal checks.
        candidate = Path(args.candidate).absolute()
        if not candidate.is_file():
            _refuse(f"candidate is not a regular file: {candidate}")
        if candidate.suffix != ".dmg":
            _refuse(f"candidate must be a .dmg: {candidate}")

        installed = Path(args.installed_app).absolute()
        if not installed.is_dir():
            _refuse(f"acceptance must follow install; app not present: {installed}")
        installed_version, installed_build = _installed_identity(installed)
        if installed_version != args.version:
            _refuse(
                f"installed version {installed_version} is not the requested {args.version}"
            )
        expected = f"{APP}-{args.version}-{installed_build}.dmg"
        if candidate.name != expected:
            _refuse(
                f"candidate must be build.sh's exact candidate {expected!r},"
                f" got {candidate.name!r}"
            )

        evidence = Path(args.evidence).absolute()
        try:
            present = evidence.is_file() and evidence.stat().st_size > 0
        except OSError as exc:
            _refuse(f"cannot read acceptance evidence {evidence}: {exc}")
        if not present:
            _refuse(f"acceptance evidence is missing or empty: {evidence}")

        project = Path(args.project).absolute()
        if not project.is_dir():
            _refuse(f"canonical project is not a directory: {project}")
        helper = Path(args.helper).absolute()
        if not helper.is_file():
            _refuse(f"shared release-dist helper is missing: {helper}")

        dist = project / "dist"
        managed = _managed(dist)
        records = {}
        for name, meta in managed.items():
            record = _managed_release(dist, name, meta)
            records[record["version"]] = record
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

        if records:
            highest = max(records, key=lambda value: _version(value, "managed version"))
            if requested_key < _version(highest, "managed version"):
                _refuse(
                    f"requested version {args.version} is lower than managed {highest};"
                    " refusing to retire the current rollback"
                )

        sha, size = _sha256(candidate)
        if records:
            if requested_key == _version(highest, "managed version"):
                record = records.get(args.version) or records[highest]
                if sha != record["sha256"] or size != record["bytes"]:
                    _refuse(
                        f"same-version candidate differs from managed {highest} bytes;"
                        " an idempotent re-run must be byte-identical (bump VERSION for a change)"
                    )

        prior = [
            record
            for value, record in records.items()
            if _version(value, "managed version") != requested_key
        ]
        prior.sort(key=lambda record: _version(record["version"], "managed version"))
        releases = prior[-1:] + [
            {
                "version": args.version,
                "source": str(candidate),
                "sha256": sha,
                "bytes": size,
                "evidence": str(evidence),
            }
        ]
        entry = {"app": APP, "project": str(project), "releases": releases}
        # This project's `_trash` is a symlink to the shared trash; the helper refuses a
        # symlinked retirement folder unless the pin names the real directory it links to.
        if (project / "_trash").is_symlink() and (project / "_trash").is_dir():
            entry["retirement_trash"] = str(Path(os.path.realpath(project / "_trash")))
        document = {"schema": 1, "apps": [entry]}
        # Scratch pins live inside the named project and are removed by the same
        # kind of ephemeral-temp cleanup the shared helper uses for its own stage.
        with tempfile.TemporaryDirectory(prefix="accept-release-", dir=project) as scratch:
            pins = Path(scratch) / "pins.json"
            pins.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(helper), str(pins)],
                text=True,
                capture_output=True,
                check=False,
            )
    except Refused as exc:
        print(f"accept-release: refused: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"accept-release: error: {exc}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        return result.returncode
    sys.stdout.write(result.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))