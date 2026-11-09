"""Explicit compatibility declaration tests."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills/ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.compatibility import CompatibilityState, declare_compatibility
from ai_sdlc.errors import ValidationError


def copied_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "skills").mkdir(parents=True)
    shutil.copy2(REPOSITORY_ROOT / "AI_ASSISTED_DEVELOPMENT_STANDARD.md", root)
    shutil.copy2(REPOSITORY_ROOT / "standard-manifest.yaml", root)
    shutil.copytree(SKILL_ROOT, root / "skills/ai-sdlc-orchestrator")
    return root


def test_wrong_requested_versions_do_not_run_tests_or_write(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    path = root / "skills/ai-sdlc-orchestrator/references/standard/compatibility.yaml"
    before = path.read_bytes()
    calls = 0

    def runner(command: tuple[str, ...]) -> int:
        nonlocal calls
        calls += 1
        return 0

    with pytest.raises(ValidationError, match="requested"):
        declare_compatibility(root, "0.2.0", "1.0.0", ("pytest",), runner)

    assert calls == 0
    assert path.read_bytes() == before


def test_failed_test_runner_preserves_undeclared_file(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    path = root / "skills/ai-sdlc-orchestrator/references/standard/compatibility.yaml"
    before = path.read_bytes()

    with pytest.raises(ValidationError, match="tests failed"):
        declare_compatibility(root, "0.1.0", "1.0.0", ("pytest",), lambda command: 7)

    assert path.read_bytes() == before


def test_stale_snapshot_cannot_be_declared(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    snapshot = root / "skills/ai-sdlc-orchestrator/references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    snapshot.write_bytes(snapshot.read_bytes() + b"stale")

    with pytest.raises(ValidationError, match="snapshot"):
        declare_compatibility(root, "0.1.0", "1.0.0", ("pytest",), lambda command: 0)


def test_successful_runner_declares_exact_current_tuple(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    observed: list[tuple[str, ...]] = []

    report = declare_compatibility(root, "0.1.0", "1.0.0", ("python", "-m", "pytest"), lambda command: observed.append(command) or 0)

    assert observed == [("python", "-m", "pytest")]
    assert report.state is CompatibilityState.COMPATIBLE
    text = (root / "skills/ai-sdlc-orchestrator/references/standard/compatibility.yaml").read_text(encoding="utf-8")
    assert "status: declared" in text
    assert 'standard_version: 1.0.0' in text or 'standard_version: "1.0.0"' in text


def test_interrupted_atomic_replace_preserves_previous_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copied_repository(tmp_path)
    target = root / "skills/ai-sdlc-orchestrator/references/standard/compatibility.yaml"
    before = target.read_bytes()
    original_replace = Path.replace

    def interrupt(path: Path, destination: Path) -> Path:
        if destination == target:
            raise OSError("interrupted atomic replace")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", interrupt)

    with pytest.raises(OSError, match="interrupted"):
        declare_compatibility(root, "0.1.0", "1.0.0", ("pytest",), lambda command: 0)

    assert target.read_bytes() == before
    assert not target.with_name(f".{target.name}.tmp").exists()


def test_changed_standard_tuple_requires_skill_version_bump(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    source = root / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    root_manifest = root / "standard-manifest.yaml"
    source.write_text(source.read_text(encoding="utf-8").replace("**Версия:** 1.0.0", "**Версия:** 1.0.1"), encoding="utf-8", newline="\n")
    root_manifest.write_text(root_manifest.read_text(encoding="utf-8").replace("1.0.0", "1.0.1", 1), encoding="utf-8", newline="\n")
    from ai_sdlc.standard_sync import sync_standard

    sync_standard(root)

    with pytest.raises(ValidationError, match="skill version bump"):
        declare_compatibility(root, "0.1.0", "1.0.1", ("pytest",), lambda command: 0)


def test_runner_tuple_mutation_preserves_previous_declaration(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    target = root / "skills/ai-sdlc-orchestrator/references/standard/compatibility.yaml"
    pyproject = root / "skills/ai-sdlc-orchestrator/pyproject.toml"
    before = target.read_bytes()

    def mutate_during_tests(command: tuple[str, ...]) -> int:
        pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('version = "0.1.0"', 'version = "0.1.1"'), encoding="utf-8")
        return 0

    with pytest.raises(ValidationError, match="changed while tests ran"):
        declare_compatibility(root, "0.1.0", "1.0.0", ("pytest",), mutate_during_tests)

    assert target.read_bytes() == before
