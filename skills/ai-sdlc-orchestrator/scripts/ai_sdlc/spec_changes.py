"""Governed, deterministic Markdown specification deltas.

This module is deliberately self contained.  A change is represented as an
immutable preview and can only reach the canonical tree through
``apply_approved_delta`` after an exact approval and production deployment
evidence have been supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .approvals import Approval
from .approvals import verify_approval
from .checkpoint import commit_checkpoint, prepare_checkpoint, validate_checkpoint
from jsonschema import Draft202012Validator


_REQ = re.compile(r"^\s*##\s+(REQ-[A-Za-z0-9][A-Za-z0-9_-]*):\s*(.*?)\s*$")
_DELTA_REQ = re.compile(r"^\s*###\s+(REQ-[A-Za-z0-9][A-Za-z0-9_-]*):\s*(.*?)\s*$")
_OP = re.compile(r"^\s*##\s+(ADDED|MODIFIED|REMOVED)\s*$", re.IGNORECASE)
_REQ_VALID = re.compile(r"^REQ-[0-9]{3,}$")
_CHANGE_VALID = re.compile(r"^CHG-[0-9]{3,}$")
_URL = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//)")


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    findings: tuple[Finding, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.findings

    @property
    def valid(self) -> bool:
        return self.is_valid


@dataclass(frozen=True, slots=True)
class DeltaOperation:
    operation: str
    requirement_id: str
    title: str
    body: str

    @property
    def op(self) -> str:
        return self.operation

    @property
    def id(self) -> str:
        return self.requirement_id


@dataclass(frozen=True, slots=True)
class Delta:
    """Convenient immutable representation for callers that already parsed a delta."""

    operations: tuple[DeltaOperation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))


@dataclass(frozen=True, slots=True)
class ChangePackage:
    path: Path
    change_id: str
    title: str
    status: str
    affects: tuple[str, ...]
    references: tuple[str, ...]
    operations: tuple[DeltaOperation, ...]
    current_root: Path | None = None
    reason: str = ""

    @property
    def delta(self) -> tuple[DeltaOperation, ...]:
        return self.operations


@dataclass(frozen=True, slots=True)
class Preview:
    """A complete, reproducible candidate specification state."""

    root: Path
    root_identity: str
    current_path: Path | None
    delta_path: Path | None
    change_id: str | None
    source_hashes: Mapping[str, str]
    result_hashes: Mapping[str, str]
    source_bytes: Mapping[str, bytes]
    result_bytes: Mapping[str, bytes]
    delta_bytes: bytes
    change_bytes: bytes
    binding_payload: bytes
    operations: tuple[DeltaOperation, ...]
    affected_ids: tuple[str, ...]
    diff: str
    fingerprint: str
    required_role: str = "product-owner"
    package_path: Path | None = None
    baseline_manifest_path: Path | None = None
    baseline_manifest_bytes: bytes = b""
    baseline_manifest_hash: str = ""

    @property
    def preview_fingerprint(self) -> str:
        return self.fingerprint

    @property
    def expected_hashes(self) -> Mapping[str, str]:
        return self.result_hashes

    @property
    def trace_ids(self) -> tuple[str, ...]:
        return self.affected_ids

    @property
    def approval_payload(self) -> bytes:
        return self.binding_payload


@dataclass(frozen=True, slots=True)
class ApplyResult:
    preview: Preview
    change_id: str | None
    verified: bool
    status: str
    before_hashes: Mapping[str, str]
    after_hashes: Mapping[str, str]
    approval: Any
    deployment_evidence: Mapping[str, Any]
    package_path: Path | None = None
    checkpoint_identity: str | None = None
    findings: tuple[str, ...] = ()
    commit_adapter: Any | None = None

    @property
    def applied(self) -> bool:
        return self.verified

    @property
    def result_hashes(self) -> Mapping[str, str]:
        return self.after_hashes


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    change_id: str
    path: Path
    verified: bool
    before_hashes: Mapping[str, str]
    after_hashes: Mapping[str, str]
    preview_fingerprint: str
    approval_fingerprint: str
    deployment_evidence: Mapping[str, Any]
    affected_paths: tuple[str, ...] = ()
    affected_ids: tuple[str, ...] = ()
    operations: tuple[Mapping[str, str], ...] = ()
    change_raw_sha256: str = ""
    delta_raw_sha256: str = ""
    checkpoint_identity: str | None = None


def _canonical_root_identity(root: Path) -> str:
    """Stable, case/separator-normalized identity for one canonical root."""
    resolved = Path(root).resolve()
    return os.path.normcase(str(resolved)).replace("\\", "/").rstrip("/")


def _root_identity_hash(root_identity: str) -> str:
    return _hash(root_identity.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class _Requirement:
    requirement_id: str
    title: str
    start: int
    end: int
    body: str


def _finding(code: str, path: str, message: str) -> Finding:
    return Finding(code, path, message)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _jsonable(value: Any) -> Any:
    """Convert frozen provider mappings into deterministic JSON values."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _is_fence(line: str) -> bool:
    return bool(re.match(r"^\s*(```+|~~~+)", line))


def _parse_requirements(text: str) -> tuple[_Requirement, ...]:
    lines = _normalize_text(text).splitlines(keepends=True)
    found: list[tuple[str, str, int]] = []
    fenced = False
    for index, line in enumerate(lines):
        if _is_fence(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _REQ.match(line.rstrip("\r\n"))
        if match:
            found.append((match.group(1), match.group(2), index))
    requirements: list[_Requirement] = []
    for position, (identifier, title, start) in enumerate(found):
        end = found[position + 1][2] if position + 1 < len(found) else len(lines)
        block = "".join(lines[start:end])
        heading_end = lines[start].find("\n")
        body = lines[start + 1 : end]
        requirements.append(_Requirement(identifier, title, start, end, "".join(body)))
    return tuple(requirements)


def _parse_delta(text: str) -> tuple[DeltaOperation, ...]:
    lines = _normalize_text(text).splitlines(keepends=True)
    sections: list[tuple[str, int]] = []
    fenced = False
    for index, line in enumerate(lines):
        if _is_fence(line):
            fenced = not fenced
            continue
        if not fenced:
            match = _OP.match(line.rstrip("\r\n"))
            if match:
                sections.append((match.group(1).upper(), index))
    operations: list[DeltaOperation] = []
    for section_index, (operation, start) in enumerate(sections):
        end = sections[section_index + 1][1] if section_index + 1 < len(sections) else len(lines)
        entries: list[tuple[str, str, int]] = []
        fenced = False
        for index in range(start + 1, end):
            line = lines[index]
            if _is_fence(line):
                fenced = not fenced
                continue
            if not fenced:
                match = _DELTA_REQ.match(line.rstrip("\r\n"))
                if match:
                    entries.append((match.group(1), match.group(2), index))
        for entry_index, (identifier, title, req_start) in enumerate(entries):
            req_end = entries[entry_index + 1][2] if entry_index + 1 < len(entries) else end
            body = "".join(lines[req_start + 1 : req_end])
            operations.append(DeltaOperation(operation, identifier, title, body))
    return tuple(operations)


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = _normalize_text(text)
    if not normalized.startswith("---\n"):
        raise ValueError("frontmatter must start with an exact --- line")
    lines = normalized.splitlines(keepends=True)
    closing = next((index for index, line in enumerate(lines[1:], 1) if line.rstrip("\n") == "---"), None)
    if closing is None:
        raise ValueError("frontmatter must end with an exact --- line")
    raw = "".join(lines[1:closing])
    data: dict[str, Any] = {}
    list_key: str | None = None
    allowed = {"id", "title", "status", "affects", "references"}
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") and list_key:
            data.setdefault(list_key, []).append(line[1:].strip().strip("'\""))
            continue
        if ":" not in line:
            raise ValueError(f"unsupported frontmatter line: {raw_line}")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key not in allowed:
            raise ValueError(f"unsupported frontmatter field: {key}")
        if not value:
            if key not in {"affects", "references"}:
                raise ValueError(f"frontmatter field must be a scalar: {key}")
            data[key] = []
            list_key = key
        else:
            if key in {"affects", "references"}:
                raise ValueError(f"frontmatter field must be a YAML list: {key}")
            data[key] = value.strip("'\"")
            list_key = None
    return data, "".join(lines[closing + 1 :]).lstrip("\n")


def _safe_path(path: Path, root: Path) -> Path:
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    _reject_reparse_components(candidate, root)
    # Resolve existing ancestors so a symlink cannot redirect a write.
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"path escapes repository: {path}") from error
    if candidate.exists() and candidate.is_symlink():
        raise ValueError(f"symlink target is not allowed: {candidate}")
    return resolved


def _is_reparse_point(path: Path) -> bool:
    """Filesystem boundary hook; tests can deterministically simulate reparse points."""
    return path.is_symlink()


def _reject_reparse_components(path: Path, boundary: Path) -> None:
    boundary = Path(boundary)
    cursor = Path(path)
    while True:
        if _is_reparse_point(cursor):
            raise ValueError(f"reparse point component is not allowed: {cursor}")
        if cursor == boundary or cursor.parent == cursor:
            break
        cursor = cursor.parent


def _normalize_affect(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return Path(normalized).as_posix()


def _affect_identity(value: str, current_root: Path) -> str:
    path = _safe_path(current_root / _normalize_affect(value), current_root)
    return path.relative_to(current_root.resolve()).as_posix().casefold()


def _paths_overlap(left: str, right: str, current_root: Path) -> bool:
    left_parts = _affect_identity(left, current_root).split("/")
    right_parts = _affect_identity(right, current_root).split("/")
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def _repo_for_package(package: Path) -> tuple[Path, Path | None]:
    package = package.resolve()
    active = package.parent
    current_candidates = [active.parent / "current", active.parent.parent / "current"]
    current = next((candidate for candidate in current_candidates if candidate.exists()), None)
    root = (current or active.parent).parent if current else active.parent
    return root.resolve(), current


def _package_from_path(path: Path) -> tuple[ChangePackage, tuple[Finding, ...]]:
    findings: list[Finding] = []
    try:
        _reject_reparse_components(path, path.parent.parent.parent)
    except ValueError as error:
        return ChangePackage(path, "", "", "", (), (), ()), (_finding("reparse_point", str(path), str(error)),)
    if not path.is_dir():
        return ChangePackage(path, "", "", "", (), (), ()), (_finding("missing_package", str(path), "change package must be a directory"),)
    change_file, delta_file = path / "change.md", path / "delta.md"
    if not change_file.is_file():
        findings.append(_finding("missing_file", "change.md", "change.md is required"))
    if not delta_file.is_file():
        findings.append(_finding("missing_file", "delta.md", "delta.md is required"))
    data: dict[str, Any] = {}
    body = ""
    if change_file.is_file():
        try:
            data, body = _frontmatter(change_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            findings.append(_finding("invalid_frontmatter", "change.md", str(error)))
    identifier = str(data.get("id", ""))
    title = str(data.get("title", ""))
    status = str(data.get("status", ""))
    affects = tuple(str(value) for value in data.get("affects", ()) if str(value).strip())
    references = tuple(str(value) for value in data.get("references", ()) if str(value).strip())
    if not _CHANGE_VALID.fullmatch(identifier):
        findings.append(_finding("invalid_change_id", "frontmatter.id", "change id must match ^CHG-[0-9]{3,}$"))
    if path.name != identifier:
        findings.append(_finding("change_id_mismatch", "path", "package directory must match frontmatter id"))
    if not title.strip():
        findings.append(_finding("missing_field", "frontmatter.title", "title is required"))
    if status not in {"draft", "applied"}:
        findings.append(_finding("invalid_status", "frontmatter.status", "status must be draft or applied"))
    if not affects:
        findings.append(_finding("missing_field", "frontmatter.affects", "at least one affected file is required"))
    operations: tuple[DeltaOperation, ...] = ()
    if delta_file.is_file():
        try:
            operations = _parse_delta(delta_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as error:
            findings.append(_finding("invalid_delta", "delta.md", str(error)))
    if not operations:
        findings.append(_finding("empty_delta", "delta.md", "delta must contain at least one operation"))
    seen: set[str] = set()
    for operation in operations:
        if operation.requirement_id in seen:
            findings.append(_finding("duplicate_delta_id", "delta.md", f"requirement appears more than once: {operation.requirement_id}"))
        seen.add(operation.requirement_id)
        if not _REQ_VALID.fullmatch(operation.requirement_id):
            findings.append(_finding("invalid_requirement_id", "delta.md", f"invalid requirement id: {operation.requirement_id}"))
        if operation.operation in {"ADDED", "MODIFIED"} and not operation.body.strip():
            findings.append(_finding("empty_requirement", "delta.md", f"{operation.requirement_id} has no requirement text"))
    current_root = None
    try:
        repo, current = _repo_for_package(path)
        current_root = current
        _safe_path(path, repo)
        _reject_reparse_components(change_file, repo)
        _reject_reparse_components(delta_file, repo)
        for affected in affects:
            _safe_path((current or repo) / affected, repo)
        for reference in references:
            if not _URL.match(reference):
                candidates = ((path / reference), (repo / reference))
                if not any(candidate.exists() for candidate in candidates):
                    findings.append(_finding("missing_reference", reference, "local reference does not exist"))
    except (OSError, ValueError) as error:
        code = "reparse_point" if "reparse" in str(error).casefold() or "symlink" in str(error).casefold() else "unsafe_path"
        findings.append(_finding(code, str(path), str(error)))
    package = ChangePackage(path.resolve(), identifier, title, status, affects, references, operations, current_root, body)
    return package, tuple(findings)


def _schema_findings(package: ChangePackage) -> tuple[Finding, ...]:
    schema_path = Path(__file__).resolve().parents[2] / "assets" / "schemas" / "spec-change.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        document = {
            "change": {
                "id": package.change_id,
                "title": package.title,
                "status": package.status,
                "affects": list(package.affects),
                "references": list(package.references),
            },
            "delta": [
                {"operation": operation.operation, "id": operation.requirement_id, "title": operation.title, "body": operation.body}
                for operation in package.operations
            ],
        }
        errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: (list(error.path), error.message))
        return tuple(_finding("schema_validation", "package", error.message) for error in errors)
    except (OSError, ValueError, TypeError) as error:
        return (_finding("schema_unavailable", str(schema_path), str(error)),)


def _current_files(current: Path) -> tuple[tuple[str, Path, bytes], ...]:
    _reject_reparse_components(current, current.parent)
    current = current.resolve()
    if current.is_file():
        return ((current.name, current, current.read_bytes()),)
    if not current.is_dir():
        raise ValueError(f"current specification path does not exist: {current}")
    rows: list[tuple[str, Path, bytes]] = []
    for path in sorted(current.rglob("*.md"), key=lambda value: value.relative_to(current).as_posix().casefold()):
        if path.is_symlink():
            raise ValueError(f"symlink specification is not allowed: {path}")
        relative = path.relative_to(current).as_posix()
        rows.append((relative, path, path.read_bytes()))
    if not rows:
        raise ValueError("current specification directory contains no Markdown files")
    return tuple(rows)


def _baseline_manifest_path(current: Path) -> Path:
    """Return the sole canonical manifest location for a current tree."""
    current = Path(current)
    return current.parent / "baseline-manifest.json"


def _manifest_document(current: Path, hashes: Mapping[str, str], *, previous: str | None = None,
                       change_id: str | None = None, fingerprint: str | None = None,
                       result_hashes: Mapping[str, str] | None = None) -> dict[str, Any]:
    identity = _canonical_root_identity(current)
    return {
        "schema_version": 1,
        "root_identity": identity,
        "root_identity_sha256": _root_identity_hash(identity),
        "specifications": dict(sorted(hashes.items(), key=lambda item: item[0].casefold())),
        "previous_manifest_sha256": previous,
        "change_id": change_id,
        "preview_fingerprint": fingerprint,
        "result_hashes": dict(sorted((result_hashes or hashes).items(), key=lambda item: item[0].casefold())),
    }


def _manifest_bytes(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def initialize_spec_baseline(current: str | Path, *, replace: bool = False,
                             previous_manifest_sha256: str | None = None,
                             change_id: str | None = None,
                             preview_fingerprint: str | None = None) -> Mapping[str, Any]:
    """Promote the current tree into an explicit, auditable baseline.

    This is intentionally a write API.  Read-only preview never creates a
    missing manifest and fails closed instead.
    """
    current_path = Path(current).resolve()
    rows = _current_files(current_path)
    manifest = _baseline_manifest_path(current_path)
    hashes = {relative: _hash(raw) for relative, _, raw in rows}
    document = _manifest_document(
        current_path, hashes, previous=previous_manifest_sha256,
        change_id=change_id, fingerprint=preview_fingerprint, result_hashes=hashes,
    )
    content = _manifest_bytes(document)
    _reject_reparse_components(manifest, current_path.parent)
    if manifest.is_symlink() or _is_reparse_point(manifest):
        raise ValueError(f"baseline manifest reparse point is not allowed: {manifest}")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if not replace:
        handle, temporary = tempfile.mkstemp(prefix=f".{manifest.name}.", dir=str(manifest.parent))
        try:
            with os.fdopen(handle, "wb") as stream:
                handle = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # A hard-link install is atomic and O_EXCL-like: it cannot replace
            # a pre-existing manifest, while the fully written temp inode is
            # what becomes visible to readers.
            os.link(temporary, manifest)
        finally:
            if handle != -1:
                os.close(handle)
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        _replace_atomic(manifest, content)
    return MappingProxyType({**document, "manifest_sha256": _hash(content)})


def _read_baseline_manifest(current: Path) -> tuple[Path, bytes, Mapping[str, Any]]:
    manifest = _baseline_manifest_path(current)
    _reject_reparse_components(manifest, current.parent)
    if manifest.is_symlink() or _is_reparse_point(manifest) or not manifest.is_file():
        raise ValueError(f"baseline manifest is missing or unsafe: {manifest}")
    raw = manifest.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"baseline manifest cannot be read: {manifest}") from error
    if not isinstance(document, Mapping) or document.get("schema_version") != 1:
        raise ValueError("baseline manifest has unsupported schema")
    identity = _canonical_root_identity(current)
    if document.get("root_identity") != identity or document.get("root_identity_sha256") != _root_identity_hash(identity):
        raise ValueError("baseline manifest root identity does not match current specifications")
    expected = document.get("specifications")
    if not isinstance(expected, Mapping):
        raise ValueError("baseline manifest specifications must be a path-to-SHA-256 mapping")
    actual = {relative: _hash(value) for relative, _, value in _current_files(current)}
    if dict(expected) != actual:
        raise ValueError("current specification changed after the promoted baseline manifest")
    return manifest, raw, document


def validate_spec_baseline(current: str | Path) -> Mapping[str, Any]:
    """Validate and return the promoted baseline without changing disk state."""
    _, raw, document = _read_baseline_manifest(Path(current).resolve())
    return MappingProxyType({**document, "manifest_sha256": _hash(raw)})


def _delta_input(delta: Any) -> tuple[tuple[DeltaOperation, ...], bytes, Path | None, Path | None, str | None]:
    if isinstance(delta, str) and ("\n" in delta or delta.lstrip().startswith("#")):
        raw = delta.encode("utf-8")
        return _parse_delta(delta), raw, None, None, None
    if isinstance(delta, Path) or isinstance(delta, str):
        delta_path = Path(delta).resolve()
        raw = delta_path.read_bytes()
        operations = _parse_delta(raw.decode("utf-8"))
        package_path = delta_path.parent if delta_path.parent.name != "active" else None
        if delta_path.parent.name and (delta_path.parent / "change.md").exists():
            package_path = delta_path.parent
        change_id = package_path.name if package_path and _CHANGE_VALID.fullmatch(package_path.name) else None
        return operations, raw, delta_path, package_path, change_id
    if isinstance(delta, ChangePackage):
        delta_path = delta.path / "delta.md"
        return delta.operations, delta_path.read_bytes() if delta_path.exists() else b"", delta_path, delta.path, delta.change_id
    if isinstance(delta, Delta):
        raw = json.dumps([{"operation": op.operation, "id": op.requirement_id, "title": op.title, "body": op.body} for op in delta.operations], sort_keys=True).encode("utf-8")
        return delta.operations, raw, None, None, None
    if isinstance(delta, bytes):
        return _parse_delta(delta.decode("utf-8")), delta, None, None, None
    if isinstance(delta, Mapping):
        if "operations" in delta:
            delta = delta["operations"]
        else:
            expanded: list[Any] = []
            for operation, values in delta.items():
                for value in (values if isinstance(values, Sequence) and not isinstance(values, (str, bytes, Mapping)) else (values,)):
                    if isinstance(value, Mapping):
                        expanded.append({**value, "operation": str(operation).upper()})
            delta = expanded
    if isinstance(delta, Sequence):
        operations: list[DeltaOperation] = []
        for item in delta:
            if isinstance(item, DeltaOperation):
                operations.append(item)
            elif isinstance(item, Mapping):
                operations.append(DeltaOperation(str(item.get("operation", item.get("op", ""))).upper(), str(item.get("id", item.get("requirement_id", ""))), str(item.get("title", "")), str(item.get("body", ""))))
        raw = json.dumps([{"operation": operation.operation, "id": operation.requirement_id, "title": operation.title, "body": operation.body} for operation in operations], sort_keys=True, default=str).encode("utf-8")
        return tuple(operations), raw, None, None, None
    raise TypeError("delta must be a path, Markdown bytes/text, ChangePackage, or operations")


def _render_changes(rows: tuple[tuple[str, Path, bytes], ...], operations: tuple[DeltaOperation, ...], package: ChangePackage | None) -> tuple[dict[str, bytes], tuple[str, ...], str]:
    contents = {relative: raw for relative, _, raw in rows}
    parsed_by_file = {relative: _parse_requirements(raw.decode("utf-8")) for relative, _, raw in rows}
    locations: dict[str, str] = {}
    for relative, requirements in parsed_by_file.items():
        for requirement in requirements:
            if requirement.requirement_id in locations:
                raise ValueError(f"duplicate current requirement id: {requirement.requirement_id}")
            locations[requirement.requirement_id] = relative
    affects = package.affects if package else ()
    for operation in operations:
        if operation.operation not in {"ADDED", "MODIFIED", "REMOVED"}:
            raise ValueError(f"unsupported operation: {operation.operation}")
        if operation.operation == "ADDED" and operation.requirement_id in locations:
            raise ValueError(f"added requirement already exists: {operation.requirement_id}")
        if operation.operation in {"MODIFIED", "REMOVED"} and operation.requirement_id not in locations:
            raise ValueError(f"missing target: {operation.requirement_id}")
    target_for_added = affects[0] if affects else (rows[0][0] if len(rows) == 1 else None)
    if any(operation.operation == "ADDED" for operation in operations) and target_for_added not in contents:
        raise ValueError(f"affected file not found: {target_for_added}")
    grouped: dict[str, list[DeltaOperation]] = {}
    for operation in operations:
        grouped.setdefault(locations.get(operation.requirement_id, target_for_added or ""), []).append(operation)
    for relative, file_operations in grouped.items():
        if relative not in contents:
            raise ValueError(f"affected file not found: {relative}")
        text = contents[relative].decode("utf-8")
        requirements = list(_parse_requirements(text))
        # Replace from the end to retain offsets.
        for operation in sorted(file_operations, key=lambda item: next((r.start for r in requirements if r.requirement_id == item.requirement_id), len(text)), reverse=True):
            requirements = list(_parse_requirements(text))
            match = next((r for r in requirements if r.requirement_id == operation.requirement_id), None)
            lines = _normalize_text(text).splitlines(keepends=True)
            if match is not None:
                replacement = "" if operation.operation == "REMOVED" else f"## {operation.requirement_id}: {operation.title}\n\n{_normalize_text(operation.body).strip()}\n"
                text = "".join(lines[: match.start]) + replacement + "".join(lines[match.end :])
            else:
                if operation.operation != "ADDED":
                    raise ValueError(f"missing target: {operation.requirement_id}")
                separator = "" if not text or text.endswith("\n\n") else "\n"
                text = text + separator + f"## {operation.requirement_id}: {operation.title}\n\n{_normalize_text(operation.body).strip()}\n"
        contents[relative] = text.encode("utf-8")
    diff_parts: list[str] = []
    for relative, _, raw in rows:
        before = raw.decode("utf-8").splitlines(keepends=True)
        after = contents[relative].decode("utf-8").splitlines(keepends=True)
        diff_parts.extend(difflib.unified_diff(before, after, fromfile=relative, tofile=relative))
    changed = tuple(sorted(grouped))
    return {relative: contents[relative] for relative in changed}, tuple(sorted({operation.requirement_id for operation in operations})), "".join(diff_parts)


def _fingerprint(metadata: bytes, source_bytes: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(metadata)
    digest.update(b"\0")
    for path in sorted(source_bytes, key=str.casefold):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_bytes[path])
        digest.update(b"\0")
    return digest.hexdigest()


def preview_delta(current: Any, delta: Any) -> Preview:
    """Compute an exact candidate without modifying ``current``."""
    package: ChangePackage | None = delta if isinstance(delta, ChangePackage) else None
    if package is not None:
        operations, delta_bytes, delta_path, package_path, change_id = _delta_input(package)
    else:
        operations, delta_bytes, delta_path, package_path, change_id = _delta_input(delta)
    current_path = Path(current).resolve() if isinstance(current, Path) or (isinstance(current, str) and "\n" not in current and not current.lstrip().startswith("#")) else None
    if current_path is None:
        if isinstance(current, Mapping):
            rows = tuple((str(name), Path(str(name)), value.encode("utf-8") if isinstance(value, str) else bytes(value)) for name, value in current.items())
            root = Path.cwd().resolve()
        elif isinstance(current, bytes):
            rows = (("current.md", Path("current.md"), current),)
            root = Path.cwd().resolve()
        elif isinstance(current, str) and ("\n" in current or current.lstrip().startswith("#")):
            raw = current.encode("utf-8")
            rows = (("current.md", Path("current.md"), raw),)
            root = Path.cwd().resolve()
        else:
            raise TypeError("current must be a path or mapping of relative names to bytes")
    else:
        rows = _current_files(current_path)
        root = current_path.parent if current_path.is_file() else current_path
    if package is None and package_path:
        package, package_findings = _package_from_path(package_path)
        package_findings = tuple(package_findings) + _schema_findings(package)
        if package_findings:
            first = package_findings[0]
            raise ValueError(f"validated package required: {first.code}: {first.message}")
    elif package is not None:
        package_model, package_findings = _package_from_path(package.path)
        package_findings = tuple(package_findings) + _schema_findings(package_model)
        if package_findings:
            first = package_findings[0]
            raise ValueError(f"validated package required: {first.code}: {first.message}")
        package = package_model
        package_path = package.path
        operations = package.operations
        delta_bytes = (package.path / "delta.md").read_bytes()
        delta_path = package.path / "delta.md"
        change_id = package.change_id
    if package is None or package_path is None:
        raise ValueError("delta must be bound to a validated change package")
    if package.current_root is None:
        raise ValueError("change package has no canonical current specification root")
    package_current = package.current_root.resolve()
    passed_current = (current_path.parent if current_path and current_path.is_file() else current_path).resolve() if current_path else package_current
    if passed_current != package_current:
        raise ValueError("current specification is outside the change package boundary")
    if package is not None and package.current_root and current_path is None:
        current_path = package.current_root
    baseline_manifest, baseline_manifest_bytes, _ = _read_baseline_manifest(package_current)
    seen_delta: set[str] = set()
    for operation in operations:
        if operation.requirement_id in seen_delta:
            raise ValueError(f"duplicate delta id: {operation.requirement_id}")
        seen_delta.add(operation.requirement_id)
    result, affected_ids, diff = _render_changes(rows, operations, package)
    candidate = {relative: result.get(relative, raw) for relative, _, raw in rows}
    candidate_ids: set[str] = set()
    for raw in candidate.values():
        for requirement in _parse_requirements(raw.decode("utf-8")):
            if not _REQ_VALID.fullmatch(requirement.requirement_id):
                raise ValueError(f"invalid requirement id in candidate: {requirement.requirement_id}")
            if requirement.requirement_id in candidate_ids:
                raise ValueError(f"duplicate requirement id in candidate: {requirement.requirement_id}")
            candidate_ids.add(requirement.requirement_id)
    source_bytes = {relative: raw for relative, _, raw in rows if relative in result}
    change_bytes = (package.path / "change.md").read_bytes()
    root_identity = _canonical_root_identity(root)
    metadata = json.dumps(
        {
            "root_identity": root_identity,
            "root_identity_sha256": _root_identity_hash(root_identity),
            "change_id": change_id,
            "title": package.title,
            "status": package.status,
            "affects": [_normalize_affect(item) for item in package.affects],
            "references": sorted(package.references),
            "context": _normalize_text(package.reason).strip(),
            "operations": [{"operation": op.operation, "id": op.requirement_id, "title": op.title, "body": _normalize_text(op.body).strip()} for op in sorted(operations, key=lambda item: (item.operation, item.requirement_id, item.title, item.body))],
            "affected_ids": affected_ids,
            "schema_version": 1,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    binding_payload = (
        metadata + b"\0BASELINE_ROOT\0" + root_identity.encode("utf-8")
        + b"\0BASELINE_HASH\0" + _hash(baseline_manifest_bytes).encode("ascii")
        + b"\0BASELINE\0" + baseline_manifest_bytes
        + b"\0CHANGE\0" + change_bytes + b"\0DELTA\0" + delta_bytes
    )
    for path in sorted(source_bytes, key=str.casefold):
        binding_payload += b"\0SOURCE\0" + path.encode("utf-8") + b"\0" + source_bytes[path]
    fingerprint = _hash(binding_payload)
    return Preview(
        root=root,
        root_identity=root_identity,
        current_path=current_path,
        delta_path=delta_path,
        change_id=change_id,
        source_hashes=MappingProxyType({path: _hash(value) for path, value in source_bytes.items()}),
        result_hashes=MappingProxyType({path: _hash(value) for path, value in result.items()}),
        source_bytes=MappingProxyType(dict(source_bytes)),
        result_bytes=MappingProxyType(dict(result)),
        delta_bytes=bytes(delta_bytes),
        change_bytes=bytes(change_bytes),
        binding_payload=binding_payload,
        operations=operations,
        affected_ids=affected_ids,
        diff=diff,
        fingerprint=fingerprint,
        package_path=package_path,
        baseline_manifest_path=baseline_manifest,
        baseline_manifest_bytes=baseline_manifest_bytes,
        baseline_manifest_hash=_hash(baseline_manifest_bytes),
    )


def validate_change_package(path: str | Path) -> ValidationResult:
    """Validate package syntax, targets, paths, and active-change overlap."""
    package, findings = _package_from_path(Path(path))
    all_findings = list(findings)
    all_findings.extend(_schema_findings(package))
    if package.current_root and package.current_root.exists():
        try:
            current_files = _current_files(package.current_root)
            _read_baseline_manifest(package.current_root)
            current_ids = {req.requirement_id for _, _, raw in current_files for req in _parse_requirements(raw.decode("utf-8"))}
            seen_current: set[str] = set()
            for _, _, raw in current_files:
                for requirement in _parse_requirements(raw.decode("utf-8")):
                    if requirement.requirement_id in seen_current:
                        all_findings.append(_finding("duplicate_current_id", "current", f"requirement appears more than once: {requirement.requirement_id}"))
                    seen_current.add(requirement.requirement_id)
            for operation in package.operations:
                if operation.operation == "ADDED" and operation.requirement_id in current_ids:
                    all_findings.append(_finding("existing_target", "delta.md", f"ADDED target already exists: {operation.requirement_id}"))
                if operation.operation in {"MODIFIED", "REMOVED"} and operation.requirement_id not in current_ids:
                    all_findings.append(_finding("missing_target", "delta.md", f"target does not exist: {operation.requirement_id}"))
            for sibling in sorted(package.path.parent.iterdir(), key=lambda item: item.name.casefold()):
                if sibling == package.path or not sibling.is_dir() or not _CHANGE_VALID.fullmatch(sibling.name):
                    continue
                other, other_findings = _package_from_path(sibling)
                if other_findings:
                    continue
                overlap = {op.requirement_id for op in package.operations} & {op.requirement_id for op in other.operations}
                current_boundary = package.current_root or other.current_root
                path_overlap: set[str] = set()
                if current_boundary is not None:
                    for left in package.affects:
                        for right in other.affects:
                            try:
                                if _paths_overlap(left, right, current_boundary):
                                    path_overlap.add(f"{left}:{right}")
                            except (OSError, ValueError):
                                continue
                if overlap or path_overlap:
                    details = sorted(overlap or path_overlap)
                    all_findings.append(_finding("active_overlap", "delta.md", f"active change overlaps {sibling.name}: {', '.join(details)}"))
        except (OSError, UnicodeError, ValueError) as error:
            all_findings.append(_finding("invalid_baseline", "baseline-manifest.json", str(error)))
    return ValidationResult(tuple(sorted(set(all_findings), key=lambda finding: (finding.path, finding.code, finding.message))))


def _deployment_is_production(evidence: Any) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    required = ("deployment_id", "change_id", "preview_fingerprint", "environment", "status", "source_hashes", "result_hashes", "assurance")
    if any(key not in evidence for key in required):
        return False
    environment = evidence.get("environment")
    status = str(evidence.get("status", "")).casefold()
    if not isinstance(evidence.get("deployment_id"), str) or not evidence["deployment_id"].strip():
        return False
    if environment != "production" or status not in {"deployed", "verified"}:
        return False
    if evidence.get("assurance") != "external-asserted":
        return False
    if not isinstance(evidence.get("source_hashes"), Mapping) or not isinstance(evidence.get("result_hashes"), Mapping):
        return False
    return True


def _deployment_matches_preview(evidence: Mapping[str, Any], preview: Preview) -> bool:
    return (
        evidence.get("change_id") == preview.change_id
        and evidence.get("preview_fingerprint") == preview.fingerprint
        and dict(evidence.get("source_hashes", {})) == dict(preview.source_hashes)
        and dict(evidence.get("result_hashes", {})) == dict(preview.result_hashes)
    )


def _approval_matches(approval: Any, preview: Preview) -> bool:
    if isinstance(approval, Approval):
        if not approval.actor.strip() or approval.gate != "specification-change" or approval.role_assurance != "asserted":
            return False
        return verify_approval(approval, preview.binding_payload, preview.required_role, "specification-change").is_valid
    if isinstance(approval, Mapping):
        required = ("gate", "actor", "role", "role_assurance", "decision", "fingerprint")
        if any(not isinstance(approval.get(name), str) or not approval.get(name, "").strip() for name in required):
            return False
        try:
            record = Approval(
                gate=approval["gate"], actor=approval["actor"], role=approval["role"],
                role_assurance=approval["role_assurance"], decision=approval["decision"], fingerprint=approval["fingerprint"],
            )
        except (TypeError, ValueError):
            return False
        return verify_approval(record, preview.binding_payload, preview.required_role, "specification-change").is_valid
    return False


def _replace_atomic(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _restore(target: Path, old: bytes | None) -> None:
    if old is None:
        if target.exists():
            target.unlink()
    else:
        _replace_atomic(target, old)


def _project_root(path: Path) -> Path:
    """Resolve the actual Git worktree root; never invent a synthetic boundary."""
    current = Path(path).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(current), "rev-parse", "--is-inside-work-tree"],
            check=True, capture_output=True,
        )
        for candidate in (current, *current.parents):
            marker = candidate / ".git"
            if marker.is_dir() or marker.is_file():
                return candidate
    except (OSError, subprocess.CalledProcessError, ValueError):
        pass
    raise ValueError("a real Git project root and Git commit adapter are required")


def apply_approved_delta(
    preview: Preview,
    approval: Any,
    deployment_evidence: Mapping[str, Any],
    *,
    adapter: Any = None,
    commit_adapter: Any = None,
) -> ApplyResult:
    """Apply exactly one approved, deployed preview with rollback on failure."""
    if not isinstance(preview, Preview):
        raise TypeError("preview must be a Preview")
    if adapter is not None and commit_adapter is not None:
        raise ValueError("provide only one Git commit adapter")
    adapter = adapter or commit_adapter
    if adapter is None:
        raise ValueError("a Git commit adapter is required")
    if _canonical_root_identity(preview.root) != preview.root_identity:
        raise ValueError("canonical root identity changed after preview")
    if not _approval_matches(approval, preview):
        raise ValueError("approval does not match preview fingerprint, gate, or role")
    if not _deployment_is_production(deployment_evidence):
        raise ValueError("production deployment evidence is required before applying a specification delta")
    if not _deployment_matches_preview(deployment_evidence, preview):
        raise ValueError("deployment evidence is not bound to this change preview")
    if preview.delta_path is not None and preview.delta_path.read_bytes() != preview.delta_bytes:
        raise ValueError("delta changed after preview")
    if preview.package_path is None or (preview.package_path / "change.md").read_bytes() != preview.change_bytes:
        raise ValueError("change package changed after preview")
    if preview.package_path is not None:
        overlap_findings = [
            finding for finding in validate_change_package(preview.package_path).findings
            if finding.code == "active_overlap"
        ]
        if overlap_findings:
            raise ValueError(f"active change overlap prevents apply: {overlap_findings[0].message}")
    if preview.current_path is None:
        raise ValueError("preview has no canonical current path")
    current_rows = _current_files(preview.current_path)
    manifest_path, manifest_bytes, _ = _read_baseline_manifest(preview.current_path)
    if (
        preview.baseline_manifest_path != manifest_path
        or manifest_bytes != preview.baseline_manifest_bytes
        or _hash(manifest_bytes) != preview.baseline_manifest_hash
    ):
        raise ValueError("baseline manifest changed after preview")
    current_source = {relative: raw for relative, _, raw in current_rows if relative in preview.source_hashes}
    if {path: _hash(value) for path, value in current_source.items()} != dict(preview.source_hashes):
        raise ValueError("current specification changed after preview")
    fresh = preview_delta(preview.current_path, preview.delta_path or preview.delta_bytes)
    if fresh.fingerprint != preview.fingerprint or fresh.root_identity != preview.root_identity:
        raise ValueError("preview fingerprint is stale")
    next_hashes = {
        relative: _hash(preview.result_bytes.get(relative, raw))
        for relative, _, raw in current_rows
    }
    next_document = _manifest_document(
        preview.current_path,
        next_hashes,
        previous=preview.baseline_manifest_hash,
        change_id=preview.change_id,
        fingerprint=preview.fingerprint,
        result_hashes=next_hashes,
    )
    checkpoint_root = _project_root(preview.root)
    current_prefix = preview.current_path.resolve().relative_to(checkpoint_root).as_posix()
    changes = {f"{current_prefix}/{relative}": value for relative, value in preview.result_bytes.items()}
    manifest_relative = manifest_path.resolve().relative_to(checkpoint_root).as_posix()
    changes[manifest_relative] = _manifest_bytes(next_document)
    for relative in changes:
        _safe_path(checkpoint_root / relative, checkpoint_root)
    prepared = prepare_checkpoint(checkpoint_root, changes, adapter=adapter)
    report = validate_checkpoint(prepared)
    if not report.is_valid:
        first = report.findings[0]
        raise ValueError(f"{first.code}: {first.message}")
    commit_identity = commit_checkpoint(prepared, f"specification change {preview.change_id or preview.fingerprint[:12]}", adapter=adapter)
    try:
        after = {relative: _hash((preview.current_path / relative).read_bytes()) for relative in preview.result_bytes}
    except (OSError, ValueError) as error:
        return ApplyResult(
            preview, preview.change_id, False, "committed_conflict",
            MappingProxyType({path: _hash(value) for path, value in preview.source_bytes.items()}),
            MappingProxyType({}), approval, MappingProxyType(dict(deployment_evidence)), preview.package_path,
            commit_identity.identity, (f"committed_conflict: unable to verify resulting bytes: {error}",), adapter,
        )
    if after != dict(preview.result_hashes):
        return ApplyResult(
            preview, preview.change_id, False, "committed_conflict",
            MappingProxyType({path: _hash(value) for path, value in preview.source_bytes.items()}),
            MappingProxyType(after), approval, MappingProxyType(dict(deployment_evidence)), preview.package_path,
            commit_identity.identity, ("committed_conflict: resulting bytes differ from the approved preview",), adapter,
        )
    return ApplyResult(
        preview, preview.change_id, True, "applied",
        MappingProxyType({path: _hash(value) for path, value in preview.source_bytes.items()}),
        MappingProxyType(after), approval, MappingProxyType(dict(deployment_evidence)), preview.package_path,
        commit_identity.identity,
        (),
        adapter,
    )


def archive_change(result: ApplyResult, *, adapter: Any = None, commit_adapter: Any = None) -> ArchiveRecord:
    """Write a complete immutable archive record, only after verification."""
    if not isinstance(result, ApplyResult) or not result.verified or result.status != "applied":
        raise ValueError("cannot archive an unverified applied change")
    if adapter is not None and commit_adapter is not None:
        raise ValueError("provide only one Git commit adapter")
    adapter = adapter or commit_adapter or result.commit_adapter
    if adapter is None:
        raise ValueError("a Git commit adapter is required to archive a specification change")
    if result.package_path is None or not result.package_path.is_dir():
        raise ValueError("applied result is not bound to a change package")
    package = result.package_path
    try:
        raw_package = Path(package)
        if raw_package.is_symlink():
            raise ValueError("change package symlink is not allowed")
        repo_root, current_root = _repo_for_package(package)
        _safe_path(package, repo_root)
        if current_root is None or current_root.resolve() != result.preview.root.resolve():
            raise ValueError("archive package/current boundary mismatch")
        package_model, package_findings = _package_from_path(package)
        package_findings = tuple(package_findings) + _schema_findings(package_model)
        if package_findings:
            first = package_findings[0]
            raise ValueError(f"archive package is no longer valid: {first.code}: {first.message}")
    except (OSError, ValueError) as error:
        raise ValueError(f"archive package boundary is unsafe: {error}") from error
    change_raw = (package / "change.md").read_bytes()
    delta_raw = (package / "delta.md").read_bytes()
    if change_raw != result.preview.change_bytes or delta_raw != result.preview.delta_bytes:
        raise ValueError("change package changed after apply; archive requires the exact preview inputs")
    archive_root = package.parent.parent / "archive"
    archive_path = archive_root / (result.change_id or package.name)
    _safe_path(archive_root, repo_root)
    _safe_path(archive_path, repo_root)
    approval_fingerprint = str(getattr(result.approval, "fingerprint", result.approval.get("fingerprint", "") if isinstance(result.approval, Mapping) else ""))
    operations = tuple({"operation": op.operation, "id": op.requirement_id, "title": op.title, "body": _normalize_text(op.body).strip()} for op in result.preview.operations)
    manifest_path, manifest_raw, manifest_document = _read_baseline_manifest(result.preview.current_path)
    payload = {
        "version": 1,
        "change_id": result.change_id,
        "status": "verified-applied",
        "root_identity": result.preview.root_identity,
        "root_identity_sha256": _root_identity_hash(result.preview.root_identity),
        "preview_fingerprint": result.preview.fingerprint,
        "approval_fingerprint": approval_fingerprint,
        "approval_actor": getattr(result.approval, "actor", result.approval.get("actor", "") if isinstance(result.approval, Mapping) else ""),
        "approval_role": getattr(result.approval, "role", result.approval.get("role", "") if isinstance(result.approval, Mapping) else ""),
        "before_hashes": dict(sorted(result.before_hashes.items())),
        "after_hashes": dict(sorted(result.after_hashes.items())),
        "result_hashes": dict(sorted(result.after_hashes.items())),
        "source_hashes": dict(sorted(result.preview.source_hashes.items())),
        "affected_paths": sorted(result.preview.result_bytes),
        "affected_ids": list(result.preview.affected_ids),
        "operations": list(operations),
        "trace_ids": list(result.preview.trace_ids),
        "change_raw_sha256": _hash(change_raw),
        "delta_raw_sha256": _hash(delta_raw),
        "baseline_manifest_path": manifest_path.name,
        "baseline_manifest_raw_sha256": _hash(manifest_raw),
        "baseline_manifest": _jsonable(manifest_document),
        "binding_fingerprint": result.preview.fingerprint,
        "checkpoint_identity": result.checkpoint_identity,
        "deployment_evidence": _jsonable(result.deployment_evidence),
        "diff": result.preview.diff,
    }
    archive_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    archive_commit_identity: str | None = None
    if archive_path.exists():
        expected = {"change.md": (package / "change.md").read_bytes(), "delta.md": (package / "delta.md").read_bytes(), "archive.json": archive_json}
        for name, content in expected.items():
            target = archive_path / name
            if not target.is_file() or target.read_bytes() != content:
                raise ValueError("archive is immutable and appears tampered")
    else:
        project_root = _project_root(repo_root)
        relative_archive = archive_path.resolve().relative_to(project_root).as_posix()
        changes = {
            f"{relative_archive}/change.md": change_raw,
            f"{relative_archive}/delta.md": delta_raw,
            f"{relative_archive}/archive.json": archive_json,
        }
        prepared = prepare_checkpoint(project_root, changes, adapter=adapter)
        report = validate_checkpoint(prepared)
        if not report.is_valid:
            first = report.findings[0]
            raise ValueError(f"{first.code}: {first.message}")
        commit = commit_checkpoint(prepared, f"specification archive {result.change_id or package.name}", adapter=adapter)
        archive_commit_identity = commit.identity
        if any((project_root / relative).read_bytes() != content for relative, content in changes.items()):
            raise ValueError("archive commit verification failed")
    return ArchiveRecord(
        result.change_id or package.name, archive_path, True, result.before_hashes, result.after_hashes,
        result.preview.fingerprint, payload["approval_fingerprint"], result.deployment_evidence,
        tuple(payload["affected_paths"]), tuple(payload["affected_ids"]), operations,
        payload["change_raw_sha256"], payload["delta_raw_sha256"], archive_commit_identity or result.checkpoint_identity,
    )


__all__ = [
    "ArchiveRecord", "ApplyResult", "ChangePackage", "Delta", "DeltaOperation", "Finding", "Preview", "ValidationResult",
    "archive_change", "apply_approved_delta", "initialize_spec_baseline", "preview_delta",
    "validate_change_package", "validate_spec_baseline",
]

# Names used by the wider orchestrator validation vocabulary.
ChangeValidationReport = ValidationResult
