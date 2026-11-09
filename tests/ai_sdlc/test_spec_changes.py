"""Task 12 contract tests for governed specification deltas."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "ai-sdlc-orchestrator" / "scripts"))

from ai_sdlc.approvals import record_approval  # noqa: E402
from ai_sdlc.spec_changes import (  # noqa: E402
    archive_change,
    apply_approved_delta,
    initialize_spec_baseline,
    preview_delta,
    validate_change_package,
)


def _evidence(preview, **extra):
    value = {
        "deployment_id": "deploy-001",
        "change_id": preview.change_id,
        "preview_fingerprint": preview.fingerprint,
        "environment": "production",
        "status": "deployed",
        "source_hashes": dict(preview.source_hashes),
        "result_hashes": dict(preview.result_hashes),
        "assurance": "external-asserted",
    }
    value.update(extra)
    return value


CURRENT = """# Current\n\n## REQ-001: Existing rule\n\nThe existing rule.\n\n## REQ-003: To remove\n\nOld rule.\n"""
DELTA = """## ADDED\n\n### REQ-002: New rule\n\nThe new rule.\n\n## MODIFIED\n\n### REQ-001: Updated rule\n\nThe updated rule.\n\n## REMOVED\n\n### REQ-003: To remove\n\nNo longer needed.\n"""


def _package(tmp_path: Path, delta: str = DELTA) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    specs = tmp_path / "specifications"
    current = specs / "current"
    active = specs / "changes" / "active"
    (active / "CHG-001").mkdir(parents=True)
    current.mkdir(parents=True)
    (current / "requirements.md").write_text(CURRENT, encoding="utf-8")
    initialize_spec_baseline(current)
    package = active / "CHG-001"
    (package / "change.md").write_text(
        "---\n"
        "id: CHG-001\n"
        "title: Update requirements\n"
        "status: draft\n"
        "affects:\n"
        "  - requirements.md\n"
        "---\n\n"
        "## Reason\n\nA governed change.\n\n## Scope\n\nRequirements.\n",
        encoding="utf-8",
    )
    (package / "delta.md").write_text(delta, encoding="utf-8")
    return current, package


class _TestCommitAdapter:
    """Explicit test-only checkpoint adapter for non-Git unit fixtures."""

    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> str:
        return "test-checkpoint"


def _adapter() -> _TestCommitAdapter:
    return _TestCommitAdapter()


def test_validate_and_preview_support_added_modified_removed_and_ignore_fenced_headings(tmp_path: Path) -> None:
    current, package = _package(tmp_path, DELTA + "\n```markdown\n## ADDED\n### REQ-999: Fake\n```\n")
    report = validate_change_package(package)
    assert report.is_valid

    preview = preview_delta(current, package / "delta.md")
    assert preview.affected_ids == ("REQ-001", "REQ-002", "REQ-003")
    result = preview.result_bytes["requirements.md"]
    assert b"REQ-002: New rule" in result
    assert b"REQ-001: Updated rule" in result
    assert b"REQ-003: To remove" not in result
    assert b"REQ-999" not in result  # fenced headings are ignored by the parser


def test_preview_and_validation_fail_closed_without_promoted_baseline(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    (current.parent / "baseline-manifest.json").unlink()
    assert any(finding.code == "invalid_baseline" for finding in validate_change_package(package).findings)
    with pytest.raises(ValueError, match="baseline manifest"):
        preview_delta(current, package / "delta.md")


def test_baseline_initialization_is_explicit_and_non_overwriting(tmp_path: Path) -> None:
    current, _ = _package(tmp_path)
    manifest = current.parent / "baseline-manifest.json"
    original = manifest.read_bytes()
    with pytest.raises(FileExistsError):
        initialize_spec_baseline(current)
    assert manifest.read_bytes() == original
    promoted = initialize_spec_baseline(current, replace=True, change_id="CHG-009")
    assert promoted["change_id"] == "CHG-009"
    assert json.loads(manifest.read_text(encoding="utf-8"))["specifications"]["requirements.md"]


def test_manifest_tamper_after_approval_is_rejected_before_mutation(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    manifest = current.parent / "baseline-manifest.json"
    raw = manifest.read_bytes()
    manifest.write_bytes(raw.replace(b'"change_id": null', b'"change_id": "TAMPERED"'))
    before = {path.name: path.read_bytes() for path in current.glob("*.md")}
    with pytest.raises(ValueError, match="baseline manifest"):
        apply_approved_delta(preview, approval, _evidence(preview), adapter=_adapter())
    assert {path.name: path.read_bytes() for path in current.glob("*.md")} == before


def test_second_change_can_preview_and_apply_after_manifest_checkpoint_update(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    first = preview_delta(current, package / "delta.md")
    first_approval = record_approval("specification-change", "alice", "product-owner", first.fingerprint)
    first_result = apply_approved_delta(first, first_approval, _evidence(first), adapter=_adapter())
    archive_change(first_result, adapter=_adapter())
    shutil.rmtree(package)

    second = package.parent / "CHG-002"
    second.mkdir()
    (second / "change.md").write_text(
        "---\n id: CHG-002\n title: Follow-up\n status: draft\n affects:\n  - requirements.md\n---\n",
        encoding="utf-8",
    )
    (second / "delta.md").write_text(
        "## MODIFIED\n\n### REQ-002: Follow-up rule\n\nFollow-up.\n", encoding="utf-8"
    )
    next_preview = preview_delta(current, second / "delta.md")
    next_approval = record_approval("specification-change", "alice", "product-owner", next_preview.fingerprint)
    next_result = apply_approved_delta(next_preview, next_approval, _evidence(next_preview, deployment_id="deploy-002"), adapter=_adapter())
    assert next_result.verified
    document = json.loads((current.parent / "baseline-manifest.json").read_text(encoding="utf-8"))
    assert document["previous_manifest_sha256"] == next_preview.baseline_manifest_hash
    assert document["previous_manifest_sha256"] != first.baseline_manifest_hash
    assert document["change_id"] == "CHG-002"


def test_validation_rejects_duplicate_ids_and_missing_targets(tmp_path: Path) -> None:
    current, package = _package(tmp_path, "## ADDED\n\n### REQ-002: One\n\nA.\n\n### REQ-002: Two\n\nB.\n")
    report = validate_change_package(package)
    assert not report.is_valid
    assert any(f.code == "duplicate_delta_id" for f in report.findings)

    _, missing_package = _package(tmp_path / "missing", "## MODIFIED\n\n### REQ-404: Missing\n\nNo target.\n")
    report = validate_change_package(missing_package)
    assert not report.is_valid
    assert any(f.code == "missing_target" for f in report.findings)


def test_apply_requires_exact_approval_and_production_evidence(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    with pytest.raises(ValueError, match="production"):
        apply_approved_delta(preview, approval, _evidence(preview, environment="staging"), adapter=_adapter())

    changed = current / "requirements.md"
    changed.write_text(CURRENT.replace("existing rule", "changed rule"), encoding="utf-8")
    with pytest.raises(ValueError, match="stale|changed|fingerprint"):
        apply_approved_delta(preview, approval, _evidence(preview), adapter=_adapter())


def test_apply_rolls_back_partial_failure_and_archive_is_verified_immutable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current, package = _package(tmp_path)
    second = current / "other.md"
    second.write_text("## REQ-010: Other\n\nOther.\n", encoding="utf-8")
    initialize_spec_baseline(current, replace=True)
    # The package targets both files so rollback must cover a multi-file write.
    (package / "change.md").write_text(
        (package / "change.md").read_text(encoding="utf-8").replace("- requirements.md", "- requirements.md\n  - other.md"),
        encoding="utf-8",
    )
    (package / "delta.md").write_text(DELTA + "\n## MODIFIED\n\n### REQ-010: Other updated\n\nChanged.\n", encoding="utf-8")
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    import ai_sdlc.checkpoint as checkpoint_module

    original = checkpoint_module._atomic_replace
    calls = 0

    def fail_second(target: Path, content: bytes) -> None:
        nonlocal calls
        if target.parent == current:
            calls += 1
        if calls == 2 and target.parent == current:
            raise OSError("injected write failure")
        original(target, content)

    monkeypatch.setattr(checkpoint_module, "_atomic_replace", fail_second)
    with pytest.raises(OSError):
        apply_approved_delta(preview, approval, _evidence(preview), adapter=_adapter())
    assert (current / "requirements.md").read_text(encoding="utf-8") == CURRENT
    assert second.read_text(encoding="utf-8") == "## REQ-010: Other\n\nOther.\n"
    from ai_sdlc.checkpoint import recover
    assert recover(current.parent).is_clean

    monkeypatch.setattr(checkpoint_module, "_atomic_replace", original)
    result = apply_approved_delta(preview, approval, _evidence(preview), adapter=_adapter())
    record = archive_change(result, adapter=_adapter())
    assert record.verified
    archive_manifest = Path(record.path) / "archive.json"
    before = archive_manifest.read_bytes()
    archive_manifest.write_bytes(before + b"\n")
    with pytest.raises(ValueError, match="immutable|tamper"):
        archive_change(result)


def test_deployment_evidence_is_bound_to_change_preview_and_hashes(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    for bad in (
        {"environment": "production", "status": "deployed"},
        _evidence(preview, change_id="CHG-999"),
        _evidence(preview, preview_fingerprint="0" * 64),
        _evidence(preview, result_hashes={"requirements.md": "0" * 64}),
    ):
        with pytest.raises(ValueError, match="evidence|bound|deployment"):
            apply_approved_delta(preview, approval, bad, adapter=_adapter())


def test_deployment_assurance_must_be_external_asserted_and_never_mutates(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    before = (current / "requirements.md").read_bytes()
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    with pytest.raises(ValueError, match="production|evidence"):
        apply_approved_delta(preview, approval, _evidence(preview, assurance="asserted"), adapter=_adapter())
    assert (current / "requirements.md").read_bytes() == before


def test_approval_requires_actor_role_assurance_and_one_exact_gate(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    evidence = _evidence(preview)
    for approval in (
        {"gate": "change-approval", "role": "product-owner", "decision": "approved", "fingerprint": preview.fingerprint},
        {"gate": "specification-change", "role": "product-owner", "decision": "approved", "fingerprint": preview.fingerprint},
        {"gate": "specification-change", "actor": "alice", "role": "product-owner", "role_assurance": "unverified", "decision": "approved", "fingerprint": preview.fingerprint},
    ):
        with pytest.raises(ValueError, match="approval"):
            apply_approved_delta(preview, approval, evidence, adapter=_adapter())


def test_preview_binds_raw_package_inputs_and_rejects_external_delta(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    (package / "change.md").write_text((package / "change.md").read_text(encoding="utf-8") + "\ncontext changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed|stale|fingerprint"):
        apply_approved_delta(preview, approval, _evidence(preview), adapter=_adapter())

    outside = tmp_path / "external-delta.md"
    outside.write_text(DELTA, encoding="utf-8")
    with pytest.raises(ValueError, match="package|bound|validated"):
        preview_delta(current, outside)


def test_active_overlap_normalizes_dot_alias_and_ancestor_paths(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    second = package.parent / "CHG-002"
    second.mkdir()
    (second / "change.md").write_text(
        "---\nid: CHG-002\ntitle: Other\nstatus: draft\naffects:\n  - ./requirements.md\n---\n", encoding="utf-8"
    )
    (second / "delta.md").write_text("## MODIFIED\n\n### REQ-001: Other\n\nOther.\n", encoding="utf-8")
    report = validate_change_package(package)
    assert any(f.code == "active_overlap" for f in report.findings)


def test_malformed_frontmatter_and_schema_runtime_mismatch_are_rejected(tmp_path: Path) -> None:
    _, package = _package(tmp_path)
    (package / "change.md").write_text(
        "---\nid: CHG-001\ntitle: Update\nstatus: draft\naffects: requirements.md\n----\n", encoding="utf-8"
    )
    report = validate_change_package(package)
    assert not report.is_valid
    assert any(f.code in {"invalid_frontmatter", "schema_validation"} for f in report.findings)


def test_archive_record_contains_full_trace_and_raw_input_binding(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    result = apply_approved_delta(preview, approval, _evidence(preview), adapter=_adapter())
    record = archive_change(result, adapter=_adapter())
    document = json.loads((record.path / "archive.json").read_text(encoding="utf-8"))
    assert set(document["affected_ids"]) == set(preview.affected_ids)
    assert document["affected_paths"] == sorted(preview.result_bytes)
    assert document["change_raw_sha256"] == __import__("hashlib").sha256((package / "change.md").read_bytes()).hexdigest()
    assert document["delta_raw_sha256"] == __import__("hashlib").sha256((package / "delta.md").read_bytes()).hexdigest()


def test_archive_rejects_symlinked_package_boundary(tmp_path: Path) -> None:
    current, package = _package(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    result = apply_approved_delta(preview, approval, _evidence(preview), adapter=_adapter())
    alias = package.parent / "CHG-ALIAS"
    try:
        alias.symlink_to(package, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError, match="boundary|symlink|unsafe"):
        archive_change(replace(result, package_path=alias))


def test_root_identity_prevents_cross_repository_preview_replay(tmp_path: Path) -> None:
    current_a, package_a = _package(tmp_path / "repo-a")
    current_b, package_b = _package(tmp_path / "repo-b")
    preview_a = preview_delta(current_a, package_a / "delta.md")
    preview_b = preview_delta(current_b, package_b / "delta.md")
    assert preview_a.fingerprint != preview_b.fingerprint
    approval = record_approval("specification-change", "alice", "product-owner", preview_a.fingerprint)
    with pytest.raises(ValueError, match="approval|fingerprint|root"):
        apply_approved_delta(preview_b, approval, _evidence(preview_b), adapter=_adapter())


def test_simulated_reparse_component_is_rejected_deterministically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current, package = _package(tmp_path)
    import ai_sdlc.spec_changes as module

    monkeypatch.setattr(module, "_is_reparse_point", lambda path: path == package / "change.md")
    report = validate_change_package(package)
    assert not report.is_valid
    assert any(f.code == "reparse_point" for f in report.findings)


def test_post_commit_verification_failure_returns_auditable_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current, package = _package(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    evidence = _evidence(preview)
    import ai_sdlc.spec_changes as module

    original_commit = module.commit_checkpoint

    def commit_then_external_change(*args, **kwargs):
        identity = original_commit(*args, **kwargs)
        (current / "requirements.md").write_text("external-after-commit\n", encoding="utf-8")
        return identity

    monkeypatch.setattr(module, "commit_checkpoint", commit_then_external_change)
    result = apply_approved_delta(preview, approval, evidence, adapter=_adapter())
    assert not result.verified
    assert result.status == "committed_conflict"
    assert result.checkpoint_identity
    assert result.findings
    with pytest.raises(ValueError, match="unverified|applied"):
        archive_change(result)
