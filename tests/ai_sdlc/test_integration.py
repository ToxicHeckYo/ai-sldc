"""Security and transaction tests for provider staging/integration."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from dataclasses import replace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "ai-sdlc-orchestrator" / "scripts"))

from ai_sdlc.contracts import load_contract  # noqa: E402
from ai_sdlc import integration as integration_module  # noqa: E402
from ai_sdlc.integration import (  # noqa: E402
    integrate_staged_result,
    stage_result,
    staged_fingerprint,
    tree_hash,
    validate_staged_result,
)
from ai_sdlc.runtime import ApprovalResult, ProviderResult  # noqa: E402


def contract():
    return load_contract("implementation.tdd", 1)


def valid_result(**kwargs) -> ProviderResult:
    values = {
        "capability": "implementation.tdd",
        "status": "completed",
        "sections": {"red": "failing-test", "green": "passing-test", "verification": "ok"},
        "evidence": {"failing-test": "pytest red", "passing-test": "pytest green"},
        "files": {"staging/report.md": "report"},
    }
    values.update(kwargs)
    return ProviderResult(**values)


def test_valid_result_is_staged_validated_then_integrated(tmp_path: Path) -> None:
    (tmp_path / "staging").mkdir()
    staged = stage_result(valid_result(), tmp_path, contract())

    assert validate_staged_result(staged, contract()).is_valid
    integrated = integrate_staged_result(staged, tmp_path)

    assert integrated.changed_paths == ("staging/report.md",)
    assert (tmp_path / "staging/report.md").read_text() == "report"


def test_out_of_scope_result_is_rejected_without_canonical_mutation(tmp_path: Path) -> None:
    (tmp_path / "staging").mkdir()
    (tmp_path / "canonical.txt").write_bytes(b"unchanged")
    before = tree_hash(tmp_path)
    staged = stage_result(valid_result(files={"../canonical.txt": "tampered"}), tmp_path, contract())

    report = validate_staged_result(staged, contract())

    assert not report.is_valid
    assert any(f.code == "out_of_scope_path" for f in report.findings)
    assert tree_hash(tmp_path) == before
    with pytest.raises(ValueError):
        integrate_staged_result(staged, tmp_path)
    assert (tmp_path / "canonical.txt").read_bytes() == b"unchanged"


def test_validation_failure_does_not_partially_write_canonical_tree(tmp_path: Path) -> None:
    (tmp_path / "staging").mkdir()
    (tmp_path / "staging/old.txt").write_bytes(b"old")
    before = tree_hash(tmp_path)
    staged = stage_result(valid_result(files={"staging/new.txt": "new", "staging/../bad.txt": "bad"}), tmp_path, contract())

    with pytest.raises(ValueError):
        integrate_staged_result(staged, tmp_path)

    assert tree_hash(tmp_path) == before
    assert not (tmp_path / "staging/new.txt").exists()


def test_absolute_and_symlink_ambiguous_paths_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "staging").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / "staging/link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    staged = stage_result(valid_result(files={"staging/link/escape.txt": "escape"}), tmp_path, contract())

    report = validate_staged_result(staged, contract())

    assert not report.is_valid
    assert any(f.code in {"symlink_path", "out_of_scope_path"} for f in report.findings)


def test_partial_result_requires_bound_human_approval_before_any_write(tmp_path: Path) -> None:
    (tmp_path / "staging").mkdir()
    staged = stage_result(
        valid_result(status="partial", files={"staging/partial.md": "partial"}),
        tmp_path,
        contract(),
    )
    before = tree_hash(tmp_path)

    report = validate_staged_result(staged, contract())

    assert not report.is_valid
    assert any(f.code == "partial_requires_approval" for f in report.findings)
    with pytest.raises(ValueError):
        integrate_staged_result(staged, tmp_path, contract())
    assert tree_hash(tmp_path) == before

    approval = ApprovalResult(
        decision="approved",
        actor="alice",
        fingerprint=staged_fingerprint(staged),
    )
    assert validate_staged_result(staged, contract(), approval).is_valid
    integrate_staged_result(staged, tmp_path, contract(), approval)
    assert (tmp_path / "staging/partial.md").read_text() == "partial"


def test_case_colliding_paths_are_rejected_before_any_write(tmp_path: Path) -> None:
    (tmp_path / "staging").mkdir()
    before = tree_hash(tmp_path)
    staged = stage_result(
        valid_result(files={"staging/Foo": "one", "staging/foo": "two"}),
        tmp_path,
        contract(),
    )

    report = validate_staged_result(staged, contract())

    assert not report.is_valid
    assert any(f.code == "path_collision" for f in report.findings)
    with pytest.raises(ValueError):
        integrate_staged_result(staged, tmp_path, contract())
    assert tree_hash(tmp_path) == before


def test_reparse_point_ancestors_are_rejected_before_any_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "staging").mkdir()
    before = tree_hash(tmp_path)
    monkeypatch.setattr(integration_module, "_is_reparse_point", lambda path: path.name == "staging")
    staged = stage_result(valid_result(), tmp_path, contract())

    report = validate_staged_result(staged, contract())

    assert not report.is_valid
    assert any(f.code == "reparse_point" for f in report.findings)
    with pytest.raises(ValueError):
        integrate_staged_result(staged, tmp_path, contract())
    assert tree_hash(tmp_path) == before


def test_mutable_bytearray_is_snapshotted_at_staging(tmp_path: Path) -> None:
    (tmp_path / "staging").mkdir()
    content = bytearray(b"before")
    staged = stage_result(valid_result(files={"staging/value.bin": content}), tmp_path, contract())
    content[:] = b"after!"

    integrate_staged_result(staged, tmp_path, contract())

    assert (tmp_path / "staging/value.bin").read_bytes() == b"before"


def test_unsupported_mutable_payload_is_rejected_fail_closed() -> None:
    with pytest.raises(TypeError):
        valid_result(files={"staging/value": object()})


def test_partial_approval_is_bound_to_canonical_root_and_scope(tmp_path: Path) -> None:
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    (root_a / "staging").mkdir(parents=True)
    (root_b / "staging").mkdir(parents=True)
    contract_value = contract()
    staged = stage_result(
        valid_result(status="partial", files={"staging/partial.md": "partial"}),
        root_a,
        contract_value,
    )
    approval = ApprovalResult(decision="approved", actor="alice", fingerprint=staged_fingerprint(staged))
    before_a = tree_hash(root_a)
    before_b = tree_hash(root_b)

    relocated = replace(staged, root=root_b)
    changed_scope = replace(staged, writable_scope=("other-scope",))

    assert not validate_staged_result(relocated, contract_value, approval).is_valid
    assert not validate_staged_result(changed_scope, contract_value, approval).is_valid
    with pytest.raises(ValueError):
        integrate_staged_result(relocated, root_b, contract_value, approval)
    with pytest.raises(ValueError):
        integrate_staged_result(changed_scope, root_a, contract_value, approval)
    assert tree_hash(root_a) == before_a
    assert tree_hash(root_b) == before_b
