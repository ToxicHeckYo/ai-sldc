"""Executable contract for compatibility guidance embedded in the skill."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills/ai-sdlc-orchestrator/SKILL.md"


def guidance() -> str:
    return SKILL.read_text(encoding="utf-8").lower()


def test_start_and_resume_run_repository_compatibility_check_first() -> None:
    text = guidance()
    assert "python scripts/check_compatibility.py --json" in text
    assert "start or resume" in text
    assert text.index("python scripts/check_compatibility.py --json") < text.index("docs/project-control/project-state.yaml")


def test_every_warning_including_invalid_requires_explicit_confirmation() -> None:
    text = guidance()
    assert "snapshot-stale" in text
    assert "implementation-stale" in text
    assert "skill-version-mismatch" in text
    assert "undeclared" in text
    assert "invalid" in text
    assert "explicit user confirmation" in text


def test_confirmation_is_scoped_and_invalid_never_claims_compliance() -> None:
    text = guidance()
    assert "current run" in text
    assert "exact tuple" in text
    assert "do not claim compliance" in text
    assert "continue after confirmation" in text


def test_warning_report_names_versions_hashes_and_next_action() -> None:
    text = guidance()
    for required in ("skill version", "source version", "snapshot version", "implemented version", "sha-256", "next action"):
        assert required in text
