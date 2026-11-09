from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import runpy
import sys
import importlib.util
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "ai-sdlc-orchestrator" / "scripts"))

from ai_sdlc.model import Criterion  # noqa: E402
from ai_sdlc.readiness import calculate_readiness  # noqa: E402
from ai_sdlc.runtime import DispatchRequest, MockRuntime  # noqa: E402
from ai_sdlc.spec_changes import apply_approved_delta, archive_change, preview_delta  # noqa: E402
from ai_sdlc.approvals import record_approval  # noqa: E402
from ai_sdlc.git_adapter import GitCommitAdapter  # noqa: E402
from ai_sdlc.orchestrator import _path_parts_overlap, _result_path_parts  # noqa: E402

_CLI_SPEC = importlib.util.spec_from_file_location("ai_sdlc_cli", ROOT / "skills" / "ai-sdlc-orchestrator" / "scripts" / "ai_sdlc.py")
assert _CLI_SPEC and _CLI_SPEC.loader
_CLI = importlib.util.module_from_spec(_CLI_SPEC)
_CLI_SPEC.loader.exec_module(_CLI)
cli_main = _CLI.main


def test_optional_invalid_readiness_status_fails_before_skip() -> None:
    with pytest.raises(Exception, match="unsupported status"):
        calculate_readiness((Criterion("C-optional", "optional", "bogus", mandatory=False),))


def test_scripted_worker_string_scope_is_one_scope_not_characters() -> None:
    runtime = MockRuntime(scripted_workers={
        "implementation.tdd": {
            "worker_id": "scripted-1",
            "workspace_id": "ws-1",
            "workspace_scope": "src/api",
        }
    })
    request = DispatchRequest("implementation.tdd", inputs={})
    first = runtime.spawn_worker(request)
    assert first.workspace_scope == ("src/api",)
    with pytest.raises(ValueError, match="already active"):
        runtime.spawn_worker(replace(request, inputs={"workspace_id": "ws-2", "workspace_scope": ("src",)}))


def test_cli_help_describes_standard_as_verified_skill_root(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["init", "--help"]) == 0
    assert "verified pinned skill/package root" in capsys.readouterr().out


def test_cli_rejects_standalone_standard_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    path.write_text("**Версия:** 4.0\n", encoding="utf-8")
    assert cli_main(["init", str(tmp_path), "--standard", str(path), "--lock", str(tmp_path / "lock.yaml")]) == 1
    assert "verified pinned skill/package root" in capsys.readouterr().err


def test_cli_accepts_verified_skill_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    lock = tmp_path / "lock.yaml"
    lock.write_text(yaml.safe_dump({"schema_version": 1, "capabilities": {}}), encoding="utf-8")
    assert cli_main(["init", str(tmp_path), "--standard", str(ROOT / "skills" / "ai-sdlc-orchestrator"), "--lock", str(lock)]) == 0
    assert "initialized" in capsys.readouterr().out


def test_scorer_uses_declared_parent_not_basename(tmp_path: Path) -> None:
    scorer = runpy.run_path(str(ROOT / "tests" / "ai_sdlc" / "evals" / "score_skill.py"))
    green = tmp_path / "green"
    green.mkdir()
    (tmp_path / "baseline").mkdir()
    (tmp_path / "baseline" / "nested").mkdir()
    (tmp_path / "baseline" / "nested" / "same.md").write_text("wrong", encoding="utf-8")
    resolved = scorer["resolve_green_path"](green, "green/nested/same.md")
    assert resolved == green / "nested" / "same.md"
    with pytest.raises(ValueError, match="under green"):
        scorer["resolve_green_path"](green, "green/../baseline/same.md")


def test_spec_apply_fails_closed_without_real_git_adapter(tmp_path: Path) -> None:
    from test_spec_changes import _package, _evidence  # type: ignore[import-not-found]

    current, package = _package(tmp_path)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    with pytest.raises(ValueError, match="Git commit adapter"):
        apply_approved_delta(preview, approval, _evidence(preview))


def test_spec_apply_and_archive_are_real_git_commits(tmp_path: Path) -> None:
    from test_spec_changes import _package, _evidence  # type: ignore[import-not-found]

    current, package = _package(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("must remain outside coordinator commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=tmp_path, check=True)
    preview = preview_delta(current, package / "delta.md")
    approval = record_approval("specification-change", "alice", "product-owner", preview.fingerprint)
    adapter = GitCommitAdapter()
    result = apply_approved_delta(preview, approval, _evidence(preview), adapter=adapter)
    archived = archive_change(result, adapter=adapter)
    assert result.verified and archived.verified
    commits = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=tmp_path, text=True).strip()
    assert commits == "3"
    staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=tmp_path, text=True).splitlines()
    assert staged == ["unrelated.txt"]


def test_git_adapter_verifies_paths_for_root_commit(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    target = tmp_path / "root.txt"
    target.write_text("root commit\n", encoding="utf-8")

    revision = GitCommitAdapter().commit(tmp_path, ("root.txt",), "root checkpoint")

    committed = subprocess.check_output(
        ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", revision, "--"],
        cwd=tmp_path,
        text=True,
    ).splitlines()
    assert committed == ["root.txt"]
    log = subprocess.check_output(["git", "log", "-1", "--format=%H %s"], cwd=tmp_path, text=True)
    assert log.startswith(f"{revision} root checkpoint")
    identity = subprocess.check_output(["git", "show", "-s", "--format=%H:%an:%ae", revision], cwd=tmp_path, text=True)
    assert identity.strip() == f"{revision}:Test:test@example.invalid"
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=tmp_path, text=True) == ""


def test_result_paths_preserve_provider_case_but_compare_casefolded() -> None:
    canonical = _result_path_parts("Src/API.md")
    assert canonical == ("Src", "API.md")
    assert _path_parts_overlap(canonical, _result_path_parts("src/api.md"))


def test_result_paths_reject_ancestor_descendant_but_allow_disjoint() -> None:
    ancestor = _result_path_parts("docs")
    descendant = _result_path_parts("docs/API.md")
    disjoint = _result_path_parts("src/API.md")
    assert _path_parts_overlap(ancestor, descendant)
    assert _path_parts_overlap(descendant, ancestor)
    assert not _path_parts_overlap(ancestor, disjoint)
