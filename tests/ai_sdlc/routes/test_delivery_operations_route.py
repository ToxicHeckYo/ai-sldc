"""Task 18: delivery, acceptance, and operations through the governed boundary."""

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

from ai_sdlc.approvals import fingerprint_payload, record_approval  # noqa: E402
from ai_sdlc.contracts import load_contract  # noqa: E402
from ai_sdlc.errors import ValidationError  # noqa: E402
from ai_sdlc.integration import normalize_result, stage_result, staged_fingerprint, tree_hash, validate_staged_result  # noqa: E402
from ai_sdlc.orchestrator import advance, initialize_project, resume_project  # noqa: E402
from ai_sdlc.providers import ProviderLock  # noqa: E402
from ai_sdlc.runtime import DispatchRequest, MockRuntime  # noqa: E402
from ai_sdlc.status_view import check_status, generate_status  # noqa: E402
from ai_sdlc.traceability import build_trace_graph, validate_traceability  # noqa: E402


REVISION = "d" * 40
ROUTE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "routes" / "delivery-project"
ROUTE = (
    ("delivery.prepare", "TASK-PREPARE", "ART-DEPLOYMENT-PACKAGE", "STAGE-10"),
    ("delivery.test-deploy", "TASK-TEST-DEPLOY", "ART-TEST-DEPLOYMENT", "STAGE-11"),
    ("delivery.health-check", "TASK-HEALTH", "ART-HEALTH", "STAGE-11"),
    ("delivery.acceptance", "TASK-ACCEPTANCE", "ART-ACCEPTANCE", "STAGE-12"),
    ("delivery.production-deploy", "TASK-PRODUCTION", "ART-RELEASE", "STAGE-13"),
    ("operations.monitor", "TASK-MONITOR", "ART-OPERATIONS", "STAGE-14"),
    ("operations.next-change", "TASK-NEXT-CHANGE", "ART-NEXT-CHANGE", "STAGE-14"),
)


class ConfirmedCheckpoint:
    def __init__(self) -> None:
        self.identities: list[str] = []

    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> str:
        assert paths
        identity = f"delivery-operations-{len(self.identities) + 1:02d}"
        self.identities.append(identity)
        return identity


def _lock() -> ProviderLock:
    return ProviderLock(
        schema_version=1,
        capabilities={
            capability: {
                "contract_version": 1,
                "provider": f"builtin-{capability.replace('.', '-')}",
                "source": f"builtin://{capability}",
                "revision": "1",
                "integrity": "sha256:" + "a" * 64,
                "status": "enabled",
            }
            for capability, *_ in ROUTE
        },
    )


def _documents() -> tuple[dict, dict, dict, dict]:
    artifact_specs = (
        ("ART-IMPLEMENTATION", "test_report", "docs/implementation/evidence.md"),
        ("ART-DEPLOYMENT-PACKAGE", "deployment_package", "staging/delivery/preparation.md"),
        ("ART-TEST-DEPLOYMENT", "deployment_package", "staging/delivery/test-deployment.md"),
        ("ART-HEALTH", "test_report", "staging/delivery/health.md"),
        ("ART-ACCEPTANCE", "test_report", "staging/delivery/acceptance.md"),
        ("ART-RELEASE", "release_record", "staging/delivery/release.md"),
        ("ART-OPERATIONS", "operations_document", "staging/operations/monitoring.md"),
        ("ART-NEXT-CHANGE", "operations_document", "staging/operations/next-change.md"),
    )
    artifacts = {"artifacts": [
        {"id": id_, "kind": kind, "path": path, "status": "approved" if id_ == "ART-IMPLEMENTATION" else "in_review"}
        for id_, kind, path in artifact_specs
    ]}
    source = {"sources": [{
        "id": "SRC-IMPLEMENTATION", "kind": "technical_specification", "title": "Verified implementation evidence",
        "origin": "route-fixture", "received_at": "2026-11-04", "version": "1",
        "location": "docs/implementation/source-evidence.md", "status": "accepted",
    }]}
    dependencies = {entry[1]: (() if index == 0 else (ROUTE[index - 1][1],)) for index, entry in enumerate(ROUTE)}
    work_by_stage: dict[str, list[dict]] = {f"STAGE-{index:02d}": [] for index in range(1, 15)}
    for capability, work_id, artifact, stage_id in ROUTE:
        work_by_stage[stage_id].append({
            "id": work_id, "title": work_id.replace("TASK-", "").replace("-", " ").title(), "status": "ready",
            "executor": "coordinator", "depends_on": list(dependencies[work_id]), "produces": [artifact],
            "scope": {"artifacts": [artifact], "paths": ["staging/delivery" if capability.startswith("delivery.") else "staging/operations"]},
            "criterion_ids": [work_id], "criterion_evidence": ["traceability"], "plan": f"capability: {capability}",
        })
    titles = {f"STAGE-{index:02d}": title for index, (_, title) in enumerate(__import__("ai_sdlc.model", fromlist=["STAGE_DEFINITIONS"]).STAGE_DEFINITIONS, start=1)}
    stages = []
    for index in range(1, 15):
        stage_id = f"STAGE-{index:02d}"
        works = work_by_stage[stage_id]
        criteria = [{"id": work["id"], "text": work["title"], "status": "pending", **(
            {"approval_role": "product-owner", "approval_gate": "acceptance-approval"}
            if work["id"] == "TASK-ACCEPTANCE" else
            {"approval_role": "release-owner", "approval_gate": "release-approval"}
            if work["id"] == "TASK-PRODUCTION" else {}
        )} for work in works]
        stages.append({
            "id": stage_id, "title": titles[stage_id], "status": "active" if index >= 10 else "completed",
            "depends_on": [f"STAGE-{index - 1:02d}"] if index > 1 else [], "entry_criteria": [],
            "exit_criteria": criteria, "required_artifacts": [work["produces"][0] for work in works], "work_items": works,
        })
    plan = {"schema_version": 1, "project_id": "DELIVERY-OPERATIONS-ROUTE", "planning_mode": "rolling_wave", "stages": stages}
    state = {
        "schema_version": 1, "project_id": "DELIVERY-OPERATIONS-ROUTE", "lifecycle_mode": "initial-development",
        "current_stage": "STAGE-10", "focus_work_items": ["TASK-PREPARE"],
        "next_actions": [{"work_item": "TASK-PREPARE", "action": "Prepare governed delivery"}],
        "updated_at": "2026-11-04", "updated_by": "orchestrator", "active_change": None,
        "current_baseline": "ART-IMPLEMENTATION", "last_approved_artifact": "ART-IMPLEMENTATION",
    }
    return plan, state, artifacts, source


def _materialize(root: Path) -> None:
    root.mkdir()
    (root / ".git").mkdir()
    shutil.copytree(ROUTE_FIXTURE / "docs", root / "docs")
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
    generate_status(root)


def _staged(
    root: Path,
    capability: str,
    work_item: str,
    artifact: str,
    *,
    status: str = "completed",
    missing: str | None = None,
    evidence_overrides: dict[str, object] | None = None,
):
    contract = load_contract(capability, 1)
    sections = {name: f"{work_item}: asserted external report" for name in contract.result["required_sections"]}
    sections["work_item"] = work_item
    evidence = {name: f"{work_item}: externally asserted and verified" for name in contract.result["required_evidence"]}
    state = yaml.safe_load((root / "docs/project-control/project-state.yaml").read_text(encoding="utf-8"))
    if "package-identity" in evidence:
        evidence["package-identity"] = state.get("deployment_package_identity") or "sha256:unbound"
    for key in {"smoke-test", "health-check", "production-smoke", "production-health"} & set(evidence):
        evidence[key] = {"status": "passed", "report": f"{work_item}: verified pass"}
    for key in {"functional-tests", "integration-tests", "system-tests", "regression-tests", "security-tests", "performance-tests", "business-scenario"} & set(evidence):
        evidence[key] = {"status": "passed", "report": f"{work_item}: acceptance pass"}
    if "acceptance-report" in evidence:
        evidence["acceptance-report"] = {"decision": "approved", "actor": "product-owner-1", "provenance": "human"}
    if "incident-record" in evidence:
        evidence["incident-record"] = {"id": f"INC-{work_item}", "status": "clear", "provenance": "operations"}
    if "feedback-record" in evidence:
        evidence["feedback-record"] = {"id": f"FDBK-{work_item}", "status": "reviewed", "provenance": "operations"}
    if "next-change-request" in evidence:
        evidence["next-change-request"] = {"id": "CR-NEXT-001", "actor": "product-owner-1", "provenance": "human"}
    evidence.update(evidence_overrides or {})
    predecessors = {"TASK-PREPARE": "SRC-IMPLEMENTATION", **{ROUTE[index][1]: ROUTE[index - 1][2] for index in range(1, len(ROUTE))}}
    evidence["traceability"] = [{"source": predecessors[work_item], "target": artifact, "relation": "traces_to"}]
    if missing:
        evidence.pop(missing, None)
    path = next(item["path"] for item in _documents()[2]["artifacts"] if item["id"] == artifact)
    raw = {
        "capability": capability, "provider": f"builtin-{capability.replace('.', '-')}", "status": status,
        "base_revision": REVISION, "sections": sections, "evidence": evidence, "produced": [artifact],
        "files": {path: f"# {artifact}\n{work_item}: route-produced evidence\n"} if status == "completed" else {},
    }
    request = DispatchRequest(
        capability=capability, contract_version=1,
        inputs={name: f"fixture:{name}" for name in contract.dispatch["required_inputs"]},
        readable_scope=tuple(contract.dispatch["readable_scope"]), writable_scope=tuple(contract.dispatch["writable_scope"]),
        task_template=contract.dispatch["task_template"], base_revision=REVISION,
    )
    result = MockRuntime({capability: raw}).invoke_skill(request)
    staged = stage_result(normalize_result(result, contract), root, contract)
    report = validate_staged_result(staged, contract)
    if not report.is_valid:
        raise ValidationError(report.findings[0].message)
    return staged


def _approval(staged, gate: str, role: str):
    return record_approval(gate, f"{role}-1", role, fingerprint_payload(staged_fingerprint(staged).encode("utf-8")))


@pytest.mark.parametrize("capability,_work,_artifact,_stage", ROUTE)
def test_delivery_and_operations_contracts_are_provider_neutral(capability: str, _work: str, _artifact: str, _stage: str) -> None:
    contract = load_contract(capability, 1)
    assert contract.dispatch["writable_scope"] == ["staging"]
    assert contract.parallelism["integration_capability"] == "provider-result.integrate"
    assert "external-asserted-report" in contract.integration["acceptance"]
    assert "automation-adapter-result" in contract.integration["acceptance"]
    assert contract.missing_provider_policy == "stop"


def test_acceptance_contract_covers_each_required_test_class_with_pass_criteria() -> None:
    contract = load_contract("delivery.acceptance", 1)
    required_evidence = set(contract.result["required_evidence"])
    assert {
        "functional-tests",
        "integration-tests",
        "system-tests",
        "regression-tests",
        "security-tests",
        "performance-tests",
        "business-scenario",
    } <= required_evidence
    assert {
        "functional-pass",
        "functional-tests-pass",
        "integration-pass",
        "integration-tests-pass",
        "system-pass",
        "system-tests-pass",
        "regression-pass",
        "regression-tests-pass",
        "security-pass",
        "security-tests-pass",
        "performance-pass",
        "performance-tests-pass",
        "business-scenario-pass",
    } <= set(contract.integration["semantic_checks"])


def test_delivery_acceptance_and_operations_route_is_traceable_and_release_authorized(tmp_path: Path) -> None:
    root = tmp_path / "delivery-project"
    _materialize(root)
    checkpoint = ConfirmedCheckpoint()
    staged_by_artifact = {}
    for capability, work_item, artifact, _stage in ROUTE:
        staged = _staged(root, capability, work_item, artifact)
        staged_by_artifact[artifact] = staged
        approvals = {}
        if work_item == "TASK-ACCEPTANCE":
            approvals[work_item] = _approval(staged, "acceptance-approval", "product-owner")
        if work_item == "TASK-PRODUCTION":
            approvals[work_item] = _approval(staged, "release-approval", "release-owner")
        advance(root, [{"work_item": work_item, "staged": staged}], approvals, adapter=checkpoint, revision_provider=REVISION)
        if work_item in {"TASK-PREPARE", "TASK-HEALTH", "TASK-ACCEPTANCE", "TASK-PRODUCTION", "TASK-NEXT-CHANGE"}:
            from ai_sdlc import orchestrator as coordinator
            transition = getattr(coordinator, "advance_stage", None)
            assert transition is not None
            transition(root, adapter=checkpoint, expected_stage=_stage, revision_provider=REVISION)

    resumed = resume_project(root)
    assert {work.id: work.status for stage in resumed.snapshot.plan.stages for work in stage.work_items} == {work: "done" for _, work, _, _ in ROUTE}
    assert {stage.id for stage in resumed.snapshot.plan.stages[9:]} == {"STAGE-10", "STAGE-11", "STAGE-12", "STAGE-13", "STAGE-14"}
    assert next(artifact for artifact in resumed.snapshot.artifacts if artifact.id == "ART-RELEASE").status == "approved"
    assert next(artifact for artifact in resumed.snapshot.artifacts if artifact.id == "ART-OPERATIONS").path == "staging/operations/monitoring.md"
    assert len(checkpoint.identities) == len(ROUTE) + 5
    assert resumed.snapshot.state.current_stage == "STAGE-14"
    assert resumed.snapshot.state.deployment_package_identity.startswith("sha256:")
    package_identity = resumed.snapshot.state.deployment_package_identity
    assert all(
        staged_by_artifact[artifact].result.evidence["package-identity"] == package_identity
        for artifact in ("ART-TEST-DEPLOYMENT", "ART-HEALTH", "ART-ACCEPTANCE", "ART-RELEASE")
    )
    assert all(stage.status == "completed" for stage in resumed.snapshot.plan.stages[9:])
    assert check_status(root).is_valid
    assert generate_status(root).read_bytes() == (root / "PROJECT_STATUS.md").read_bytes()
    graph = build_trace_graph(resumed.snapshot)
    assert validate_traceability(graph).is_valid
    assert {edge.target for edge in graph.edges} >= {artifact for _, _, artifact, _ in ROUTE}
    assert all((root / path).is_file() for path in ("staging/delivery/preparation.md", "staging/delivery/release.md", "staging/operations/monitoring.md"))
    assert (root / "docs/implementation/evidence.md").read_text(encoding="utf-8").startswith("# Verified implementation evidence")
    assert staged_by_artifact["ART-RELEASE"].result.evidence["package-identity"]
    assert staged_by_artifact["ART-RELEASE"].result.evidence["release-authority"]


def test_failed_health_missing_rollback_or_rejected_acceptance_never_reaches_production(tmp_path: Path) -> None:
    root = tmp_path / "blocked-delivery"
    _materialize(root)
    checkpoint = ConfirmedCheckpoint()
    prepare = _staged(root, "delivery.prepare", "TASK-PREPARE", "ART-DEPLOYMENT-PACKAGE")
    advance(root, [{"work_item": "TASK-PREPARE", "staged": prepare}], adapter=checkpoint, revision_provider=REVISION)
    from ai_sdlc import orchestrator as coordinator
    coordinator.advance_stage(root, adapter=checkpoint, expected_stage="STAGE-10", revision_provider=REVISION)
    before = tree_hash(root)
    with pytest.raises(ValidationError, match="evidence|required"):
        _staged(root, "delivery.test-deploy", "TASK-TEST-DEPLOY", "ART-TEST-DEPLOYMENT", missing="rollback-plan")
    assert tree_hash(root) == before

    test_deploy = _staged(root, "delivery.test-deploy", "TASK-TEST-DEPLOY", "ART-TEST-DEPLOYMENT")
    advance(root, [{"work_item": "TASK-TEST-DEPLOY", "staged": test_deploy}], adapter=checkpoint, revision_provider=REVISION)
    before = tree_hash(root)
    failed_health = _staged(root, "delivery.health-check", "TASK-HEALTH", "ART-HEALTH", status="failed")
    with pytest.raises(ValidationError, match="completed"):
        advance(root, [{"work_item": "TASK-HEALTH", "staged": failed_health}], adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(root) == before

    health = _staged(root, "delivery.health-check", "TASK-HEALTH", "ART-HEALTH")
    advance(root, [{"work_item": "TASK-HEALTH", "staged": health}], adapter=checkpoint, revision_provider=REVISION)
    coordinator.advance_stage(root, adapter=checkpoint, expected_stage="STAGE-11", revision_provider=REVISION)
    rejected = _staged(
        root,
        "delivery.acceptance",
        "TASK-ACCEPTANCE",
        "ART-ACCEPTANCE",
        evidence_overrides={"acceptance-report": {"decision": "rejected", "actor": "product-owner-1", "provenance": "human"}},
    )
    with pytest.raises(ValidationError, match="rejected|acceptance|decision"):
        advance(
            root,
            [{"work_item": "TASK-ACCEPTANCE", "staged": rejected}],
            {"TASK-ACCEPTANCE": _approval(rejected, "acceptance-approval", "product-owner")},
            adapter=checkpoint,
            revision_provider=REVISION,
        )
    production = _staged(root, "delivery.production-deploy", "TASK-PRODUCTION", "ART-RELEASE")
    with pytest.raises(ValidationError, match="blocked dependency"):
        advance(
            root,
            [{"work_item": "TASK-PRODUCTION", "staged": production}],
            {"TASK-PRODUCTION": _approval(production, "release-approval", "release-owner")},
            adapter=checkpoint,
            revision_provider=REVISION,
        )
    assert next(work for stage in resume_project(root).snapshot.plan.stages for work in stage.work_items if work.id == "TASK-PRODUCTION").status == "ready"


def _advance_to_production(root: Path, checkpoint: ConfirmedCheckpoint):
    for capability, work_item, artifact, _stage in ROUTE[:4]:
        staged = _staged(root, capability, work_item, artifact)
        approvals = {}
        if work_item == "TASK-ACCEPTANCE":
            approvals[work_item] = _approval(staged, "acceptance-approval", "product-owner")
        advance(root, [{"work_item": work_item, "staged": staged}], approvals, adapter=checkpoint, revision_provider=REVISION)
        if work_item in {"TASK-PREPARE", "TASK-HEALTH", "TASK-ACCEPTANCE"}:
            from ai_sdlc import orchestrator as coordinator
            coordinator.advance_stage(root, adapter=checkpoint, expected_stage=_stage, revision_provider=REVISION)
    return _staged(root, "delivery.production-deploy", "TASK-PRODUCTION", "ART-RELEASE")


def test_production_release_requires_fresh_authority_and_immutable_package_identity(tmp_path: Path) -> None:
    root = tmp_path / "release-guards"
    _materialize(root)
    checkpoint = ConfirmedCheckpoint()
    production = _advance_to_production(root, checkpoint)

    before = tree_hash(root)
    with pytest.raises(ValidationError, match="approval"):
        advance(root, [{"work_item": "TASK-PRODUCTION", "staged": production}], adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(root) == before

    invalid_authorities = (
        record_approval("wrong-gate", "release-owner-1", "release-owner", fingerprint_payload(staged_fingerprint(production).encode("utf-8"))),
        record_approval("release-approval", "product-owner-1", "product-owner", fingerprint_payload(staged_fingerprint(production).encode("utf-8"))),
        record_approval("release-approval", "release-owner-1", "release-owner", "0" * 64),
    )
    for invalid in invalid_authorities:
        with pytest.raises(ValidationError, match="approval"):
            advance(root, [{"work_item": "TASK-PRODUCTION", "staged": production}], {"TASK-PRODUCTION": invalid}, adapter=checkpoint, revision_provider=REVISION)
        assert tree_hash(root) == before

    drifted = replace(
        production,
        result=replace(
            production.result,
            evidence={**production.result.evidence, "package-identity": "sha256:drifted"},
        ),
    )
    valid_authority = _approval(drifted, "release-approval", "release-owner")
    with pytest.raises(ValidationError, match="package|identity"):
        advance(root, [{"work_item": "TASK-PRODUCTION", "staged": drifted}], {"TASK-PRODUCTION": valid_authority}, adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(root) == before


def test_production_release_rejects_completed_reports_with_failed_smoke_or_health(tmp_path: Path) -> None:
    root = tmp_path / "failed-production-checks"
    _materialize(root)
    checkpoint = ConfirmedCheckpoint()
    production = _advance_to_production(root, checkpoint)
    failed = replace(
        production,
        result=replace(
            production.result,
            evidence={
                **production.result.evidence,
                "production-smoke": "FAILED",
                "production-health": {"status": "failed", "report": "health failed"},
            },
        ),
    )
    before = tree_hash(root)
    with pytest.raises(ValidationError, match="pass|health|smoke"):
        advance(
            root,
            [{"work_item": "TASK-PRODUCTION", "staged": failed}],
            {"TASK-PRODUCTION": _approval(failed, "release-approval", "release-owner")},
            adapter=checkpoint,
            revision_provider=REVISION,
        )
    assert tree_hash(root) == before


def test_operations_require_incident_feedback_and_human_next_change_provenance(tmp_path: Path) -> None:
    root = tmp_path / "operations-provenance"
    _materialize(root)
    checkpoint = ConfirmedCheckpoint()
    production = _advance_to_production(root, checkpoint)
    advance(
        root,
        [{"work_item": "TASK-PRODUCTION", "staged": production}],
        {"TASK-PRODUCTION": _approval(production, "release-approval", "release-owner")},
        adapter=checkpoint,
        revision_provider=REVISION,
    )
    from ai_sdlc import orchestrator as coordinator
    coordinator.advance_stage(root, adapter=checkpoint, expected_stage="STAGE-13", revision_provider=REVISION)

    missing_records = _staged(
        root,
        "operations.monitor",
        "TASK-MONITOR",
        "ART-OPERATIONS",
        evidence_overrides={
            "incident-record": {"id": "INC-1", "provenance": "operations"},
            "feedback-record": {"id": "FDBK-1"},
        },
    )
    before = tree_hash(root)
    with pytest.raises(ValidationError, match="incident|feedback|record"):
        advance(root, [{"work_item": "TASK-MONITOR", "staged": missing_records}], adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(root) == before

    monitor = _staged(root, "operations.monitor", "TASK-MONITOR", "ART-OPERATIONS")
    advance(root, [{"work_item": "TASK-MONITOR", "staged": monitor}], adapter=checkpoint, revision_provider=REVISION)
    forged_request = _staged(
        root,
        "operations.next-change",
        "TASK-NEXT-CHANGE",
        "ART-NEXT-CHANGE",
        evidence_overrides={"next-change-request": {"id": "CR-FORGED", "actor": "", "provenance": "automation"}},
    )
    before = tree_hash(root)
    with pytest.raises(ValidationError, match="human|actor|provenance|change"):
        advance(root, [{"work_item": "TASK-NEXT-CHANGE", "staged": forged_request}], adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(root) == before

    failed = _staged(root, "delivery.production-deploy", "TASK-PRODUCTION", "ART-RELEASE", status="failed")
    with pytest.raises(ValidationError, match="completed"):
        advance(root, [{"work_item": "TASK-PRODUCTION", "staged": failed}], {"TASK-PRODUCTION": _approval(failed, "release-approval", "release-owner")}, adapter=checkpoint, revision_provider=REVISION)
    assert tree_hash(root) == before
