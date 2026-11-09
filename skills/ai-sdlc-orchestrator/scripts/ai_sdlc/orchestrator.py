"""Canonical lifecycle orchestration for the portable AI SDLC skill.

The module is intentionally a small coordinator.  It reads canonical YAML,
delegates result validation to the Task 10 integration port, and delegates the
single mutation boundary to the Task 11 checkpoint manager.  Markdown status is
always regenerated as a projection; it is never used to recover state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

import yaml

from .checkpoint import PreparedCheckpoint, commit_checkpoint, prepare_checkpoint, recover, validate_checkpoint
from .approvals import Approval, verify_approval
from .contracts import CapabilityContract, load_contract
from .errors import OrchestratorError, PolicyError, ValidationError
from .integration import StagedResult, stage_result, staged_fingerprint, validate_staged_result
from .model import (
    NextAction,
    ProjectPlan,
    ProjectSnapshot,
    ProjectState,
    STAGE_DEFINITIONS,
    Stage,
    WorkItem,
    load_project,
)
from .providers import ProviderLock, resolve_provider, verify_provider_lock
from .runtime import ApprovalResult, ContractResult, ProviderResult
from .scheduler import build_safe_batch, ready_work, validate_dag
from .standard import StandardIdentity, verify_standard
from .status_view import check_status, generate_status
from .traceability import TraceEdge, TraceGraph, TraceNode, build_trace_graph, validate_traceability
from .validation import validate_snapshot, validate_transition


_SKILL_ROOT = Path(__file__).resolve().parents[2]
_PLAN = "docs/project-control/project-plan.yaml"
_STATE = "docs/project-control/project-state.yaml"
_ARTIFACTS = "docs/artifact-register.yaml"
_SOURCES = "docs/product-definition/sources/source-register.yaml"
_LOCK = "docs/project-control/capability-lock.yaml"
_IDENTITY = "docs/project-control/standard-identity.yaml"
_STATUS = "PROJECT_STATUS.md"
_TRACE = "docs/project-control/traceability.yaml"
_PROTECTED = frozenset({_PLAN, _STATE, _ARTIFACTS, _SOURCES, _LOCK, _IDENTITY, _STATUS, _TRACE})
_PACKAGE_IDENTITY_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PACKAGE_CAPABILITIES = frozenset({
    "delivery.prepare", "delivery.test-deploy", "delivery.health-check",
    "delivery.acceptance", "delivery.production-deploy",
})
_PASS_EVIDENCE = frozenset({
    "smoke-test", "health-check", "production-smoke", "production-health",
    "functional-tests", "integration-tests", "system-tests", "regression-tests",
    "security-tests", "performance-tests", "business-scenario",
})


def _result_path_parts(path: Any) -> tuple[str, ...]:
    """Return a strict, normalized relative path preserving provider case."""
    if not isinstance(path, str) or not path.strip():
        raise ValidationError("provider result path must be a non-empty relative string")
    value = path.replace("\\", "/")
    parts = tuple(part for part in value.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts) or value.startswith("/"):
        raise ValidationError(f"provider result path is not safely relative: {path!r}")
    return parts


def _path_parts_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    left = tuple(part.casefold() for part in left)
    right = tuple(part.casefold() for part in right)
    common = min(len(left), len(right))
    return left[:common] == right[:common]


@dataclass(frozen=True, slots=True)
class ResumeContext:
    """Minimal, canonical context required to continue a project."""

    root: Path
    snapshot: ProjectSnapshot
    standard: StandardIdentity
    provider_lock: ProviderLock
    recovery: Any = None

    @property
    def plan(self) -> ProjectPlan:
        return self.snapshot.plan

    @property
    def state(self) -> ProjectState:
        return self.snapshot.state

    @property
    def lock(self) -> ProviderLock:
        return self.provider_lock

    @property
    def standard_identity(self) -> StandardIdentity:
        return self.standard


@dataclass(frozen=True, slots=True)
class Action:
    """A deterministic proposal; it has no runtime side effects."""

    work_item: str
    action: str
    capability: str | None = None
    contract_version: int | None = None
    provider: str | None = None
    dependencies: tuple[str, ...] = ()
    provider_execution: bool = False
    requires_human_gate: bool = False
    human_gate: str | None = None

    @property
    def id(self) -> str:
        return self.work_item


@dataclass(frozen=True, slots=True)
class ActionSet:
    actions: tuple[Action, ...] = ()
    batches: tuple[tuple[str, ...], ...] = ()
    blocked: tuple[str, ...] = ()
    human_gates: tuple[str, ...] = ()

    @property
    def next_actions(self) -> tuple[Action, ...]:
        return self.actions

    @property
    def ready(self) -> tuple[Action, ...]:
        return self.actions


@dataclass(frozen=True, slots=True)
class _Accepted:
    work_item: str
    staged: StagedResult
    contract: CapabilityContract | None
    approval: Any | None


@dataclass(frozen=True, slots=True)
class _OwnedFile:
    """Identity of a file exclusively created by this initialization transaction."""

    path: Path
    device: int
    inode: int
    size: int
    digest: str


def initialize_project(root: Path, standard: Path | StandardIdentity | None, lock: ProviderLock | Mapping[str, Any] | Path) -> ProjectSnapshot:
    """Create a new project without overwriting any pre-existing target file."""
    identity = _verify_requested_standard(standard)
    root = _canonical_project_root(root)
    lock_value = ProviderLock.from_value(lock)
    _verify_lock(lock_value)
    targets = (_PLAN, _STATE, _ARTIFACTS, _SOURCES, _LOCK, _IDENTITY, _STATUS)
    existing = [relative for relative in targets if (root / relative).exists()]
    if existing:
        raise ValidationError(f"project already contains canonical files; refusing overwrite: {', '.join(existing)}")
    init_lock = _acquire_init_lock(root)

    documents = _initial_documents(identity, lock_value)
    temporary = Path(tempfile.mkdtemp(prefix=".ai-sdlc-init-", dir=str(root)))
    created: list[_OwnedFile] = []
    try:
        for relative, document in documents.items():
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_yaml_bytes(document))
        identity_path = temporary / _IDENTITY
        identity_path.write_bytes(_yaml_bytes(_identity_document(identity)))
        lock_path = temporary / _LOCK
        lock_path.write_bytes(_yaml_bytes(_lock_document(lock_value)))
        snapshot = load_project(temporary)
        report = validate_snapshot(snapshot)
        if not report.is_valid:
            first = report.findings[0]
            raise ValidationError(f"{first.code}: {first.message}")
        status = generate_status(temporary).read_bytes()
        (temporary / _STATUS).write_bytes(status)
        # All validation happens before the first target path is created.
        for relative in (*documents, _IDENTITY, _LOCK, _STATUS):
            destination = root / relative
            if destination.exists():
                raise ValidationError(f"project changed during initialization; refusing overwrite: {relative}")
        for relative in (*documents, _IDENTITY, _LOCK, _STATUS):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            created.append(_exclusive_install(temporary / relative, destination))
        return load_project(root)
    except Exception:
        # Never remove a target that appeared concurrently or was not created
        # by this transaction.
        for owned in reversed(created):
            _remove_owned_file(owned)
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
        _release_init_lock(init_lock)


def resume_project(root: Path) -> ResumeContext:
    """Recover and validate only canonical project state and its projection."""
    root = _canonical_project_root(root)
    # Standard identity is checked before reading project-state, as required by
    # the lifecycle recovery protocol.
    standard = verify_standard(_SKILL_ROOT)
    identity_document = _read_yaml(root / _IDENTITY)
    _verify_identity_document(identity_document, standard)
    lock = ProviderLock.from_value(root / _LOCK)
    _verify_lock(lock)
    recovery = recover(root)
    if not recovery.is_clean:
        first = recovery.findings[0]
        raise ValidationError(f"recovery conflict: {first.code}: {first.message}")
    snapshot = load_project(root)
    _reconcile_source_files(root, snapshot)
    report = validate_snapshot(snapshot)
    if not report.is_valid:
        first = report.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    _validate_plan_state_consistency(snapshot)
    dag_report = validate_dag(tuple(work for stage in snapshot.plan.stages for work in stage.work_items))
    if not dag_report.is_valid:
        first = dag_report.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    trace_report = validate_traceability(build_trace_graph(snapshot))
    if not trace_report.is_valid:
        first = trace_report.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    _validate_optional_trace(root, snapshot)
    # PROJECT_STATUS.md is deliberately inspected only as a derived projection.
    # A stale or edited projection can never override canonical YAML; callers
    # that need a strict projection check use ``status check`` explicitly.
    check_status(root)
    return ResumeContext(root=root, snapshot=snapshot, standard=standard, provider_lock=lock, recovery=recovery)


def propose_next_actions(context: ResumeContext | ProjectSnapshot) -> ActionSet:
    """Compute a read-only, capability-aware ready DAG proposal."""
    if isinstance(context, ProjectSnapshot):
        snapshot = context
        root = None
        lock = None
    elif isinstance(context, ResumeContext):
        snapshot, root, lock = context.snapshot, context.root, context.provider_lock
    else:
        raise TypeError("context must be a ResumeContext or ProjectSnapshot")
    report = validate_snapshot(snapshot)
    if not report.is_valid:
        first = report.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    dag_report = validate_dag(tuple(work for stage in snapshot.plan.stages for work in stage.work_items))
    if not dag_report.is_valid:
        first = dag_report.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    ready = ready_work(snapshot)
    current_stage = snapshot.state.current_stage
    current_items = tuple(work for stage in snapshot.plan.stages if stage.id == current_stage for work in stage.work_items)
    current_ids = {work.id for work in current_items}
    candidates = [item for item in ready if item.id in current_ids]
    capacity = 8
    batch = build_safe_batch(candidates, capacity)
    actions: list[Action] = []
    blocked: list[str] = []
    all_by_id = {work.id: work for stage in snapshot.plan.stages for work in stage.work_items}
    for work in sorted(current_items, key=lambda item: item.id):
        if work.status not in {"ready", "in_progress"} or work.id in {item.id for item in candidates}:
            continue
        missing = [dependency for dependency in work.depends_on if dependency not in all_by_id]
        unfinished = [dependency for dependency in work.depends_on if dependency in all_by_id and all_by_id[dependency].status not in {"done", "completed"}]
        if missing or unfinished:
            details = ", ".join((*missing, *unfinished))
            blocked.append(f"{work.id}: blocked dependency ({details})")
    for work in sorted(candidates, key=lambda item: item.id):
        capability = _work_capability(work)
        provider: str | None = None
        contract_version: int | None = None
        if capability:
            try:
                contract = load_contract(capability, 1)
                contract_version = contract.version
                if lock is not None:
                    resolution = resolve_provider(contract, lock, {})
                    if resolution.status != "resolved":
                        blocked.append(f"{work.id}: provider unavailable ({resolution.status})")
                        continue
                    provider = resolution.provider
            except (ValidationError, OrchestratorError) as error:
                blocked.append(f"{work.id}: {error}")
                continue
        actions.append(Action(work.id, work.title, capability, contract_version, provider, tuple(work.depends_on)))
    human_gates = tuple(
        criterion.id
        for stage in snapshot.plan.stages
        if stage.id == current_stage
        for criterion in (*stage.entry_criteria, *stage.exit_criteria)
        if criterion.approval_role and criterion.status in {"pending", "in_review"}
    )
    if human_gates:
        actions = [
            Action(a.work_item, a.action, a.capability, a.contract_version, a.provider, a.dependencies, False, True, human_gates[0])
            for a in actions
        ]
    selected = {item.id for item in batch.items}
    actions = [action for action in actions if action.work_item in selected]
    return ActionSet(tuple(sorted(actions, key=lambda action: action.work_item)),
                     (tuple(item.id for item in batch.items),) if batch.items else (), tuple(sorted(blocked)), human_gates)


def advance(
    root: Path,
    accepted_results: Sequence[Any],
    approvals: Mapping[str, ApprovalResult] | None = None,
    *,
    adapter: Any = None,
    runtime: Any = None,
    revision_provider: Any = None,
) -> ProjectSnapshot:
    """Validate, integrate, checkpoint, and confirm provider results atomically."""
    context = resume_project(root)
    root = context.root
    if adapter is None and runtime is None:
        raise ValidationError("a confirmed commit adapter is required")
    if not accepted_results:
        raise ValidationError("advance requires at least one staged provider result")
    approval_map = dict(approvals or {})
    accepted = tuple(_accepted_result(root, value, approval_map, context.provider_lock, revision_provider) for value in accepted_results)
    before = context.snapshot
    by_id = {work.id: work for stage in before.plan.stages for work in stage.work_items}
    changes: dict[str, bytes | str | None] = {}
    seen_paths: dict[tuple[str, ...], tuple[str, str]] = {}
    seen_artifacts: dict[str, str] = {}
    for item in accepted:
        work = by_id.get(item.work_item)
        if work is None:
            raise ValidationError(f"unregistered work item: {item.work_item}")
        expected_capability = _work_capability(work)
        if expected_capability and item.contract and item.contract.id != expected_capability:
            raise ValidationError(f"capability for {item.work_item} does not match the planned contract")
        if work.status != "ready":
            raise ValidationError(f"work item {item.work_item} is not ready")
        if any(by_id.get(dep) is None or by_id[dep].status not in {"done", "completed"} for dep in work.depends_on):
            raise ValidationError(f"blocked dependency for {item.work_item}")
        _reject_protected_paths(item.staged)
        _validate_registered_evidence(item.staged, before)
        _validate_produced_artifacts(item.staged, work, before, item.contract)
        _validate_delivery_semantics(item.staged, item.contract, before)
        for artifact_id in item.staged.result.produced:
            key = str(artifact_id).casefold()
            previous = seen_artifacts.get(key)
            if previous is not None:
                raise ValidationError(f"cross-result artifact collision: {artifact_id} produced by {previous} and {item.work_item}")
            seen_artifacts[key] = item.work_item
        for path, value in item.staged.result.files.items():
            normalized = _result_path_parts(path)
            collision_key = tuple(part.casefold() for part in normalized)
            for prior_parts, (prior_work, prior_raw) in seen_paths.items():
                if _path_parts_overlap(normalized, prior_parts):
                    raise ValidationError(f"cross-result path collision: {path!r} from {item.work_item} overlaps {prior_raw!r} from {prior_work}")
            seen_paths[collision_key] = (item.work_item, str(path))
            changes["/".join(normalized)] = _content_bytes(value)

    # Re-check the exact base revision immediately before staging the
    # checkpoint so a concurrent commit cannot make accepted results stale.
    for item in accepted:
        expected = item.staged.result.base_revision
        if expected is None:
            raise ValidationError(f"provider result for {item.work_item} has no base revision")
        if str(expected).strip().lower() != _working_revision(root, revision_provider):
            raise ValidationError(f"stale provider result for {item.work_item}")

    plan_doc = _read_yaml(root / _PLAN)
    state_doc = _read_yaml(root / _STATE)
    artifact_doc = _read_yaml(root / _ARTIFACTS)
    artifacts_by_id = {str(item.get("id")): item for item in artifact_doc.get("artifacts", ()) if isinstance(item, Mapping)}
    for accepted_item in accepted:
        _mark_work_done(plan_doc, state_doc, accepted_item.work_item)
        if accepted_item.contract and accepted_item.contract.id == "delivery.prepare":
            package_identity = _coordinator_package_identity(accepted_item.staged)
            existing_identity = state_doc.get("deployment_package_identity")
            if existing_identity is not None and existing_identity != package_identity:
                raise ValidationError("deployment package identity cannot be replaced once bound")
            state_doc["deployment_package_identity"] = package_identity
        work = by_id[accepted_item.work_item]
        required_gates = tuple(accepted_item.contract.integration.get("human_approvals", ())) if accepted_item.contract else ()
        approved = bool(required_gates and isinstance(accepted_item.approval, Approval) and accepted_item.approval.decision == "approved")
        for artifact_id in work.produces:
            artifact = artifacts_by_id.get(artifact_id)
            if artifact is None:
                raise ValidationError(f"unregistered produced artifact: {artifact_id}")
            artifact["status"] = "approved" if approved else "in_review"
            if approved and artifact.get("kind") == "architecture_baseline":
                state_doc["current_baseline"] = artifact_id
                state_doc["last_approved_artifact"] = artifact_id
        _mark_route_criterion(plan_doc, work, accepted_item, approved)
    changes[_PLAN] = _yaml_bytes(plan_doc)
    changes[_STATE] = _yaml_bytes(state_doc)
    changes[_ARTIFACTS] = _yaml_bytes(artifact_doc)
    trace_document = _promoted_trace_document(root, before, accepted)
    generated = _candidate_status(root, changes)
    changes[_STATUS] = generated

    candidate = _candidate_snapshot(root, changes)
    if trace_document is not None:
        trace_document = _trace_document_for_snapshot(candidate, trace_document.get("edges", ()))
        changes[_TRACE] = _yaml_bytes(trace_document)
        _validate_promoted_trace(candidate, trace_document, before, accepted)
    transition_approval = None
    transition_payload = None
    approval_items = [item for item in accepted if item.contract and item.contract.integration.get("human_approvals")]
    if len(approval_items) > 1:
        raise ValidationError("human-gated results must be checkpointed one at a time")
    if approval_items:
        transition_approval = approval_items[0].approval
        transition_payload = staged_fingerprint(approval_items[0].staged).encode("utf-8")
    transition = _validate_completion_transition(
        before, candidate, tuple(item.work_item for item in accepted),
        approval=transition_approval, payload=transition_payload,
    )
    if not transition.is_valid:
        first = transition.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    trace = validate_traceability(build_trace_graph(candidate))
    if not trace.is_valid:
        first = trace.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    prepared = prepare_checkpoint(root, changes, adapter=adapter or runtime)
    checkpoint_report = validate_checkpoint(prepared)
    if not checkpoint_report.is_valid:
        first = checkpoint_report.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    commit = adapter or runtime
    try:
        commit_checkpoint(prepared, "orchestrator: integrate provider results", adapter=commit)
    except Exception as error:
        raise ValidationError(f"checkpoint failed: {error}") from error
    result = check_status(root)
    if not result.is_valid:
        raise ValidationError(f"checkpoint status verification failed: {result.message}")
    return load_project(root)


def advance_stage(
    root: Path,
    *,
    adapter: Any = None,
    runtime: Any = None,
    expected_stage: str | None = None,
    revision_provider: Any = None,
) -> ProjectSnapshot:
    """Complete the current stage and move the canonical focus to its successor.

    Stage progression is coordinator-owned: providers can only submit work
    results, while this boundary verifies all work and exit criteria before
    changing the canonical current stage.
    """
    context = resume_project(root)
    root = context.root
    if adapter is None and runtime is None:
        raise ValidationError("a confirmed commit adapter is required")
    if expected_stage is not None and expected_stage != context.state.current_stage:
        raise ValidationError(
            f"cannot advance stage {context.state.current_stage}; expected {expected_stage}"
        )
    current_index = next(
        (index for index, stage in enumerate(context.plan.stages) if stage.id == context.state.current_stage),
        None,
    )
    if current_index is None:
        raise ValidationError("current stage is unknown")
    current = context.plan.stages[current_index]
    if current.status != "in_review":
        raise ValidationError(f"stage {current.id} is not ready for coordinator transition")
    unfinished = [work.id for work in current.work_items if work.status != "done"]
    if unfinished:
        raise ValidationError(f"stage {current.id} has unfinished work: {', '.join(unfinished)}")
    unverified = [
        criterion.id
        for criterion in (*current.entry_criteria, *current.exit_criteria)
        if criterion.mandatory and criterion.status not in {"verified", "not_applicable"}
    ]
    if unverified:
        raise ValidationError(f"stage {current.id} has unverified criteria: {', '.join(unverified)}")
    next_stage = context.plan.stages[current_index + 1] if current_index + 1 < len(context.plan.stages) else None
    if next_stage is not None:
        next_unverified = [
            criterion.id
            for criterion in (*next_stage.entry_criteria,)
            if criterion.mandatory and criterion.status not in {"verified", "not_applicable"}
        ]
        if next_unverified:
            raise ValidationError(f"stage {next_stage.id} has unverified entry criteria: {', '.join(next_unverified)}")

    plan_doc = _read_yaml(root / _PLAN)
    state_doc = _read_yaml(root / _STATE)
    plan_stages = plan_doc.get("stages", ())
    for stage in plan_stages:
        if stage.get("id") == current.id:
            stage["status"] = "completed"
        elif next_stage is not None and stage.get("id") == next_stage.id and stage.get("status") == "not_started":
            stage["status"] = "planning"
    if next_stage is None:
        state_doc["current_stage"] = current.id
        state_doc["focus_work_items"] = []
        state_doc["next_actions"] = []
    else:
        next_work = next_stage.work_items
        state_doc["current_stage"] = next_stage.id
        focus = [
            work.id
            for work in next_work
            if work.status not in {"done", "cancelled"}
        ]
        state_doc["focus_work_items"] = focus
        state_doc["next_actions"] = [
            {"work_item": work.id, "action": work.title}
            for work in next_work
            if work.status not in {"done", "cancelled"}
        ]

    changes: dict[str, bytes | str | None] = {
        _PLAN: _yaml_bytes(plan_doc),
        _STATE: _yaml_bytes(state_doc),
    }
    changes[_STATUS] = _candidate_status(root, changes)
    before = context.snapshot
    candidate = _candidate_snapshot(root, changes)
    _validate_plan_state_consistency(candidate)
    transition = validate_transition(before, candidate)
    if not transition.is_valid:
        first = transition.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    _working_revision(root, revision_provider)
    prepared = prepare_checkpoint(root, changes, adapter=adapter or runtime)
    checkpoint_report = validate_checkpoint(prepared)
    if not checkpoint_report.is_valid:
        first = checkpoint_report.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    commit = adapter or runtime
    try:
        commit_checkpoint(prepared, "orchestrator: advance lifecycle stage", adapter=commit)
    except Exception as error:
        raise ValidationError(f"checkpoint failed: {error}") from error
    result = check_status(root)
    if not result.is_valid:
        raise ValidationError(f"checkpoint status verification failed: {result.message}")
    return load_project(root)


def _accepted_result(root: Path, value: Any, approvals: Mapping[str, Any], provider_lock: ProviderLock, revision_provider: Any = None) -> _Accepted:
    if isinstance(value, StagedResult):
        work_item = value.result.sections.get("work_item") if isinstance(value.result.sections, Mapping) else None
        if not isinstance(work_item, str) or not work_item.strip():
            raise ValidationError("a staged result must declare work_item metadata")
        value = {"work_item": work_item, "staged": value}
    elif isinstance(value, (ContractResult, ProviderResult)):
        work_item = value.sections.get("work_item") if isinstance(value.sections, Mapping) else None
        if not isinstance(work_item, str) or not work_item.strip():
            raise ValidationError("a contract result must declare work_item metadata")
        value = {"work_item": work_item, "result": value}
    if not isinstance(value, Mapping):
        raise ValidationError("advance accepts only staged/validated result records")
    work_item = value.get("work_item")
    if not isinstance(work_item, str) or not work_item.strip():
        raise ValidationError("accepted result must name a work_item")
    source = value.get("staged", value.get("result", value))
    contract_value = value.get("contract")
    contract: CapabilityContract | None = None
    capability = value.get("capability")
    if isinstance(source, (ContractResult, ProviderResult)):
        capability = source.capability
    elif isinstance(source, StagedResult):
        if Path(source.root).resolve() != Path(root).resolve():
            raise ValidationError("staged result root does not match project root")
        capability = source.result.capability
    elif isinstance(source, Mapping):
        capability = source.get("capability", capability)
    if not isinstance(capability, str) or not capability.strip():
        raise ValidationError(f"result for {work_item} must declare a capability contract")
    if capability not in provider_lock.capabilities:
        raise ValidationError(f"capability {capability} is not present in the provider lock")
    declared_version = value.get("contract_version")
    if declared_version is None and isinstance(source, Mapping):
        declared_version = source.get("contract_version")
    if declared_version is not None and declared_version != 1:
        raise ValidationError(f"unsupported capability contract version for {capability}: {declared_version!r}")
    authoritative_contract = load_contract(capability, 1)
    if contract_value is not None:
        if isinstance(contract_value, CapabilityContract):
            if _contract_fingerprint(contract_value) != _contract_fingerprint(authoritative_contract):
                raise ValidationError("supplied capability contract does not match the pinned contract")
        elif not isinstance(contract_value, str) or contract_value != capability:
            raise ValidationError("supplied capability contract is not the pinned contract")
    contract = authoritative_contract
    if contract.version != 1 or (declared_version is not None and contract.version != declared_version):
        raise ValidationError(f"result contract version does not match the pinned version for {capability}")
    resolution = resolve_provider(contract, provider_lock, {})
    if resolution.status != "resolved":
        raise ValidationError(f"provider for {capability} is not resolved: {resolution.status}")
    if not all(isinstance(item, str) and item.strip() for item in (resolution.provider, resolution.source, resolution.revision)):
        raise ValidationError(f"provider resolution for {capability} is incomplete")
    result_provider = _result_provider(source)
    if not result_provider:
        raise ValidationError(f"provider result for {work_item} has no provider identity")
    if result_provider != resolution.provider:
        raise ValidationError(f"provider result for {work_item} does not match the locked provider")
    if isinstance(source, Mapping):
        for field, expected in (("provider_source", resolution.source), ("provider_revision", resolution.revision)):
            supplied = source.get(field)
            if supplied is not None and supplied != expected:
                raise ValidationError(f"provider result for {work_item} has mismatched {field}")
    staging_source = source
    if isinstance(source, Mapping) and "provider" not in source and "provider_id" in source:
        staging_source = dict(source)
        staging_source["provider"] = source["provider_id"]
    staged = source if isinstance(source, StagedResult) else stage_result(staging_source, root, contract)
    if _result_provider(staged) != resolution.provider:
        raise ValidationError(f"normalized provider result for {work_item} does not match the locked provider")
    try:
        if Path(staged.root).resolve() != Path(root).resolve():
            raise ValidationError("staged result root does not match project root")
    except OSError as error:
        raise ValidationError(f"staged result root cannot be resolved: {error}") from error
    if staged.contract_id != contract.id or staged.contract_version != contract.version:
        raise ValidationError("staged result contract binding does not match the resolved contract")
    expected_scope = tuple(str(value) for value in contract.dispatch.get("writable_scope", ()))
    if tuple(staged.writable_scope) != expected_scope:
        raise ValidationError("staged result writable scope does not match the resolved contract")
    supplied_fingerprint = value.get("fingerprint")
    if supplied_fingerprint is not None and supplied_fingerprint != staged_fingerprint(staged):
        raise ValidationError("staged result fingerprint mismatch")
    approval = approvals.get(work_item) or (approvals.get(staged.result.capability) if approvals else None)
    if approval is not None and not isinstance(approval, ApprovalResult) and not _is_task8_approval(approval):
        raise ValidationError("approval must be an external approval record")
    validation_approval = approval
    if isinstance(approval, Approval) and staged.result.status == "partial":
        validation_approval = ApprovalResult(
            decision=approval.decision,
            actor=approval.actor,
            role=approval.role,
            fingerprint=staged_fingerprint(staged),
        )
    report = validate_staged_result(staged, contract, validation_approval)
    if not report.is_valid:
        first = report.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")
    required_gates = tuple(contract.integration.get("human_approvals", ())) if contract is not None else ()
    if required_gates:
        if not isinstance(approval, Approval):
            raise ValidationError(f"approval required for contract gate {required_gates[0]}")
        if approval.gate not in required_gates:
            raise ValidationError(f"approval gate {approval.gate!r} is not accepted by the contract")
        payload = staged_fingerprint(staged).encode("utf-8")
        role_map = contract.integration.get("human_approval_roles", {})
        expected_role = role_map.get(approval.gate, approval.role) if isinstance(role_map, Mapping) else approval.role
        approval_report = verify_approval(approval, payload, expected_role, approval.gate)
        if not approval_report.is_valid:
            first = approval_report.findings[0]
            raise ValidationError(f"approval: {first.code}: {first.message}")
    if staged.result.status != "completed":
        approved_partial = staged.result.status == "partial" and (
            isinstance(approval, ApprovalResult) and approval.approved
            or isinstance(approval, Approval) and approval.decision == "approved"
        )
        if not approved_partial:
            raise ValidationError(f"provider result for {work_item} is not completed or explicitly approved partial")
        if isinstance(approval, ApprovalResult) and approval.fingerprint != staged_fingerprint(staged):
            raise ValidationError(f"stale approval for {work_item}")
    expected_revision = value.get("base_revision", staged.result.base_revision)
    if expected_revision is None:
        raise ValidationError(f"provider result for {work_item} has no base revision")
    if str(expected_revision).strip().lower() != _working_revision(root, revision_provider):
        raise ValidationError(f"stale provider result for {work_item}")
    return _Accepted(work_item, staged, contract, approval)


def _is_task8_approval(value: Any) -> bool:
    return isinstance(value, Approval)


def _contract_fingerprint(contract: CapabilityContract) -> str:
    document = {
        "id": contract.id,
        "version": contract.version,
        "dispatch": _plain_contract_value(contract.dispatch),
        "result": _plain_contract_value(contract.result),
        "integration": _plain_contract_value(contract.integration),
        "parallelism": _plain_contract_value(contract.parallelism),
        "missing_provider_policy": contract.missing_provider_policy,
        "applicable_when": _plain_contract_value(contract.applicable_when),
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plain_contract_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_contract_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_contract_value(item) for item in value]
    return value


def _result_provider(source: Any) -> str | None:
    if isinstance(source, (StagedResult, ContractResult, ProviderResult)):
        result = source.result if isinstance(source, StagedResult) else source
        return result.provider.strip() if isinstance(result.provider, str) and result.provider.strip() else None
    if isinstance(source, Mapping):
        value = source.get("provider_id", source.get("provider"))
        return value.strip() if isinstance(value, str) and value.strip() else None
    return None


def _verify_requested_standard(value: Path | StandardIdentity | None) -> StandardIdentity:
    if value is None:
        return verify_standard(_SKILL_ROOT)
    if isinstance(value, StandardIdentity):
        verified = verify_standard(_SKILL_ROOT)
        if value != verified:
            raise ValidationError("provided standard identity does not match the pinned standard")
        return verified
    path = Path(value)
    if path.is_file():
        raise ValidationError("--standard requires a verified pinned skill/package root; standalone standard documents are rejected")
    if not path.is_dir():
        raise ValidationError("--standard requires a verified pinned skill/package root directory")
    return verify_standard(path)


def _verify_lock(lock: ProviderLock) -> None:
    report = verify_provider_lock(lock)
    if not report.is_valid:
        first = report.findings[0]
        raise ValidationError(f"provider lock {first.code}: {first.message}")


def _canonical_project_root(root: Path) -> Path:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValidationError(f"project root is not a directory: {root}")
    if not (root / ".git").exists():
        raise ValidationError("managed project must be a Git repository")
    return root


def _acquire_init_lock(root: Path) -> _OwnedFile:
    path = root / ".ai-sdlc-init.lock"
    fd = -1
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        identity = os.fstat(fd)
    except OSError as error:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise ValidationError(f"project initialization is already in progress: {path}") from error
    os.close(fd)
    return _owned_from_identity(path, identity, b"")


def _release_init_lock(owned: _OwnedFile) -> None:
    _remove_owned_file(owned)


def _exclusive_install(source: Path, destination: Path) -> _OwnedFile:
    """Install one staged file without replacing a concurrently created file."""
    data = source.read_bytes()
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(destination), flags)
    identity: os.stat_result | None = None
    try:
        identity = os.fstat(fd)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if identity is not None:
            _remove_created_inode(destination, identity)
        raise
    else:
        os.close(fd)
        if identity is None:
            raise OSError(f"cannot identify installed file: {destination}")
        return _owned_from_identity(destination, identity, data)


def _owned_from_identity(path: Path, identity: os.stat_result, data: bytes) -> _OwnedFile:
    return _OwnedFile(path, int(identity.st_dev), int(identity.st_ino), len(data), hashlib.sha256(data).hexdigest())


def _remove_created_inode(path: Path, identity: os.stat_result) -> None:
    """Remove a failed O_EXCL target only while it is still our inode."""
    try:
        if path.is_symlink() or not path.is_file():
            return
        current = path.stat()
        if (int(current.st_dev), int(current.st_ino)) != (int(identity.st_dev), int(identity.st_ino)):
            return
        path.unlink()
    except OSError:
        return


def _remove_owned_file(owned: _OwnedFile) -> None:
    try:
        path = owned.path
        if not path.is_file() or path.is_symlink():
            return
        stat = path.stat()
        if (int(stat.st_dev), int(stat.st_ino)) != (owned.device, owned.inode):
            return
        data = path.read_bytes()
        if len(data) != owned.size or hashlib.sha256(data).hexdigest() != owned.digest:
            return
        path.unlink()
    except OSError:
        return


def _initial_documents(identity: StandardIdentity, lock: ProviderLock) -> dict[str, Mapping[str, Any]]:
    stages: list[dict[str, Any]] = []
    for index, (stage_id, title) in enumerate(STAGE_DEFINITIONS):
        status = "active" if index == 0 else "planning" if index == 1 else "not_started"
        stage: dict[str, Any] = {"id": stage_id, "title": title, "status": status, "depends_on": [STAGE_DEFINITIONS[index - 1][0]] if index else []}
        if index == 0:
            stage.update({
                "entry_criteria": [{"id": "EC-01", "text": "Исходные материалы зарегистрированы", "status": "verified"}],
                "exit_criteria": [{"id": "XC-01", "text": "Анализ продукта проверен", "status": "pending"}],
                "work_items": [{"id": "TASK-001", "title": "Зарегистрировать и проанализировать исходные материалы", "status": "ready", "executor": "coordinator", "depends_on": [], "produces": [], "scope": {"artifacts": [], "paths": ["docs/product-definition/"]}}],
            })
        elif index == 1:
            stage["work_items"] = [{"id": "TASK-002", "title": "Определить границы системы", "status": "planned", "executor": "coordinator", "depends_on": ["TASK-001"], "produces": [], "scope": {"artifacts": [], "paths": ["docs/system-context/"]}}]
        stages.append(stage)
    return {
        _PLAN: {"schema_version": 1, "project_id": "PROJECT-001", "planning_mode": "rolling_wave", "stages": stages},
        _STATE: {"schema_version": 1, "project_id": "PROJECT-001", "lifecycle_mode": "initial-development", "current_stage": "STAGE-01", "focus_work_items": ["TASK-001"], "next_actions": [{"work_item": "TASK-001", "action": "Зарегистрировать и проанализировать исходные материалы"}], "updated_at": "1970-01-01", "updated_by": "orchestrator", "active_change": None, "current_baseline": None, "last_approved_artifact": None},
        _ARTIFACTS: {"artifacts": []},
        _SOURCES: {"sources": []},
    }


def _identity_document(identity: StandardIdentity) -> Mapping[str, Any]:
    return {"version": identity.version, "updated_at": identity.updated_at, "sha256": identity.sha256}


def _lock_document(lock: ProviderLock) -> Mapping[str, Any]:
    return {"schema_version": lock.schema_version, "capabilities": dict(lock.capabilities)}


def _yaml_bytes(document: Mapping[str, Any]) -> bytes:
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValidationError(f"canonical YAML cannot be read: {path}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"canonical YAML must contain an object: {path}")
    return value


def _verify_identity_document(document: Mapping[str, Any], expected: StandardIdentity) -> None:
    actual = tuple(document.get(name) for name in ("version", "updated_at", "sha256"))
    wanted = (expected.version, expected.updated_at, expected.sha256)
    if actual != wanted:
        raise ValidationError("standard identity is tampered or does not match the pinned standard")


def _validate_optional_trace(root: Path, snapshot: ProjectSnapshot) -> None:
    """Validate a persisted trace projection when present, without trusting it."""
    for relative in ("docs/project-control/traceability.yaml", "docs/traceability.yaml"):
        path = root / relative
        if not path.is_file():
            continue
        document = _read_yaml(path)
        try:
            nodes = tuple(TraceNode(**dict(value)) for value in document.get("nodes", ()))
            edges = tuple(TraceEdge(**dict(value)) for value in document.get("edges", document.get("links", ())))
        except (TypeError, ValueError) as error:
            raise ValidationError(f"traceability is malformed: {error}") from error
        report = validate_traceability(TraceGraph(nodes=nodes, edges=edges))
        if not report.is_valid:
            first = report.findings[0]
            raise ValidationError(f"{first.code}: {first.message}")
        registered = {source.id for source in snapshot.sources} | {artifact.id for artifact in snapshot.artifacts}
        unknown = sorted(node.id for node in nodes if node.id not in registered)
        if unknown:
            raise ValidationError(f"unregistered trace source or artifact: {unknown[0]}")


def _reconcile_source_files(root: Path, snapshot: ProjectSnapshot) -> None:
    source_dir = root / "docs/product-definition/sources"
    if not source_dir.is_dir():
        return
    register = source_dir / "source-register.yaml"
    registered: set[Path] = {register.resolve()}
    for source in snapshot.sources:
        location = Path(source.location)
        if not location.is_absolute():
            location = root / location
        try:
            registered.add(location.resolve())
        except OSError:
            continue
    for path in source_dir.rglob("*"):
        if path.is_file() and path.resolve() not in registered:
            raise ValidationError(f"unregistered source file: {path.relative_to(root).as_posix()}")


def _validate_plan_state_consistency(snapshot: ProjectSnapshot) -> None:
    stages = snapshot.plan.stages
    current_index = next((index for index, stage in enumerate(stages) if stage.id == snapshot.state.current_stage), None)
    if current_index is None:
        raise ValidationError("state/plan contradiction: current stage is unknown")
    current = stages[current_index]
    if current.status == "completed" and current_index != len(stages) - 1:
        raise ValidationError("state/plan contradiction: current stage is completed")
    if any(stage.status != "completed" for stage in stages[:current_index]):
        raise ValidationError("state/plan contradiction: preceding stage is not completed")
    by_id = {work.id: work for stage in stages for work in stage.work_items}
    for work_id in snapshot.state.focus_work_items:
        if work_id not in by_id or by_id[work_id].status in {"done", "cancelled"}:
            raise ValidationError(f"state/plan contradiction: focus work item {work_id} is not active")


def _work_capability(work: WorkItem) -> str | None:
    value = work.plan or ""
    match = re.search(r"(?:capability|contract)\s*[:=]\s*([A-Za-z0-9_.-]+)", value)
    return match.group(1) if match else None


def _reject_protected_paths(staged: StagedResult) -> None:
    for path in staged.result.changed_paths or tuple(staged.result.files):
        normalized = str(path).replace("\\", "/").lstrip("./")
        if normalized in _PROTECTED:
            raise ValidationError(f"provider result cannot modify canonical path: {normalized}")


def _validate_registered_evidence(staged: StagedResult, snapshot: ProjectSnapshot) -> None:
    source_ids = {source.id for source in snapshot.sources}
    text = repr(staged.result.evidence)
    for token in re.findall(r"SRC-[A-Za-z0-9_-]+", text):
        if token not in source_ids:
            raise ValidationError(f"unregistered source in provider evidence: {token}")


def _coordinator_package_identity(staged: StagedResult) -> str:
    """Derive the immutable deployment-package identity from staged bytes."""
    entries: list[tuple[str, str]] = []
    for path, value in sorted(staged.result.files.items(), key=lambda item: str(item[0]).casefold()):
        content = _content_bytes(value)
        if content is None:
            raise ValidationError("deployment package identity cannot include deleted files")
        normalized = str(path).replace("\\", "/").lstrip("./")
        entries.append((normalized, hashlib.sha256(content).hexdigest()))
    if not entries:
        raise ValidationError("deployment package identity requires a non-empty package")
    payload = json.dumps(
        {"capability": staged.result.capability, "produced": list(staged.result.produced), "files": entries},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_pass_evidence(evidence: Mapping[str, Any], key: str, capability: str) -> None:
    value = evidence.get(key)
    if not isinstance(value, Mapping):
        raise ValidationError(f"{capability} evidence {key} must be a structured pass result")
    status = value.get("status", value.get("outcome", value.get("result")))
    if not isinstance(status, str) or status.strip().casefold() not in {"pass", "passed", "success", "successful", "ok"}:
        raise ValidationError(f"{capability} evidence {key} must have a passing outcome")


def _require_record(
    evidence: Mapping[str, Any], key: str, capability: str, *, human: bool = False, record_id_required: bool = True,
) -> Mapping[str, Any]:
    value = evidence.get(key)
    if not isinstance(value, Mapping):
        raise ValidationError(f"{capability} evidence {key} must be a structured record")
    record_id = value.get("id", value.get("record_id"))
    if record_id_required and (not isinstance(record_id, str) or not record_id.strip()):
        raise ValidationError(f"{capability} evidence {key} must identify a record")
    provenance = value.get("provenance")
    if not isinstance(provenance, str) or not provenance.strip():
        raise ValidationError(f"{capability} evidence {key} must declare provenance")
    if human:
        actor = value.get("actor")
        if not isinstance(actor, str) or not actor.strip():
            raise ValidationError(f"{capability} evidence {key} must declare a human actor")
        actor_type = value.get("actor_type")
        if actor_type is not None and (not isinstance(actor_type, str) or actor_type.strip().casefold() != "human"):
            raise ValidationError(f"{capability} evidence {key} actor_type must be human")
        if actor.strip().casefold() in {"automation", "automation-adapter", "automated", "system", "bot"}:
            raise ValidationError(f"{capability} evidence {key} actor must be human-originated")
        if not provenance.strip().casefold().startswith("human"):
            raise ValidationError(f"{capability} evidence {key} must declare human provenance")
    return value


def _validate_delivery_semantics(
    staged: StagedResult, contract: CapabilityContract | None, snapshot: ProjectSnapshot,
) -> None:
    """Enforce delivery/operations invariants before canonical mutation."""
    if contract is None:
        return
    capability = contract.id
    evidence = staged.result.evidence
    if capability in _PACKAGE_CAPABILITIES:
        if capability == "delivery.prepare":
            # The provider's declaration is evidence only; the coordinator
            # computes and persists the package identity from staged bytes.
            return
        expected = snapshot.state.deployment_package_identity
        if not isinstance(expected, str) or not _PACKAGE_IDENTITY_RE.fullmatch(expected):
            raise ValidationError("deployment package identity is not bound by the coordinator")
        supplied = evidence.get("package-identity")
        if supplied != expected:
            raise ValidationError("deployment package identity does not match the coordinator-bound package")

    for key in _PASS_EVIDENCE:
        if key in evidence and capability in {
            "delivery.health-check", "delivery.acceptance", "delivery.production-deploy", "operations.monitor",
        }:
            _require_pass_evidence(evidence, key, capability)

    if capability == "delivery.acceptance":
        report = _require_record(evidence, "acceptance-report", capability, human=True, record_id_required=False)
        decision = report.get("decision", report.get("outcome"))
        if not isinstance(decision, str) or not decision.strip():
            raise ValidationError("delivery.acceptance acceptance-report must declare a decision")
        if decision.strip().casefold() in {"rejected", "denied", "failed"}:
            raise ValidationError("delivery.acceptance human acceptance decision is rejected")
    elif capability == "operations.monitor":
        _require_record(evidence, "incident-record", capability)
        _require_record(evidence, "feedback-record", capability)
    elif capability == "operations.next-change":
        _require_record(evidence, "incident-record", capability)
        _require_record(evidence, "feedback-record", capability)
        _require_record(evidence, "next-change-request", capability, human=True)


def _validate_produced_artifacts(
    staged: StagedResult, work: WorkItem, snapshot: ProjectSnapshot, contract: CapabilityContract | None = None,
) -> None:
    """Bind provider output IDs and paths to the coordinator-owned registry."""
    produced = tuple(staged.result.produced)
    expected_ids = tuple(work.produces)
    if not expected_ids:
        if produced:
            raise ValidationError(f"provider result for {work.id} declares artifacts but the plan declares none")
        return
    if produced != expected_ids:
        raise ValidationError(f"provider result for {work.id} produced IDs do not match the planned artifacts")
    artifacts = {artifact.id: artifact for artifact in snapshot.artifacts}
    expected = []
    for artifact_id in expected_ids:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise ValidationError(f"unregistered produced artifact: {artifact_id}")
        expected.append(artifact)
    expected_kinds = {str(kind).replace("-", "_") for kind in (contract.result.get("expected_artifacts", ()) if contract else ())}
    mapped_kinds = {str(kind).replace("-", "_") for kind in (contract.integration.get("mappings", ()) if contract else ())}
    if expected_kinds and any(artifact.kind.replace("-", "_") not in expected_kinds for artifact in expected):
        raise ValidationError(f"provider result for {work.id} produced kinds outside the contract mapping")
    if mapped_kinds and any(artifact.kind.replace("-", "_") not in mapped_kinds for artifact in expected):
        raise ValidationError(f"provider result for {work.id} produced kinds outside the integration mapping")
    actual_paths = tuple(sorted(str(path).replace("\\", "/") for path in staged.result.files))
    expected_paths = tuple(sorted(artifact.path.replace("\\", "/") for artifact in expected))
    if actual_paths != expected_paths:
        raise ValidationError(f"provider output paths do not match registered artifact paths for {work.id}")
    if any(_content_bytes(value) is None for value in staged.result.files.values()):
        raise ValidationError(f"provider result for {work.id} cannot delete a registered produced artifact")
    evidence_links = staged.result.evidence.get("traceability")
    if not isinstance(evidence_links, (list, tuple)) or not evidence_links:
        raise ValidationError(f"provider result for {work.id} is missing traceability evidence")
    registered_ids = {source.id for source in snapshot.sources} | set(artifacts)
    for link in evidence_links:
        if not isinstance(link, Mapping):
            raise ValidationError(f"traceability evidence for {work.id} must contain link objects")
        source_id, target_id = link.get("source"), link.get("target")
        if source_id not in registered_ids or target_id not in expected_ids:
            raise ValidationError(f"traceability evidence for {work.id} references an unregistered ID")


def _mark_route_criterion(plan: dict[str, Any], work: WorkItem, accepted: _Accepted, approved: bool) -> None:
    """Promote only criteria and evidence explicitly authorized by the work item."""
    work_id = work.id
    if accepted.contract is None:
        return
    declared = set(work.criterion_ids)
    criteria = [criterion for stage in plan.get("stages", ()) for criterion in stage.get("exit_criteria", ())]
    legacy_matches = [criterion for criterion in criteria if criterion.get("id") == work_id]
    if legacy_matches and not declared:
        raise ValidationError(f"work item {work_id} does not declare criterion IDs")
    if not declared:
        return
    # Traceability is coordinator-validated against the canonical artifact DAG;
    # it is a work-item requirement, not a retroactive C4 v1 contract input.
    allowed_evidence = set(accepted.contract.result.get("required_evidence", ())) | {"traceability"}
    missing_evidence = set(work.criterion_evidence) - allowed_evidence
    if missing_evidence:
        raise ValidationError(f"work item {work_id} declares evidence absent from its contract")
    for evidence_key in work.criterion_evidence:
        if not accepted.staged.result.evidence.get(evidence_key):
            raise ValidationError(f"work item {work_id} is missing declared criterion evidence: {evidence_key}")
    gates = tuple(accepted.contract.integration.get("human_approvals", ()))
    has_gate = bool(gates)
    target_status = "verified" if not has_gate or approved else "in_review"
    required_roles = accepted.contract.integration.get("human_approval_roles", {})
    matched: set[str] = set()
    for stage in plan.get("stages", ()):
        for criterion in stage.get("exit_criteria", ()):
            if criterion.get("id") in declared:
                matched.add(str(criterion["id"]))
                gate = criterion.get("approval_gate")
                role = criterion.get("approval_role")
                if has_gate and (gate not in gates or not isinstance(role, str) or not role.strip()):
                    raise ValidationError(f"criterion for {work_id} does not declare the contract human gate")
                if not has_gate and (gate is not None or role is not None):
                    raise ValidationError(f"criterion for {work_id} declares a human gate absent from the contract")
                expected_role = required_roles.get(gate) if isinstance(required_roles, Mapping) else None
                if expected_role and role != expected_role:
                    raise ValidationError(f"criterion for {work_id} role does not match the contract human gate")
                criterion["status"] = target_status
    if matched != declared:
        raise ValidationError(f"work item {work_id} declares an unknown criterion")


def _promoted_trace_document(root: Path, snapshot: ProjectSnapshot, accepted: Sequence[_Accepted]) -> Mapping[str, Any] | None:
    """Persist only links declared by accepted provider evidence."""
    links: list[dict[str, str]] = []
    trace_path = root / _TRACE
    if trace_path.is_file():
        document = _read_yaml(trace_path)
        links.extend(dict(edge) for edge in document.get("edges", ()) if isinstance(edge, Mapping))
    for item in accepted:
        values = item.staged.result.evidence.get("traceability", ())
        for value in values:
            edge = {"source": str(value["source"]), "target": str(value["target"]), "relation": str(value.get("relation", "traces_to"))}
            if edge not in links:
                links.append(edge)
    if not links:
        return None
    nodes = [
        {"id": source.id, "category": "source", "kind": source.kind, "status": source.status, "path": source.location}
        for source in snapshot.sources
    ] + [
        {"id": artifact.id, "category": "artifact", "kind": artifact.kind, "status": artifact.status, "path": artifact.path}
        for artifact in snapshot.artifacts
    ]
    return {"nodes": nodes, "edges": links}


def _trace_document_for_snapshot(snapshot: ProjectSnapshot, edges: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    return {
        "nodes": [{"id": source.id, "category": "source", "kind": source.kind, "status": source.status, "path": source.location} for source in snapshot.sources] + [
            {"id": artifact.id, "category": "artifact", "kind": artifact.kind, "status": artifact.status, "path": artifact.path}
            for artifact in snapshot.artifacts
        ],
        "edges": [dict(edge) for edge in edges],
    }


def _validate_promoted_trace(
    candidate: ProjectSnapshot, document: Mapping[str, Any], before: ProjectSnapshot, accepted: Sequence[_Accepted],
) -> None:
    """Validate provider-declared provenance against the canonical work DAG."""
    known = {source.id for source in candidate.sources} | {artifact.id for artifact in candidate.artifacts}
    artifacts = {artifact.id: artifact for artifact in candidate.artifacts}
    work = {item.id: item for stage in before.plan.stages for item in stage.work_items}
    by_target: dict[str, list[Mapping[str, Any]]] = {}
    edges: list[TraceEdge] = []
    for value in document.get("edges", ()):
        if not isinstance(value, Mapping):
            raise ValidationError("traceability edge must be an object")
        source, target, relation = value.get("source"), value.get("target"), value.get("relation")
        if not all(isinstance(item, str) and item for item in (source, target, relation)):
            raise ValidationError("traceability edge must declare source, target, and relation")
        if relation != "traces_to" or source == target or source not in known or target not in known:
            raise ValidationError("traceability edge has invalid relation or provenance IDs")
        by_target.setdefault(target, []).append(value)
        edges.append(TraceEdge(source, target, relation))
    nodes = tuple(TraceNode(source.id, "source", source.kind, source.status, source.location) for source in candidate.sources) + tuple(
        TraceNode(artifact.id, "artifact", artifact.kind, artifact.status, artifact.path) for artifact in candidate.artifacts
    )
    report = validate_traceability(TraceGraph(nodes=nodes, edges=tuple(edges)))
    if not report.is_valid:
        raise ValidationError(f"{report.findings[0].code}: {report.findings[0].message}")
    for item in accepted:
        planned = work[item.work_item]
        expected_sources = set()
        for dependency in planned.depends_on:
            expected_sources.update(work[dependency].produces)
        if not expected_sources:
            expected_sources = {source.id for source in candidate.sources}
        for target in planned.produces:
            declarations = by_target.get(target, ())
            declared_sources = {declaration["source"] for declaration in declarations}
            valid_fan_in = len(expected_sources) > 1 and declared_sources == expected_sources and len(declarations) == len(expected_sources)
            valid_single = len(expected_sources) <= 1 and len(declarations) == 1 and declared_sources.issubset(expected_sources)
            if not (valid_fan_in or valid_single):
                raise ValidationError(f"traceability evidence for {item.work_item} has wrong predecessor provenance")
    if any(item.contract and item.contract.id == "architecture.baseline" for item in accepted):
        ordered = [source.id for source in candidate.sources[:1]]
        for stage in before.plan.stages:
            for item in stage.work_items:
                ordered.extend(item.produces)
        report = validate_traceability(TraceGraph(nodes=nodes, edges=tuple(edges)), tuple(ordered))
        if not report.is_valid:
            raise ValidationError(f"{report.findings[0].code}: {report.findings[0].message}")


def _content_bytes(value: Any) -> bytes | None:
    if isinstance(value, Mapping) and value.get("delete") is True:
        return None
    if isinstance(value, Mapping):
        value = value.get("content", value.get("value", b""))
    if value is None:
        return None
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def _mark_work_done(plan: dict[str, Any], state: dict[str, Any], work_id: str) -> None:
    for stage in plan.get("stages", []):
        for work in stage.get("work_items", []):
            if work.get("id") == work_id:
                work["status"] = "done"
                stage["status"] = "in_review" if stage.get("exit_criteria") else stage.get("status", "active")
    state["focus_work_items"] = [value for value in state.get("focus_work_items", []) if value != work_id]
    state["next_actions"] = []


def _validate_completion_transition(
    before: ProjectSnapshot,
    after: ProjectSnapshot,
    work_ids: tuple[str, ...],
    *,
    approval: Any | None = None,
    payload: bytes | None = None,
):
    """Validate a completion as the prescribed ready→in_progress→review→done path."""
    criterion_ids = {
        criterion_id
        for stage in after.plan.stages
        for work in stage.work_items
        if work.id in work_ids
        for criterion_id in work.criterion_ids
    }
    current = before
    for status in ("in_progress", "review", "done"):
        stages: list[Stage] = []
        for stage in current.plan.stages:
            works = tuple(replace(work, status=status) if work.id in work_ids else work for work in stage.work_items)
            criteria = tuple(
                replace(
                    criterion,
                    status=(
                        "in_review"
                        if status != "done" and criterion.id in criterion_ids and criterion.status == "pending"
                        else next(
                            (
                                target.status
                                for target_stage in after.plan.stages
                                for target in (*target_stage.entry_criteria, *target_stage.exit_criteria)
                                if target.id == criterion.id
                            ),
                            criterion.status,
                        )
                        if status == "done" and criterion.id in criterion_ids
                        else criterion.status
                    ),
                )
                if criterion.id in criterion_ids
                else criterion
                for criterion in stage.exit_criteria
            )
            stages.append(replace(stage, work_items=works, exit_criteria=criteria))
        intermediate = replace(current, plan=replace(current.plan, stages=tuple(stages)))
        report = validate_transition(
            current,
            intermediate,
            approval=approval if status == "done" else None,
            payload=payload if status == "done" else None,
        )
        if not report.is_valid:
            return report
        current = intermediate
    # The final status and any stage/state changes are checked together.  The
    # work statuses already match, so no invalid direct jump is reported.
    return validate_transition(current, after, approval=approval, payload=payload)


def _candidate_snapshot(root: Path, changes: Mapping[str, Any]) -> ProjectSnapshot:
    temp = Path(tempfile.mkdtemp(prefix=".ai-sdlc-candidate-", dir=str(root)))
    try:
        for relative in (_PLAN, _STATE, _ARTIFACTS, _SOURCES):
            source = root / relative
            destination = temp / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(changes.get(relative, source.read_bytes()))
        snapshot = load_project(temp)
        report = validate_snapshot(snapshot)
        if not report.is_valid:
            first = report.findings[0]
            raise ValidationError(f"{first.code}: {first.message}")
        return snapshot
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _candidate_status(root: Path, changes: Mapping[str, Any]) -> bytes:
    temp = Path(tempfile.mkdtemp(prefix=".ai-sdlc-status-", dir=str(root)))
    try:
        for relative in (_PLAN, _STATE, _ARTIFACTS, _SOURCES):
            source = root / relative
            destination = temp / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(changes.get(relative, source.read_bytes()))
        return generate_status(temp).read_bytes()
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _working_revision(root: Path, revision_provider: Any = None) -> str:
    if revision_provider is not None:
        try:
            value = revision_provider(root) if callable(revision_provider) else revision_provider
        except Exception as error:
            raise ValidationError(f"cannot resolve project revision: {error}") from error
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value.strip()):
            raise ValidationError("revision provider must return a resolved commit SHA")
        return value.strip().lower()
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValidationError(f"cannot resolve Git revision: {error}") from error
    revision = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValidationError("Git revision is not a resolved commit SHA")
    return revision.lower()


__all__ = ("Action", "ActionSet", "ResumeContext", "advance", "advance_stage", "initialize_project", "propose_next_actions", "resume_project")
