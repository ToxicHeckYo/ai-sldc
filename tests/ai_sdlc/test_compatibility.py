"""Repository and standalone compatibility report contracts."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills/ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.compatibility import CompatibilityState, check_compatibility


def copied_tree(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    skill = repository / "skills/ai-sdlc-orchestrator"
    skill.parent.mkdir(parents=True)
    shutil.copy2(REPOSITORY_ROOT / "AI_ASSISTED_DEVELOPMENT_STANDARD.md", repository)
    shutil.copy2(REPOSITORY_ROOT / "standard-manifest.yaml", repository)
    shutil.copytree(SKILL_ROOT, skill)
    return repository, skill


def declare(skill: Path) -> None:
    path = skill / "references/standard/compatibility.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: undeclared", "status: declared\n  declared_at: \"2026-11-08\""), encoding="utf-8", newline="\n")


def test_repository_report_is_compatible_only_for_exact_declared_tuple(tmp_path: Path) -> None:
    repository, skill = copied_tree(tmp_path)
    declare(skill)

    report = check_compatibility(skill, repository)

    assert report.state is CompatibilityState.COMPATIBLE
    assert report.mode == "repository"
    assert report.requires_confirmation is False
    assert report.skill_version == "0.1.0"
    assert report.source_version == "1.0.0"
    assert report.snapshot_version == "1.0.0"
    assert report.implemented_version == "1.0.0"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("snapshot", CompatibilityState.SNAPSHOT_STALE),
        ("implementation", CompatibilityState.IMPLEMENTATION_STALE),
        ("skill", CompatibilityState.SKILL_VERSION_MISMATCH),
        ("undeclared", CompatibilityState.UNDECLARED),
        ("invalid", CompatibilityState.INVALID),
    ),
)
def test_warning_states_are_structured_and_require_confirmation(tmp_path: Path, mutation: str, expected: CompatibilityState) -> None:
    repository, skill = copied_tree(tmp_path)
    declare(skill)
    compatibility = skill / "references/standard/compatibility.yaml"
    document = yaml.safe_load(compatibility.read_text(encoding="utf-8"))
    if mutation == "snapshot":
        snapshot = skill / "references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md"
        snapshot.write_bytes(snapshot.read_bytes() + b"stale")
    elif mutation == "implementation":
        document["implements"]["standard_version"] = "3.9.0"
        compatibility.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    elif mutation == "skill":
        document["skill"]["version"] = "0.2.0"
        compatibility.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    elif mutation == "undeclared":
        document["verification"] = {"status": "undeclared"}
        compatibility.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    else:
        compatibility.write_text("not: [valid", encoding="utf-8")

    report = check_compatibility(skill, repository)

    assert report.state is expected
    assert report.requires_confirmation is True
    payload = report.to_dict()
    assert list(payload) == ["mode", "state", "requires_confirmation", "skill_version", "source_version", "source_sha256", "snapshot_version", "snapshot_sha256", "implemented_version", "implemented_sha256", "detail", "next_action"]


def test_standalone_report_does_not_discover_parent_repository(tmp_path: Path) -> None:
    repository, skill = copied_tree(tmp_path)
    declare(skill)
    standalone = tmp_path / "installed/ai-sdlc-orchestrator"
    shutil.copytree(skill, standalone)

    report = check_compatibility(standalone)

    assert report.mode == "standalone"
    assert report.state is CompatibilityState.COMPATIBLE
    assert report.source_version is None
    assert "latest" not in report.detail.lower()


def test_skill_cli_returns_zero_and_stable_json_for_declared_tuple() -> None:
    result = subprocess.run([sys.executable, str(SKILL_ROOT / "scripts/ai_sdlc.py"), "compatibility", "check", "--json"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "standalone"
    assert payload["state"] == "compatible"
