#!/usr/bin/env python3
"""Safely rewrite absolute paths in a copied Gateway profile.

The command is deliberately dry-run by default. It never copies or moves an
asset; every mapped destination must already be a real file under ``new_root``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
from typing import Any, Callable, Iterable
from urllib.parse import quote


class RelocationError(RuntimeError):
    """The profile is unsafe to inspect or could not be migrated atomically."""


@dataclass(frozen=True)
class Reference:
    location: str
    old: str
    new: str
    target: Path


@dataclass(frozen=True)
class DatabaseChange:
    rowid: int
    values: dict[str, str]


@dataclass(frozen=True)
class RelocationPlan:
    profile_root: Path
    data_root: Path
    old_root: Path
    new_root: Path
    outputs: dict[Path, bytes]
    database_changes: tuple[DatabaseChange, ...]
    references: tuple[Reference, ...]
    database_path: Path | None

    @property
    def changed_files(self) -> tuple[Path, ...]:
        paths = list(self.outputs)
        if self.database_changes and self.database_path is not None:
            paths.append(self.database_path)
        return tuple(paths)


Replace = Callable[[Path, Path], None]


def _lexical_path(value: str | Path) -> Path:
    """Make an absolute, normalized path without resolving symlinks."""
    return Path(os.path.abspath(os.path.normpath(os.path.expanduser(os.fspath(value)))))


def _relative(path: Path, root: Path, *, label: str) -> Path:
    try:
        return path.relative_to(root)
    except ValueError as exc:
        raise RelocationError(f"{label} escapes explicit root {root}: {path}") from exc


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError as exc:
        raise RelocationError(f"missing {label}: {path}") from exc
    except OSError as exc:
        raise RelocationError(f"cannot inspect {label} {path}: {exc}") from exc


def _reject_symlinks_below(root: Path, path: Path, *, label: str) -> None:
    """Reject a symlink in a referenced path, including parent components."""
    current = root
    for part in _relative(path, root, label=label).parts:
        current /= part
        try:
            if current.is_symlink():
                raise RelocationError(f"symlink is not allowed for {label}: {current}")
        except OSError as exc:
            raise RelocationError(f"cannot inspect {label} {current}: {exc}") from exc


def _require_root(path: Path, *, label: str) -> Path:
    path = _lexical_path(path)
    info = _lstat(path, label=label)
    if stat.S_ISLNK(info.st_mode):
        raise RelocationError(f"symlink is not allowed for {label}: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise RelocationError(f"{label} is not a directory: {path}")
    return path


def _require_file(path: Path, *, label: str) -> None:
    info = _lstat(path, label=label)
    if stat.S_ISLNK(info.st_mode):
        raise RelocationError(f"symlink is not allowed for {label}: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise RelocationError(f"{label} is not a regular file: {path}")


def _optional_file(path: Path, *, label: str) -> bool:
    if path.is_symlink():
        raise RelocationError(f"symlink is not allowed for {label}: {path}")
    if not path.exists():
        return False
    _require_file(path, label=label)
    return True


def _read_json(path: Path, *, label: str) -> Any:
    _require_file(path, label=label)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RelocationError(f"cannot read valid JSON from {label} {path}: {exc}") from exc


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _map_path(
    value: object,
    *,
    location: str,
    old_root: Path,
    new_root: Path,
    seen_targets: dict[tuple[int, int], Path],
) -> tuple[str, Reference] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RelocationError(f"{location} must be a string path or null")
    source = _lexical_path(value)
    relative = _relative(source, old_root, label=location)
    target = _lexical_path(new_root / relative)
    _relative(target, new_root, label=location)
    _reject_symlinks_below(old_root, source, label=location)
    _reject_symlinks_below(new_root, target, label=location)
    target_info = _lstat(target, label=f"destination for {location}")
    if stat.S_ISLNK(target_info.st_mode):
        raise RelocationError(f"symlink is not allowed for destination for {location}: {target}")
    if not stat.S_ISREG(target_info.st_mode):
        raise RelocationError(f"destination for {location} is not a regular file: {target}")

    target_key = (target_info.st_dev, target_info.st_ino)
    previous = seen_targets.get(target_key)
    if previous is not None and previous != source:
        raise RelocationError(
            f"conflicting source paths map to {target}: {previous} and {source}"
        )
    seen_targets[target_key] = source

    reference = Reference(location=location, old=str(source), new=str(target), target=target)
    return str(target), reference


def _map_voice_catalogue(
    path: Path,
    *,
    old_root: Path,
    new_root: Path,
    seen_targets: dict[tuple[int, int], Path],
) -> tuple[bytes | None, list[Reference]]:
    payload = _read_json(path, label="voice catalogue")
    if not isinstance(payload, dict) or not isinstance(payload.get("voices"), list):
        raise RelocationError(f"voice catalogue has no voices list: {path}")
    references: list[Reference] = []
    for index, voice in enumerate(payload["voices"]):
        if not isinstance(voice, dict) or "reference_path" not in voice:
            continue
        mapped = _map_path(
            voice["reference_path"],
            location=f"data/voices.json voices[{index}].reference_path",
            old_root=old_root,
            new_root=new_root,
            seen_targets=seen_targets,
        )
        if mapped is not None:
            voice["reference_path"], reference = mapped
            references.append(reference)
    return _json_bytes(payload), references


def _map_kept(
    path: Path,
    *,
    old_root: Path,
    new_root: Path,
    seen_targets: dict[tuple[int, int], Path],
) -> tuple[bytes | None, list[Reference]]:
    payload = _read_json(path, label="kept pins")
    if not isinstance(payload, dict) or not isinstance(payload.get("kept"), list):
        raise RelocationError(f"kept pins have no kept list: {path}")
    references: list[Reference] = []
    for index, entry in enumerate(payload["kept"]):
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        mapped = _map_path(
            entry["path"],
            location=f"data/kept.json kept[{index}].path",
            old_root=old_root,
            new_root=new_root,
            seen_targets=seen_targets,
        )
        if mapped is not None:
            entry["path"], reference = mapped
            references.append(reference)
    return _json_bytes(payload), references


def _database_inventory(
    path: Path,
    *,
    old_root: Path,
    new_root: Path,
    seen_targets: dict[tuple[int, int], Path],
) -> tuple[tuple[DatabaseChange, ...], list[Reference]]:
    _require_file(path, label="profile database")
    if path.with_name(path.name + "-wal").exists() or path.with_name(path.name + "-shm").exists():
        raise RelocationError(f"database sidecars must be checkpointed before relocation: {path}")
    uri = f"file:{quote(path.as_posix())}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise RelocationError(f"cannot open profile database read-only: {exc}") from exc
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone()
        if table is None:
            return (), []
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        tracked = [name for name in ("asset_path", "artifacts_json", "params") if name in columns]
        if not tracked:
            return (), []
        selected = ", ".join(["rowid", *tracked])
        changes: list[DatabaseChange] = []
        references: list[Reference] = []
        for row in connection.execute(f"SELECT {selected} FROM jobs"):
            values = dict(zip(tracked, row[1:]))
            updated = dict(values)
            row_references: list[Reference] = []

            if "asset_path" in values:
                mapped = _map_path(
                    values["asset_path"],
                    location=f"gateway.sqlite3 jobs[rowid={row[0]}].asset_path",
                    old_root=old_root,
                    new_root=new_root,
                    seen_targets=seen_targets,
                )
                if mapped is not None:
                    updated["asset_path"], reference = mapped
                    row_references.append(reference)

            if "artifacts_json" in values and values["artifacts_json"] not in (None, ""):
                try:
                    artifacts = json.loads(values["artifacts_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RelocationError(
                        f"invalid artifacts_json in jobs[rowid={row[0]}]"
                    ) from exc
                if isinstance(artifacts, list):
                    artifacts_changed = False
                    for index, artifact in enumerate(artifacts):
                        if not isinstance(artifact, dict) or "path" not in artifact:
                            continue
                        mapped = _map_path(
                            artifact["path"],
                            location=(
                                f"gateway.sqlite3 jobs[rowid={row[0]}].artifacts_json"
                                f"[{index}].path"
                            ),
                            old_root=old_root,
                            new_root=new_root,
                            seen_targets=seen_targets,
                        )
                        if mapped is not None:
                            artifact["path"], reference = mapped
                            row_references.append(reference)
                            artifacts_changed = True
                    if artifacts_changed:
                        encoded = json.dumps(artifacts, ensure_ascii=False)
                        if encoded != values["artifacts_json"]:
                            updated["artifacts_json"] = encoded

            if "params" in values and values["params"] not in (None, ""):
                try:
                    params = json.loads(values["params"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise RelocationError(f"invalid params in jobs[rowid={row[0]}]") from exc
                if isinstance(params, dict) and "_upload_path" in params:
                    mapped = _map_path(
                        params["_upload_path"],
                        location=f"gateway.sqlite3 jobs[rowid={row[0]}].params._upload_path",
                        old_root=old_root,
                        new_root=new_root,
                        seen_targets=seen_targets,
                    )
                    if mapped is not None:
                        params["_upload_path"], reference = mapped
                        row_references.append(reference)
                        encoded = json.dumps(params, ensure_ascii=False)
                        if encoded != values["params"]:
                            updated["params"] = encoded

            if updated != values:
                changes.append(DatabaseChange(rowid=int(row[0]), values=updated))
            references.extend(row_references)
        return tuple(changes), references
    except sqlite3.Error as exc:
        raise RelocationError(f"cannot inspect profile database: {exc}") from exc
    finally:
        connection.close()


def plan_relocation(
    profile_root: str | Path,
    old_root: str | Path,
    new_root: str | Path,
) -> RelocationPlan:
    """Read a copied profile and return a validated, write-free relocation plan."""
    profile = _require_root(Path(profile_root), label="profile root")
    # The source Mac's home is a lexical prefix, not a mounted prerequisite.
    # Only destination files are read/required. Still refuse an existing symlink
    # prefix rather than accidentally interpreting a different source identity.
    old = _lexical_path(old_root)
    if old.is_symlink():
        raise RelocationError(f"symlink is not allowed for old root: {old}")
    new = _require_root(Path(new_root), label="new root")
    data = profile / "data"
    if not data.is_dir() or data.is_symlink():
        if profile.name == "data" and profile.is_dir() and not profile.is_symlink():
            data = profile
        else:
            raise RelocationError(f"copied profile has no real data directory: {data}")

    seen_targets: dict[tuple[int, int], Path] = {}
    outputs: dict[Path, bytes] = {}
    references: list[Reference] = []

    voices = data / "voices.json"
    if _optional_file(voices, label="voice catalogue"):
        _reject_symlinks_below(data, voices, label="voice catalogue")
        content, found = _map_voice_catalogue(
            voices, old_root=old, new_root=new, seen_targets=seen_targets
        )
        if found and content is not None and content != voices.read_bytes():
            outputs[voices] = content
        references.extend(found)

    kept = data / "kept.json"
    if _optional_file(kept, label="kept pins"):
        _reject_symlinks_below(data, kept, label="kept pins")
        content, found = _map_kept(
            kept, old_root=old, new_root=new, seen_targets=seen_targets
        )
        if found and content is not None and content != kept.read_bytes():
            outputs[kept] = content
        references.extend(found)

    database = data / "gateway.sqlite3"
    database_changes: tuple[DatabaseChange, ...] = ()
    database_path: Path | None = None
    if _optional_file(database, label="profile database"):
        _reject_symlinks_below(data, database, label="profile database")
        database_changes, found = _database_inventory(
            database, old_root=old, new_root=new, seen_targets=seen_targets
        )
        references.extend(found)
        database_path = database

    return RelocationPlan(
        profile_root=profile,
        data_root=data,
        old_root=old,
        new_root=new,
        outputs=outputs,
        database_changes=database_changes,
        references=tuple(references),
        database_path=database_path,
    )


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _temp_bytes(target: Path, content: bytes) -> Path:
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.relocate-", dir=target.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        return temp
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _temp_database(path: Path, changes: Iterable[DatabaseChange]) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.relocate-", dir=path.parent)
    os.close(fd)
    temp = Path(name)
    try:
        shutil.copy2(path, temp)
        connection = sqlite3.connect(temp)
        try:
            connection.execute("BEGIN")
            for change in changes:
                assignments = ", ".join(f"{column} = ?" for column in change.values)
                connection.execute(
                    f"UPDATE jobs SET {assignments} WHERE rowid = ?",
                    (*change.values.values(), change.rowid),
                )
            connection.commit()
        finally:
            connection.close()
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        return temp
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _backup_name(profile: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    parent = profile / ".relocation-backups"
    candidate = parent / stamp
    counter = 1
    while candidate.exists():
        candidate = parent / f"{stamp}-{counter}"
        counter += 1
    return candidate


def _backup_path(backup_dir: Path, data_root: Path, path: Path) -> Path:
    return backup_dir / path.relative_to(data_root)


def _restore_backup(backup_dir: Path, data_root: Path, path: Path) -> None:
    backup = _backup_path(backup_dir, data_root, path)
    restore = _temp_bytes(path, backup.read_bytes())
    try:
        os.replace(restore, path)
    finally:
        restore.unlink(missing_ok=True)


def apply_plan(
    plan: RelocationPlan,
    *,
    backup_dir: str | Path | None = None,
    replace: Replace = os.replace,
) -> dict[str, object]:
    """Apply a validated plan with backups and cross-file rollback."""
    targets = list(plan.outputs)
    if plan.database_changes and plan.database_path is not None:
        targets.append(plan.database_path)
    if not targets:
        return {"changed_files": 0, "backup_dir": None}

    backup = _lexical_path(backup_dir) if backup_dir is not None else _backup_name(plan.profile_root)
    if backup.exists():
        raise RelocationError(f"backup directory already exists: {backup}")
    try:
        backup.mkdir(parents=True)
        for path in targets:
            _require_file(path, label="profile state")
            destination = _backup_path(backup, plan.data_root, path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    except (OSError, RelocationError) as exc:
        raise RelocationError(f"could not create relocation backup: {exc}") from exc

    staged: dict[Path, Path] = {}
    try:
        # Keep this order observable: if the database stage fails, JSON stages
        # still exercise the rollback path in tests and in real partial failures.
        ordered = [
            path
            for path in (plan.data_root / "voices.json", plan.database_path, plan.data_root / "kept.json")
            if path in targets
        ]
        for path in ordered:
            if path in plan.outputs:
                staged[path] = _temp_bytes(path, plan.outputs[path])
            elif path == plan.database_path:
                staged[path] = _temp_database(path, plan.database_changes)

        for path in ordered:
            replace(staged[path], path)
            staged.pop(path, None)
            _fsync_directory(path.parent)
        return {"changed_files": len(ordered), "backup_dir": str(backup)}
    except BaseException as exc:
        for temp in staged.values():
            temp.unlink(missing_ok=True)
        rollback_errors: list[str] = []
        for path in targets:
            try:
                _restore_backup(backup, plan.data_root, path)
            except (OSError, RelocationError) as rollback_error:
                rollback_errors.append(f"{path}: {rollback_error}")
        message = f"migration failed; originals restored from {backup}: {exc}"
        if rollback_errors:
            message += "; rollback errors: " + ", ".join(rollback_errors)
        raise RelocationError(message) from exc


def _summary(plan: RelocationPlan) -> dict[str, object]:
    return {
        "profile_root": str(plan.profile_root),
        "old_root": str(plan.old_root),
        "new_root": str(plan.new_root),
        "references": [
            {"location": row.location, "old": row.old, "new": row.new}
            for row in plan.references
        ],
        "changed_files": [str(path) for path in plan.changed_files],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", help="copied profile root")
    parser.add_argument("--profile-root", "--profile", dest="profile_root", help="copied profile root")
    parser.add_argument("--old-root", required=True, help="absolute root in stored references")
    parser.add_argument("--new-root", required=True, help="absolute root containing copied files")
    parser.add_argument("--apply", action="store_true", help="write the validated mapping")
    parser.add_argument("--dry-run", action="store_true", help="inspect only (the default)")
    parser.add_argument("--backup-dir", help="backup directory used with --apply")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    profile = args.profile_root or args.profile
    if not profile:
        parser.error("a copied profile root is required (use --profile-root or a positional path)")
    if args.profile_root and args.profile and _lexical_path(args.profile_root) != _lexical_path(args.profile):
        parser.error("positional profile and --profile-root disagree")
    try:
        plan = plan_relocation(profile, args.old_root, args.new_root)
        if not args.apply:
            result = _summary(plan)
            result["mode"] = "dry-run"
        else:
            result = _summary(plan)
            result["mode"] = "apply"
            applied = apply_plan(plan, backup_dir=args.backup_dir)
            result["changed_file_count"] = applied["changed_files"]
            result["backup_dir"] = applied["backup_dir"]
    except (RelocationError, OSError, sqlite3.Error) as exc:
        print(f"relocate_profile: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("DRY-RUN" if result["mode"] == "dry-run" else "APPLIED")
        print(f"profile: {result['profile_root']}")
        print(f"references: {len(result['references'])}")
        print(f"changed files: {len(result['changed_files'])}")
        if result.get("backup_dir"):
            print(f"backup: {result['backup_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
