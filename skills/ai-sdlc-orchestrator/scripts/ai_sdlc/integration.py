"""Provider result normalization, staging, validation, and safe integration."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .contracts import CapabilityContract
from .runtime import ApprovalResult, ContractResult, ProviderResult
from .validation import ValidationFinding, ValidationReport


@dataclass(frozen=True, slots=True)
class StagedResult:
    """An in-memory staged result; creation never writes the canonical tree."""

    root: Path
    result: ContractResult
    writable_scope: tuple[str, ...] = ()
    contract_id: str | None = None
    contract_version: int | None = None

    @property
    def normalized_result(self) -> ContractResult:
        return self.result

    @property
    def provider_result(self) -> ContractResult:
        return self.result


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    """Deterministic changed-path manifest returned after a successful apply."""

    changed_paths: tuple[str, ...]
    manifest: Mapping[str, str]

    @property
    def changed_path_manifest(self) -> Mapping[str, str]:
        return self.manifest


def normalize_result(provider_result: ProviderResult | ContractResult | Mapping[str, Any], contract: CapabilityContract | None = None) -> ContractResult:
    """Map provider-specific output to the stable contract result shape."""
    if isinstance(provider_result, ContractResult):
        if contract is not None and provider_result.capability != contract.id:
            raise ValueError(f"provider capability {provider_result.capability!r} does not match contract {contract.id!r}")
        return provider_result
    raw = ProviderResult.from_value(provider_result)
    sections = dict(raw.sections)
    evidence = dict(raw.evidence)
    files = dict(raw.files)
    if isinstance(raw.payload, Mapping):
        payload = raw.payload
        if not sections:
            sections = dict(payload.get("sections", payload.get("normalized_result", {})) or {})
        if not evidence:
            evidence = dict(payload.get("evidence", {}) or {})
        if not files:
            files = dict(payload.get("files", payload.get("changes", {})) or {})
    changed_paths = tuple(raw.changed_paths) or tuple(str(path) for path in files)
    if contract is not None and raw.capability not in {contract.id, "unknown"}:
        raise ValueError(f"provider capability {raw.capability!r} does not match contract {contract.id!r}")
    return ContractResult(
        capability=contract.id if contract is not None else raw.capability,
        status=raw.status,
        sections=sections,
        evidence=evidence,
        files=files,
        changed_paths=changed_paths,
        produced=raw.produced,
        unresolved=raw.unresolved,
        provider=raw.provider,
        base_revision=raw.base_revision,
    )


def stage_result(
    provider_result: ProviderResult | ContractResult | Mapping[str, Any],
    root: Path,
    contract: CapabilityContract | None = None,
) -> StagedResult:
    """Normalize a raw result into a staging record without touching ``root``."""
    # Accept the natural ``stage_result(root, result, contract)`` spelling as
    # well as the canonical ``stage_result(result, root, contract)`` API.
    if isinstance(provider_result, (str, Path)) and isinstance(root, (ProviderResult, Mapping)):
        provider_result, root = root, Path(provider_result)
    root = _canonical_root(root)
    result = normalize_result(provider_result, contract)
    writable_scope = tuple(contract.dispatch.get("writable_scope", ())) if contract is not None else ()
    return StagedResult(
        root=root,
        result=result,
        writable_scope=writable_scope,
        contract_id=contract.id if contract is not None else None,
        contract_version=contract.version if contract is not None else None,
    )


def validate_staged_result(
    staged: StagedResult,
    contract: CapabilityContract | None = None,
    approval: ApprovalResult | None = None,
) -> ValidationReport:
    """Validate shape, evidence, scope, and filesystem ambiguity before writes."""
    findings: list[ValidationFinding] = []
    if not isinstance(staged, StagedResult):
        return ValidationReport((_finding("invalid_staging", "staged", "staged result must be a StagedResult"),))
    result = staged.result
    if contract is not None and result.capability != contract.id:
        findings.append(_finding("contract_mismatch", "result.capability", "result capability does not match the requested contract"))
    if contract is not None:
        statuses = tuple(contract.result.get("statuses", ()))
        if result.status not in statuses:
            findings.append(_finding("invalid_result_status", "result.status", f"result status {result.status!r} is not accepted by the contract"))
        for section in contract.result.get("required_sections", ()):
            if section not in result.sections or result.sections[section] in (None, "", (), {}):
                findings.append(_finding("missing_result_section", f"result.sections.{section}", f"required result section is missing: {section}"))
        for evidence in contract.result.get("required_evidence", ()):
            if evidence not in result.evidence or result.evidence[evidence] in (None, "", (), {}):
                findings.append(_finding("missing_result_evidence", f"result.evidence.{evidence}", f"required evidence is missing: {evidence}"))
        partial_policy = contract.integration.get("partial_result_policy")
        if result.status == "partial" and partial_policy == "reject":
            findings.append(_finding("partial_result_rejected", "result.status", "the contract rejects partial provider results"))
        elif result.status == "partial" and partial_policy == "require_human_decision":
            if not _valid_partial_approval(approval, staged):
                findings.append(_finding("partial_requires_approval", "result.status", "partial provider results require an explicit approval bound to this staged payload"))

    try:
        root = _canonical_root(staged.root)
    except (OSError, ValueError) as error:
        findings.append(_finding("invalid_canonical_root", "root", str(error)))
        root = staged.root
    writable_scope = staged.writable_scope or tuple(contract.dispatch.get("writable_scope", ())) if contract is not None else staged.writable_scope
    paths = tuple(result.changed_paths) or tuple(result.files)
    if set(paths) != set(result.files) and result.files:
        findings.append(_finding("changed_paths_mismatch", "result.changed_paths", "changed_paths must name exactly the supplied file changes"))
    _path_collision_findings(findings, paths)
    for path in sorted(set(paths), key=lambda item: str(item).casefold()):
        _validate_path(findings, root, str(path), writable_scope)
    return ValidationReport(tuple(sorted(findings, key=lambda finding: (finding.path, finding.code, finding.message))))


def integrate_staged_result(
    staged: StagedResult,
    root: Path | None = None,
    contract: CapabilityContract | None = None,
    approval: ApprovalResult | None = None,
) -> IntegrationResult:
    """Apply one fully validated result atomically to canonical files.

    Every target is preflighted first.  If an unexpected write error occurs,
    already-applied targets are restored from their original bytes.
    """
    if not isinstance(staged, StagedResult):
        raise TypeError("staged result must be a StagedResult")
    canonical_root = _canonical_root(root or staged.root)
    if canonical_root != _canonical_root(staged.root):
        raise ValueError("staged result root does not match integration root")
    report = validate_staged_result(staged, contract, approval)
    if not report.is_valid:
        first = report.findings[0]
        raise ValueError(f"{first.code}: {first.message}")
    changes: list[tuple[str, Path, bytes | None]] = []
    for relative in sorted(staged.result.files, key=lambda item: str(item).replace("\\", "/").casefold()):
        safe = _safe_relative(relative)
        target = canonical_root.joinpath(*safe)
        _reject_symlink_ambiguity(canonical_root, target)
        value = staged.result.files[relative]
        changes.append(("/".join(safe), target, _change_bytes(value)))

    old: dict[Path, bytes | None] = {}
    for _, target, _ in changes:
        old[target] = target.read_bytes() if target.is_file() else None
    applied: list[tuple[Path, bytes | None]] = []
    try:
        for _, target, content in changes:
            target.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content)
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            applied.append((target, old[target]))
    except Exception:
        for target, previous in reversed(applied):
            try:
                if previous is None:
                    if target.exists() and not target.is_symlink():
                        target.unlink()
                else:
                    target.write_bytes(previous)
            except OSError:
                pass
        raise
    manifest = {relative: _sha256_bytes(content) if content is not None else "<deleted>" for relative, _, content in changes}
    return IntegrationResult(tuple(relative for relative, _, _ in changes), manifest)


def tree_hash(root: Path) -> str:
    """Hash every regular file and relative path in deterministic order."""
    root = _canonical_root(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            digest.update(f"L:{relative}:{os.readlink(path)}".encode())
        elif path.is_file():
            digest.update(f"F:{relative}:".encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def staged_fingerprint(staged: StagedResult) -> str:
    """Return a stable fingerprint for approval of this exact staged payload."""
    if not isinstance(staged, StagedResult):
        raise TypeError("staged result must be a StagedResult")
    document = {
        "root": _root_identity(staged.root),
        "writable_scope": _normalized_scope(staged.writable_scope),
        "contract_id": staged.contract_id,
        "contract_version": staged.contract_version,
        "capability": staged.result.capability,
        "status": staged.result.status,
        "sections": staged.result.sections,
        "evidence": staged.result.evidence,
        "files": staged.result.files,
        "changed_paths": staged.result.changed_paths,
        "produced": staged.result.produced,
        "unresolved": staged.result.unresolved,
    }
    encoded = json.dumps(_json_safe(document), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_path(findings: list[ValidationFinding], root: Path, value: str, writable_scope: tuple[str, ...]) -> None:
    try:
        safe = _safe_relative(value)
        target = root.joinpath(*safe)
        _reject_symlink_ambiguity(root, target)
    except (OSError, ValueError) as error:
        message = str(error)
        folded = message.casefold()
        code = "reparse_point" if "reparse point" in folded else "symlink_path" if "symlink" in folded else "out_of_scope_path"
        findings.append(_finding(code, f"result.files[{value}]", str(error)))
        return
    if not writable_scope:
        findings.append(_finding("out_of_scope_path", f"result.files[{value}]", "contract declares no writable scope"))
        return
    scope_match = False
    for scope in writable_scope:
        try:
            scope_parts = _safe_relative(scope)
        except ValueError:
            continue
        if _is_descendant(safe, scope_parts):
            scope_match = True
            break
    if not scope_match:
        findings.append(_finding("out_of_scope_path", f"result.files[{value}]", f"path is outside writable_scope: {writable_scope!r}"))


def _canonical_root(root: Path) -> Path:
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"canonical root is not a directory: {root}")
    if _is_reparse_point(root):
        raise ValueError("canonical root symlink is ambiguous")
    return root.resolve(strict=True)


def _safe_relative(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a non-empty relative string")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ValueError("absolute paths are not permitted")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("path traversal is not permitted")
        parts.append(part)
    if not parts:
        raise ValueError("path resolves to the canonical root")
    return tuple(parts)


def _is_descendant(path: tuple[str, ...], ancestor: tuple[str, ...]) -> bool:
    folded_path = tuple(value.casefold() for value in path)
    folded_ancestor = tuple(value.casefold() for value in ancestor)
    return len(folded_path) > len(folded_ancestor) and folded_path[: len(folded_ancestor)] == folded_ancestor


def _reject_symlink_ambiguity(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if _is_reparse_point(current):
            label = "symlink" if current.is_symlink() else "reparse point"
            raise ValueError(f"{label} path is rejected conservatively: {current}")


def _is_reparse_point(path: Path) -> bool:
    """Detect Windows reparse points (junctions/symlinks) portably."""
    if os.name == "nt":
        get_attributes = ctypes.windll.kernel32.GetFileAttributesW  # type: ignore[attr-defined]
        get_attributes.restype = ctypes.c_uint32
        attributes = get_attributes(str(path))
        if attributes in {0xFFFFFFFF, -1}:
            return False
        return bool(attributes & 0x400)
    return path.is_symlink()


def _path_collision_findings(findings: list[ValidationFinding], paths: tuple[str, ...]) -> None:
    seen: dict[tuple[str, ...], str] = {}
    for value in paths:
        try:
            key = tuple(part.casefold() for part in _safe_relative(value))
        except ValueError:
            continue
        previous = seen.get(key)
        if previous is not None and previous != value:
            findings.append(_finding("path_collision", f"result.files[{value}]", f"path collides case-insensitively with {previous!r}"))
        else:
            seen[key] = value


def _valid_partial_approval(approval: ApprovalResult | None, staged: StagedResult) -> bool:
    try:
        return bool(
            isinstance(approval, ApprovalResult)
            and approval.decision == "approved"
            and isinstance(approval.actor, str)
            and approval.actor.strip()
            and approval.fingerprint == staged_fingerprint(staged)
        )
    except (OSError, TypeError, ValueError):
        return False


def _root_identity(root: Path) -> str:
    canonical = _canonical_root(root)
    return os.path.normcase(os.path.normpath(str(canonical))).replace("\\", "/").casefold()


def _normalized_scope(scope: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in scope:
        try:
            normalized.append("/".join(part.casefold() for part in _safe_relative(value)))
        except ValueError:
            invalid_value = str(value).replace("\\", "/").casefold()
            normalized.append(f"!invalid:{invalid_value}")
    return tuple(sorted(normalized))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list, frozenset, set)):
        values = [_json_safe(item) for item in value]
        if isinstance(value, (frozenset, set)):
            return sorted(values, key=repr)
        return values
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    return value


def _change_bytes(value: Any) -> bytes | None:
    if isinstance(value, Mapping):
        if value.get("delete") is True:
            return None
        value = value.get("content", value.get("value", b""))
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if value is None:
        return None
    return str(value).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _finding(code: str, path: str, message: str) -> ValidationFinding:
    return ValidationFinding(code, path, message)


__all__ = (
    "IntegrationResult", "StagedResult", "integrate_staged_result", "normalize_result",
    "stage_result", "staged_fingerprint", "tree_hash", "validate_staged_result",
)
