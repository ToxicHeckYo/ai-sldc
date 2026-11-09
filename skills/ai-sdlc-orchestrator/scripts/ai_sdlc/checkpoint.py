"""Crash-safe, runtime-neutral canonical checkpointing.

Checkpoint preparation never writes the canonical tree.  A prepared journal and
immutable old/new byte snapshots are kept in a sibling directory; validation
re-checks both the journal and the target files immediately before applying.
The final commit operation is supplied by a neutral adapter, so the core has no
Git or shell dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol
import uuid

from .integration import (
    _canonical_root,
    _change_bytes,
    _is_reparse_point,
    _reject_symlink_ambiguity,
    _safe_relative,
)
from .validation import ValidationFinding, ValidationReport


_MISSING = "<missing>"
_DELETED = "<deleted>"
_JOURNAL_VERSION = 1
_STATES = {"prepared", "validated", "staged", "committing", "committed", "recovered", "recovery_required"}


@dataclass(frozen=True, slots=True)
class CommitIdentity:
    """An identity returned by a commit adapter only after it confirms commit."""

    identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str):
            raise TypeError("commit identity must be a string")

    @property
    def commit_id(self) -> str:
        return self.identity

    @property
    def revision(self) -> str:
        return self.identity


class CommitAdapter(Protocol):
    """Neutral boundary for a host's VCS/checkpoint implementation."""

    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> CommitIdentity | str: ...


@dataclass(frozen=True, slots=True)
class PreparedCheckpoint:
    """Immutable handle to a prepared checkpoint and its target snapshot."""

    root: Path
    prepared_dir: Path
    manifest: Mapping[str, Mapping[str, Any]]
    manifest_hash: str
    journal_path: Path
    adapter: Any = None
    generation: int = 0
    seal_path: Path | None = None

    @property
    def area(self) -> Path:
        return self.prepared_dir

    @property
    def state(self) -> str:
        try:
            return str(_read_json(self.journal_path).get("state", ""))
        except (OSError, ValueError, TypeError):
            return ""

    @property
    def checkpoint_id(self) -> str:
        return self.prepared_dir.name


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Deterministic result of replaying any unfinished journals."""

    recovered: tuple[str, ...] = ()
    findings: tuple[ValidationFinding, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.findings

    @property
    def is_valid(self) -> bool:
        return self.is_clean

    @property
    def restored(self) -> tuple[str, ...]:
        return self.recovered


FaultInjector = Callable[[str], None]


def prepare_checkpoint(
    root: Path,
    changes: Mapping[str, Any] | Iterable[tuple[str, Any]],
    adapter: Any = None,
    commit_adapter: Any = None,
    fault_injector: FaultInjector | None = None,
) -> PreparedCheckpoint:
    """Stage ``changes`` in an isolated sibling area and write a journal.

    ``changes`` maps relative target paths to bytes/text, or to ``None`` (a
    deletion).  The canonical root remains byte-for-byte untouched.
    """
    canonical = _canonical_root(Path(root))
    _inject(fault_injector, "before_staging")
    entries = _snapshot_entries(canonical, changes)
    workspace = _workspace(canonical)
    _ensure_safe_directory(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    index_dir = workspace / "_index"
    _ensure_safe_directory(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    prepared_dir = workspace / uuid.uuid4().hex
    _ensure_safe_directory(prepared_dir)
    old_dir = prepared_dir / "old"
    new_dir = prepared_dir / "new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir()
    try:
        manifest: dict[str, dict[str, Any]] = {}
        for row_index, entry in enumerate(entries):
            path, target, old_bytes, new_bytes = entry
            old_file = f"old/{row_index:04d}.bin" if old_bytes is not None else None
            new_file = f"new/{path}" if new_bytes is not None else None
            if old_bytes is not None:
                _write_bytes(prepared_dir / old_file, old_bytes)  # type: ignore[arg-type]
            if new_bytes is not None:
                staged_path = prepared_dir / new_file  # type: ignore[arg-type]
                _ensure_safe_directory(staged_path.parent)
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                _write_bytes(staged_path, new_bytes)
            manifest[path] = {
                "old_exists": old_bytes is not None,
                "old_hash": _hash_bytes(old_bytes) if old_bytes is not None else _MISSING,
                "old_file": old_file,
                "new_exists": new_bytes is not None,
                "new_hash": _hash_bytes(new_bytes) if new_bytes is not None else _DELETED,
                "new_file": new_file,
            }
        manifest_hash = _manifest_hash(manifest)
        generation, reservation_path = _reserve_generation(index_dir, prepared_dir.name)
        seal_path = index_dir / f"{prepared_dir.name}.seal.json"
        _atomic_json_write(
            seal_path,
            {
                "version": _JOURNAL_VERSION,
                "checkpoint_id": prepared_dir.name,
                "root_identity": _root_identity(canonical),
                "generation": generation,
                "reservation": reservation_path.name,
                "manifest_sha256": manifest_hash,
            },
        )
        manifest_path = prepared_dir / "manifest.json"
        _atomic_json_write(manifest_path, {"version": _JOURNAL_VERSION, "entries": manifest})
        journal_path = prepared_dir / "journal.json"
        _atomic_json_write(
            journal_path,
            {
                "version": _JOURNAL_VERSION,
                "state": "prepared",
                "root": str(canonical),
                "root_identity": _root_identity(canonical),
                "manifest_sha256": manifest_hash,
                "manifest_file": "manifest.json",
                "checkpoint_id": prepared_dir.name,
                "generation": generation,
                "reservation": reservation_path.name,
                "attempted_paths": [],
                "applied_paths": [],
                "created_dirs": [],
            },
        )
        prepared = PreparedCheckpoint(
            canonical,
            prepared_dir,
            _freeze_manifest(manifest),
            manifest_hash,
            journal_path,
            adapter if adapter is not None else commit_adapter,
            generation,
            seal_path,
        )
        _inject(fault_injector, "after_staging")
        return prepared
    except Exception:
        # Leave an on-disk journal when one exists: recover() can deterministically
        # clean it after a crash/fault.  Before the journal exists there is no
        # canonical mutation and an orphan area is safe to remove.
        if not (prepared_dir / "journal.json").exists():
            shutil.rmtree(prepared_dir, ignore_errors=True)
        raise


def validate_checkpoint(prepared: PreparedCheckpoint) -> ValidationReport:
    """Validate manifest integrity, target cleanliness, and staged bytes."""
    findings: list[ValidationFinding] = []
    state: Any = None
    if not isinstance(prepared, PreparedCheckpoint):
        return ValidationReport((_finding("invalid_checkpoint", "prepared", "expected PreparedCheckpoint"),))
    try:
        root = _canonical_root(prepared.root)
        _ensure_safe_path(prepared.prepared_dir)
    except (OSError, ValueError) as error:
        return ValidationReport((_finding("invalid_canonical_root", "root", str(error)),))
    try:
        journal = _read_json(prepared.journal_path)
        if journal.get("version") != _JOURNAL_VERSION:
            findings.append(_finding("journal_version", "journal.version", "unsupported checkpoint journal version"))
        state = journal.get("state")
        if state not in _STATES:
            findings.append(_finding("journal_state", "journal.state", f"unsupported journal state: {state!r}"))
        if journal.get("root_identity") != _root_identity(root):
            findings.append(_finding("root_changed", "journal.root_identity", "canonical root identity changed"))
        if journal.get("checkpoint_id") != prepared.prepared_dir.name:
            findings.append(_finding("journal_binding_mismatch", "journal.checkpoint_id", "journal is not bound to this prepared area"))
        if journal.get("manifest_file") != "manifest.json":
            findings.append(_finding("manifest_binding_mismatch", "journal.manifest_file", "manifest path must be the prepared manifest"))
        if int(journal.get("generation", -1)) != prepared.generation:
            findings.append(_finding("journal_binding_mismatch", "journal.generation", "journal generation does not match prepared checkpoint"))
        manifest_document = _read_json(prepared.prepared_dir / "manifest.json")
        on_disk_manifest = manifest_document.get("entries")
        if not isinstance(on_disk_manifest, dict):
            findings.append(_finding("manifest_invalid", "manifest.entries", "manifest entries must be an object"))
            on_disk_manifest = {}
        disk_hash = _manifest_hash(on_disk_manifest)
        expected_hash = str(journal.get("manifest_sha256", ""))
        if disk_hash != expected_hash or disk_hash != prepared.manifest_hash:
            findings.append(_finding("manifest_tampered", "manifest", "manifest hash does not match the prepared immutable manifest"))
        seal_path = prepared.seal_path or _seal_path(root, prepared.prepared_dir.name)
        seal = _read_json(seal_path)
        if (
            seal.get("checkpoint_id") != prepared.prepared_dir.name
            or seal.get("root_identity") != _root_identity(root)
            or int(seal.get("generation", -1)) != prepared.generation
            or seal.get("manifest_sha256") != disk_hash
        ):
            findings.append(_finding("manifest_binding_mismatch", "seal", "independent checkpoint seal does not match journal and manifest"))
        if journal.get("reservation") != seal.get("reservation"):
            findings.append(_finding("generation_reservation_invalid", "journal.reservation", "journal generation reservation does not match independent seal"))
        reservation = seal.get("reservation")
        if not isinstance(reservation, str) or not reservation.endswith(".reserve.json"):
            findings.append(_finding("generation_reservation_invalid", "seal.reservation", "checkpoint generation reservation is missing or malformed"))
        else:
            reservation_path = _reservation_path(root, reservation)
            try:
                reservation_document = _read_json(reservation_path)
                if (
                    reservation_document.get("version") != _JOURNAL_VERSION
                    or reservation_document.get("generation") != prepared.generation
                    or reservation_document.get("checkpoint_id") != prepared.prepared_dir.name
                ):
                    findings.append(_finding("generation_reservation_invalid", "seal.reservation", "generation reservation does not bind this checkpoint"))
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                findings.append(_finding("generation_reservation_invalid", "seal.reservation", "generation reservation cannot be read"))
        if state == "committed" and not _commit_marker(prepared):
            findings.append(_finding("commit_marker_missing", "journal.state", "committed journal has no durable commit marker"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        findings.append(_finding("journal_tampered", "journal", f"cannot read checkpoint journal: {error}"))
        on_disk_manifest = {}

    paths = tuple(str(path) for path in on_disk_manifest)
    _path_findings(findings, root, paths)
    for path in sorted(paths, key=lambda value: value.casefold()):
        entry = on_disk_manifest.get(path)
        if not isinstance(entry, Mapping):
            findings.append(_finding("manifest_invalid", f"manifest.entries[{path}]", "manifest entry must be an object"))
            continue
        try:
            safe = _safe_relative(path)
            target = root.joinpath(*safe)
            _reject_symlink_ambiguity(root, target)
        except (OSError, ValueError) as error:
            text = str(error).casefold()
            code = "reparse_point" if "reparse" in text or "symlink" in text else "file_ancestor" if "regular file ancestor" in text else "path_escape"
            findings.append(_finding(code, f"manifest.entries[{path}]", str(error)))
            continue
        old_exists = bool(entry.get("old_exists"))
        old_hash = str(entry.get("old_hash", ""))
        try:
            current = _read_target(target)
        except (OSError, ValueError) as error:
            findings.append(_finding("target_changed", path, str(error)))
            current = None
        if old_exists:
            if current is None or _hash_bytes(current) != old_hash:
                findings.append(_finding("target_changed", path, "target no longer matches the prepared cleanliness snapshot"))
        elif current is not None:
            findings.append(_finding("target_changed", path, "target was created or changed after checkpoint preparation"))
        new_exists = bool(entry.get("new_exists"))
        new_file = entry.get("new_file")
        if new_exists:
            try:
                staged = _safe_staged_path(prepared.prepared_dir, new_file)
                if not staged.is_file() or _hash_bytes(staged.read_bytes()) != str(entry.get("new_hash", "")):
                    findings.append(_finding("staged_tampered", path, "staged bytes do not match manifest"))
            except (OSError, ValueError):
                findings.append(_finding("staged_tampered", path, "staged bytes are unavailable"))
        elif entry.get("new_hash") != _DELETED:
            findings.append(_finding("manifest_invalid", path, "deleted entry must use the deleted hash marker"))
        old_file = entry.get("old_file")
        if old_exists:
            try:
                old_snapshot = _safe_staged_path(prepared.prepared_dir, old_file)
                if not old_snapshot.is_file() or _hash_bytes(old_snapshot.read_bytes()) != old_hash:
                    findings.append(_finding("snapshot_tampered", path, "old snapshot bytes do not match manifest"))
            except (OSError, ValueError):
                findings.append(_finding("snapshot_missing", path, "old snapshot bytes are unavailable"))
    return ValidationReport(tuple(sorted(findings, key=lambda finding: (finding.path, finding.code, finding.message))))


def commit_checkpoint(
    prepared: PreparedCheckpoint,
    message: str,
    *,
    adapter: Any = None,
    runtime: Any = None,
    fault_injector: FaultInjector | None = None,
) -> CommitIdentity:
    """Apply only a validated checkpoint and ask a neutral adapter to commit it."""
    _validate_message(message)
    report = validate_checkpoint(prepared)
    if not report.is_valid:
        first = report.findings[0]
        raise ValueError(f"{first.code}: {first.message}")
    _set_state(prepared, "validated")
    _inject(fault_injector, "after_validation")
    # The validation hook is an intentional TOCTOU boundary in tests and in
    # host adapters.  Re-check target cleanliness immediately before the first
    # canonical write so a change between validation and staging is fail-closed.
    latest = validate_checkpoint(prepared)
    if not latest.is_valid:
        first = latest.findings[0]
        raise ValueError(f"{first.code}: {first.message}")
    adapter_confirmed = False
    try:
        _apply_new(prepared)
        _set_state(prepared, "staged")
        _inject(fault_injector, "after_staging")
        _set_state(prepared, "committing")
        _inject(fault_injector, "before_commit")
        commit_adapter = adapter or runtime or prepared.adapter
        if commit_adapter is None:
            raise RuntimeError("a neutral commit adapter is required")
        result = _invoke_adapter(commit_adapter, prepared, message)
        identity = _identity(result)
        if identity is None:
            raise RuntimeError("commit adapter did not confirm a non-empty commit identity")
        adapter_confirmed = True
        # The marker is independent of the provider-modifiable prepared area.
        # Once durable, recovery must never roll back this checkpoint even if
        # the journal update below fails or a stale journal is replayed.
        _write_commit_marker(prepared, identity)
        _set_state(prepared, "committed", identity=identity.identity)
        return identity
    except Exception as error:
        # A host adapter may discover a post-commit verification problem after
        # Git has already returned a durable SHA.  Preserve canonical bytes
        # in that case; rolling them back would make the worktree disagree with
        # the new HEAD.  Adapters expose the confirmed identity explicitly via
        # ``commit_identity`` on the raised error.
        confirmed = _identity(getattr(error, "commit_identity", None))
        if confirmed is not None:
            adapter_confirmed = True
            try:
                _write_commit_marker(prepared, confirmed)
                _set_state(prepared, "committed", identity=confirmed.identity)
            except Exception:
                pass
        if adapter_confirmed:
            # The host has confirmed a commit.  Do not restore canonical files
            # merely because recording the journal state failed.
            raise
        else:
            findings = _rollback_pending(prepared)
            if findings:
                _set_state_safe(prepared, "recovery_required")
            else:
                try:
                    journal = _read_json(prepared.journal_path)
                    attempted = tuple(str(path) for path in journal.get("attempted_paths", ()))
                    _mark_recovered(prepared, attempted)
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                    _set_state_safe(prepared, "recovered")
        raise


def recover(root: Path) -> RecoveryReport:
    """Idempotently restore all unfinished checkpoints for ``root``."""
    try:
        canonical = _canonical_root(Path(root))
    except (OSError, ValueError) as error:
        return RecoveryReport(findings=(_finding("invalid_canonical_root", "root", str(error)),))
    workspace = _workspace(canonical)
    try:
        _ensure_safe_path(workspace)
    except (OSError, ValueError) as error:
        return RecoveryReport(findings=(_finding("reparse_point", "workspace", str(error)),))
    if not workspace.is_dir():
        return RecoveryReport()
    recovered: list[str] = []
    findings: list[ValidationFinding] = []
    latest_generation, index_findings = _latest_committed_generation(canonical)
    findings.extend(index_findings)
    for prepared_dir in sorted((item for item in workspace.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
        try:
            _ensure_safe_path(prepared_dir)
        except (OSError, ValueError) as error:
            findings.append(_finding("reparse_point", str(prepared_dir), str(error)))
            continue
        journal_path = prepared_dir / "journal.json"
        if not journal_path.is_file():
            continue
        try:
            journal = _read_json(journal_path)
            if journal.get("root_identity") != _root_identity(canonical):
                continue
            prepared = _load_prepared(canonical, prepared_dir, journal)
            marker_valid = _commit_marker(prepared)
            if journal.get("state") == "committed":
                if marker_valid:
                    continue
                findings.append(_finding("commit_marker_missing", prepared_dir.name, "committed state has no valid durable commit marker"))
                continue
            if journal.get("state") == "recovered":
                if _valid_recovery_terminal(prepared, journal):
                    continue
                findings.append(_finding("recovery_evidence_missing", prepared_dir.name, "recovered state has no valid recovery evidence"))
                continue
            if marker_valid or prepared.generation < latest_generation:
                # A newer durable commit supersedes this journal.  In
                # particular, never let stale old bytes overwrite a newer
                # canonical snapshot.
                continue
            if journal.get("state") == "committing":
                # The adapter may have been entered but its identity marker is
                # absent.  Never guess that a VCS commit did not happen.
                findings.append(_finding("commit_ambiguous", prepared_dir.name, "commit attempt has no durable identity marker; manual recovery required"))
                continue
            validation = validate_checkpoint(prepared)
            # target_changed is expected after a partial apply; manifest/staged
            # tampering is not trusted for recovery.
            fatal = tuple(f for f in validation.findings if f.code in {"manifest_tampered", "manifest_binding_mismatch", "journal_tampered", "path_escape", "file_ancestor", "reparse_point", "manifest_invalid", "staged_tampered", "snapshot_tampered", "snapshot_missing", "commit_marker_missing"})
            if fatal:
                findings.extend(fatal)
                continue
            attempted = tuple(str(path) for path in journal.get("attempted_paths", ()))
            if not attempted:
                # No durable mutation intent exists.  External target changes
                # are not ours to undo; leave them untouched and request
                # manual handling rather than restoring the old snapshot.
                if any(f.code == "target_changed" for f in validation.findings):
                    findings.append(_finding("recovery_conflict", prepared_dir.name, "target changed before any durable mutation"))
                    continue
                _mark_recovered(prepared, ())
                recovered.append(prepared_dir.name)
                continue
            rollback_findings = _restore_old(prepared, attempted)
            if rollback_findings:
                findings.extend(rollback_findings)
                continue
            _mark_recovered(prepared, attempted)
            recovered.append(prepared_dir.name)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as error:
            findings.append(_finding("recovery_failed", str(prepared_dir), str(error)))
    return RecoveryReport(tuple(recovered), tuple(sorted(findings, key=lambda finding: (finding.path, finding.code))))


def _snapshot_entries(root: Path, changes: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> list[tuple[str, Path, bytes | None, bytes | None]]:
    items = list(changes.items()) if isinstance(changes, Mapping) else list(changes)
    normalized: list[tuple[str, Path, bytes | None, bytes | None]] = []
    seen: dict[tuple[str, ...], str] = {}
    for raw_path, value in items:
        safe = _safe_relative(raw_path)
        key = tuple(part.casefold() for part in safe)
        if key in seen:
            raise ValueError(f"path collision between {seen[key]!r} and {raw_path!r}")
        seen[key] = str(raw_path)
        target = root.joinpath(*safe)
        _reject_symlink_ambiguity(root, target)
        _reject_file_ancestors(root, target)
        canonical_path = "/".join(safe)
        old = _read_target(target)
        new = _checkpoint_bytes(value)
        normalized.append((canonical_path, target, old, new))
    keys = sorted(seen)
    for left, right in zip(keys, keys[1:]):
        if len(left) < len(right) and right[: len(left)] == left:
            raise ValueError(f"path collision between file {seen[left]!r} and descendant {seen[right]!r}")
    return sorted(normalized, key=lambda entry: entry[0].casefold())


def _path_findings(findings: list[ValidationFinding], root: Path, paths: tuple[str, ...]) -> None:
    seen: dict[tuple[str, ...], str] = {}
    safe_paths: list[tuple[str, ...]] = []
    for path in paths:
        try:
            safe = _safe_relative(path)
            _reject_symlink_ambiguity(root, root.joinpath(*safe))
            _reject_file_ancestors(root, root.joinpath(*safe))
        except (OSError, ValueError) as error:
            text = str(error).casefold()
            code = "reparse_point" if "reparse" in text or "symlink" in text else "file_ancestor" if "regular file ancestor" in text else "path_escape"
            findings.append(_finding(code, f"manifest.entries[{path}]", str(error)))
            continue
        folded = tuple(part.casefold() for part in safe)
        if folded in seen and seen[folded] != path:
            findings.append(_finding("path_collision", f"manifest.entries[{path}]", f"collides with {seen[folded]!r}"))
        seen[folded] = path
        safe_paths.append(safe)
    for left, right in zip(sorted(safe_paths), sorted(safe_paths)[1:]):
        if len(left) < len(right) and right[: len(left)] == left:
            findings.append(_finding("path_collision", f"manifest.entries[{'/'.join(right)}]", "path is both a file and an ancestor"))


def _apply_new(prepared: PreparedCheckpoint) -> None:
    journal = _read_json(prepared.journal_path)
    attempted = [str(path) for path in journal.get("attempted_paths", ())]
    created_dirs = [str(path) for path in journal.get("created_dirs", ())]
    for path in sorted(prepared.manifest, key=str.casefold):
        journal = _read_json(prepared.journal_path)
        attempted = [str(value) for value in journal.get("attempted_paths", attempted)]
        created_dirs = [str(value) for value in journal.get("created_dirs", created_dirs)]
        entry = prepared.manifest[path]
        target = prepared.root.joinpath(*_safe_relative(path))
        _reject_symlink_ambiguity(prepared.root, target)
        _reject_file_ancestors(prepared.root, target)
        if path not in attempted:
            attempted.append(path)
            _set_state(prepared, journal.get("state", "validated"), attempted_paths=attempted, applied_paths=journal.get("applied_paths", ()), created_dirs=created_dirs)
        missing_dirs = _missing_parent_dirs(prepared.root, target)
        for directory in missing_dirs:
            value = "/".join(directory.relative_to(prepared.root).parts)
            if value not in created_dirs:
                created_dirs.append(value)
        if missing_dirs:
            _set_state(prepared, journal.get("state", "validated"), attempted_paths=attempted, applied_paths=journal.get("applied_paths", ()), created_dirs=created_dirs)
        for directory in missing_dirs:
            _ensure_safe_directory(directory)
            directory.mkdir()
        if entry["new_exists"]:
            staged = _safe_staged_path(prepared.prepared_dir, entry["new_file"])
            content = staged.read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_replace(target, content)
        elif target.exists():
            target.unlink()
        if not _target_matches(target, bool(entry["new_exists"]), str(entry["new_hash"])):
            raise RuntimeError(f"applied target does not match staged hash: {path}")
        journal = _read_json(prepared.journal_path)
        applied_paths = list(journal.get("applied_paths", ()))
        if path not in applied_paths:
            applied_paths.append(path)
        _set_state(prepared, journal.get("state", "validated"), attempted_paths=attempted, applied_paths=applied_paths, created_dirs=created_dirs)


def _restore_old(prepared: PreparedCheckpoint, paths: Iterable[str] | None = None) -> tuple[ValidationFinding, ...]:
    findings: list[ValidationFinding] = []
    selected = tuple(paths) if paths is not None else tuple(prepared.manifest)
    for path in sorted(selected, key=str.casefold, reverse=True):
        if path not in prepared.manifest:
            findings.append(_finding("recovery_conflict", path, "attempted target is absent from the immutable manifest"))
            continue
        entry = prepared.manifest[path]
        target = prepared.root.joinpath(*_safe_relative(path))
        try:
            _reject_symlink_ambiguity(prepared.root, target)
            _reject_file_ancestors(prepared.root, target)
        except (OSError, ValueError) as error:
            findings.append(_finding("recovery_conflict", path, str(error)))
            continue
        if not _target_matches(target, bool(entry["new_exists"]), str(entry["new_hash"])):
            if _target_matches(target, bool(entry["old_exists"]), str(entry["old_hash"])):
                continue
            findings.append(_finding("recovery_conflict", path, "target does not match expected post-apply or old snapshot; manual recovery required"))
            continue
        if entry["old_exists"]:
            old_file = _safe_staged_path(prepared.prepared_dir, entry["old_file"])
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_replace(target, old_file.read_bytes())
        elif target.exists() and not target.is_dir():
            target.unlink()
    if not findings:
        _remove_created_dirs(prepared)
    return tuple(findings)


def _rollback_pending(prepared: PreparedCheckpoint) -> tuple[ValidationFinding, ...]:
    try:
        journal = _read_json(prepared.journal_path)
        attempted = tuple(str(path) for path in journal.get("attempted_paths", ()))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return (_finding("recovery_failed", "journal", str(error)),)
    return _restore_old(prepared, attempted)


def _recovery_evidence(prepared: PreparedCheckpoint, attempted: Iterable[str]) -> str:
    values: dict[str, Any] = {"attempted": sorted(str(path) for path in attempted), "targets": {}}
    for path in sorted(prepared.manifest, key=str.casefold):
        target = prepared.root.joinpath(*_safe_relative(path))
        current = _read_target(target)
        values["targets"][path] = _hash_bytes(current) if current is not None else _MISSING
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _hash_bytes(encoded)


def _valid_recovery_terminal(prepared: PreparedCheckpoint, journal: Mapping[str, Any]) -> bool:
    evidence = journal.get("recovery_evidence")
    if not _is_sha256(evidence):
        return False
    try:
        attempted = journal.get("attempted_paths", ())
        seal = _read_json(prepared.seal_path or _seal_path(prepared.root, prepared.checkpoint_id))
        return evidence == _recovery_evidence(prepared, attempted) and seal.get("recovery_evidence") == evidence
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


def _mark_recovered(prepared: PreparedCheckpoint, attempted: Iterable[str]) -> None:
    values = tuple(str(path) for path in attempted)
    evidence = _recovery_evidence(prepared, values)
    seal_path = prepared.seal_path or _seal_path(prepared.root, prepared.checkpoint_id)
    seal = _read_json(seal_path)
    seal["recovery_evidence"] = evidence
    seal["recovery_attempted"] = sorted(values)
    _atomic_json_write(seal_path, seal)
    _set_state(prepared, "recovered", recovery_evidence=evidence)


def _missing_parent_dirs(root: Path, target: Path) -> tuple[Path, ...]:
    relative = target.parent.relative_to(root)
    missing: list[Path] = []
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists():
            if not current.is_dir():
                raise ValueError(f"regular file ancestor blocks target: {current}")
        else:
            missing.append(current)
    return tuple(missing)


def _remove_created_dirs(prepared: PreparedCheckpoint) -> None:
    try:
        journal = _read_json(prepared.journal_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    values = tuple(str(path) for path in journal.get("created_dirs", ()))
    for value in sorted(values, key=lambda path: (path.count("/"), path), reverse=True):
        try:
            path = prepared.root.joinpath(*_safe_relative(value))
            if path.is_dir() and not _is_reparse_point(path):
                path.rmdir()
        except (OSError, ValueError):
            continue


def _load_prepared(root: Path, prepared_dir: Path, journal: Mapping[str, Any]) -> PreparedCheckpoint:
    if journal.get("manifest_file") != "manifest.json":
        raise ValueError("journal manifest path is not bound to the prepared area")
    if journal.get("checkpoint_id") != prepared_dir.name:
        raise ValueError("journal checkpoint identity is malformed")
    if not isinstance(journal.get("root_identity"), str) or not isinstance(journal.get("manifest_sha256"), str):
        raise ValueError("journal binding fields are malformed")
    if not _is_sha256(journal.get("manifest_sha256")):
        raise ValueError("journal manifest hash is malformed")
    if not isinstance(journal.get("generation"), int) or isinstance(journal.get("generation"), bool) or journal["generation"] < 1:
        raise ValueError("journal generation is malformed")
    for field in ("attempted_paths", "applied_paths", "created_dirs"):
        if not isinstance(journal.get(field), list) or not all(isinstance(value, str) for value in journal[field]):
            raise ValueError(f"journal {field} is malformed")
    _ensure_safe_path(prepared_dir / "manifest.json")
    document = _read_json(prepared_dir / "manifest.json")
    if document.get("version") != _JOURNAL_VERSION or not isinstance(document.get("entries"), dict):
        raise ValueError("manifest document is malformed")
    entries = document["entries"]
    for path, entry in entries.items():
        if not isinstance(path, str) or not isinstance(entry, Mapping):
            raise ValueError("manifest entry is malformed")
    seal = _read_json(_seal_path(root, prepared_dir.name))
    required = ("version", "checkpoint_id", "root_identity", "generation", "reservation", "manifest_sha256")
    if any(key not in seal for key in required):
        raise ValueError("seal document is missing required binding fields")
    if seal.get("version") != _JOURNAL_VERSION or seal.get("checkpoint_id") != prepared_dir.name:
        raise ValueError("seal document identity is malformed")
    if not isinstance(seal.get("root_identity"), str) or not isinstance(seal.get("reservation"), str):
        raise ValueError("seal document types are malformed")
    if not isinstance(seal.get("generation"), int) or isinstance(seal.get("generation"), bool) or seal["generation"] < 1:
        raise ValueError("seal generation is malformed")
    if not _is_sha256(seal.get("manifest_sha256")):
        raise ValueError("seal manifest hash is malformed")
    _reservation_path(root, seal["reservation"])
    generation = seal["generation"]
    manifest_hash = seal["manifest_sha256"]
    return PreparedCheckpoint(root, prepared_dir, _freeze_manifest(entries), manifest_hash, prepared_dir / "journal.json", None, generation, _seal_path(root, prepared_dir.name))


def _invoke_adapter(adapter: Any, prepared: PreparedCheckpoint, message: str) -> Any:
    paths = tuple(sorted(prepared.manifest, key=str.casefold))
    if hasattr(adapter, "commit_checkpoint"):
        return adapter.commit_checkpoint(prepared, message)
    if hasattr(adapter, "commit"):
        return adapter.commit(prepared.root, paths, message)
    if callable(adapter):
        return adapter(prepared, message)
    raise TypeError("commit adapter must provide commit_checkpoint, commit, or be callable")


def _identity(value: Any) -> CommitIdentity | None:
    if isinstance(value, CommitIdentity):
        return value if value.identity.strip() else None
    if isinstance(value, str):
        return CommitIdentity(value) if value.strip() else None
    if isinstance(value, Mapping):
        for key in ("identity", "commit_id", "revision", "id"):
            if key in value and isinstance(value[key], str) and value[key].strip():
                return CommitIdentity(value[key])
    return None


def _validate_message(message: str) -> None:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("commit message must be non-empty")
    if "\x00" in message or "\r" in message or "\n" in message:
        raise ValueError("commit message must not contain control/newline characters")
    if len(message) > 1000:
        raise ValueError("commit message is too long")


def _workspace(root: Path) -> Path:
    return root.parent / f".{root.name}.ai-sdlc-checkpoints"


def _seal_path(root: Path, checkpoint_id: str) -> Path:
    return _workspace(root) / "_index" / f"{checkpoint_id}.seal.json"


def _commit_marker_path(root: Path, checkpoint_id: str) -> Path:
    return _workspace(root) / "_index" / f"{checkpoint_id}.commit.json"


def _reservation_path(root: Path, reservation: Any) -> Path:
    if not isinstance(reservation, str) or Path(reservation).name != reservation or not reservation.startswith("generation-") or not reservation.endswith(".reserve.json"):
        raise ValueError("generation reservation path is malformed")
    try:
        int(reservation[len("generation-") : -len(".reserve.json")])
    except (ValueError, TypeError) as error:
        raise ValueError("generation reservation number is malformed") from error
    path = _workspace(root) / "_index" / reservation
    _ensure_safe_path(path)
    return path


def _next_generation(index: Path) -> int:
    maximum = 0
    if not index.is_dir():
        return 1
    for path in sorted(index.glob("*.seal.json"), key=lambda item: item.name.casefold()):
        try:
            maximum = max(maximum, int(_read_json(path).get("generation", 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    for path in sorted(index.glob("generation-*.reserve.json"), key=lambda item: item.name.casefold()):
        try:
            maximum = max(maximum, int(path.name[len("generation-") : -len(".reserve.json")]))
        except (ValueError, TypeError):
            continue
    return maximum + 1


def _exclusive_generation_reservation(index: Path, generation: int, checkpoint_id: str) -> Path:
    path = index / f"generation-{generation}.reserve.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(path), flags)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(
                {
                    "version": _JOURNAL_VERSION,
                    "generation": generation,
                    "checkpoint_id": checkpoint_id,
                },
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return path


def _reserve_generation(index: Path, checkpoint_id: str) -> tuple[int, Path]:
    candidate = _next_generation(index)
    for _ in range(10000):
        try:
            return candidate, _exclusive_generation_reservation(index, candidate, checkpoint_id)
        except FileExistsError:
            candidate += 1
    raise RuntimeError("unable to reserve a unique checkpoint generation")


def _write_commit_marker(prepared: PreparedCheckpoint, identity: CommitIdentity) -> None:
    marker = _commit_marker_path(prepared.root, prepared.checkpoint_id)
    _atomic_json_write(
        marker,
        {
            "version": _JOURNAL_VERSION,
            "state": "committed",
            "checkpoint_id": prepared.checkpoint_id,
            "root_identity": _root_identity(prepared.root),
            "generation": prepared.generation,
            "manifest_sha256": prepared.manifest_hash,
            "identity": identity.identity,
        },
    )


def _commit_marker(prepared: PreparedCheckpoint) -> bool:
    valid, _finding_value, _generation = _validate_commit_marker(prepared.root, _commit_marker_path(prepared.root, prepared.checkpoint_id), prepared.checkpoint_id)
    return valid


def _validate_commit_marker(root: Path, marker_path: Path, expected_checkpoint: str | None = None) -> tuple[bool, ValidationFinding | None, int]:
    try:
        _ensure_safe_path(marker_path)
        marker = _read_json(marker_path)
        required = ("version", "state", "checkpoint_id", "root_identity", "generation", "manifest_sha256", "identity")
        if any(key not in marker for key in required):
            raise ValueError("commit marker is missing required fields")
        checkpoint_id = marker["checkpoint_id"]
        generation = marker["generation"]
        if (
            not isinstance(marker.get("version"), int)
            or isinstance(marker.get("version"), bool)
            or marker.get("version") != _JOURNAL_VERSION
            or marker.get("state") != "committed"
            or not isinstance(checkpoint_id, str)
            or not checkpoint_id
            or "/" in checkpoint_id
            or "\\" in checkpoint_id
            or (expected_checkpoint is not None and checkpoint_id != expected_checkpoint)
            or marker.get("root_identity") != _root_identity(root)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not _is_sha256(marker.get("manifest_sha256"))
            or not isinstance(marker.get("identity"), str)
            or not marker["identity"].strip()
        ):
            raise ValueError("commit marker fields are malformed")
        if marker_path.name != f"{checkpoint_id}.commit.json":
            raise ValueError("commit marker filename is not bound to checkpoint identity")
        prepared_dir = _workspace(root) / checkpoint_id
        _ensure_safe_path(prepared_dir)
        seal = _read_json(_seal_path(root, checkpoint_id))
        if (
            not isinstance(seal.get("version"), int)
            or isinstance(seal.get("version"), bool)
            or seal.get("version") != _JOURNAL_VERSION
            or seal.get("checkpoint_id") != checkpoint_id
            or seal.get("root_identity") != _root_identity(root)
            or seal.get("generation") != generation
            or seal.get("manifest_sha256") != marker["manifest_sha256"]
            or not isinstance(seal.get("reservation"), str)
        ):
            raise ValueError("commit marker seal binding is malformed")
        reservation_path = _reservation_path(root, seal["reservation"])
        reservation = _read_json(reservation_path)
        if (
            not isinstance(reservation.get("version"), int)
            or isinstance(reservation.get("version"), bool)
            or reservation.get("version") != _JOURNAL_VERSION
            or reservation.get("generation") != generation
            or reservation.get("checkpoint_id") != checkpoint_id
        ):
            raise ValueError("commit marker generation reservation is malformed")
        manifest_document = _read_json(prepared_dir / "manifest.json")
        entries = manifest_document.get("entries")
        if manifest_document.get("version") != _JOURNAL_VERSION or not isinstance(entries, dict) or _manifest_hash(entries) != marker["manifest_sha256"]:
            raise ValueError("commit marker manifest binding is malformed")
        journal = _read_json(prepared_dir / "journal.json")
        if (
            journal.get("checkpoint_id") != checkpoint_id
            or journal.get("root_identity") != _root_identity(root)
            or journal.get("generation") != generation
            or journal.get("manifest_sha256") != marker["manifest_sha256"]
            or journal.get("manifest_file") != "manifest.json"
            or journal.get("reservation") != seal.get("reservation")
            or journal.get("state") not in {"committing", "committed"}
        ):
            raise ValueError("commit marker journal binding is malformed")
        return True, None, generation
    except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as error:
        return False, _finding("commit_marker_invalid", str(marker_path), str(error)), 0


def _latest_committed_generation(root: Path) -> tuple[int, tuple[ValidationFinding, ...]]:
    index = _workspace(root) / "_index"
    maximum = 0
    findings: list[ValidationFinding] = []
    try:
        _ensure_safe_path(index)
    except (OSError, ValueError) as error:
        return maximum, (_finding("reparse_point", str(index), str(error)),)
    if not index.is_dir():
        return maximum, ()
    for path in sorted(index.glob("*.commit.json"), key=lambda item: item.name.casefold()):
        valid, finding, generation = _validate_commit_marker(root, path)
        if valid:
            maximum = max(maximum, generation)
        elif finding is not None:
            findings.append(finding)
    return maximum, tuple(findings)


def _root_identity(root: Path) -> str:
    return os.path.normcase(os.path.normpath(str(_canonical_root(root)))).replace("\\", "/").casefold()


def _freeze_manifest(manifest: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType({str(path): MappingProxyType(dict(entry)) for path, entry in manifest.items()})


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return _hash_bytes(encoded)


def _read_target(target: Path) -> bytes | None:
    if not target.exists():
        return None
    if not target.is_file():
        raise ValueError(f"target is not a regular file: {target}")
    return target.read_bytes()


def _safe_staged_path(prepared_dir: Path, value: Any) -> Path:
    """Resolve a manifest snapshot path without allowing it to escape staging."""
    if not isinstance(value, str) or not value:
        raise ValueError("staged snapshot path must be a non-empty string")
    raw_candidate = prepared_dir / value
    _ensure_safe_path(raw_candidate)
    candidate = raw_candidate.resolve()
    base = prepared_dir.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as error:
        raise ValueError("staged snapshot path escapes prepared area") from error
    return candidate


def _checkpoint_bytes(value: Any) -> bytes | None:
    if isinstance(value, Mapping):
        if value.get("delete") is True:
            return None
        value = value.get("content", value.get("value", b""))
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    if value is None:
        return None
    raise TypeError(f"unsupported checkpoint payload: {type(value).__name__}")


def _reject_file_ancestors(root: Path, target: Path) -> None:
    relative = target.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_file():
            raise ValueError(f"regular file ancestor blocks target: {current}")


def _ensure_safe_path(path: Path) -> None:
    """Reject reparse/symlink components before any mkdir/read/write."""
    current = path
    components: list[Path] = []
    while current != current.parent:
        components.append(current)
        current = current.parent
    components.append(current)
    for component in reversed(components):
        if _is_reparse_point(component):
            raise ValueError(f"reparse point path is rejected: {component}")


def _ensure_safe_directory(path: Path) -> None:
    _ensure_safe_path(path)
    if path.exists() and not path.is_dir():
        raise ValueError(f"checkpoint directory is not a directory: {path}")


def _target_matches(target: Path, exists: bool, expected_hash: str) -> bool:
    if not exists:
        return not target.exists()
    return target.is_file() and _hash_bytes(target.read_bytes()) == expected_hash


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_replace(target: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json_write(path: Path, document: Mapping[str, Any]) -> None:
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _atomic_replace(path, encoded)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("checkpoint document must be an object")
    return value


def _set_state(prepared: PreparedCheckpoint, state: str, **extra: Any) -> None:
    if state not in _STATES:
        raise ValueError(f"invalid checkpoint state: {state}")
    journal = _read_json(prepared.journal_path)
    journal["state"] = state
    journal.update(extra)
    _atomic_json_write(prepared.journal_path, journal)


def _set_state_safe(prepared: PreparedCheckpoint, state: str) -> None:
    try:
        _set_state(prepared, state)
    except OSError:
        pass


def _inject(injector: FaultInjector | None, phase: str) -> None:
    if injector is not None:
        injector(phase)


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value.casefold())


def _finding(code: str, path: str, message: str) -> ValidationFinding:
    return ValidationFinding(code, path, message)


__all__ = (
    "CommitAdapter", "CommitIdentity", "PreparedCheckpoint", "RecoveryReport",
    "commit_checkpoint", "prepare_checkpoint", "recover", "validate_checkpoint",
)
