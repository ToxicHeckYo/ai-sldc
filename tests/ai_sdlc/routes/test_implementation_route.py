"""Task 16: the governed parallel implementation route.

This fixture drives the real scheduler, runtime port, staging/integration
boundary, checkpoint coordinator, and canonical orchestrator.  The route is
deliberately small, but every promoted item has evidence bound to its pinned
contract and exact base revision.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.adapters import build_dispatch, normalize_provider_output  # noqa: E402
from ai_sdlc.contracts import load_contract  # noqa: E402
from ai_sdlc.errors import ValidationError  # noqa: E402
from ai_sdlc.integration import integrate_staged_result, stage_result, tree_hash, validate_staged_result  # noqa: E402
from ai_sdlc.model import STAGE_DEFINITIONS, load_project  # noqa: E402
from ai_sdlc.orchestrator import advance, initialize_project, resume_project  # noqa: E402
from ai_sdlc.providers import ProviderLock  # noqa: E402
from ai_sdlc.runtime import DispatchRequest, MockRuntime, ProviderResult, WorkerLifecyclePort  # noqa: E402
from ai_sdlc.scheduler import build_safe_batch, conflicts, ready_work, validate_dag  # noqa: E402


REVISION = "1" * 40
ROUTE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "routes" / "implementation-project"
CAPABILITIES = {
    "planning.implementation": "builtin-planning-implementation",
    "implementation.tdd": "builtin-implementation-tdd",
    "implementation.execute": "builtin-implementation-execute",
    "quality.code-review": "builtin-quality-code-review",
    "quality.verify": "builtin-quality-verify",
}


class ConfirmedCheckpoint:
    def __init__(self) -> None:
        self.identities: list[str] = []

    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> str:
        assert paths
        identity = f"confirmed-task16-{len(self.identities) + 1:02d}"
        self.identities.append(identity)
        return identity


def _lock() -> ProviderLock:
    metadata = {
        "builtin-planning-implementation": ("builtin://planning-implementation", "1", "sha256:" + "a" * 64),
        "builtin-implementation-tdd": ("builtin://implementation-tdd", "1", "sha256:" + "a" * 64),
    }
    return ProviderLock(
        schema_version=1,
        capabilities={
            capability: {
                "contract_version": 1,
                "provider": provider,
                "source": metadata.get(provider, (f"builtin://{provider}", "1", "sha256:" + "a" * 64))[0],
                "revision": metadata.get(provider, ("", "1", ""))[1],
                "integrity": metadata.get(provider, ("", "", "sha256:" + "a" * 64))[2],
                "status": "enabled",
            }
            for capability, provider in CAPABILITIES.items()
        },
    )


def _documents() -> tuple[dict, dict, dict, dict]:
    artifacts = [
        ("ART-PLAN", "technical_specification_manifest", "staging/implementation/plan.yaml"),
        ("ART-API", "test_report", "staging/implementation/api-report.md"),
        ("ART-DATA", "test_report", "staging/implementation/data-report.md"),
        ("ART-INTEGRATION", "test_report", "staging/implementation/integration-report.md"),
        ("ART-REVIEW", "test_report", "staging/implementation/review-report.md"),
        ("ART-VERIFY", "test_report", "staging/implementation/verification-report.md"),
    ]
    artifact_doc = {"artifacts": [{"id": id_, "kind": kind, "path": path, "status": "in_review"} for id_, kind, path in artifacts]}
    source_doc = {"sources": [{
        "id": "SRC-001", "kind": "technical_specification", "title": "Approved implementation design",
        "origin": "fixture", "received_at": "2026-11-04", "version": "1", "location": "docs/design.md", "status": "registered",
    }]}
    work_specs = [
        ("TASK-PLAN", "Create implementation plan", "planning.implementation", (), ("ART-PLAN",), ("docs/implementation/plan",)),
        ("TASK-API", "Implement API branch", "implementation.tdd", ("TASK-PLAN",), ("ART-API",), ("src/api",)),
        ("TASK-DATA", "Implement data branch", "implementation.tdd", ("TASK-PLAN",), ("ART-DATA",), ("src/data",)),
        ("TASK-CONFLICT", "Competing API implementation", "implementation.tdd", ("TASK-PLAN",), ("ART-API",), ("src/api",)),
        ("TASK-INTEGRATE", "Integrate implementation branches", "implementation.execute", ("TASK-API", "TASK-DATA"), ("ART-INTEGRATION",), ("src/integration",)),
        ("TASK-REVIEW", "Review integrated implementation", "quality.code-review", ("TASK-INTEGRATE",), ("ART-REVIEW",), ("src",)),
        ("TASK-VERIFY", "Verify implementation", "quality.verify", ("TASK-REVIEW",), ("ART-VERIFY",), ("src",)),
    ]
    work_items = [{
        "id": id_, "title": title, "status": "ready", "executor": "coordinator", "depends_on": list(deps),
        "produces": list(produces), "scope": {"artifacts": list(produces), "paths": list(paths)},
        "criterion_ids": [id_] + (["IMPLEMENTATION-COMPLETE"] if id_ == "TASK-INTEGRATE" else []), "criterion_evidence": ["traceability"], "plan": f"capability: {capability}",
    } for id_, title, capability, deps, produces, paths in work_specs]
    stages = []
    for index, (stage_id, title) in enumerate(STAGE_DEFINITIONS, start=1):
        stages.append({
            "id": stage_id, "title": title, "status": "active" if index == 1 else "not_started",
            "depends_on": [f"STAGE-{index - 1:02d}"] if index > 1 else [], "entry_criteria": [],
            "exit_criteria": ([{"id": id_, "text": title, "status": "pending"} for id_, title, *_ in work_specs] + [{"id": "IMPLEMENTATION-COMPLETE", "text": "Integrated implementation is verified", "status": "pending"}]) if index == 1 else [],
            "work_items": work_items if index == 1 else [],
        })
    plan_doc = {"schema_version": 1, "project_id": "IMPLEMENTATION-ROUTE", "planning_mode": "rolling_wave", "stages": stages}
    state_doc = {
        "schema_version": 1, "project_id": "IMPLEMENTATION-ROUTE", "lifecycle_mode": "initial-development",
        "current_stage": "STAGE-01", "focus_work_items": ["TASK-PLAN"],
        "next_actions": [{"work_item": "TASK-PLAN", "action": "Create implementation plan"}],
        "updated_at": "2026-11-04", "updated_by": "orchestrator", "active_change": None,
        "current_baseline": None, "last_approved_artifact": None,
    }
    return plan_doc, state_doc, artifact_doc, source_doc


def _materialize(root: Path) -> None:
    root.mkdir()
    (root / ".git").mkdir()
    (root / "docs").mkdir()
    shutil.copy2(ROUTE_FIXTURE / "design.md", root / "docs/design.md")
    initialize_project(root, SKILL_ROOT, _lock())
    plan, state, artifacts, sources = _documents()
    for relative, document in {
        "docs/project-control/project-plan.yaml": plan,
        "docs/project-control/project-state.yaml": state,
        "docs/artifact-register.yaml": artifacts,
        "docs/product-definition/sources/source-register.yaml": sources,
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    from ai_sdlc.status_view import generate_status
    generate_status(root)


def _staged(root: Path, capability: str, work_item: str, artifact: str, *, files: dict[str, str] | None = None, missing: str | None = None, status: str = "completed", review_decision: str | None = None, security_status: str | None = None, section_overrides: dict[str, str] | None = None, evidence_overrides: dict[str, str] | None = None):
    contract = load_contract(capability, 1)
    sections = {name: f"{work_item}:{name}:completed" for name in contract.result["required_sections"]}
    sections["work_item"] = work_item
    if capability == "implementation.tdd":
        sections.update(red="pytest command: pytest tests; output: assertion failed", green="pytest pass", verification="pytest exit_code:0")
    elif capability == "implementation.execute":
        sections.update(execution="pytest exit_code:0", integration="integration exit_code:0", verification="verified")
    elif capability == "quality.code-review":
        sections.update(review="reviewed", findings="blocking findings: none", decision="approved")
    elif capability == "quality.verify":
        sections.update(verification="verified", result="passed", evidence="tests and security passed")
    evidence = {name: f"{work_item}:{name}:evidence" for name in contract.result["required_evidence"]}
    if capability == "implementation.tdd":
        evidence.update({"failing-test": "command: pytest tests; output: assertion failed", "passing-test": "command: pytest tests; exit_code:0; output: pass"})
    elif capability == "implementation.execute":
        evidence.update({"execution": "command: pytest; exit_code:0", "integration": "command: integration tests; exit_code:0"})
    elif capability == "quality.code-review":
        evidence.update({"review": "review complete", "scope": "scope checked"})
    elif capability == "quality.verify":
        evidence.update({"tests": "command: pytest; exit_code:0", "build": "command: build; exit_code:0", "security": "passed", "review": "approved"})
    if review_decision is not None:
        sections["decision"] = review_decision
    if security_status is not None:
        evidence["security"] = security_status
    if section_overrides:
        sections.update(section_overrides)
    if evidence_overrides:
        evidence.update(evidence_overrides)
    predecessors = {
        "TASK-PLAN": ("SRC-001",), "TASK-API": ("ART-PLAN",), "TASK-DATA": ("ART-PLAN",),
        "TASK-CONFLICT": ("ART-PLAN",), "TASK-INTEGRATE": ("ART-API", "ART-DATA"),
        "TASK-REVIEW": ("ART-INTEGRATION",), "TASK-VERIFY": ("ART-REVIEW",),
    }
    evidence["traceability"] = [{"source": source, "target": artifact, "relation": "traces_to"} for source in predecessors[work_item]]
    if missing:
        evidence.pop(missing, None)
    registered = next(item for item in _documents()[2]["artifacts"] if item["id"] == artifact)
    paths = files or {registered["path"]: f"# {artifact}\n"}
    raw = {"provider": {"id": CAPABILITIES[capability], "revision": "1"}, "base_revision": REVISION, "outcome": {"completed": "complete", "failed": "failure"}.get(status, status), "sections": sections, "evidence": evidence, "files": {} if status == "failed" else paths, "produced": [] if status == "failed" else [artifact], "unresolved": [], "lifecycle_changes": [], "project_state_changes": [], "control_file_changes": []}
    request = build_dispatch(CAPABILITIES[capability], {name: f"fixture:{name}" for name in contract.dispatch["required_inputs"]}, base_revision=REVISION)
    runtime = MockRuntime({capability: {"capability": capability, "status": status, "payload": raw}})
    provider_result = runtime.invoke_skill(request)
    adapted = normalize_provider_output(CAPABILITIES[capability], provider_result.payload, base_revision=REVISION)
    staged = stage_result(adapted.contract_result, root, contract)
    report = validate_staged_result(staged, contract)
    if not report.is_valid:
        raise ValidationError(report.findings[0].message)
    probe = root.parent / f".integration-probe-{work_item}"
    (probe / "staging").mkdir(parents=True, exist_ok=True)
    integrate_staged_result(replace(staged, root=probe), probe, contract)
    return staged


def _advance_to_review(root: Path, checkpoint: ConfirmedCheckpoint) -> None:
    advance(root, [{"work_item": "TASK-PLAN", "staged": _staged(root, "planning.implementation", "TASK-PLAN", "ART-PLAN")}], adapter=checkpoint, revision_provider=REVISION)
    advance(root, [
        {"work_item": "TASK-API", "staged": _staged(root, "implementation.tdd", "TASK-API", "ART-API")},
        {"work_item": "TASK-DATA", "staged": _staged(root, "implementation.tdd", "TASK-DATA", "ART-DATA")},
    ], adapter=checkpoint, revision_provider=REVISION)
    advance(root, [{"work_item": "TASK-INTEGRATE", "staged": _staged(root, "implementation.execute", "TASK-INTEGRATE", "ART-INTEGRATION")}], adapter=checkpoint, revision_provider=REVISION)


def _advance_to_verify(root: Path, checkpoint: ConfirmedCheckpoint) -> None:
    _advance_to_review(root, checkpoint)
    advance(root, [{"work_item": "TASK-REVIEW", "staged": _staged(root, "quality.code-review", "TASK-REVIEW", "ART-REVIEW")}], adapter=checkpoint, revision_provider=REVISION)


def _dispatch(capability: str, work_item: str, workspace_id: str, scope: tuple[str, ...]) -> DispatchRequest:
    contract = load_contract(capability, 1)
    return DispatchRequest(
        capability=capability, contract_version=1,
        inputs={"work_item": work_item, "acceptance_criteria": ("tests-pass",), "workspace_id": workspace_id, "workspace_scope": scope},
        readable_scope=tuple(contract.dispatch["readable_scope"]), writable_scope=tuple(contract.dispatch["writable_scope"]),
        task_template=contract.dispatch["task_template"], base_revision=REVISION,
    )


@pytest.mark.parametrize("capability", tuple(CAPABILITIES))
def test_implementation_route_contracts_are_versioned_and_dispatchable(capability: str) -> None:
    contract = load_contract(capability, 1)
    assert contract.version == 1
    assert (SKILL_ROOT / contract.dispatch["task_template"]).is_file()
    assert contract.dispatch["writable_scope"] == ["staging"]


def test_parallel_implementation_route_requires_isolated_evidence_and_confirmed_integration(tmp_path: Path) -> None:
    root = tmp_path / "implementation-project"
    _materialize(root)
    assert validate_dag(tuple(work for stage in load_project(root).plan.stages for work in stage.work_items)).is_valid

    checkpoint = ConfirmedCheckpoint()
    plan = _staged(root, "planning.implementation", "TASK-PLAN", "ART-PLAN")
    advance(root, [{"work_item": "TASK-PLAN", "staged": plan}], adapter=checkpoint, revision_provider=REVISION)
    snapshot = resume_project(root).snapshot
    ready = ready_work(snapshot)
    assert {item.id for item in ready} == {"TASK-API", "TASK-DATA", "TASK-CONFLICT"}
    integration = next(item for stage in snapshot.plan.stages for item in stage.work_items if item.id == "TASK-INTEGRATE")
    ready.append(integration)
    batch = build_safe_batch(ready, capacity=3)
    assert {item.id for item in batch.items} == {"TASK-API", "TASK-DATA"}
    assert batch.integration_work == integration
    assert conflicts(next(item for item in ready if item.id == "TASK-API"), next(item for item in ready if item.id == "TASK-CONFLICT"))
    assert not batch.is_complete(("TASK-API", "TASK-DATA"))
    assert set(integration.depends_on) == {"TASK-API", "TASK-DATA"}

    runtime = MockRuntime()
    assert isinstance(runtime, WorkerLifecyclePort)
    api_handle = runtime.spawn_worker(_dispatch("implementation.tdd", "TASK-API", "ws-api", ("src/api",)))
    data_handle = runtime.spawn_worker(_dispatch("implementation.tdd", "TASK-DATA", "ws-data", ("src/data",)))
    assert api_handle.worker_id != data_handle.worker_id
    assert api_handle.workspace_id != data_handle.workspace_id
    assert set(api_handle.workspace_scope).isdisjoint(data_handle.workspace_scope)
    runtime.complete_worker(api_handle)
    runtime.complete_worker(data_handle)

    api = _staged(root, "implementation.tdd", "TASK-API", "ART-API")
    data = _staged(root, "implementation.tdd", "TASK-DATA", "ART-DATA")
    advance(root, [{"work_item": "TASK-API", "staged": api}, {"work_item": "TASK-DATA", "staged": data}], adapter=checkpoint, revision_provider=REVISION)
    assert not batch.is_complete(("TASK-API", "TASK-DATA"))
    assert next(item for stage in resume_project(root).snapshot.plan.stages for item in stage.work_items if item.id == "TASK-INTEGRATE").status == "ready"
    assert next(criterion for criterion in resume_project(root).snapshot.plan.stages[0].exit_criteria if criterion.id == "IMPLEMENTATION-COMPLETE").status == "pending"

    integrated = _staged(root, "implementation.execute", "TASK-INTEGRATE", "ART-INTEGRATION")
    advance(root, [{"work_item": "TASK-INTEGRATE", "staged": integrated}], adapter=checkpoint, revision_provider=REVISION)
    reviewed = _staged(root, "quality.code-review", "TASK-REVIEW", "ART-REVIEW")
    advance(root, [{"work_item": "TASK-REVIEW", "staged": reviewed}], adapter=checkpoint, revision_provider=REVISION)
    verified = _staged(root, "quality.verify", "TASK-VERIFY", "ART-VERIFY")
    final = advance(root, [{"work_item": "TASK-VERIFY", "staged": verified}], adapter=checkpoint, revision_provider=REVISION)
    assert next(item for stage in final.plan.stages for item in stage.work_items if item.id == "TASK-VERIFY").status == "done"
    assert next(item for stage in final.plan.stages for item in stage.work_items if item.id == "TASK-CONFLICT").status == "ready"
    assert next(criterion for criterion in final.plan.stages[0].exit_criteria if criterion.id == "IMPLEMENTATION-COMPLETE").status == "verified"
    trace = yaml.safe_load((root / "docs/project-control/traceability.yaml").read_text(encoding="utf-8"))
    assert {edge["source"] for edge in trace["edges"] if edge["target"] == "ART-INTEGRATION"} == {"ART-API", "ART-DATA"}
    assert len(checkpoint.identities) == 5


@pytest.mark.parametrize("capability,work_item,artifact,missing", [
    ("implementation.tdd", "TASK-API", "ART-API", "passing-test"),
    ("quality.code-review", "TASK-REVIEW", "ART-REVIEW", "review"),
    ("quality.verify", "TASK-VERIFY", "ART-VERIFY", "tests"),
])
def test_missing_evidence_review_failure_or_scope_error_never_partially_commits(
    tmp_path: Path, capability: str, work_item: str, artifact: str, missing: str,
) -> None:
    root = tmp_path / capability.replace(".", "-")
    _materialize(root)
    checkpoint = ConfirmedCheckpoint()
    if capability == "implementation.tdd":
        advance(root, [{"work_item": "TASK-PLAN", "staged": _staged(root, "planning.implementation", "TASK-PLAN", "ART-PLAN")}], adapter=checkpoint, revision_provider=REVISION)
    elif capability == "quality.code-review":
        _advance_to_review(root, checkpoint)
    else:
        _advance_to_verify(root, checkpoint)
    before = tree_hash(root)
    with pytest.raises(ValidationError, match="evidence|schema|completed"):
        bad = _staged(root, capability, work_item, artifact, missing=missing)
        advance(root, [{"work_item": work_item, "staged": bad}], adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(root) == before
    assert next(item for item in resume_project(root).snapshot.plan.stages[0].work_items if item.id == work_item).status == "ready"

    if capability == "implementation.tdd":
        with pytest.raises(ValidationError, match="scope|path"):
            out_of_scope = _staged(root, capability, work_item, artifact, files={"../canonical.txt": "nope"})
            advance(root, [{"work_item": work_item, "staged": out_of_scope}], adapter=checkpoint, revision_provider=REVISION)
        assert tree_hash(root) == before


def test_failed_test_or_review_is_not_done_and_cannot_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "failed-results"
    _materialize(root)
    checkpoint = ConfirmedCheckpoint()
    advance(root, [{"work_item": "TASK-PLAN", "staged": _staged(root, "planning.implementation", "TASK-PLAN", "ART-PLAN")}], adapter=checkpoint, revision_provider=REVISION)
    before = tree_hash(root)
    failed = _staged(root, "implementation.tdd", "TASK-API", "ART-API", status="failed")
    with pytest.raises(ValidationError, match="completed"):
        advance(root, [{"work_item": "TASK-API", "staged": failed}], adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(root) == before
    assert next(item for item in resume_project(root).snapshot.plan.stages[0].work_items if item.id == "TASK-API").status == "ready"

    review_root = tmp_path / "review-failed"
    _materialize(review_root)
    _advance_to_review(review_root, checkpoint)
    before_review = tree_hash(review_root)
    rejected = _staged(review_root, "quality.code-review", "TASK-REVIEW", "ART-REVIEW", status="failed")
    with pytest.raises(ValidationError, match="completed"):
        advance(review_root, [{"work_item": "TASK-REVIEW", "staged": rejected}], adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(review_root) == before_review
    assert next(item for item in resume_project(review_root).snapshot.plan.stages[0].work_items if item.id == "TASK-REVIEW").status == "ready"


def test_rejected_review_and_failed_security_cannot_promote(tmp_path: Path) -> None:
    review_root = tmp_path / "rejected-review"
    _materialize(review_root)
    checkpoint = ConfirmedCheckpoint()
    _advance_to_review(review_root, checkpoint)
    before = tree_hash(review_root)
    with pytest.raises(ValidationError, match="approved|review"):
        _staged(review_root, "quality.code-review", "TASK-REVIEW", "ART-REVIEW", review_decision="rejected")
    assert tree_hash(review_root) == before
    assert next(item for item in resume_project(review_root).snapshot.plan.stages[0].work_items if item.id == "TASK-REVIEW").status == "ready"

    verify_root = tmp_path / "failed-security"
    _materialize(verify_root)
    _advance_to_verify(verify_root, checkpoint)
    before_verify = tree_hash(verify_root)
    with pytest.raises(ValidationError, match="security"):
        _staged(verify_root, "quality.verify", "TASK-VERIFY", "ART-VERIFY", security_status="failed")
    assert tree_hash(verify_root) == before_verify


@pytest.mark.parametrize(("capability", "work_item", "artifact", "section_overrides", "evidence_overrides", "message"), [
    ("implementation.tdd", "TASK-API", "ART-API", None, {"failing-test": "command: pytest; output: no tests collected"}, "RED evidence"),
    ("quality.code-review", "TASK-REVIEW", "ART-REVIEW", {"findings": "blocking findings: none; unresolved blocking issue"}, None, "blocking findings"),
    ("quality.verify", "TASK-VERIFY", "ART-VERIFY", {"result": "failed"}, None, "verification result"),
    ("quality.verify", "TASK-VERIFY", "ART-VERIFY", None, {"security": "not passed"}, "security"),
])
def test_completed_results_require_semantic_success_evidence(
    tmp_path: Path,
    capability: str,
    work_item: str,
    artifact: str,
    section_overrides: dict[str, str] | None,
    evidence_overrides: dict[str, str] | None,
    message: str,
) -> None:
    root = tmp_path / capability.replace(".", "-")
    _materialize(root)
    with pytest.raises(ValidationError, match=message):
        _staged(
            root, capability, work_item, artifact,
            section_overrides=section_overrides,
            evidence_overrides=evidence_overrides,
        )


def test_integration_requires_trace_evidence_from_every_parallel_predecessor(tmp_path: Path) -> None:
    root = tmp_path / "incomplete-fan-in"
    _materialize(root)
    checkpoint = ConfirmedCheckpoint()
    advance(root, [{"work_item": "TASK-PLAN", "staged": _staged(root, "planning.implementation", "TASK-PLAN", "ART-PLAN")}], adapter=checkpoint, revision_provider=REVISION)
    advance(root, [
        {"work_item": "TASK-API", "staged": _staged(root, "implementation.tdd", "TASK-API", "ART-API")},
        {"work_item": "TASK-DATA", "staged": _staged(root, "implementation.tdd", "TASK-DATA", "ART-DATA")},
    ], adapter=checkpoint, revision_provider=REVISION)

    before = tree_hash(root)
    staged = _staged(root, "implementation.execute", "TASK-INTEGRATE", "ART-INTEGRATION")
    incomplete = replace(
        staged,
        result=replace(
            staged.result,
            evidence={**staged.result.evidence, "traceability": staged.result.evidence["traceability"][:1]},
        ),
    )
    with pytest.raises(ValidationError, match="predecessor provenance"):
        advance(root, [{"work_item": "TASK-INTEGRATE", "staged": incomplete}], adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(root) == before
