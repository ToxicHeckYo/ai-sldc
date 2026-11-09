"""Task 15: an end-to-end source-to-approved-baseline route.

The route deliberately uses the real orchestrator boundary: provider results are
normalized into staging, checked against the locked contract, then committed by
the coordinator's test-only checkpoint adapter.  It is not a transcript or a
mock-only assertion.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import hashlib
import shutil
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.approvals import fingerprint_payload, record_approval  # noqa: E402
from ai_sdlc.adapters import build_dispatch, load_provider_adapter, normalize_provider_output  # noqa: E402
from ai_sdlc.contracts import load_contract  # noqa: E402
from ai_sdlc.errors import ValidationError  # noqa: E402
from ai_sdlc.integration import normalize_result, stage_result, staged_fingerprint, tree_hash  # noqa: E402
from ai_sdlc.orchestrator import advance, initialize_project, resume_project  # noqa: E402
from ai_sdlc.providers import ProviderLock  # noqa: E402
from ai_sdlc.readiness import calculate_readiness  # noqa: E402
from ai_sdlc.runtime import DispatchRequest, MockRuntime  # noqa: E402
from ai_sdlc.standard import verify_standard  # noqa: E402
from ai_sdlc.status_view import check_status  # noqa: E402
from ai_sdlc.traceability import TraceEdge, TraceGraph, build_trace_graph, validate_traceability  # noqa: E402


ROUTE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "routes" / "baseline-project"
REVISION = "0" * 40
ROUTE_INTEGRITY = "sha256:" + "a" * 64
ADAPTER_IDS = {
    "requirements.discovery": "requirements-analysis", "requirements.formalize": "requirements-formalize",
    "requirements.scenarios": "requirements-scenarios", "architecture.c4": "architecture-c4",
    "architecture.decision": "architecture-decision", "architecture.system-design": "architecture-system-design",
    "architecture.sdd": "architecture-sdd", "architecture.baseline": "architecture-baseline",
}
ROUTE_WORK = (
    ("requirements.discovery", "TASK-ANALYSIS", "ART-ANALYSIS"),
    ("requirements.formalize", "TASK-FORMAL", "ART-FORMAL"),
    ("requirements.scenarios", "TASK-SCENARIOS", "ART-SCENARIOS"),
    ("architecture.c4", "TASK-C1", "ART-C1"),
    ("architecture.c4", "TASK-C2", "ART-C2"),
    ("architecture.decision", "TASK-ADR", "ART-ADR"),
    ("architecture.system-design", "TASK-C3", "ART-C3"),
    ("architecture.sdd", "TASK-SDD", "ART-SDD"),
    ("architecture.baseline", "TASK-BASELINE", "ART-BASELINE"),
)


@pytest.mark.parametrize(
    ("adapter_id", "capability"),
    (
        ("requirements-formalize", "requirements.formalize"),
        ("requirements-scenarios", "requirements.scenarios"),
        ("architecture-c4", "architecture.c4"),
        ("architecture-decision", "architecture.decision"),
        ("architecture-system-design", "architecture.system-design"),
        ("architecture-sdd", "architecture.sdd"),
        ("architecture-baseline", "architecture.baseline"),
    ),
)
def test_route_contracts_have_dispatchable_declarative_adapters(adapter_id: str, capability: str) -> None:
    contract = load_contract(capability, 1)
    adapter = load_provider_adapter(adapter_id)
    request = build_dispatch(
        adapter_id,
        {name: f"fixture:{name}" for name in contract.dispatch["required_inputs"]},
        base_revision=REVISION,
    )

    assert adapter.capability == capability
    assert request.capability == capability
    assert request.task_template == contract.dispatch["task_template"]
    assert (SKILL_ROOT / request.task_template).is_file()


class _ConfirmedTestAdapter:
    """Explicitly test-only checkpoint identity; never used in production code."""

    def __init__(self) -> None:
        self.identities: list[str] = []

    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> str:
        assert paths
        identity = "test-confirmed-baseline-checkpoint"
        self.identities.append(identity)
        return identity


def _lock() -> ProviderLock:
    capabilities = {
        "requirements.discovery": {
            "contract_version": 1, "provider": "requirements-analysis",
            "source": "https://github.com/jwynia/agent-skills",
            "revision": "e02ec7e226a6e4f8419fd3b88a1d8e472d421b32",
            "integrity": "sha256:87a6688bf03f9175ff56c9dd9331250ef814cf5c098ce305c9966210d209e69a",
            "status": "enabled",
        },
    }
    for capability, provider in ADAPTER_IDS.items():
        if capability == "requirements.discovery":
            continue
        capabilities[capability] = {
            "contract_version": 1, "provider": provider,
            "source": f"builtin://{provider}", "revision": "1",
            "integrity": ROUTE_INTEGRITY, "status": "enabled",
        }
    return ProviderLock(schema_version=1, capabilities=capabilities)


def _route_documents() -> tuple[dict, dict, dict, dict, list[dict]]:
    artifacts = [
        ("ART-ANALYSIS", "requirements_analysis", "staging/requirements/analysis.md"),
        ("ART-FORMAL", "functional_requirements", "staging/requirements/formal.md"),
        ("ART-SCENARIOS", "business_scenario_catalog", "staging/requirements/scenarios.md"),
        ("ART-C1", "c4_context", "staging/architecture/c1.md"),
        ("ART-C2", "c4_container", "staging/architecture/c2.md"),
        ("ART-ADR", "adr", "staging/architecture/adr.md"),
        ("ART-C3", "c4_component", "staging/architecture/c3.md"),
        ("ART-SDD", "sdd_specification", "staging/architecture/sdd.md"),
        ("ART-BASELINE", "architecture_baseline", "staging/baseline/baseline.md"),
    ]
    artifact_doc = {"artifacts": [{"id": id_, "kind": kind, "path": path, "status": "in_review"} for id_, kind, path in artifacts]}
    source_doc = {"sources": [{
        "id": "SRC-001", "kind": "business_document", "title": "Baseline route brief",
        "origin": "fixture", "received_at": "2026-11-03", "version": "1",
        "location": "docs/product-definition/sources/baseline-brief.md", "status": "registered",
    }]}
    work_specs = [
        ("TASK-ANALYSIS", "Discover and analyze source", "requirements.discovery", (), ("ART-ANALYSIS",)),
        ("TASK-FORMAL", "Write formal requirements", "requirements.formalize", ("TASK-ANALYSIS",), ("ART-FORMAL",)),
        ("TASK-SCENARIOS", "Define business scenarios", "requirements.scenarios", ("TASK-FORMAL",), ("ART-SCENARIOS",)),
        ("TASK-C1", "Create C1 system context", "architecture.c4", ("TASK-SCENARIOS",), ("ART-C1",)),
        ("TASK-C2", "Create C2 container model", "architecture.c4", ("TASK-C1",), ("ART-C2",)),
        ("TASK-ADR", "Record architecture decision", "architecture.decision", ("TASK-C2",), ("ART-ADR",)),
        ("TASK-C3", "Detail C3 components", "architecture.system-design", ("TASK-ADR",), ("ART-C3",)),
        ("TASK-SDD", "Specify implementation scenario", "architecture.sdd", ("TASK-C3",), ("ART-SDD",)),
        ("TASK-BASELINE", "Validate and approve baseline", "architecture.baseline", ("TASK-SDD",), ("ART-BASELINE",)),
    ]
    work_items = [{
        "id": id_, "title": title, "status": "ready", "executor": "coordinator", "depends_on": list(deps),
        "produces": list(produces), "scope": {"artifacts": list(produces), "paths": ["staging/"]},
        "criterion_ids": [id_], "criterion_evidence": ["traceability"],
        "plan": f"capability: {capability}",
    } for id_, title, capability, deps, produces in work_specs]
    stages = []
    titles = {
        "STAGE-01": "Анализ и определение продукта", "STAGE-02": "Определение границ системы",
        "STAGE-03": "Проектирование и проверка C2", "STAGE-04": "Уточнение ТЗ",
        "STAGE-05": "Архитектурная проработка", "STAGE-06": "Сценарная детализация архитектуры и SDD",
        "STAGE-07": "Фиксация исходного baseline", "STAGE-08": "Детальное планирование реализации",
        "STAGE-09": "Реализация с помощью ИИ", "STAGE-10": "Подготовка к развертыванию",
        "STAGE-11": "Развертывание на тестовом контуре", "STAGE-12": "Тестирование и приёмка",
        "STAGE-13": "Развертывание в промышленном контуре", "STAGE-14": "Эксплуатация и развитие",
    }
    route_criteria = [{"id": id_, "text": title, "status": "pending", **({"approval_role": "architect", "approval_gate": "architecture-approval"} if capability in {"architecture.c4", "architecture.decision"} else {"approval_role": "product-owner", "approval_gate": "baseline-approval"} if capability == "architecture.baseline" else {})} for id_, title, capability, _, _ in work_specs]
    for index in range(1, 15):
        stage_id = f"STAGE-{index:02d}"
        stages.append({
            "id": stage_id, "title": titles[stage_id], "status": "active" if index == 1 else "not_started",
            "depends_on": [f"STAGE-{index - 1:02d}"] if index > 1 else [],
            "entry_criteria": [], "exit_criteria": route_criteria if index == 1 else [], "work_items": work_items if index == 1 else [],
        })
    plan_doc = {"schema_version": 1, "project_id": "BASELINE-ROUTE", "planning_mode": "rolling_wave", "stages": stages}
    state_doc = {
        "schema_version": 1, "project_id": "BASELINE-ROUTE", "lifecycle_mode": "initial-development",
        "current_stage": "STAGE-01", "focus_work_items": ["TASK-ANALYSIS"],
        "next_actions": [{"work_item": "TASK-ANALYSIS", "action": "Discover and analyze source"}],
        "updated_at": "2026-11-03", "updated_by": "orchestrator", "active_change": None,
        "current_baseline": None, "last_approved_artifact": None,
    }
    return plan_doc, state_doc, artifact_doc, source_doc


def _materialize_fixture(root: Path) -> None:
    root.mkdir()
    (root / ".git").mkdir()
    (root / "docs/product-definition/sources").mkdir(parents=True)
    shutil.copy2(ROUTE_FIXTURE / "baseline-brief.md", root / "docs/product-definition/sources/baseline-brief.md")
    initialize_project(root, SKILL_ROOT, _lock())
    plan, state, artifacts, sources = _route_documents()
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


def _raw_result(root: Path, capability: str, work_item: str, artifact: str, *, sections: dict | None = None, missing_evidence: bool = False) -> dict:
    contract = load_contract(capability, 1)
    required = {name: f"{work_item} evidence" for name in contract.result["required_sections"]}
    required.update(sections or {})
    required["work_item"] = work_item
    evidence = {name: "SRC-001" if name in {"source-register", "traceability"} else f"evidence:{artifact}" for name in contract.result["required_evidence"]}
    if missing_evidence:
        evidence.pop(next(iter(contract.result["required_evidence"])), None)
    trace_source = "SRC-001" if artifact == "ART-ANALYSIS" else {"ART-FORMAL": "ART-ANALYSIS", "ART-SCENARIOS": "ART-FORMAL", "ART-C1": "ART-SCENARIOS", "ART-C2": "ART-C1", "ART-ADR": "ART-C2", "ART-C3": "ART-ADR", "ART-SDD": "ART-C3", "ART-BASELINE": "ART-SDD"}[artifact]
    evidence["traceability"] = [{"source": trace_source, "target": artifact, "relation": "traces_to"}]
    path_by_artifact = {id_: path for id_, _, path in _route_documents()[2]["artifacts"]} if False else {
        "ART-ANALYSIS": "staging/requirements/analysis.md", "ART-FORMAL": "staging/requirements/formal.md", "ART-SCENARIOS": "staging/requirements/scenarios.md", "ART-C1": "staging/architecture/c1.md", "ART-C2": "staging/architecture/c2.md", "ART-ADR": "staging/architecture/adr.md", "ART-C3": "staging/architecture/c3.md", "ART-SDD": "staging/architecture/sdd.md", "ART-BASELINE": "staging/baseline/baseline.md",
    }
    provider = ADAPTER_IDS[capability]
    if capability == "requirements.discovery":
        value = {
            "provider": {"id": provider, "revision": "e02ec7e226a6e4f8419fd3b88a1d8e472d421b32"},
            "base_revision": REVISION, "outcome": "complete",
            "analysis": {"requirements": [{"id": "BR-001", "statement": "route requirement"}], "conflicts": ["none"], "constraints": {"confirmed": ["route"], "preferences": ["route"]}, "assumptions": ["route"], "open_questions": ["none"]},
            "artifacts": [artifact],
            "evidence": {"sources": [{"source_id": "SRC-001", "checksum": "sha256:" + "a" * 64}], "traceability": [{"source": "SRC-001", "target": artifact, "relation": "traces_to"}]},
            "files": {path_by_artifact[artifact]: f"# {artifact}\nProduced by {capability}.\n"},
        }
        if missing_evidence:
            value["evidence"].pop("sources")
        return value
    result = {
        "capability": capability, "provider": provider,
        "status": "completed", "base_revision": REVISION, "sections": required, "evidence": evidence,
        "produced": [artifact], "files": {path_by_artifact[artifact]: f"# {artifact}\nProduced by {capability}.\n"},
    }
    return {
        "provider": {"id": provider, "revision": "1"},
        "base_revision": REVISION, "outcome": "complete", "sections": required, "evidence": evidence,
        "files": result["files"], "produced": [artifact], "unresolved": [],
        "lifecycle_changes": [], "project_state_changes": [], "control_file_changes": [],
    }


def _result(root: Path, capability: str, work_item: str, artifact: str, *, sections: dict | None = None, missing_evidence: bool = False) -> object:
    contract = load_contract(capability, 1)
    raw = _raw_result(root, capability, work_item, artifact, sections=sections, missing_evidence=missing_evidence)
    if capability in ADAPTER_IDS:
        request = build_dispatch(ADAPTER_IDS[capability], {name: f"fixture:{name}" for name in contract.dispatch["required_inputs"]}, base_revision=REVISION)
    else:
        request = DispatchRequest(
            capability=capability, contract_version=contract.version,
            inputs={name: f"fixture:{name}" for name in contract.dispatch["required_inputs"]},
            readable_scope=tuple(contract.dispatch["readable_scope"]), writable_scope=tuple(contract.dispatch["writable_scope"]),
            task_template=contract.dispatch["task_template"], base_revision=REVISION,
        )
    scripted = {"capability": capability, "status": "completed", "payload": raw} if capability in ADAPTER_IDS else raw
    runtime = MockRuntime({capability: scripted})
    provider_raw = runtime.invoke_skill(request)
    normalized = (
        normalize_provider_output(ADAPTER_IDS[capability], provider_raw.payload, base_revision=REVISION).contract_result
        if capability in ADAPTER_IDS else normalize_result(provider_raw, contract)
    )
    assert normalized.produced == (artifact,)
    return stage_result(normalized, root, contract)


def _external_approval(staged: object, gate: str) -> object:
    role = "product-owner" if gate == "baseline-approval" else "architect"
    return record_approval(
        gate, f"{role}-1", role,
        fingerprint_payload(staged_fingerprint(staged).encode("utf-8")),
    )


def _advance_prefix(root: Path, count: int) -> _ConfirmedTestAdapter:
    adapter = _ConfirmedTestAdapter()
    for capability, work_item, artifact in ROUTE_WORK[:count]:
        staged = _result(root, capability, work_item, artifact)
        approvals = {}
        if capability in {"architecture.c4", "architecture.decision"}:
            approvals[work_item] = _external_approval(staged, "architecture-approval")
        advance(root, [{"work_item": work_item, "staged": staged}], approvals, adapter=adapter, revision_provider=REVISION)
    return adapter


def test_source_to_approved_baseline_route_is_traceable_and_provider_bound(tmp_path: Path) -> None:
    root = tmp_path / "baseline-project"
    _materialize_fixture(root)
    context = resume_project(root)
    assert {source.id for source in context.snapshot.sources} == {"SRC-001"}
    assert len(context.snapshot.artifacts) == 9

    work = ROUTE_WORK
    assert calculate_readiness(context.snapshot.plan.stages[0].exit_criteria).percentage == 0
    adapter = _ConfirmedTestAdapter()
    staged_results = {}
    for capability, work_item, artifact in work[:-1]:
        staged = _result(root, capability, work_item, artifact)
        staged_results[artifact] = staged
        approvals = {}
        if capability in {"architecture.c4", "architecture.decision"}:
            approvals[work_item] = _external_approval(staged, "architecture-approval")
        snapshot = advance(root, [{"work_item": work_item, "staged": staged}], approvals, adapter=adapter, revision_provider=REVISION)
        assert next(item for stage in snapshot.plan.stages for item in stage.work_items if item.id == work_item).status == "done"

    baseline = _result(root, "architecture.baseline", "TASK-BASELINE", "ART-BASELINE")
    staged_results["ART-BASELINE"] = baseline
    pending = calculate_readiness(resume_project(root).snapshot.plan.stages[0].exit_criteria)
    assert pending.percentage < 100
    approval = _external_approval(baseline, "baseline-approval")
    snapshot = advance(root, [{"work_item": "TASK-BASELINE", "staged": baseline}], {"TASK-BASELINE": approval}, adapter=adapter, revision_provider=REVISION)
    assert snapshot.plan.stages[0].work_items[-1].status == "done"
    assert next(artifact for artifact in snapshot.artifacts if artifact.id == "ART-BASELINE").status == "approved"
    assert snapshot.state.current_baseline == "ART-BASELINE"
    assert snapshot.state.last_approved_artifact == "ART-BASELINE"
    resumed = resume_project(root)
    assert calculate_readiness(resumed.snapshot.plan.stages[0].exit_criteria).percentage == 100
    assert adapter.identities == ["test-confirmed-baseline-checkpoint"] * len(work)
    assert check_status(root).is_valid

    graph = build_trace_graph(resumed.snapshot)
    assert validate_traceability(graph, "baseline").is_valid
    exact_chain = ("SRC-001", "ART-ANALYSIS", "ART-FORMAL", "ART-SCENARIOS", "ART-C1", "ART-C2", "ART-ADR", "ART-C3", "ART-SDD", "ART-BASELINE")
    assert validate_traceability(graph, exact_chain).is_valid
    persisted = yaml.safe_load((root / "docs/project-control/traceability.yaml").read_text(encoding="utf-8"))
    persisted_edges = {(edge["source"], edge["target"], edge["relation"]) for edge in persisted["edges"]}
    assert persisted_edges
    persisted_graph = TraceGraph(
        nodes=graph.nodes,
        edges=tuple(TraceEdge(**edge) for edge in persisted["edges"]),
    )
    assert validate_traceability(persisted_graph).is_valid
    registered_ids = {node.id for node in graph.nodes}
    assert all(source in registered_ids and target in registered_ids for source, target, _ in persisted_edges)
    assert {artifact.id for artifact in snapshot.artifacts} == set(staged_results)
    for artifact in resumed.snapshot.artifacts:
        path = root / artifact.path
        assert path.is_file()
        actual = path.read_bytes()
        staged = staged_results[artifact.id]
        expected = staged.result.files[artifact.path].encode("utf-8") if isinstance(staged.result.files[artifact.path], str) else staged.result.files[artifact.path]
        assert actual == expected
        assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(expected).hexdigest()
        assert any(link["target"] == artifact.id for link in staged.result.evidence["traceability"])
    assert tree_hash(root)
    assert resumed.snapshot.plan.stages[0].work_items[-1].status == "done"


def test_baseline_route_rejects_missing_or_stale_external_approval(tmp_path: Path) -> None:
    root = tmp_path / "baseline-project"
    _materialize_fixture(root)
    baseline = _result(root, "architecture.baseline", "TASK-BASELINE", "ART-BASELINE")
    with pytest.raises(ValidationError, match="approval"):
        advance(root, [{"work_item": "TASK-BASELINE", "staged": baseline}], adapter=_ConfirmedTestAdapter(), revision_provider=REVISION)
    stale = record_approval("baseline-approval", "product-owner-1", "product-owner", "b" * 64)
    with pytest.raises(ValidationError, match="approval"):
        advance(root, [{"work_item": "TASK-BASELINE", "staged": baseline}], {"TASK-BASELINE": stale}, adapter=_ConfirmedTestAdapter(), revision_provider=REVISION)
    wrong_role = record_approval(
        "baseline-approval", "architect-1", "system-architect",
        fingerprint_payload(staged_fingerprint(baseline).encode("utf-8")),
    )
    with pytest.raises(ValidationError, match="approval"):
        advance(root, [{"work_item": "TASK-BASELINE", "staged": baseline}], {"TASK-BASELINE": wrong_role}, adapter=_ConfirmedTestAdapter(), revision_provider=REVISION)


@pytest.mark.parametrize(
    ("target", "capability", "work_item", "artifact", "gate", "role"),
    [
        (3, "architecture.c4", "TASK-C1", "ART-C1", "architecture-approval", "architect"),
        (4, "architecture.c4", "TASK-C2", "ART-C2", "architecture-approval", "architect"),
        (5, "architecture.decision", "TASK-ADR", "ART-ADR", "architecture-approval", "architect"),
        (8, "architecture.baseline", "TASK-BASELINE", "ART-BASELINE", "baseline-approval", "product-owner"),
    ],
)
def test_route_human_gates_require_exact_role_fingerprint_and_gate(
    tmp_path: Path, target: int, capability: str, work_item: str, artifact: str, gate: str, role: str,
) -> None:
    for mode in ("missing", "stale", "wrong-role"):
        root = tmp_path / mode
        _materialize_fixture(root)
        _advance_prefix(root, target)
        staged = _result(root, capability, work_item, artifact)
        if mode == "missing":
            approvals = {}
        elif mode == "stale":
            approvals = {work_item: record_approval(gate, f"{role}-1", role, "b" * 64)}
        else:
            wrong_role = "system-architect" if role == "architect" else "architect"
            approvals = {work_item: record_approval(gate, "wrong-role", wrong_role, fingerprint_payload(staged_fingerprint(staged).encode("utf-8")))}
        with pytest.raises(ValidationError, match="approval"):
            advance(root, [{"work_item": work_item, "staged": staged}], approvals, adapter=_ConfirmedTestAdapter(), revision_provider=REVISION)

    root = tmp_path / "exact"
    _materialize_fixture(root)
    adapter = _advance_prefix(root, target)
    staged = _result(root, capability, work_item, artifact)
    approval = _external_approval(staged, gate)
    snapshot = advance(root, [{"work_item": work_item, "staged": staged}], {work_item: approval}, adapter=adapter, revision_provider=REVISION)
    assert next(item for stage in snapshot.plan.stages for item in stage.work_items if item.id == work_item).status == "done"


def test_route_rejects_missing_evidence_and_agent_approval_flags(tmp_path: Path) -> None:
    root = tmp_path / "missing-evidence"
    _materialize_fixture(root)
    with pytest.raises(ValidationError, match="evidence"):
        _result(root, "requirements.discovery", "TASK-ANALYSIS", "ART-ANALYSIS", missing_evidence=True)

    root = tmp_path / "agent-approval-flag"
    _materialize_fixture(root)
    staged = _result(root, "architecture.baseline", "TASK-BASELINE", "ART-BASELINE", sections={"approval": "approved"})
    with pytest.raises(ValidationError, match="approval"):
        advance(root, [{"work_item": "TASK-BASELINE", "staged": staged}], adapter=_ConfirmedTestAdapter(), revision_provider=REVISION)


def test_route_rejects_criterion_promotion_not_declared_by_work_item(tmp_path: Path) -> None:
    root = tmp_path / "undeclared-criterion"
    _materialize_fixture(root)
    plan_path = root / "docs/project-control/project-plan.yaml"
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["stages"][0]["work_items"][0].pop("criterion_ids")
    plan["stages"][0]["work_items"][0].pop("criterion_evidence")
    plan_path.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False), encoding="utf-8")
    staged = _result(root, "requirements.discovery", "TASK-ANALYSIS", "ART-ANALYSIS")

    with pytest.raises(ValidationError, match="does not declare criterion"):
        advance(root, [{"work_item": "TASK-ANALYSIS", "staged": staged}], adapter=_ConfirmedTestAdapter(), revision_provider=REVISION)


@pytest.mark.parametrize(
    ("prefix", "capability", "work_item", "artifact", "source"),
    [
        (0, "requirements.discovery", "TASK-ANALYSIS", "ART-ANALYSIS", "ART-ANALYSIS"),
        (4, "architecture.c4", "TASK-C2", "ART-C2", "SRC-001"),
    ],
)
def test_invalid_explicit_trace_cannot_checkpoint_and_project_remains_resumable(
    tmp_path: Path, prefix: int, capability: str, work_item: str, artifact: str, source: str,
) -> None:
    root = tmp_path / artifact
    _materialize_fixture(root)
    adapter = _advance_prefix(root, prefix)
    staged = _result(root, capability, work_item, artifact)
    evidence = dict(staged.result.evidence)
    evidence["traceability"] = [{"source": source, "target": artifact, "relation": "traces_to"}]
    staged = replace(staged, result=replace(staged.result, evidence=evidence))
    approvals = {work_item: _external_approval(staged, "architecture-approval")} if capability == "architecture.c4" else {}

    with pytest.raises(ValidationError, match="traceability"):
        advance(root, [{"work_item": work_item, "staged": staged}], approvals, adapter=adapter, revision_provider=REVISION)

    assert not (root / {"ART-ANALYSIS": "staging/requirements/analysis.md", "ART-C2": "staging/architecture/c2.md"}[artifact]).exists()
    assert resume_project(root).snapshot.plan.stages[0].work_items[prefix].status == "ready"


@pytest.mark.parametrize("mutation", ("id", "path", "kind"))
def test_route_rejects_provider_outputs_that_do_not_match_registered_artifacts(tmp_path: Path, mutation: str) -> None:
    root = tmp_path / mutation
    _materialize_fixture(root)
    staged = _result(root, "requirements.discovery", "TASK-ANALYSIS", "ART-ANALYSIS")
    if mutation == "id":
        staged = replace(staged, result=replace(staged.result, produced=("ART-NOT-REGISTERED",)))
    elif mutation == "path":
        staged = replace(staged, result=replace(staged.result, files={"staging/requirements/other.md": "wrong path"}))
    else:
        register = root / "docs/artifact-register.yaml"
        document = yaml.safe_load(register.read_text(encoding="utf-8"))
        document["artifacts"][0]["kind"] = "sdd_specification"
        register.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValidationError, match="produced|paths|kinds"):
        advance(root, [{"work_item": "TASK-ANALYSIS", "staged": staged}], adapter=_ConfirmedTestAdapter(), revision_provider=REVISION)
