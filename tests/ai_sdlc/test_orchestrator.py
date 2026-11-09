"""Task 13 RED tests for canonical lifecycle orchestration."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import subprocess
from dataclasses import replace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.errors import ValidationError  # noqa: E402
from ai_sdlc.orchestrator import (  # noqa: E402
    ActionSet,
    advance,
    advance_stage,
    initialize_project,
    propose_next_actions,
    resume_project,
)
from ai_sdlc.approvals import fingerprint_payload, record_approval  # noqa: E402
from ai_sdlc.contracts import load_contract  # noqa: E402
from ai_sdlc.integration import stage_result, staged_fingerprint  # noqa: E402
from ai_sdlc.runtime import ApprovalResult  # noqa: E402
from ai_sdlc.providers import ProviderLock  # noqa: E402
from ai_sdlc.standard import verify_standard  # noqa: E402
from ai_sdlc.status_view import generate_status  # noqa: E402


def _lock(*capabilities: str) -> ProviderLock:
    entries = {
        "workflow.skill-selection": {"contract_version": 1, "provider": "superpowers-using-superpowers", "source": "https://github.com/obra/superpowers", "revision": "7.0.1", "integrity": "sha256:" + "2" * 64, "status": "enabled"},
        "provider-result.integrate": {"contract_version": 1, "provider": "builtin-provider-result-integrate", "source": "builtin://provider-result-integrate", "revision": "1", "integrity": "sha256:" + "0" * 64, "status": "enabled"},
        "architecture.c4": {"contract_version": 1, "provider": "c4-modeling", "source": "package://c4-modeling", "revision": "1", "integrity": "sha256:" + "5" * 64, "status": "enabled"},
    }
    return ProviderLock(schema_version=1, capabilities={name: entries[name] for name in capabilities if name in entries})


def _git_root(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git").mkdir()
    return tmp_path


class _CommitAdapter:
    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> str:
        return "test-confirmed-checkpoint"


def _real_git_root(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    marker = root / "README.md"
    marker.write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return root, revision


def _workflow_result(base_revision: str, *, work_item: str = "TASK-001", capability: str = "workflow.skill-selection") -> dict[str, object]:
    return {"work_item": work_item, "capability": capability, "provider": "superpowers-using-superpowers", "status": "completed", "base_revision": base_revision, "sections": {"selection": "provider", "rationale": "deterministic"}, "evidence": {"capability-contract": "workflow.skill-selection@1"}}


def test_initialize_creates_fourteen_stages_and_only_two_decomposed(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    snapshot = initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))

    assert len(snapshot.plan.stages) == 14
    assert len(snapshot.plan.stages[0].work_items) > 0
    assert len(snapshot.plan.stages[1].work_items) > 0
    assert all(not stage.work_items for stage in snapshot.plan.stages[2:])
    assert (root / "docs/project-control/project-plan.yaml").is_file()
    assert (root / "docs/project-control/project-state.yaml").is_file()
    assert (root / "PROJECT_STATUS.md").is_file()


def test_initialize_is_fail_closed_before_writes_for_invalid_lock(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    with pytest.raises(ValidationError):
        initialize_project(root, SKILL_ROOT, {"schema_version": 1, "capabilities": {"x": {}}})
    assert list(root.iterdir()) == [root / ".git"]


def test_resume_ignores_derived_markdown_as_authority(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    status = root / "PROJECT_STATUS.md"
    status.write_text("# contradictory derived status\n", encoding="utf-8")
    resumed = resume_project(root)
    assert resumed.snapshot.state.current_stage == "STAGE-01"


def test_resume_rejects_state_plan_contradiction(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    state_path = root / "docs/project-control/project-state.yaml"
    state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    state["current_stage"] = "STAGE-03"
    state_path.write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValidationError, match="contradiction"):
        resume_project(root)


def test_propose_returns_ready_dag_and_explicit_human_gates(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    context = resume_project(root)
    actions = propose_next_actions(context)
    assert isinstance(actions, ActionSet)
    assert actions.actions
    assert all(action.provider_execution is False for action in actions.actions)


def test_advance_rejects_unknown_work_without_mutation(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    before = (root / "docs/project-control/project-plan.yaml").read_bytes()
    with pytest.raises(ValidationError, match="work|scope|registered"):
        advance(root, [_workflow_result("0" * 40, work_item="TASK-UNKNOWN")], adapter=_CommitAdapter(), revision_provider="0" * 40)
    assert (root / "docs/project-control/project-plan.yaml").read_bytes() == before


def test_advance_requires_confirmed_checkpoint_before_reporting_success(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    snapshot = advance(root, [_workflow_result("0" * 40)], adapter=_CommitAdapter(), revision_provider="0" * 40)
    work = snapshot.plan.stages[0].work_items[0]
    assert work.status == "done"
    assert snapshot.state.focus_work_items == ()


def test_advance_stage_coordinator_moves_focus_only_after_stage_is_complete(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    plan_path = root / "docs/project-control/project-plan.yaml"
    state_path = root / "docs/project-control/project-state.yaml"
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    stage = plan["stages"][0]
    stage["status"] = "in_review"
    stage["work_items"][0]["status"] = "done"
    stage["exit_criteria"][0]["status"] = "verified"
    state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    state["focus_work_items"] = []
    state["next_actions"] = []
    plan_path.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False), encoding="utf-8")
    state_path.write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")
    generate_status(root)

    snapshot = advance_stage(root, adapter=_CommitAdapter(), expected_stage="STAGE-01", revision_provider="0" * 40)

    assert snapshot.state.current_stage == "STAGE-02"
    assert snapshot.plan.stages[0].status == "completed"
    assert snapshot.plan.stages[1].status == "planning"
    assert snapshot.state.focus_work_items == ("TASK-002",)
    assert snapshot.state.next_actions[0].work_item == "TASK-002"


def test_advance_stage_rejects_unfinished_work_without_mutation(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    before = {
        relative: (root / relative).read_bytes()
        for relative in ("docs/project-control/project-plan.yaml", "docs/project-control/project-state.yaml", "PROJECT_STATUS.md")
    }
    with pytest.raises(ValidationError, match="not ready|unfinished"):
        advance_stage(root, adapter=_CommitAdapter(), expected_stage="STAGE-01", revision_provider="0" * 40)
    assert {relative: (root / relative).read_bytes() for relative in before} == before


def test_advance_checkpoint_failure_has_no_partial_canonical_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    before = {
        relative: (root / relative).read_bytes()
        for relative in ("docs/project-control/project-plan.yaml", "docs/project-control/project-state.yaml", "PROJECT_STATUS.md")
    }
    import ai_sdlc.orchestrator as module

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(module, "commit_checkpoint", fail)
    with pytest.raises(ValidationError, match="checkpoint failed"):
        advance(root, [_workflow_result("0" * 40)], adapter=_CommitAdapter(), revision_provider="0" * 40)
    assert {relative: (root / relative).read_bytes() for relative in before} == before


def test_advance_without_confirmed_adapter_fails_closed(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    with pytest.raises(ValidationError, match="commit adapter"):
        advance(root, [_workflow_result("0" * 40)])


def test_advance_rejects_unknown_or_unlocked_capability(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    with pytest.raises(ValidationError, match="contract|provider|capability"):
        advance(root, [{"work_item": "TASK-001", "capability": "unknown.capability", "status": "completed"}], adapter=_CommitAdapter())


def test_advance_requires_contract_human_approval_for_completed_result(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("architecture.c4"))
    result = {
        "work_item": "TASK-001",
        "capability": "architecture.c4",
        "provider": "c4-modeling",
        "status": "completed",
        "sections": {"work_item": "TASK-001", "context": "ok", "containers": "ok", "decisions": "ok"},
        "evidence": {"requirements": "ok"},
    }
    with pytest.raises(ValidationError, match="approval"):
        advance(root, [result], adapter=_CommitAdapter())


def test_advance_accepts_exact_contract_approval(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("architecture.c4"))
    contract = load_contract("architecture.c4", 1)
    staged = stage_result({"capability": "architecture.c4", "provider": "c4-modeling", "status": "completed", "base_revision": "0" * 40, "sections": {"context": "ok", "containers": "ok", "decisions": "ok"}, "evidence": {"requirements": "ok"}}, root, contract)
    approval = record_approval("architecture-approval", "alice", "architect", fingerprint_payload(staged_fingerprint(staged).encode("utf-8")))
    snapshot = advance(root, [{"work_item": "TASK-001", "staged": staged}], {"TASK-001": approval}, adapter=_CommitAdapter(), revision_provider="0" * 40)
    assert snapshot.plan.stages[0].work_items[0].status == "done"


def test_advance_rejects_forged_same_identity_contract(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("architecture.c4"))
    contract = load_contract("architecture.c4", 1)
    forged = replace(contract, integration={**contract.integration, "human_approvals": []})
    staged = stage_result({"capability": "architecture.c4", "provider": "c4-modeling", "status": "completed", "base_revision": "0" * 40, "sections": {"context": "ok", "containers": "ok", "decisions": "ok"}, "evidence": {"requirements": "ok"}}, root, contract)
    with pytest.raises(ValidationError, match="pinned contract"):
        advance(root, [{"work_item": "TASK-001", "staged": staged, "contract": forged}], adapter=_CommitAdapter(), revision_provider="0" * 40)


def test_advance_rejects_staged_result_without_contract_binding(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    contract = load_contract("workflow.skill-selection", 1)
    staged = stage_result(_workflow_result("0" * 40), root, contract)
    unbound = type(staged)(staged.root, staged.result, staged.writable_scope)
    with pytest.raises(ValidationError, match="contract binding"):
        advance(root, [{"work_item": "TASK-001", "staged": unbound}], adapter=_CommitAdapter(), revision_provider="0" * 40)


def test_advance_accepts_exact_contract_approval_for_partial_result(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("architecture.c4"))
    contract = load_contract("architecture.c4", 1)
    staged = stage_result({"capability": "architecture.c4", "provider": "c4-modeling", "status": "partial", "base_revision": "0" * 40, "sections": {"context": "ok", "containers": "ok", "decisions": "ok"}, "evidence": {"requirements": "ok"}}, root, contract)
    approval = record_approval("architecture-approval", "alice", "architect", fingerprint_payload(staged_fingerprint(staged).encode("utf-8")))
    snapshot = advance(root, [{"work_item": "TASK-001", "staged": staged}], {"TASK-001": approval}, adapter=_CommitAdapter(), revision_provider="0" * 40)
    assert snapshot.plan.stages[0].work_items[0].status == "done"


def test_advance_rejects_known_capability_missing_from_provider_lock(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    with pytest.raises(ValidationError, match="provider lock"):
        advance(root, [{**_workflow_result("0" * 40), "capability": "architecture.c4"}], adapter=_CommitAdapter(), revision_provider="0" * 40)


def test_advance_rejects_staged_result_from_another_project(tmp_path: Path) -> None:
    root = _git_root(tmp_path / "root")
    other = _git_root(tmp_path / "other")
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    initialize_project(other, SKILL_ROOT, _lock("workflow.skill-selection"))
    staged = stage_result({"capability": "workflow.skill-selection", "provider": "superpowers-using-superpowers", "status": "completed", "sections": {"work_item": "TASK-001"}}, other)
    with pytest.raises(ValidationError, match="root|project"):
        advance(root, [{"work_item": "TASK-001", "staged": staged}], adapter=_CommitAdapter())


def test_initialize_does_not_remove_a_concurrent_target_on_finalize_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_root(tmp_path)
    import ai_sdlc.orchestrator as module
    original_open = module.os.open
    target = root / "docs/project-control/project-plan.yaml"

    def race(path: str, flags: int, *args: object) -> int:
        if Path(path) == target and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("concurrent\n", encoding="utf-8")
            raise OSError("concurrent creation")
        return original_open(path, flags, *args)

    monkeypatch.setattr(module.os, "open", race)
    with pytest.raises(OSError, match="concurrent"):
        initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    assert target.read_text(encoding="utf-8") == "concurrent\n"


def test_initialize_rollback_preserves_replaced_owned_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_root(tmp_path)
    import ai_sdlc.orchestrator as module
    original_install = module._exclusive_install
    replaced = root / "docs/project-control/project-state.yaml"
    failing = root / "docs/artifact-register.yaml"

    def install(source: Path, destination: Path):
        if destination == failing:
            raise OSError("finalize failure")
        owner = original_install(source, destination)
        if destination == replaced:
            destination.write_text("replacement\n", encoding="utf-8")
        return owner

    monkeypatch.setattr(module, "_exclusive_install", install)
    with pytest.raises(OSError, match="finalize failure"):
        initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    assert replaced.read_text(encoding="utf-8") == "replacement\n"
    assert not (root / ".ai-sdlc-init.lock").exists()


def test_cli_git_adapter_commits_only_checkpoint_paths(tmp_path: Path) -> None:
    root, revision = _real_git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    unrelated = root / "unrelated.txt"
    unrelated.write_text("staged separately\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=root, check=True)
    from ai_sdlc import orchestrator as module
    from importlib.util import spec_from_file_location, module_from_spec
    cli_spec = spec_from_file_location("ai_sdlc_git_cli", SKILL_ROOT / "scripts" / "ai_sdlc.py")
    assert cli_spec is not None and cli_spec.loader is not None
    cli = module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)
    snapshot = module.advance(root, [_workflow_result(revision)], adapter=cli.GitCommitAdapter())
    committed = set(subprocess.check_output(["git", "show", "--format=", "--name-only", "HEAD"], cwd=root, text=True).splitlines())
    assert "unrelated.txt" not in committed
    assert "unrelated.txt" in subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=root, text=True).splitlines()
    assert snapshot.plan.stages[0].work_items[0].status == "done"


def test_cli_git_adapter_omits_noop_paths_and_preserves_unrelated_staging(tmp_path: Path) -> None:
    root, _ = _real_git_root(tmp_path)
    noop = root / "NOOP.md"
    noop.write_text("unchanged\n", encoding="utf-8")
    subprocess.run(["git", "add", "NOOP.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "noop seed"], cwd=root, check=True)
    changed = root / "README.md"
    changed.write_text("changed\n", encoding="utf-8")
    unrelated = root / "unrelated.txt"
    unrelated.write_text("staged separately\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=root, check=True)
    from importlib.util import spec_from_file_location, module_from_spec
    cli_spec = spec_from_file_location("ai_sdlc_git_cli_mixed", SKILL_ROOT / "scripts" / "ai_sdlc.py")
    assert cli_spec is not None and cli_spec.loader is not None
    cli = module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    revision = cli.GitCommitAdapter().commit(root, ("README.md", "NOOP.md"), "mixed checkpoint")
    assert revision != before
    committed = set(subprocess.check_output(["git", "show", "--format=", "--name-only", "HEAD"], cwd=root, text=True).splitlines())
    assert committed == {"README.md"}
    assert "unrelated.txt" in subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=root, text=True).splitlines()
    after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    with pytest.raises(RuntimeError, match="no changes"):
        cli.GitCommitAdapter().commit(root, ("NOOP.md",), "all-no-op checkpoint")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() == after


def test_initialize_write_failure_removes_partial_file_and_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_root(tmp_path)
    import ai_sdlc.orchestrator as module

    def fail_fsync(fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(module.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync"):
        initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    assert not (root / "docs/project-control/project-plan.yaml").exists()
    assert not (root / ".ai-sdlc-init.lock").exists()


def test_initialize_write_failure_preserves_replacement_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_root(tmp_path)
    import ai_sdlc.orchestrator as module
    target = root / "docs/project-control/project-plan.yaml"
    replacement = root / "replacement.yaml"
    replacement.write_text("replacement\n", encoding="utf-8")

    def replace_then_fail(fd: int) -> None:
        os.close(fd)
        os.replace(replacement, target)
        raise OSError("injected fsync failure")

    import os
    monkeypatch.setattr(module.os, "fsync", replace_then_fail)
    with pytest.raises(OSError, match="fsync"):
        initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    assert target.read_text(encoding="utf-8") == "replacement\n"
    assert not (root / ".ai-sdlc-init.lock").exists()


def test_advance_rejects_mismatched_provider_identity(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    result = _workflow_result("0" * 40)
    result["provider"] = "evil-provider"
    with pytest.raises(ValidationError, match="provider"):
        advance(root, [result], adapter=_CommitAdapter(), revision_provider="0" * 40)


def test_advance_binds_result_to_resolved_git_revision(tmp_path: Path) -> None:
    root, revision = _real_git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    stale = _workflow_result("ref: refs/heads/main")
    with pytest.raises(ValidationError, match="stale"):
        advance(root, [stale], adapter=_CommitAdapter())
    fresh = _workflow_result(revision)
    snapshot = advance(root, [fresh], adapter=_CommitAdapter())
    assert snapshot.plan.stages[0].work_items[0].status == "done"


def test_resume_rejects_unregistered_file_in_sources_directory(tmp_path: Path) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    rogue = root / "docs/product-definition/sources/unregistered.txt"
    rogue.write_text("not registered\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="unregistered"):
        resume_project(root)


def test_check_cli_accepts_root_option_and_returns_controlled_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    import importlib.util
    cli_spec = importlib.util.spec_from_file_location("ai_sdlc_cli", SKILL_ROOT / "scripts" / "ai_sdlc.py")
    cli = importlib.util.module_from_spec(cli_spec)
    assert cli_spec.loader is not None
    cli_spec.loader.exec_module(cli)
    assert cli.main(["check", "--root", str(root)]) == 0
    assert "current" in capsys.readouterr().out


def test_cli_malformed_results_returns_controlled_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _git_root(tmp_path)
    initialize_project(root, SKILL_ROOT, _lock("workflow.skill-selection"))
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("results: [\n", encoding="utf-8")
    import importlib.util
    cli_spec = importlib.util.spec_from_file_location("ai_sdlc_cli_bad_yaml", SKILL_ROOT / "scripts" / "ai_sdlc.py")
    cli = importlib.util.module_from_spec(cli_spec)
    assert cli_spec.loader is not None
    cli_spec.loader.exec_module(cli)
    assert cli.main(["advance", str(root), "--results", str(malformed)]) == 1
    assert "traceback" not in capsys.readouterr().err.casefold()
