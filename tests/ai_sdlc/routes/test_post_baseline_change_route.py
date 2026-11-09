"""Task 17: controlled change after an approved baseline.

This route deliberately exercises the real specification-change capability.  A
change request is staged in a fixture, validated and previewed without touching
``specifications/current``; only an exact human approval and externally asserted
production evidence may advance the durable checkpoint.  The negative cases are
part of the route because a safe change workflow is defined as much by what it
refuses as by the final apply.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import sys
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.approvals import fingerprint_payload, record_approval  # noqa: E402
from ai_sdlc.contracts import load_contract  # noqa: E402
from ai_sdlc.checkpoint import recover  # noqa: E402
from ai_sdlc.adapters import build_dispatch, normalize_provider_output  # noqa: E402
from ai_sdlc.integration import integrate_staged_result, stage_result, validate_staged_result  # noqa: E402
from ai_sdlc.runtime import MockRuntime  # noqa: E402
from ai_sdlc.orchestrator import advance, initialize_project, resume_project  # noqa: E402
from ai_sdlc.providers import ProviderLock  # noqa: E402
from ai_sdlc.status_view import check_status, generate_status  # noqa: E402
from ai_sdlc.traceability import build_trace_graph, validate_traceability  # noqa: E402
from ai_sdlc.spec_changes import (  # noqa: E402
    ApplyResult,
    archive_change,
    apply_approved_delta,
    initialize_spec_baseline,
    preview_delta,
    validate_change_package,
)


ROUTE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "routes" / "change-project"
DOMAINS = ("requirements", "architecture", "data", "security", "deployment", "operations")


@pytest.mark.parametrize("domain", DOMAINS)
def test_each_change_impact_domain_is_dispatchable(domain: str) -> None:
    contract = load_contract(f"change.{domain}", 1)
    assert contract.dispatch["writable_scope"] == ["staging"]
    assert contract.integration["mappings"] == [f"change-{domain}"]
    assert contract.integration["human_approvals"] == ["specification-change"]
    assert contract.parallelism["allowed"] == "never"


def _runtime_result(root: Path, adapter_id: str, capability: str, work_item: str, artifact: str, raw: dict) -> object:
    contract = load_contract(capability, 1)
    request = build_dispatch(
        adapter_id,
        {name: f"fixture:{name}" for name in contract.dispatch["required_inputs"]},
        base_revision="0" * 40,
    )
    provider = MockRuntime({capability: {"capability": capability, "status": "completed", "payload": raw}}).invoke_skill(request)
    adapted = normalize_provider_output(adapter_id, provider.payload, base_revision="0" * 40)
    staged = stage_result(adapted.contract_result, root, contract)
    report = validate_staged_result(staged, contract)
    assert report.is_valid, report.findings
    return staged


def test_change_route_uses_locked_runtime_adapters_for_impact_implementation_and_deployment(tmp_path: Path) -> None:
    root, _, _ = _materialize_fixture(tmp_path)
    impact = _runtime_result(root, "change-route-impact", "change.impact", "TASK-IMPACT", "ART-IMPACT", {
        "provider": {"id": "change-route-impact", "revision": "1"}, "base_revision": "0" * 40,
        "outcome": "complete", "sections": {"work_item": "TASK-IMPACT", "impact": {domain: f"{domain} impact" for domain in DOMAINS}},
        "evidence": {"current-specification": "baseline-manifest.json", "traceability": [{"source": "SRC-CHG-017", "target": "ART-IMPACT", "relation": "traces_to"}]},
        "files": {"staging/change/impact.md": "# Impact\nrequirements, architecture, data, security, deployment, operations\n"}, "produced": ["ART-IMPACT"], "unresolved": [],
        "lifecycle_changes": [], "project_state_changes": [], "control_file_changes": [],
    })
    implementation = _runtime_result(root, "change-route-implementation", "change.implementation", "TASK-IMPLEMENTATION", "ART-IMPLEMENTATION", {
        "provider": {"id": "change-route-implementation", "revision": "1"}, "base_revision": "0" * 40,
        "outcome": "complete", "sections": {"work_item": "TASK-IMPLEMENTATION", "implementation": "tests=passed; review=approved; verification=passed"},
        "evidence": {"implementation": "tests exit_code:0; review approved; verification passed", "traceability": [{"source": "ART-IMPACT", "target": "ART-IMPLEMENTATION", "relation": "traces_to"}]},
        "files": {"staging/change/implementation.md": "# Implementation\nverified\n"}, "produced": ["ART-IMPLEMENTATION"], "unresolved": [],
        "lifecycle_changes": [], "project_state_changes": [], "control_file_changes": [],
    })
    preview = preview_delta(root / "specifications/current", root / "specifications/changes/active/CHG-017/delta.md")
    deployment = _runtime_result(root, "change-route-deployment", "change.route-deployment", "TASK-DEPLOYMENT", "ART-DEPLOYMENT", {
        "provider": {"id": "change-route-deployment", "revision": "1"}, "base_revision": "0" * 40,
        "outcome": "complete", "sections": {"work_item": "TASK-DEPLOYMENT", "deployment": "production deployed and externally verified"},
        "evidence": {"production-deployment": _production_evidence(preview, deployment_id="provider-deploy-017"), "traceability": [{"source": "ART-IMPLEMENTATION", "target": "ART-DEPLOYMENT", "relation": "traces_to"}]},
        "files": {"staging/change/deployment.md": "# Deployment\nproduction\n"}, "produced": ["ART-DEPLOYMENT"], "unresolved": [],
        "lifecycle_changes": [], "project_state_changes": [], "control_file_changes": [],
    })
    for staged in (impact, implementation, deployment):
        integrate_staged_result(staged, root, load_contract(staged.contract_id, 1))
        assert (root / "staging/change").is_dir()


class _ConfirmedRouteCheckpoint:
    def __init__(self) -> None:
        self.identities: list[str] = []

    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> str:
        self.identities.append("route-checkpoint")
        return "route-checkpoint"


def _spec_adapter() -> _ConfirmedRouteCheckpoint:
    return _ConfirmedRouteCheckpoint()


def _canonical_route(root: Path) -> ProviderLock:
    lock = ProviderLock(schema_version=1, capabilities={
        "change.impact": {"contract_version": 1, "provider": "change-route-impact", "source": "builtin://change-route-impact", "revision": "1", "integrity": "sha256:" + "a" * 64, "status": "enabled"},
        "change.implementation": {"contract_version": 1, "provider": "change-route-implementation", "source": "builtin://change-route-implementation", "revision": "1", "integrity": "sha256:" + "b" * 64, "status": "enabled"},
        "change.route-deployment": {"contract_version": 1, "provider": "change-route-deployment", "source": "builtin://change-route-deployment", "revision": "1", "integrity": "sha256:" + "c" * 64, "status": "enabled"},
    })
    initialize_project(root, SKILL_ROOT, lock)
    plan_path = root / "docs/project-control/project-plan.yaml"
    state_path = root / "docs/project-control/project-state.yaml"
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["project_id"] = "CHANGE-ROUTE"
    plan["stages"][0]["exit_criteria"] = [{"id": id_, "text": id_, "status": "pending"} for id_ in ("TASK-IMPACT", "TASK-IMPLEMENTATION", "TASK-DEPLOYMENT")]
    plan["stages"][0]["work_items"] = [{"id": "TASK-001", "title": "Registered baseline", "status": "done", "executor": "coordinator", "depends_on": [], "produces": [], "scope": {"artifacts": [], "paths": ["docs/change-control"]}},
        {"id": "TASK-IMPACT", "title": "Promote validated impact", "status": "ready", "executor": "coordinator", "depends_on": [], "produces": ["ART-IMPACT"], "scope": {"artifacts": ["ART-IMPACT"], "paths": ["staging/change"]}, "criterion_ids": ["TASK-IMPACT"], "criterion_evidence": ["traceability"], "plan": "capability: change.impact"},
        {"id": "TASK-IMPLEMENTATION", "title": "Promote implementation evidence", "status": "ready", "executor": "coordinator", "depends_on": ["TASK-IMPACT"], "produces": ["ART-IMPLEMENTATION"], "scope": {"artifacts": ["ART-IMPLEMENTATION"], "paths": ["staging/change"]}, "criterion_ids": ["TASK-IMPLEMENTATION"], "criterion_evidence": ["traceability"], "plan": "capability: change.implementation"},
        {"id": "TASK-DEPLOYMENT", "title": "Promote deployment evidence", "status": "ready", "executor": "coordinator", "depends_on": ["TASK-IMPLEMENTATION"], "produces": ["ART-DEPLOYMENT"], "scope": {"artifacts": ["ART-DEPLOYMENT"], "paths": ["staging/change"]}, "criterion_ids": ["TASK-DEPLOYMENT"], "criterion_evidence": ["traceability"], "plan": "capability: change.route-deployment"},
    ]
    state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    state.update({"project_id": "CHANGE-ROUTE", "focus_work_items": ["TASK-IMPACT"], "next_actions": [{"work_item": "TASK-IMPACT", "action": "Promote validated impact"}]})
    artifacts = {"artifacts": [
        {"id": "ART-IMPACT", "kind": "requirements_analysis", "path": "staging/change/impact.md", "status": "in_review"},
        {"id": "ART-IMPLEMENTATION", "kind": "test_report", "path": "staging/change/implementation.md", "status": "in_review"},
        {"id": "ART-DEPLOYMENT", "kind": "deployment_package", "path": "staging/change/deployment.md", "status": "in_review"},
    ]}
    sources = {"sources": [{"id": "SRC-CHG-017", "kind": "business_document", "title": "Change request", "origin": "route-fixture", "received_at": "2026-11-04", "version": "1", "location": "docs/change-control/change-request.md", "status": "registered"}]}
    (root / "docs/change-control").mkdir(parents=True, exist_ok=True)
    (root / "docs/change-control/change-request.md").write_text("# CR-017\n", encoding="utf-8")
    for path, document in ((plan_path, plan), (state_path, state), (root / "docs/artifact-register.yaml", artifacts), (root / "docs/product-definition/sources/source-register.yaml", sources)):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    generate_status(root)
    return lock


def test_change_route_promotes_runtime_results_then_resumes_canonical_trace_and_status(tmp_path: Path) -> None:
    root, _, _ = _materialize_fixture(tmp_path)
    lock = _canonical_route(root)
    checkpoint = _ConfirmedRouteCheckpoint()
    impact = _runtime_result(root, "change-route-impact", "change.impact", "TASK-IMPACT", "ART-IMPACT", {
        "provider": {"id": "change-route-impact", "revision": "1"}, "base_revision": "0" * 40, "outcome": "complete",
        "sections": {"work_item": "TASK-IMPACT", "impact": {domain: f"{domain} impact" for domain in DOMAINS}},
        "evidence": {"current-specification": "baseline-manifest.json", "traceability": [{"source": "SRC-CHG-017", "target": "ART-IMPACT", "relation": "traces_to"}]}, "files": {"staging/change/impact.md": "impact\n"}, "produced": ["ART-IMPACT"], "unresolved": [], "lifecycle_changes": [], "project_state_changes": [], "control_file_changes": [],
    })
    advance(root, [{"work_item": "TASK-IMPACT", "staged": impact}], adapter=checkpoint, revision_provider="0" * 40)
    implementation = _runtime_result(root, "change-route-implementation", "change.implementation", "TASK-IMPLEMENTATION", "ART-IMPLEMENTATION", {
        "provider": {"id": "change-route-implementation", "revision": "1"}, "base_revision": "0" * 40, "outcome": "complete",
        "sections": {"work_item": "TASK-IMPLEMENTATION", "implementation": "tests exit_code:0; review approved; verification passed"}, "evidence": {"implementation": "tests and review passed", "traceability": [{"source": "ART-IMPACT", "target": "ART-IMPLEMENTATION", "relation": "traces_to"}]}, "files": {"staging/change/implementation.md": "implementation\n"}, "produced": ["ART-IMPLEMENTATION"], "unresolved": [], "lifecycle_changes": [], "project_state_changes": [], "control_file_changes": [],
    })
    advance(root, [{"work_item": "TASK-IMPLEMENTATION", "staged": implementation}], adapter=checkpoint, revision_provider="0" * 40)
    preview = preview_delta(root / "specifications/current", root / "specifications/changes/active/CHG-017/delta.md")
    deployment = _runtime_result(root, "change-route-deployment", "change.route-deployment", "TASK-DEPLOYMENT", "ART-DEPLOYMENT", {
        "provider": {"id": "change-route-deployment", "revision": "1"}, "base_revision": "0" * 40, "outcome": "complete",
        "sections": {"work_item": "TASK-DEPLOYMENT", "deployment": "production deployed externally"}, "evidence": {"production-deployment": _production_evidence(preview, deployment_id="provider-deploy-017", impact_evidence={"content": "six-domain impact", "sha256": hashlib.sha256(b"six-domain impact").hexdigest()}, implementation_evidence={"content": "tests/review/verification passed", "sha256": hashlib.sha256(b"tests/review/verification passed").hexdigest()}), "traceability": [{"source": "ART-IMPLEMENTATION", "target": "ART-DEPLOYMENT", "relation": "traces_to"}]}, "files": {"staging/change/deployment.md": "deployment\n"}, "produced": ["ART-DEPLOYMENT"], "unresolved": [], "lifecycle_changes": [], "project_state_changes": [], "control_file_changes": [],
    })
    final = advance(root, [{"work_item": "TASK-DEPLOYMENT", "staged": deployment}], adapter=checkpoint, revision_provider="0" * 40)
    assert {item.id: item.status for item in final.plan.stages[0].work_items} == {"TASK-001": "done", "TASK-IMPACT": "done", "TASK-IMPLEMENTATION": "done", "TASK-DEPLOYMENT": "done"}
    approval = record_approval("specification-change", "release-owner-017", "product-owner", fingerprint_payload(preview.binding_payload))
    result = apply_approved_delta(preview, approval, deployment.result.evidence["production-deployment"], adapter=checkpoint)
    archived = archive_change(result, adapter=checkpoint)
    resumed = resume_project(root)
    assert check_status(root).is_valid
    assert generate_status(root).read_bytes() == (root / "PROJECT_STATUS.md").read_bytes()
    graph = build_trace_graph(resumed.snapshot)
    assert validate_traceability(graph).is_valid
    links = {(edge.source, edge.target) for edge in graph.edges}
    assert {("SRC-CHG-017", "ART-IMPACT"), ("ART-IMPACT", "ART-IMPLEMENTATION"), ("ART-IMPLEMENTATION", "ART-DEPLOYMENT")} <= links
    assert archived.verified
    document = json.loads((archived.path / "archive.json").read_text(encoding="utf-8"))
    assert document["root_identity"] == result.preview.root_identity
    assert document["root_identity_sha256"]
    assert document["before_hashes"] == dict(result.before_hashes)
    assert document["source_hashes"] == dict(preview.source_hashes)
    assert document["result_hashes"] == dict(preview.result_hashes)
    assert document["after_hashes"] == dict(result.after_hashes)
    assert document["change_raw_sha256"]
    assert document["delta_raw_sha256"]
    assert document["baseline_manifest_raw_sha256"] == hashlib.sha256(
        (root / "specifications" / "baseline-manifest.json").read_bytes()
    ).hexdigest()
    assert document["baseline_manifest"]["previous_manifest_sha256"] == preview.baseline_manifest_hash
    assert document["checkpoint_identity"] == result.checkpoint_identity
    assert document["operations"]
    assert document["trace_ids"] == list(preview.trace_ids)
    assert document["deployment_evidence"]["impact_evidence"]["sha256"] == hashlib.sha256(b"six-domain impact").hexdigest()
    assert document["deployment_evidence"]["implementation_evidence"]["sha256"] == hashlib.sha256(b"tests/review/verification passed").hexdigest()
    archive_json = archived.path / "archive.json"
    original_archive = archive_json.read_bytes()
    archive_json.write_bytes(original_archive + b"tampered\n")
    with pytest.raises(ValueError, match="immutable|tamper"):
        archive_change(result, adapter=checkpoint)


def _materialize_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "change-project"
    shutil.copytree(ROUTE_FIXTURE, root)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    current = root / "specifications" / "current"
    package = root / "specifications" / "changes" / "active" / "CHG-017"
    initialize_spec_baseline(current)
    return root, current, package


def _production_evidence(preview, **overrides: object) -> dict[str, object]:
    evidence: dict[str, object] = {
        "deployment_id": "prod-change-017",
        "change_id": preview.change_id,
        "preview_fingerprint": preview.fingerprint,
        "environment": "production",
        "status": "deployed",
        "source_hashes": dict(preview.source_hashes),
        "result_hashes": dict(preview.result_hashes),
        "assurance": "external-asserted",
    }
    evidence.update(overrides)
    return evidence


def test_post_baseline_change_route_requires_exact_approval_and_production_evidence(tmp_path: Path) -> None:
    root, current, package = _materialize_fixture(tmp_path)

    # Intake/source registration is an immutable input; impact, implementation,
    # and deployment evidence are produced by the route runtime below.
    assert (root / "docs/change-control/source-register.yaml").is_file()

    validation = validate_change_package(package)
    assert validation.is_valid, validation.findings

    # A preview is deterministic and does not mutate any current specification.
    before = {path.name: path.read_bytes() for path in current.glob("*.md")}
    manifest_before = (current.parent / "baseline-manifest.json").read_bytes()
    first = preview_delta(current, package / "delta.md")
    second = preview_delta(current, package / "delta.md")
    assert first.fingerprint == second.fingerprint
    assert first.diff == second.diff
    assert dict(first.source_hashes) == dict(second.source_hashes)
    assert set(first.affected_ids) == {f"REQ-{index:03d}" for index in range(101, 107)}
    assert {path.name: path.read_bytes() for path in current.glob("*.md")} == before
    assert (current.parent / "baseline-manifest.json").read_bytes() == manifest_before

    approval = record_approval(
        "specification-change",
        "release-owner-017",
        "product-owner",
        fingerprint_payload(first.binding_payload),
    )

    # Every failed guard is pre-mutation and recoverable.
    invalid_approvals = (
        record_approval("wrong-gate", "release-owner-017", "product-owner", fingerprint_payload(first.binding_payload)),
        record_approval("specification-change", "release-owner-017", "architect", fingerprint_payload(first.binding_payload)),
        record_approval("specification-change", "release-owner-017", "product-owner", "0" * 64),
    )
    for invalid in invalid_approvals:
        with pytest.raises(ValueError, match="approval"):
            apply_approved_delta(first, invalid, _production_evidence(first), adapter=_spec_adapter())
        assert {path.name: path.read_bytes() for path in current.glob("*.md")} == before

    for evidence in (
        {},
        _production_evidence(first, environment="staging"),
        _production_evidence(first, assurance="asserted"),
        _production_evidence(first, status="failed"),
    ):
        with pytest.raises(ValueError, match="production|evidence"):
            apply_approved_delta(first, approval, evidence, adapter=_spec_adapter())
        assert {path.name: path.read_bytes() for path in current.glob("*.md")} == before

    # A stale source after preview is rejected without mutation, then recovered.
    changed = current / "requirements.md"
    original = changed.read_bytes()
    changed.write_bytes(original + b"\n# unapproved edit\n")
    with pytest.raises(ValueError, match="changed|stale|fingerprint"):
        apply_approved_delta(first, approval, _production_evidence(first), adapter=_spec_adapter())
    changed.write_bytes(original)
    assert {path.name: path.read_bytes() for path in current.glob("*.md")} == before

    # Delta and change metadata are both bound into the approval payload.
    delta_path = package / "delta.md"
    delta_bytes = delta_path.read_bytes()
    delta_path.write_bytes(delta_bytes + b"\n")
    with pytest.raises(ValueError, match="delta changed|stale|fingerprint"):
        apply_approved_delta(first, approval, _production_evidence(first), adapter=_spec_adapter())
    delta_path.write_bytes(delta_bytes)

    # A concurrent active package with overlapping impact cannot be accepted.
    overlap = package.parent / "CHG-018"
    overlap.mkdir()
    (overlap / "change.md").write_text(
        "---\n"
        "id: CHG-018\n"
        "title: Conflicting change\n"
        "status: draft\n"
        "affects:\n"
        "  - requirements.md\n"
        "---\n",
        encoding="utf-8",
    )
    (overlap / "delta.md").write_text(
        "## MODIFIED\n\n### REQ-101: Conflicting target\n\nConflicting.\n",
        encoding="utf-8",
    )
    assert any(finding.code == "active_overlap" for finding in validate_change_package(package).findings)
    with pytest.raises(ValueError, match="overlap|active change"):
        apply_approved_delta(first, approval, _production_evidence(first), adapter=_spec_adapter())
    assert {path.name: path.read_bytes() for path in current.glob("*.md")} == before
    shutil.rmtree(overlap)

    result = apply_approved_delta(first, approval, _production_evidence(first), adapter=_spec_adapter())
    assert result.verified
    assert result.status == "applied"
    assert result.checkpoint_identity == "route-checkpoint"
    for relative, expected in first.result_bytes.items():
        assert (current / relative).read_bytes() == expected

    # Verification/resume/trace/status are represented by the durable archive:
    # it contains exact hashes, all affected IDs, and the external deployment.
    archive = archive_change(result)
    assert archive.verified
    document = json.loads((archive.path / "archive.json").read_text(encoding="utf-8"))
    assert document["status"] == "verified-applied"
    assert document["preview_fingerprint"] == first.fingerprint
    assert document["approval_fingerprint"] == approval.fingerprint
    assert document["after_hashes"] == dict(first.result_hashes)
    assert document["affected_ids"] == list(first.affected_ids)
    assert document["deployment_evidence"]["assurance"] == "external-asserted"
    assert set(document["affected_paths"]) == set(first.result_bytes)
    assert document["baseline_manifest"]["change_id"] == first.change_id
    assert document["baseline_manifest"]["preview_fingerprint"] == first.fingerprint


def test_post_baseline_route_rejects_mutated_change_metadata_and_unverified_archive(tmp_path: Path) -> None:
    _, current, package = _materialize_fixture(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval(
        "specification-change", "release-owner-017", "product-owner", fingerprint_payload(preview.binding_payload)
    )
    evidence = _production_evidence(preview)

    change_path = package / "change.md"
    original = change_path.read_bytes()
    change_path.write_bytes(original + b"\nchanged after approval\n")
    with pytest.raises(ValueError, match="change package changed|changed|stale"):
        apply_approved_delta(preview, approval, evidence, adapter=_spec_adapter())
    change_path.write_bytes(original)

    # Archiving is available only after an applied, verified result.
    with pytest.raises(ValueError, match="unverified|applied"):
        archive_change(ApplyResult(
            preview, preview.change_id, False, "pending", {}, {}, approval, evidence, package
        ))


def test_post_baseline_route_rejects_current_drift_against_promoted_baseline_manifest(tmp_path: Path) -> None:
    _, current, package = _materialize_fixture(tmp_path)
    changed = current / "security.md"
    changed.write_bytes(changed.read_bytes() + b"\n# out-of-band edit\n")
    with pytest.raises(ValueError, match="baseline|current|drift|changed"):
        preview_delta(current, package / "delta.md")


def test_post_baseline_route_checkpoint_failure_recovers_without_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, current, package = _materialize_fixture(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval(
        "specification-change", "release-owner-017", "product-owner", fingerprint_payload(preview.binding_payload)
    )
    before = {path.name: path.read_bytes() for path in current.glob("*.md")}
    manifest_before = (current.parent / "baseline-manifest.json").read_bytes()
    import ai_sdlc.checkpoint as checkpoint_module

    original_replace = checkpoint_module._atomic_replace
    calls = 0

    def fail_during_write(target: Path, content: bytes) -> None:
        nonlocal calls
        if target.parent == current:
            calls += 1
            if calls == 2:
                raise OSError("injected route checkpoint failure")
        original_replace(target, content)

    monkeypatch.setattr(checkpoint_module, "_atomic_replace", fail_during_write)
    with pytest.raises(OSError, match="checkpoint|injected"):
        apply_approved_delta(preview, approval, _production_evidence(preview), adapter=_spec_adapter())
    assert {path.name: path.read_bytes() for path in current.glob("*.md")} == before
    assert (current.parent / "baseline-manifest.json").read_bytes() == manifest_before
    assert recover(current.parent).is_clean
    assert recover(current.parent).is_clean
    assert not (package.parent.parent / "archive" / "CHG-017").exists()
