"""Provider-neutral compatibility reporting for repository and standalone use."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
import re
from collections.abc import Callable, Sequence
from typing import Any

import yaml

from .errors import ValidationError
from .standard import SemVer, verify_standard
from .standard_sync import check_standard_sync

TestRunner = Callable[[tuple[str, ...]], int]


class CompatibilityState(str, Enum):
    COMPATIBLE = "compatible"
    SNAPSHOT_STALE = "snapshot-stale"
    IMPLEMENTATION_STALE = "implementation-stale"
    SKILL_VERSION_MISMATCH = "skill-version-mismatch"
    UNDECLARED = "undeclared"
    INVALID = "invalid"


@dataclass(frozen=True)
class CompatibilityReport:
    mode: str
    state: CompatibilityState
    requires_confirmation: bool
    skill_version: str | None
    source_version: str | None
    source_sha256: str | None
    snapshot_version: str | None
    snapshot_sha256: str | None
    implemented_version: str | None
    implemented_sha256: str | None
    detail: str
    next_action: str

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "state": self.state.value,
            "requires_confirmation": self.requires_confirmation,
            "skill_version": self.skill_version,
            "source_version": self.source_version,
            "source_sha256": self.source_sha256,
            "snapshot_version": self.snapshot_version,
            "snapshot_sha256": self.snapshot_sha256,
            "implemented_version": self.implemented_version,
            "implemented_sha256": self.implemented_sha256,
            "detail": self.detail,
            "next_action": self.next_action,
        }


def _skill_version(skill_root: Path) -> str:
    text = (skill_root / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r"(?ms)^\[project\].*?^version\s*=\s*\"(?P<version>[^\"]+)\"", text)
    if project is None:
        raise ValidationError("skill pyproject does not declare a project version")
    value = project.group("version")
    SemVer.parse(value)
    return value


def _declaration(skill_root: Path) -> dict[str, Any]:
    path = skill_root / "references/standard/compatibility.yaml"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValidationError("compatibility declaration cannot be read") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "skill", "implements", "verification"} or value["schema_version"] != 1:
        raise ValidationError("compatibility declaration has an invalid schema")
    skill = value["skill"]
    implemented = value["implements"]
    verification = value["verification"]
    if not isinstance(skill, dict) or set(skill) != {"name", "version"} or skill.get("name") != "ai-sdlc-orchestrator":
        raise ValidationError("compatibility skill identity is invalid")
    if not isinstance(implemented, dict) or set(implemented) != {"standard_version", "standard_sha256"}:
        raise ValidationError("compatibility implementation identity is invalid")
    if not isinstance(verification, dict) or verification.get("status") not in {"undeclared", "declared"}:
        raise ValidationError("compatibility verification is invalid")
    if verification["status"] == "undeclared" and set(verification) != {"status"}:
        raise ValidationError("undeclared compatibility has unexpected verification fields")
    if verification["status"] == "declared" and set(verification) != {"status", "declared_at"}:
        raise ValidationError("declared compatibility requires declared_at")
    for version in (skill.get("version"), implemented.get("standard_version")):
        SemVer.parse(version)
    sha = implemented.get("standard_sha256")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
        raise ValidationError("implemented standard hash is invalid")
    return value


def _invalid(mode: str, detail: str) -> CompatibilityReport:
    return CompatibilityReport(mode, CompatibilityState.INVALID, True, None, None, None, None, None, None, None, detail, "Ask the user to confirm continuation without a compliance claim.")


def check_compatibility(skill_root: Path, repository_root: Path | None = None) -> CompatibilityReport:
    """Compare only identities explicitly available in the selected mode."""
    mode = "repository" if repository_root is not None else "standalone"
    try:
        skill_version = _skill_version(skill_root)
        declaration = _declaration(skill_root)
        sync = check_standard_sync(repository_root) if repository_root is not None else None
    except (OSError, ValidationError, TypeError, ValueError) as error:
        return _invalid(mode, str(error))
    declared_skill = declaration["skill"]["version"]
    implemented_version = declaration["implements"]["standard_version"]
    implemented_hash = declaration["implements"]["standard_sha256"]
    status = declaration["verification"]["status"]
    if sync and not sync.clean:
        state = CompatibilityState.INVALID if sync.state == "invalid" else CompatibilityState.SNAPSHOT_STALE
        return CompatibilityReport(
            mode,
            state,
            True,
            skill_version,
            sync.source_version,
            sync.source_sha256,
            sync.snapshot_version,
            sync.snapshot_sha256,
            implemented_version,
            implemented_hash,
            sync.detail,
            "Synchronize or repair the standard identities, or confirm continuation.",
        )
    try:
        snapshot = verify_standard(skill_root)
    except (OSError, ValidationError) as error:
        return _invalid(mode, str(error))
    source_version = sync.source_version if sync else None
    source_hash = sync.source_sha256 if sync else None
    state = CompatibilityState.COMPATIBLE
    detail = "Exact available identities match; standalone mode makes no claim about a newer external standard."
    next_action = "Continue."
    if skill_version != declared_skill:
        state = CompatibilityState.SKILL_VERSION_MISMATCH
        detail = "pyproject skill version differs from compatibility declaration"
        next_action = "Update the compatibility declaration for this skill version."
    elif implemented_version != snapshot.version or implemented_hash != snapshot.sha256:
        state = CompatibilityState.IMPLEMENTATION_STALE
        detail = "declared implementation differs from the bundled snapshot"
        next_action = "Update and test the skill, then declare the exact tuple."
    elif status != "declared":
        state = CompatibilityState.UNDECLARED
        detail = "the exact tuple has not been declared after verification"
        next_action = "Run the full tests and explicitly declare compatibility."
    requires_confirmation = state is not CompatibilityState.COMPATIBLE
    return CompatibilityReport(mode, state, requires_confirmation, skill_version, source_version, source_hash, snapshot.version, snapshot.sha256, implemented_version, implemented_hash, detail, next_action)


def declare_compatibility(
    repository_root: Path,
    skill_version: str,
    standard_version: str,
    test_command: Sequence[str],
    runner: TestRunner,
) -> CompatibilityReport:
    """Declare one exact tuple only after a successful caller-supplied test run."""
    skill_root = repository_root / "skills/ai-sdlc-orchestrator"
    target = skill_root / "references/standard/compatibility.yaml"
    declaration_before = target.read_bytes()
    previous = _declaration(skill_root)
    actual_skill = _skill_version(skill_root)
    sync = check_standard_sync(repository_root)
    if not sync.clean:
        raise ValidationError(f"snapshot is not synchronized: {sync.state}")
    snapshot = verify_standard(skill_root)
    SemVer.parse(skill_version)
    SemVer.parse(standard_version)
    if skill_version != actual_skill or standard_version != snapshot.version:
        raise ValidationError("requested versions do not match the current skill and standard")
    previous_tuple = (
        previous["implements"]["standard_version"],
        previous["implements"]["standard_sha256"],
    )
    current_tuple = (snapshot.version, snapshot.sha256)
    if previous_tuple != current_tuple and previous["skill"]["version"] == actual_skill:
        raise ValidationError("a changed standard tuple requires a skill version bump")
    command = tuple(test_command)
    if not command:
        raise ValidationError("test command must not be empty")
    if runner(command) != 0:
        raise ValidationError("compatibility tests failed; declaration was not written")
    current_sync = check_standard_sync(repository_root)
    current_skill = _skill_version(skill_root)
    current_snapshot = verify_standard(skill_root)
    if (
        not current_sync.clean
        or current_skill != actual_skill
        or (current_snapshot.version, current_snapshot.sha256) != current_tuple
        or target.read_bytes() != declaration_before
    ):
        raise ValidationError("skill, standard, or declaration changed while tests ran")
    document = {
        "schema_version": 1,
        "skill": {"name": "ai-sdlc-orchestrator", "version": actual_skill},
        "implements": {"standard_version": snapshot.version, "standard_sha256": snapshot.sha256},
        "verification": {"status": "declared", "declared_at": date.today().isoformat()},
    }
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    report = check_compatibility(skill_root, repository_root)
    if report.state is not CompatibilityState.COMPATIBLE:
        raise ValidationError(f"written declaration did not produce compatible state: {report.state.value}")
    return report
