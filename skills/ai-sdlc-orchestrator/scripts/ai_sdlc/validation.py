"""Deterministic structural, referential, and lifecycle validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from .model import (
    ARTIFACT_KINDS,
    ARTIFACT_STATUSES,
    CRITERION_STATUSES,
    SOURCE_KINDS,
    SOURCE_STATUSES,
    STAGE_DEFINITIONS,
    STAGE_STATUSES,
    WORK_STATUSES,
    Artifact,
    Criterion,
    ProjectSnapshot,
    Scope,
    Source,
    Stage,
    WorkItem,
)


@dataclass(frozen=True, order=True, slots=True)
class ValidationFinding:
    """One stable, machine-readable validation result."""

    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """An immutable report with findings in deterministic traversal order."""

    findings: tuple[ValidationFinding, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.findings


_SCHEMA_NAMES = (
    "project-plan.schema.json",
    "project-state.schema.json",
    "artifact-register.schema.json",
    "source-register.schema.json",
)

_STAGE_TRANSITIONS = {
    "not_started": {"planning", "blocked"},
    "planning": {"ready", "blocked"},
    "ready": {"active", "blocked"},
    "active": {"in_review", "blocked"},
    "in_review": {"completed", "active", "blocked"},
    "blocked": {"planning", "ready", "active", "in_review"},
    "completed": set(),
}
_WORK_TRANSITIONS = {
    "planned": {"ready", "needs_clarification", "blocked", "deferred", "cancelled"},
    "ready": {"in_progress", "needs_clarification", "blocked", "deferred", "cancelled"},
    "in_progress": {"review", "needs_clarification", "blocked", "deferred", "cancelled"},
    "review": {"done", "in_progress", "needs_clarification", "blocked", "deferred", "cancelled"},
    "needs_clarification": {"planned", "ready", "in_progress", "review"},
    "blocked": {"planned", "ready", "in_progress", "review"},
    "deferred": {"planned", "ready", "in_progress", "review"},
    "done": set(),
    "cancelled": set(),
}
_ARTIFACT_TRANSITIONS = {
    "draft": {"in_review"}, "in_review": {"approved", "draft"}, "approved": {"superseded", "retired"},
    "superseded": set(), "retired": set(),
}
_SOURCE_TRANSITIONS = {
    "registered": {"accepted", "rejected", "unavailable"}, "accepted": {"superseded"},
    "superseded": set(), "rejected": set(), "unavailable": set(),
}
_CRITERION_TRANSITIONS = {
    "pending": {"in_review"}, "in_review": {"verified", "not_applicable", "pending"},
    "verified": set(), "not_applicable": set(),
}


def validate_documents(root: Path, *documents: dict[str, Any]) -> ValidationReport:
    """Validate raw canonical documents against the shipped JSON Schemas."""
    schema_root = root / "assets/schemas"
    if not schema_root.is_dir():
        schema_root = Path(__file__).resolve().parents[2] / "assets/schemas"
    findings: list[ValidationFinding] = []
    for schema_name, document in zip(_SCHEMA_NAMES, documents, strict=True):
        schema = json.loads((schema_root / schema_name).read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(_json_compatible(document)), key=lambda error: (list(error.path), error.message))
        for error in errors:
            pointer = ".".join(str(part) for part in error.path) or "$"
            findings.append(ValidationFinding("schema_validation", f"{schema_name}:{pointer}", error.message))
    return ValidationReport(tuple(findings))


def validate_snapshot(snapshot: ProjectSnapshot) -> ValidationReport:
    """Validate canonical status ownership, controlled values, and all references."""
    findings: list[ValidationFinding] = []
    plan, state = snapshot.plan, snapshot.state
    if plan.schema_version != 1:
        findings.append(_finding("unsupported_schema_version", "plan.schema_version", "project plan schema_version must be 1"))
    if state.schema_version != 1:
        findings.append(_finding("unsupported_schema_version", "state.schema_version", "project state schema_version must be 1"))
    if plan.project_id != state.project_id:
        findings.append(_finding("project_id_mismatch", "state.project_id", "project state project_id must match project plan"))

    expected = STAGE_DEFINITIONS
    actual = tuple((stage.id, stage.title) for stage in plan.stages)
    if actual != expected:
        findings.append(_finding("invalid_stage_catalogue", "plan.stages", "project plan must contain the fourteen ordered stages defined by the standard"))

    _duplicate_findings(findings, "stage", (stage.id for stage in plan.stages), "plan.stages")
    _duplicate_findings(findings, "artifact", (artifact.id for artifact in snapshot.artifacts), "artifacts")
    _duplicate_findings(findings, "source", (source.id for source in snapshot.sources), "sources")
    _duplicate_artifact_paths(findings, snapshot.artifacts)

    stage_ids = tuple(stage.id for stage in plan.stages)
    work_items = tuple(work for stage in plan.stages for work in stage.work_items)
    work_ids = tuple(work.id for work in work_items)
    criterion_ids = tuple(criterion.id for criterion in _criteria(plan.stages))
    artifact_ids = tuple(artifact.id for artifact in snapshot.artifacts)
    _duplicate_findings(findings, "work_item", (work.id for work in work_items), "plan.stages.work_items")
    _duplicate_findings(findings, "criterion", (criterion.id for criterion in _criteria(plan.stages)), "plan.stages.criteria")

    for index, stage in enumerate(plan.stages):
        path = f"plan.stages[{index}]"
        _allowed_value(findings, stage.status, STAGE_STATUSES, f"{path}.status", "stage")
        _references(findings, stage.depends_on, stage_ids, "unknown_stage_dependency", f"{path}.depends_on", "stage dependency")
        _references(findings, stage.required_artifacts, artifact_ids, "unknown_required_artifact", f"{path}.required_artifacts", "required artifact")
        _validate_criteria(findings, stage.entry_criteria, f"{path}.entry_criteria")
        _validate_criteria(findings, stage.exit_criteria, f"{path}.exit_criteria")
        for work_index, work in enumerate(stage.work_items):
            work_path = f"{path}.work_items[{work_index}]"
            _allowed_value(findings, work.status, WORK_STATUSES, f"{work_path}.status", "work item")
            if not isinstance(work.executor, str) or not work.executor.strip():
                findings.append(_finding("missing_executor", f"{work_path}.executor", "work item executor must be non-empty"))
            _references(findings, work.depends_on, work_ids, "unknown_work_dependency", f"{work_path}.depends_on", "work dependency")
            _references(findings, work.produces, artifact_ids, "unknown_produced_artifact", f"{work_path}.produces", "produced artifact")
            _references(findings, work.criterion_ids, criterion_ids, "unknown_work_criterion", f"{work_path}.criterion_ids", "work criterion")
            if work.criterion_ids and not work.criterion_evidence:
                findings.append(_finding("missing_work_criterion_evidence", f"{work_path}.criterion_evidence", "work criteria require declared evidence keys"))
            if not isinstance(work.scope, Scope):
                findings.append(_finding("invalid_scope", f"{work_path}.scope", "work item scope must be a Scope"))
            else:
                _references(findings, work.scope.artifacts, artifact_ids, "unknown_scope_artifact", f"{work_path}.scope.artifacts", "scope artifact")

    for index, artifact in enumerate(snapshot.artifacts):
        _allowed_value(findings, artifact.kind, ARTIFACT_KINDS, f"artifacts[{index}].kind", "artifact kind")
        _allowed_value(findings, artifact.status, ARTIFACT_STATUSES, f"artifacts[{index}].status", "artifact")
    for index, source in enumerate(snapshot.sources):
        _allowed_value(findings, source.kind, SOURCE_KINDS, f"sources[{index}].kind", "source kind")
        _allowed_value(findings, source.status, SOURCE_STATUSES, f"sources[{index}].status", "source")

    if state.current_stage not in stage_ids:
        findings.append(_finding("unknown_current_stage", "state.current_stage", "current stage must resolve in project plan"))
    _references(findings, state.focus_work_items, work_ids, "unknown_focus_work_item", "state.focus_work_items", "focus work item")
    _references(findings, (action.work_item for action in state.next_actions), work_ids, "unknown_next_action_work_item", "state.next_actions", "next-action work item")
    if state.last_approved_artifact is not None and state.last_approved_artifact not in artifact_ids:
        findings.append(_finding("unknown_last_approved_artifact", "state.last_approved_artifact", "last approved artifact must resolve in artifact register"))
    return ValidationReport(tuple(findings))


def validate_transition(
    before: ProjectSnapshot,
    after: ProjectSnapshot,
    approval: Any | None = None,
    payload: bytes | None = None,
) -> ValidationReport:
    """Validate only standard status transitions after both snapshots are sound."""
    findings = list(validate_snapshot(before).findings) + list(validate_snapshot(after).findings)
    _transition_findings(findings, "stage", _stages(before), _stages(after), _STAGE_TRANSITIONS)
    _transition_findings(findings, "work", _works(before), _works(after), _WORK_TRANSITIONS)
    _transition_findings(findings, "artifact", _artifacts(before), _artifacts(after), _ARTIFACT_TRANSITIONS)
    _transition_findings(findings, "source", _sources(before), _sources(after), _SOURCE_TRANSITIONS)
    _transition_findings(findings, "criterion", _criteria(before.plan.stages), _criteria(after.plan.stages), _CRITERION_TRANSITIONS)
    _human_approval_findings(findings, before, after, approval, payload)
    return ValidationReport(tuple(findings))


def _human_approval_findings(
    findings: list[ValidationFinding], before: ProjectSnapshot, after: ProjectSnapshot,
    approval: Any | None, payload: bytes | None,
) -> None:
    """Require external approval evidence for criteria controlled by a person."""
    before_by_id = {criterion.id: criterion for criterion in _criteria(before.plan.stages)}
    for criterion in _criteria(after.plan.stages):
        previous = before_by_id.get(criterion.id)
        if criterion.status != "verified":
            continue
        if previous is not None and previous.status == "verified":
            if criterion.approval_role != previous.approval_role:
                findings.append(_finding("approval_role_changed", f"criterion:{criterion.id}.approval_role", "already verified human criterion approval role cannot be changed"))
            continue
        required_role = (previous.approval_role if previous is not None else None) or criterion.approval_role
        if not required_role:
            continue
        role_changed = previous is not None and previous.approval_role and criterion.approval_role != previous.approval_role
        if approval is None or payload is None:
            findings.append(_finding("missing_approval", f"criterion:{criterion.id}", "human criterion verification requires an external approval record and exact payload"))
            continue
        if role_changed:
            findings.append(_finding("approval_role_changed", f"criterion:{criterion.id}.approval_role", "human criterion approval role cannot be removed or changed while verifying"))
        from .approvals import verify_approval

        report = verify_approval(approval, payload, required_role, criterion.approval_gate or criterion.id)
        findings.extend(report.findings)


def _transition_findings(
    findings: list[ValidationFinding], object_type: str, before: Iterable[Any], after: Iterable[Any], transitions: dict[str, set[str]]
) -> None:
    before_items = tuple(before)
    after_items = tuple(after)
    for before_item in before_items:
        for after_item in after_items:
            if before_item.id != after_item.id:
                continue
            old, new = before_item.status, after_item.status
            if not isinstance(old, str) or not isinstance(new, str):
                continue
            if old != new and new not in transitions.get(old, set()):
                findings.append(_finding("invalid_transition", f"{object_type}:{before_item.id}", f"{object_type} transition {old!r} -> {new!r} is not permitted by the standard"))


def _stages(snapshot: ProjectSnapshot) -> tuple[Stage, ...]:
    return snapshot.plan.stages


def _works(snapshot: ProjectSnapshot) -> tuple[WorkItem, ...]:
    return tuple(work for stage in snapshot.plan.stages for work in stage.work_items)


def _artifacts(snapshot: ProjectSnapshot) -> tuple[Artifact, ...]:
    return snapshot.artifacts


def _sources(snapshot: ProjectSnapshot) -> tuple[Source, ...]:
    return snapshot.sources


def _criteria(stages: Iterable[Stage]) -> tuple[Criterion, ...]:
    return tuple(criterion for stage in stages for criterion in (*stage.entry_criteria, *stage.exit_criteria))


def _validate_criteria(findings: list[ValidationFinding], criteria: tuple[Criterion, ...], path: str) -> None:
    for index, criterion in enumerate(criteria):
        criterion_path = f"{path}[{index}]"
        _allowed_value(findings, criterion.status, CRITERION_STATUSES, f"{criterion_path}.status", "criterion")
        if criterion.status == "not_applicable" and not criterion.reason:
            findings.append(_finding("missing_not_applicable_reason", f"{criterion_path}.reason", "not_applicable criterion must declare a reason"))


def _references(
    findings: list[ValidationFinding], identifiers: Iterable[str], known: Iterable[str], code: str, path: str, label: str
) -> None:
    for index, identifier in enumerate(identifiers):
        if identifier not in known:
            findings.append(_finding(code, f"{path}[{index}]", f"{label} {identifier!r} does not resolve"))


def _allowed_value(findings: list[ValidationFinding], value: str, allowed: Iterable[str], path: str, label: str) -> None:
    if value not in allowed:
        findings.append(_finding("invalid_status_or_kind", path, f"{label} value {value!r} is not allowed by the standard"))


def _duplicate_findings(findings: list[ValidationFinding], label: str, identifiers: Iterable[str], path: str) -> None:
    seen: list[Any] = []
    for index, identifier in enumerate(identifiers):
        if identifier in seen:
            findings.append(_finding("duplicate_id", f"{path}[{index}]", f"duplicate {label} ID {identifier!r}"))
        seen.append(identifier)


def _duplicate_artifact_paths(findings: list[ValidationFinding], artifacts: Iterable[Artifact]) -> None:
    seen: dict[str, str] = {}
    for index, artifact in enumerate(artifacts):
        previous_id = seen.get(artifact.path)
        if previous_id is not None and previous_id != artifact.id:
            findings.append(_finding("duplicate_artifact_path", f"artifacts[{index}].path", f"artifact path {artifact.path!r} is used by IDs {previous_id!r} and {artifact.id!r}"))
        else:
            seen[artifact.path] = artifact.id


def _finding(code: str, path: str, message: str) -> ValidationFinding:
    return ValidationFinding(code=code, path=path, message=message)


def _json_compatible(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    return value.isoformat() if hasattr(value, "isoformat") else value
