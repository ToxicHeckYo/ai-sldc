"""Journaled synchronization of the repository standard and skill snapshot."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import shutil
from typing import Callable

import yaml

from .errors import ValidationError
from .standard import SemVer, StandardManifest, load_standard_manifest


_SOURCE = Path("AI_ASSISTED_DEVELOPMENT_STANDARD.md")
_ROOT_MANIFEST = Path("standard-manifest.yaml")
_SKILL_STANDARD = Path("skills/ai-sdlc-orchestrator/references/standard")
_SNAPSHOT = _SKILL_STANDARD / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
_SNAPSHOT_MANIFEST = _SKILL_STANDARD / "manifest.yaml"
_TRANSACTION = Path(".ai-sdlc-standard-sync")
_PREPARING = Path(".ai-sdlc-standard-sync.preparing")
_VERSION = re.compile(r"^\*\*Версия:\*\*\s*(?P<version>\S+)\s*$", re.MULTILINE)
ReplaceFile = Callable[[Path, Path], None]


@dataclass(frozen=True)
class SyncStatus:
    state: str
    clean: bool
    source_version: str | None
    source_sha256: str | None
    snapshot_version: str | None
    snapshot_sha256: str | None
    detail: str = ""


@dataclass(frozen=True)
class RecoveryResult:
    recovered: bool
    action: str


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _document_version(data: bytes) -> str:
    try:
        document = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("canonical standard must be valid UTF-8") from error
    match = _VERSION.search(document)
    if match is None:
        raise ValidationError("canonical standard does not declare a version")
    value = match.group("version")
    SemVer.parse(value)
    return value


def _manifest_bytes(manifest: StandardManifest, sha256: str) -> bytes:
    document = {
        "version": manifest.version,
        "updated_at": manifest.updated_at,
        "sha256": sha256,
    }
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8")


def _read_state(repository_root: Path) -> tuple[bytes, StandardManifest, bytes, StandardManifest]:
    source_path = repository_root / _SOURCE
    snapshot_path = repository_root / _SNAPSHOT
    if not source_path.is_file() or not snapshot_path.is_file():
        raise ValidationError("standard source or snapshot is missing")
    source = source_path.read_bytes()
    snapshot = snapshot_path.read_bytes()
    root_manifest = load_standard_manifest(repository_root / _ROOT_MANIFEST)
    snapshot_manifest = load_standard_manifest(repository_root / _SNAPSHOT_MANIFEST)
    if _document_version(source) != root_manifest.version:
        raise ValidationError("canonical standard version does not match root manifest")
    if _document_version(snapshot) != snapshot_manifest.version:
        raise ValidationError("snapshot version does not match snapshot manifest")
    return source, root_manifest, snapshot, snapshot_manifest


def check_standard_sync(repository_root: Path) -> SyncStatus:
    """Inspect source and snapshot without modifying tracked files."""
    recover_standard_sync(repository_root)
    try:
        source, root_manifest, snapshot, snapshot_manifest = _read_state(repository_root)
    except (OSError, ValidationError) as error:
        return SyncStatus("invalid", False, None, None, None, None, str(error))
    source_hash = _digest(source)
    snapshot_hash = _digest(snapshot)
    if source_hash != root_manifest.sha256:
        state = "invalid" if root_manifest.version == snapshot_manifest.version else "snapshot-stale"
        return SyncStatus(state, False, root_manifest.version, source_hash, snapshot_manifest.version, snapshot_hash, "root manifest hash is stale")
    if (
        snapshot_hash != snapshot_manifest.sha256
        or source != snapshot
        or root_manifest != snapshot_manifest
    ):
        return SyncStatus("snapshot-stale", False, root_manifest.version, source_hash, snapshot_manifest.version, snapshot_hash, "snapshot differs from canonical source")
    return SyncStatus("clean", True, root_manifest.version, source_hash, snapshot_manifest.version, snapshot_hash)


def _default_replace(source: Path, target: Path) -> None:
    source.replace(target)


def _write_journal(path: Path, state: str, entries: list[dict[str, object]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(yaml.safe_dump({"state": state, "entries": entries}, sort_keys=False), encoding="utf-8", newline="\n")
    temporary.replace(path)


def sync_standard(repository_root: Path, *, replace_file: ReplaceFile = _default_replace) -> SyncStatus:
    """Synchronize all identity files, rolling back ordinary failures."""
    recover_standard_sync(repository_root)
    source, root_manifest, snapshot, snapshot_manifest = _read_state(repository_root)
    source_hash = _digest(source)
    if source_hash != root_manifest.sha256 and root_manifest.version == snapshot_manifest.version:
        raise ValidationError("standard bytes changed without a version bump")

    manifest = _manifest_bytes(root_manifest, source_hash)
    desired = {
        _ROOT_MANIFEST: manifest,
        _SNAPSHOT: source,
        _SNAPSHOT_MANIFEST: manifest,
    }
    tx = repository_root / _TRANSACTION
    preparing = repository_root / _PREPARING
    entries: list[dict[str, object]] = []
    try:
        staged = preparing / "staged"
        backups = preparing / "backups"
        staged.mkdir(parents=True)
        backups.mkdir(parents=True)
        for index, (relative, data) in enumerate(desired.items()):
            target = repository_root / relative
            stage = staged / str(index)
            backup = backups / str(index)
            stage.write_bytes(data)
            existed = target.exists()
            original_hash = _digest(target.read_bytes()) if existed else None
            if existed:
                shutil.copy2(target, backup)
            entries.append({"path": relative.as_posix(), "stage": stage.relative_to(preparing).as_posix(), "backup": backup.relative_to(preparing).as_posix(), "existed": existed, "original_sha256": original_hash, "target_sha256": _digest(data)})
        _write_journal(preparing / "transaction.yaml", "prepared", entries)
        preparing.replace(tx)
    except Exception:
        if preparing.exists():
            shutil.rmtree(preparing)
        raise
    journal = tx / "transaction.yaml"
    _write_journal(journal, "replacing", entries)
    try:
        for entry in entries:
            replace_file(tx / str(entry["stage"]), repository_root / str(entry["path"]))
    except Exception:
        _rollback(repository_root, tx, entries)
        raise
    _write_journal(journal, "complete", entries)
    shutil.rmtree(tx)
    return check_standard_sync(repository_root)


def _rollback(repository_root: Path, tx: Path, entries: list[dict[str, object]]) -> None:
    for entry in entries:
        target = repository_root / str(entry["path"])
        if bool(entry["existed"]):
            shutil.copy2(tx / str(entry["backup"]), target)
        elif target.exists():
            target.unlink()
    shutil.rmtree(tx)


def recover_standard_sync(repository_root: Path) -> RecoveryResult:
    """Recover an interrupted transaction before any subsequent check."""
    preparing = repository_root / _PREPARING
    discarded_preparation = preparing.exists()
    if discarded_preparation:
        shutil.rmtree(preparing)
    tx = repository_root / _TRANSACTION
    journal = tx / "transaction.yaml"
    if not journal.is_file():
        if tx.exists():
            shutil.rmtree(tx)
            return RecoveryResult(True, "discarded-incomplete-transaction")
        return RecoveryResult(discarded_preparation, "discarded-preparation" if discarded_preparation else "none")
    try:
        document = yaml.safe_load(journal.read_text(encoding="utf-8"))
        entries = document["entries"]
        if not isinstance(entries, list):
            raise TypeError("entries")
        complete = all(
            (repository_root / str(entry["path"])).is_file()
            and _digest((repository_root / str(entry["path"])).read_bytes()) == entry["target_sha256"]
            for entry in entries
        )
    except (OSError, KeyError, TypeError, yaml.YAMLError) as error:
        raise ValidationError("standard sync journal is unreadable") from error
    if complete:
        shutil.rmtree(tx)
        return RecoveryResult(True, "completed")
    _rollback(repository_root, tx, entries)
    return RecoveryResult(True, "rolled-back")
