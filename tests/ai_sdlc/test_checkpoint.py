"""Transactional checkpoint and recovery contract tests."""

from __future__ import annotations

from pathlib import Path
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "ai-sdlc-orchestrator" / "scripts"))

from ai_sdlc.checkpoint import (  # noqa: E402
    CommitIdentity,
    _set_state,
    commit_checkpoint,
    prepare_checkpoint,
    recover,
    validate_checkpoint,
)
from ai_sdlc import checkpoint as checkpoint_module  # noqa: E402
from ai_sdlc.integration import tree_hash  # noqa: E402


class Adapter:
    def __init__(self, identity: str = "commit-001", fail: bool = False) -> None:
        self.identity = identity
        self.fail = fail
        self.calls: list[tuple[Path, tuple[str, ...], str]] = []

    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> CommitIdentity:
        self.calls.append((root, paths, message))
        if self.fail:
            raise RuntimeError("commit failed")
        return CommitIdentity(self.identity)


def test_prepare_isolated_manifest_and_commit_identity(tmp_path: Path) -> None:
    target = tmp_path / "state.yaml"
    target.write_bytes(b"old")
    before = tree_hash(tmp_path)
    adapter = Adapter()

    prepared = prepare_checkpoint(tmp_path, {"state.yaml": b"new", "created.txt": "created"}, adapter=adapter)

    assert tree_hash(tmp_path) == before
    assert prepared.manifest["state.yaml"]["old_hash"]
    assert prepared.manifest["state.yaml"]["new_hash"]
    assert validate_checkpoint(prepared).is_valid
    identity = commit_checkpoint(prepared, "checkpoint: update state", adapter=adapter)

    assert identity.identity == "commit-001"
    assert target.read_bytes() == b"new"
    assert (tmp_path / "created.txt").read_bytes() == b"created"


@pytest.mark.parametrize("phase", ("before_staging", "after_staging", "after_validation", "before_commit"))
def test_faults_recover_last_agreed_snapshot(tmp_path: Path, phase: str) -> None:
    (tmp_path / "keep.txt").write_bytes(b"canonical")
    (tmp_path / "unrelated.txt").write_bytes(b"unrelated")
    before = tree_hash(tmp_path)
    adapter = Adapter()

    def fault(name: str) -> None:
        if name == phase:
            raise RuntimeError(name)

    if phase in {"before_staging", "after_staging"}:
        with pytest.raises(RuntimeError):
            prepared = prepare_checkpoint(tmp_path, {"keep.txt": b"changed"}, adapter=adapter, fault_injector=fault)
        assert tree_hash(tmp_path) == before
        assert recover(tmp_path).is_clean
    else:
        prepared = prepare_checkpoint(tmp_path, {"keep.txt": b"changed"}, adapter=adapter)
        with pytest.raises(RuntimeError):
            commit_checkpoint(prepared, "checkpoint: update", adapter=adapter, fault_injector=fault)
        assert tree_hash(tmp_path) == before
        assert recover(tmp_path).is_clean

    assert (tmp_path / "keep.txt").read_bytes() == b"canonical"
    assert (tmp_path / "unrelated.txt").read_bytes() == b"unrelated"


def test_validation_detects_target_change_and_staged_tamper(tmp_path: Path) -> None:
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    (tmp_path / "state.txt").write_bytes(b"outside-change")
    report = validate_checkpoint(prepared)
    assert not report.is_valid
    assert any(f.code == "target_changed" for f in report.findings)

    (tmp_path / "state.txt").unlink()
    staged_file = prepared.prepared_dir / "new" / "state.txt"
    staged_file.write_bytes(b"tampered")
    report = validate_checkpoint(prepared)
    assert not report.is_valid
    assert any(f.code == "staged_tampered" for f in report.findings)


def test_commit_fail_closed_for_empty_identity_and_message(tmp_path: Path) -> None:
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    with pytest.raises(ValueError):
        commit_checkpoint(prepared, " ", adapter=Adapter())
    with pytest.raises(RuntimeError):
        commit_checkpoint(prepared, "checkpoint: update", adapter=Adapter(identity=""))
    assert not (tmp_path / "state.txt").exists()


def test_rollback_restores_deletions_and_creations(tmp_path: Path) -> None:
    (tmp_path / "deleted.txt").write_bytes(b"old")
    prepared = prepare_checkpoint(tmp_path, {"deleted.txt": None, "new.txt": b"new"})
    with pytest.raises(RuntimeError):
        commit_checkpoint(prepared, "checkpoint: update", adapter=Adapter(fail=True))
    assert (tmp_path / "deleted.txt").read_bytes() == b"old"
    assert not (tmp_path / "new.txt").exists()


def test_toc_tou_change_after_validation_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "state.txt").write_bytes(b"old")
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})

    def fault(name: str) -> None:
        if name == "after_validation":
            (tmp_path / "state.txt").write_bytes(b"outside-change")

    with pytest.raises(ValueError, match="target_changed"):
        commit_checkpoint(prepared, "checkpoint: update", adapter=Adapter(), fault_injector=fault)
    assert (tmp_path / "state.txt").read_bytes() == b"outside-change"
    recovery = recover(tmp_path)
    assert not recovery.is_clean
    assert any(f.code == "recovery_conflict" for f in recovery.findings)


def test_manifest_path_escape_collision_and_reparse_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    manifest_path = prepared.prepared_dir / "manifest.json"
    manifest_path.write_text(
        '{"entries":{"../escape.txt": {"old_exists": false, "old_hash":"<missing>", "old_file":null, "new_exists":true, "new_hash":"bad", "new_file":"new/../escape.txt"}},"version":1}',
        encoding="utf-8",
    )
    report = validate_checkpoint(prepared)
    assert not report.is_valid
    assert any(f.code in {"path_escape", "manifest_tampered"} for f in report.findings)

    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    manifest_path = prepared.prepared_dir / "manifest.json"
    manifest_path.write_text(
        '{"entries":{"Foo": {"old_exists": false, "old_hash":"<missing>", "old_file":null, "new_exists":true, "new_hash":"bad", "new_file":"new/Foo"}, "foo": {"old_exists": false, "old_hash":"<missing>", "old_file":null, "new_exists":true, "new_hash":"bad", "new_file":"new/foo"}},"version":1}',
        encoding="utf-8",
    )
    report = validate_checkpoint(prepared)
    assert any(f.code == "path_collision" for f in report.findings)

    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    monkeypatch.setattr(checkpoint_module, "_reject_symlink_ambiguity", lambda root, target: (_ for _ in ()).throw(ValueError("reparse point")) if target.name == "state.txt" else None)
    report = validate_checkpoint(prepared)
    assert any(f.code == "reparse_point" for f in report.findings)


def test_partial_apply_failure_rolls_back_every_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.txt").write_bytes(b"a-old")
    (tmp_path / "b.txt").write_bytes(b"b-old")
    prepared = prepare_checkpoint(tmp_path, {"a.txt": b"a-new", "b.txt": b"b-new"})
    original = checkpoint_module._atomic_replace
    canonical_replaces = 0

    def fail_second_canonical(target: Path, content: bytes) -> None:
        nonlocal canonical_replaces
        if target.parent == tmp_path:
            canonical_replaces += 1
            if canonical_replaces == 2:
                raise OSError("injected second replace failure")
        original(target, content)

    monkeypatch.setattr(checkpoint_module, "_atomic_replace", fail_second_canonical)
    with pytest.raises(OSError):
        commit_checkpoint(prepared, "checkpoint: partial failure", adapter=Adapter())
    assert (tmp_path / "a.txt").read_bytes() == b"a-old"
    assert (tmp_path / "b.txt").read_bytes() == b"b-old"


def test_bytes_like_changes_are_snapshotted_as_exact_bytes(tmp_path: Path) -> None:
    value = bytearray(b"before")
    prepared = prepare_checkpoint(tmp_path, {"value.bin": value})
    value[:] = b"after!"
    commit_checkpoint(prepared, "checkpoint: bytes", adapter=Adapter())
    assert (tmp_path / "value.bin").read_bytes() == b"before"


def test_existing_file_ancestor_is_rejected_before_prepare(tmp_path: Path) -> None:
    (tmp_path / "ancestor").write_bytes(b"file")
    with pytest.raises(ValueError, match="ancestor"):
        prepare_checkpoint(tmp_path, {"ancestor/child.txt": b"new"})


def test_rollback_removes_only_transaction_created_directories(tmp_path: Path) -> None:
    (tmp_path / "existing").mkdir()
    prepared = prepare_checkpoint(tmp_path, {"existing/new/created.txt": b"new", "fresh/deep/file.txt": b"new"})
    with pytest.raises(RuntimeError):
        commit_checkpoint(prepared, "checkpoint: directory rollback", adapter=Adapter(fail=True))
    assert (tmp_path / "existing").is_dir()
    assert not (tmp_path / "existing/new").exists()
    assert not (tmp_path / "fresh").exists()


def test_recovery_does_not_overwrite_external_change_before_mutation(tmp_path: Path) -> None:
    (tmp_path / "state.txt").write_bytes(b"old")
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    _set_state(prepared, "staged", attempted_paths=["state.txt"], applied_paths=[])
    (tmp_path / "state.txt").write_bytes(b"external")
    report = recover(tmp_path)
    assert (tmp_path / "state.txt").read_bytes() == b"external"
    assert any(f.code == "recovery_conflict" for f in report.findings)


def test_workspace_reparse_point_is_rejected_before_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path.parent / f".{tmp_path.name}.ai-sdlc-checkpoints"
    original = checkpoint_module._is_reparse_point
    monkeypatch.setattr(checkpoint_module, "_is_reparse_point", lambda path: path == workspace or original(path))
    with pytest.raises(ValueError, match="reparse"):
        prepare_checkpoint(tmp_path, {"state.txt": b"new"})


def test_prepared_area_reparse_point_is_rejected_during_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    original = checkpoint_module._is_reparse_point
    monkeypatch.setattr(checkpoint_module, "_is_reparse_point", lambda path: path == prepared.prepared_dir or original(path))
    report = validate_checkpoint(prepared)
    assert not report.is_valid
    assert any(f.code == "invalid_canonical_root" for f in report.findings)


def test_recovery_rejects_manifest_and_journal_co_tamper(tmp_path: Path) -> None:
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    _set_state(prepared, "staged", attempted_paths=["state.txt"], applied_paths=[])
    manifest_path = prepared.prepared_dir / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["entries"]["escape.txt"] = document["entries"]["state.txt"]
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    journal_path = prepared.journal_path
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["manifest_sha256"] = checkpoint_module._manifest_hash(document["entries"])
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    report = recover(tmp_path)
    assert not report.is_clean
    assert any(f.code in {"seal_tampered", "manifest_binding_mismatch"} for f in report.findings)


def test_confirmed_commit_marker_prevents_rollback_after_journal_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    original = checkpoint_module._set_state

    def fail_committed(checkpoint, state, **extra):
        if state == "committed":
            raise OSError("journal write failed")
        return original(checkpoint, state, **extra)

    monkeypatch.setattr(checkpoint_module, "_set_state", fail_committed)
    with pytest.raises(OSError):
        commit_checkpoint(prepared, "checkpoint: durable marker", adapter=Adapter("confirmed-001"))
    assert (tmp_path / "state.txt").read_bytes() == b"new"
    assert recover(tmp_path).is_clean


def test_marker_write_failure_is_ambiguous_and_never_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    monkeypatch.setattr(checkpoint_module, "_write_commit_marker", lambda *_args: (_ for _ in ()).throw(OSError("marker unavailable")))
    with pytest.raises(OSError):
        commit_checkpoint(prepared, "checkpoint: marker failure", adapter=Adapter("confirmed-002"))
    report = recover(tmp_path)
    assert not report.is_clean
    assert any(f.code == "commit_ambiguous" for f in report.findings)
    assert (tmp_path / "state.txt").read_bytes() == b"new"


def test_stale_uncommitted_journal_cannot_restore_over_newer_commit(tmp_path: Path) -> None:
    (tmp_path / "state.txt").write_bytes(b"old")
    stale = prepare_checkpoint(tmp_path, {"state.txt": b"stale"})
    current = prepare_checkpoint(tmp_path, {"state.txt": b"current"})
    commit_checkpoint(current, "checkpoint: current", adapter=Adapter("current-002"))
    _set_state(stale, "committing", attempted_paths=["state.txt"], applied_paths=[])
    report = recover(tmp_path)
    assert report.is_clean
    assert (tmp_path / "state.txt").read_bytes() == b"current"


def test_forged_terminal_journal_state_requires_durable_evidence(tmp_path: Path) -> None:
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    journal = json.loads(prepared.journal_path.read_text(encoding="utf-8"))
    journal["state"] = "committed"
    prepared.journal_path.write_text(json.dumps(journal), encoding="utf-8")
    report = recover(tmp_path)
    assert not report.is_clean
    assert any(f.code == "commit_marker_missing" for f in report.findings)

    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    journal = json.loads(prepared.journal_path.read_text(encoding="utf-8"))
    journal["state"] = "recovered"
    prepared.journal_path.write_text(json.dumps(journal), encoding="utf-8")
    report = recover(tmp_path)
    assert not report.is_clean
    assert any(f.code == "recovery_evidence_missing" for f in report.findings)


@pytest.mark.parametrize("document", ({"entries": []}, {"entries": {"state.txt": None}}, {"version": 1}))
def test_recovery_malformed_manifest_is_reported_without_mutation(tmp_path: Path, document: dict[str, object]) -> None:
    (tmp_path / "state.txt").write_bytes(b"old")
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    _set_state(prepared, "staged", attempted_paths=["state.txt"], applied_paths=[])
    (prepared.prepared_dir / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    before = (tmp_path / "state.txt").read_bytes()
    report = recover(tmp_path)
    assert not report.is_clean
    assert any(f.code in {"manifest_invalid", "recovery_failed", "manifest_binding_mismatch"} for f in report.findings)
    assert (tmp_path / "state.txt").read_bytes() == before


def test_recovery_malformed_seal_is_reported_without_mutation(tmp_path: Path) -> None:
    (tmp_path / "state.txt").write_bytes(b"old")
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    _set_state(prepared, "staged", attempted_paths=["state.txt"], applied_paths=[])
    prepared.seal_path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    report = recover(tmp_path)
    assert not report.is_clean
    assert any(f.code in {"recovery_failed", "manifest_binding_mismatch"} for f in report.findings)
    assert (tmp_path / "state.txt").read_bytes() == b"old"


def test_generation_reservation_retries_collision_and_is_unique(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = checkpoint_module._exclusive_generation_reservation
    calls = 0

    def collide_once(index: Path, generation: int, checkpoint_id: str) -> Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileExistsError("simulated reservation collision")
        return original(index, generation, checkpoint_id)

    monkeypatch.setattr(checkpoint_module, "_exclusive_generation_reservation", collide_once)
    first = prepare_checkpoint(tmp_path, {"one.txt": b"one"})
    second = prepare_checkpoint(tmp_path, {"two.txt": b"two"})
    assert calls >= 2
    assert first.generation != second.generation


def test_forged_high_generation_marker_cannot_hide_unfinished_journal(tmp_path: Path) -> None:
    (tmp_path / "state.txt").write_bytes(b"old")
    prepared = prepare_checkpoint(tmp_path, {"state.txt": b"new"})
    _set_state(prepared, "staged", attempted_paths=["state.txt"], applied_paths=[])
    index = prepared.seal_path.parent
    forged = index / "forged.commit.json"
    forged.write_text(
        json.dumps({
            "version": 1,
            "state": "committed",
            "checkpoint_id": "forged",
            "root_identity": checkpoint_module._root_identity(tmp_path),
            "generation": 999999,
            "manifest_sha256": "0" * 64,
            "identity": "forged-high",
        }),
        encoding="utf-8",
    )
    report = recover(tmp_path)
    assert not report.is_clean
    assert any(f.code in {"commit_marker_invalid", "commit_index_invalid"} for f in report.findings)
    assert (tmp_path / "state.txt").read_bytes() == b"old"
