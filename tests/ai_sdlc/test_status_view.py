"""Deterministic PROJECT_STATUS renderer and CLI-facing operations."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.model import (  # noqa: E402
    STAGE_DEFINITIONS,
    Artifact,
    Criterion,
    NextAction,
    ProjectPlan,
    ProjectSnapshot,
    ProjectState,
    Scope,
    Source,
    Stage,
    WorkItem,
)
from ai_sdlc.status_view import check_status, generate_status, render_project_status  # noqa: E402
from ai_sdlc.errors import ValidationError  # noqa: E402


def snapshot() -> ProjectSnapshot:
    work = WorkItem("TASK-001", "Подготовить обзор", "in_progress", "analyst", produces=("ART-01",), scope=Scope())
    first = Stage(
        STAGE_DEFINITIONS[0][0], STAGE_DEFINITIONS[0][1], "active", work_items=(work,),
        entry_criteria=(Criterion("EC-01", "Brief", "verified"),),
        exit_criteria=(Criterion("XC-01", "Review", "in_review"),),
    )
    stages = (first,) + tuple(Stage(stage_id, title, "not_started") for stage_id, title in STAGE_DEFINITIONS[1:])
    return ProjectSnapshot(
        ProjectPlan(1, "PROJECT-001", stages),
        ProjectState(
            1, "PROJECT-001", "initial-development", "STAGE-01", ("TASK-001",),
            (NextAction("TASK-001", "Продолжить анализ"),), "2026-11-03", "coordinator",
            active_change="CHANGE-01", current_baseline="BASELINE-01",
        ),
        (Artifact("ART-01", "product_vision", "docs/vision.md", "draft"),),
        (Source("SRC-01", "business_document", "Brief", "customer", "2026-11-03", "1", "brief.md", "accepted"),),
    )


def _write_fixture(root: Path) -> None:
    plan_stages = []
    for index, (stage_id, title) in enumerate(STAGE_DEFINITIONS):
        stage = {"id": stage_id, "title": title, "status": "active" if index == 0 else "not_started", "depends_on": []}
        if index == 0:
            stage.update({
                "entry_criteria": [{"id": "EC-01", "text": "Brief", "status": "verified"}],
                "exit_criteria": [{"id": "XC-01", "text": "Review", "status": "in_review"}],
                "work_items": [{"id": "TASK-001", "title": "Подготовить обзор", "status": "in_progress", "executor": "analyst", "depends_on": [], "produces": ["ART-01"]}],
            })
        plan_stages.append(stage)
    (root / "docs/project-control").mkdir(parents=True)
    (root / "docs/product-definition/sources").mkdir(parents=True)
    (root / "docs").mkdir(exist_ok=True)
    documents = {
        "docs/project-control/project-plan.yaml": {"schema_version": 1, "project_id": "PROJECT-001", "planning_mode": "rolling_wave", "stages": plan_stages},
        "docs/project-control/project-state.yaml": {"schema_version": 1, "project_id": "PROJECT-001", "lifecycle_mode": "initial-development", "current_stage": "STAGE-01", "focus_work_items": ["TASK-001"], "next_actions": [{"work_item": "TASK-001", "action": "Продолжить анализ"}], "updated_at": "2026-11-03", "updated_by": "coordinator", "active_change": "CHANGE-01", "current_baseline": "BASELINE-01"},
        "docs/artifact-register.yaml": {"artifacts": [{"id": "ART-01", "kind": "product_vision", "path": "docs/vision.md", "status": "draft"}]},
        "docs/product-definition/sources/source-register.yaml": {"sources": [{"id": "SRC-01", "kind": "business_document", "title": "Brief", "origin": "customer", "received_at": "2026-11-03", "version": "1", "location": "brief.md", "status": "accepted"}]},
    }
    for relative, document in documents.items():
        path = root / relative
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n")
    (root / "assets/schemas").mkdir(parents=True)
    for schema in (SKILL_ROOT / "assets/schemas").glob("*.json"):
        (root / "assets/schemas" / schema.name).write_bytes(schema.read_bytes())


def test_renderer_contains_bilingual_statuses_and_state_sections() -> None:
    rendered = render_project_status(snapshot())

    assert "В работе (active)" in rendered
    assert "На проверке (in_review)" in rendered
    assert "Черновик (draft)" in rendered
    assert "## Рабочий фокус" in rendered
    assert "## Ближайшие действия" in rendered
    assert "## Критерии" in rendered
    assert "2026-11-03" in rendered
    assert "generated_at" not in rendered


def test_generate_and_check_use_exact_source_bytes_and_are_deterministic(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    first = generate_status(tmp_path).read_bytes()
    second = generate_status(tmp_path).read_bytes()
    assert first == second
    plan_sha = hashlib.sha256((tmp_path / "docs/project-control/project-plan.yaml").read_bytes()).hexdigest()
    state_sha = hashlib.sha256((tmp_path / "docs/project-control/project-state.yaml").read_bytes()).hexdigest()
    combined = hashlib.sha256(f"{plan_sha}\n{state_sha}\n".encode()).hexdigest()
    text = first.decode("utf-8")
    assert f"Общий SHA-256: `{combined}`" in text
    assert check_status(tmp_path).is_valid


@pytest.mark.parametrize("mutation", ["missing", "stale", "manual"])
def test_check_rejects_missing_stale_and_manual_status(tmp_path: Path, mutation: str) -> None:
    _write_fixture(tmp_path)
    status_path = generate_status(tmp_path)
    if mutation == "missing":
        status_path.unlink()
    elif mutation == "stale":
        plan = tmp_path / "docs/project-control/project-plan.yaml"
        plan.write_bytes(plan.read_bytes() + b"\n")
    else:
        status_path.write_bytes(status_path.read_bytes() + b"\nmanual\n")

    result = check_status(tmp_path)

    assert not result.is_valid


def test_generate_rejects_unresolved_references_without_writing_status(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    plan_path = tmp_path / "docs/project-control/project-plan.yaml"
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["stages"][0]["required_artifacts"] = ["ART-MISSING"]
    plan_path.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n")

    with pytest.raises(ValidationError, match="unknown_required_artifact"):
        generate_status(tmp_path)

    assert not (tmp_path / "PROJECT_STATUS.md").exists()


def test_check_rejects_unresolved_references_read_only(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    status_path = generate_status(tmp_path)
    before = status_path.read_bytes()
    plan_path = tmp_path / "docs/project-control/project-plan.yaml"
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["stages"][0]["required_artifacts"] = ["ART-MISSING"]
    plan_path.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False), encoding="utf-8", newline="\n")

    result = check_status(tmp_path)

    assert not result.is_valid
    assert "unknown_required_artifact" in result.message
    assert status_path.read_bytes() == before
