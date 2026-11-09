"""Contract tests for canonical lifecycle state and transitions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.model import (
    ARTIFACT_STATUSES,
    CRITERION_STATUSES,
    SOURCE_STATUSES,
    STAGE_DEFINITIONS,
    STAGE_STATUSES,
    WORK_STATUSES,
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
from ai_sdlc.validation import validate_snapshot, validate_transition


def test_work_item_positional_scope_api_remains_compatible() -> None:
    scope = Scope(("ART-001",), ("staging/",))
    work = WorkItem("TASK-001", "Title", "ready", "executor", (), ("ART-001",), scope)

    assert work.scope == scope
    assert work.plan is None
    assert work.criterion_ids == ()
    assert work.criterion_evidence == ()


def snapshot_with(*, stage_status: str = "active") -> ProjectSnapshot:
    """Create the smallest structurally valid state for transition tests."""
    artifact = Artifact(
        id="ART-VISION",
        kind="product_vision",
        path="docs/product-definition/product-overview/product-vision.md",
        status="draft",
    )
    source = Source(
        id="SRC-001",
        kind="business_document",
        title="Brief",
        origin="customer",
        received_at="2026-11-03",
        version="1",
        location="docs/product-definition/sources/brief.md",
        status="registered",
    )
    work = WorkItem(
        id="TASK-001",
        title="Create vision",
        status="planned",
        executor="analyst-agent-01",
        produces=("ART-VISION",),
    )
    stage = Stage(
        id="STAGE-01",
        title="Анализ и определение продукта",
        status=stage_status,
        required_artifacts=("ART-VISION",),
        work_items=(work,),
        entry_criteria=(Criterion(id="EC-01", text="Brief exists", status="pending"),),
    )
    remaining_stages = tuple(
        Stage(id=stage_id, title=title, status="not_started")
        for stage_id, title in STAGE_DEFINITIONS[1:]
    )
    plan = ProjectPlan(
        schema_version=1,
        project_id="PROJECT-001",
        stages=(stage, *remaining_stages),
    )
    state = ProjectState(
        schema_version=1,
        project_id="PROJECT-001",
        lifecycle_mode="initial-development",
        current_stage="STAGE-01",
        focus_work_items=("TASK-001",),
        next_actions=(),
        updated_at="2026-11-03",
        updated_by="planning-coordinator",
    )
    return ProjectSnapshot(plan=plan, state=state, artifacts=(artifact,), sources=(source,))


def replace_status(snapshot: ProjectSnapshot, object_type: str, value: str) -> ProjectSnapshot:
    """Return one snapshot whose selected canonical status has changed."""
    stage = snapshot.plan.stages[0]
    if object_type == "stage":
        changed_stage = replace(stage, status=value)
        return replace(snapshot, plan=replace(snapshot.plan, stages=(changed_stage, *snapshot.plan.stages[1:])))
    if object_type == "work":
        changed_work = replace(stage.work_items[0], status=value)
        changed_stage = replace(stage, work_items=(changed_work,))
        return replace(snapshot, plan=replace(snapshot.plan, stages=(changed_stage, *snapshot.plan.stages[1:])))
    if object_type == "criterion":
        reason = "Not applicable for this transition" if value == "not_applicable" else None
        changed_criterion = replace(stage.entry_criteria[0], status=value, reason=reason)
        changed_stage = replace(stage, entry_criteria=(changed_criterion,))
        return replace(snapshot, plan=replace(snapshot.plan, stages=(changed_stage, *snapshot.plan.stages[1:])))
    if object_type == "artifact":
        return replace(snapshot, artifacts=(replace(snapshot.artifacts[0], status=value),))
    if object_type == "source":
        return replace(snapshot, sources=(replace(snapshot.sources[0], status=value),))
    raise AssertionError(f"unknown type: {object_type}")


@pytest.mark.parametrize(
    ("object_type", "before_status", "after_status"),
    [
        ("stage", "not_started", "planning"),
        ("stage", "planning", "ready"),
        ("stage", "ready", "active"),
        ("stage", "active", "in_review"),
        ("stage", "in_review", "completed"),
        ("stage", "in_review", "active"),
        ("stage", "active", "blocked"),
        ("stage", "blocked", "active"),
        ("work", "planned", "ready"),
        ("work", "ready", "in_progress"),
        ("work", "in_progress", "review"),
        ("work", "review", "done"),
        ("work", "review", "in_progress"),
        ("work", "in_progress", "blocked"),
        ("work", "blocked", "in_progress"),
        ("artifact", "draft", "in_review"),
        ("artifact", "in_review", "approved"),
        ("artifact", "in_review", "draft"),
        ("artifact", "approved", "superseded"),
        ("artifact", "approved", "retired"),
        ("source", "registered", "accepted"),
        ("source", "registered", "rejected"),
        ("source", "registered", "unavailable"),
        ("source", "accepted", "superseded"),
        ("criterion", "pending", "in_review"),
        ("criterion", "in_review", "verified"),
        ("criterion", "in_review", "not_applicable"),
        ("criterion", "in_review", "pending"),
    ],
)
def test_validate_transition_accepts_standard_transitions(
    object_type: str, before_status: str, after_status: str
) -> None:
    """Every normal, temporary, and failed-check transition stays valid."""
    before = replace_status(snapshot_with(stage_status=before_status if object_type == "stage" else "active"), object_type, before_status)
    after = replace_status(before, object_type, after_status)

    assert validate_transition(before, after).is_valid


@pytest.mark.parametrize(
    ("object_type", "before_status", "after_status"),
    [
        ("stage", "not_started", "active"),
        ("stage", "completed", "active"),
        ("work", "planned", "done"),
        ("work", "done", "review"),
        ("artifact", "draft", "approved"),
        ("artifact", "superseded", "draft"),
        ("source", "registered", "superseded"),
        ("source", "rejected", "accepted"),
        ("criterion", "pending", "verified"),
        ("criterion", "verified", "pending"),
    ],
)
def test_validate_transition_rejects_nonstandard_transitions(
    object_type: str, before_status: str, after_status: str
) -> None:
    """Skipping lifecycle gates or reviving terminals is rejected deterministically."""
    before = replace_status(snapshot_with(stage_status=before_status if object_type == "stage" else "active"), object_type, before_status)
    after = replace_status(before, object_type, after_status)

    report = validate_transition(before, after)

    assert not report.is_valid
    assert report.findings[0].code == "invalid_transition"


def test_model_contains_exactly_fourteen_ordered_stages() -> None:
    """The immutable catalogue retains the lifecycle defined by the standard."""
    assert STAGE_DEFINITIONS == (
        ("STAGE-01", "Анализ и определение продукта"),
        ("STAGE-02", "Определение границ системы"),
        ("STAGE-03", "Проектирование и проверка C2"),
        ("STAGE-04", "Уточнение ТЗ"),
        ("STAGE-05", "Архитектурная проработка"),
        ("STAGE-06", "Сценарная детализация архитектуры и SDD"),
        ("STAGE-07", "Фиксация исходного baseline"),
        ("STAGE-08", "Детальное планирование реализации"),
        ("STAGE-09", "Реализация с помощью ИИ"),
        ("STAGE-10", "Подготовка к развертыванию"),
        ("STAGE-11", "Развертывание на тестовом контуре"),
        ("STAGE-12", "Тестирование и приёмка"),
        ("STAGE-13", "Развертывание в промышленном контуре"),
        ("STAGE-14", "Эксплуатация и развитие"),
    )


def test_snapshot_validation_resolves_status_owners_and_cross_references() -> None:
    """Plans own stage/work state and every plan/state reference resolves once."""
    snapshot = snapshot_with()

    assert validate_snapshot(snapshot).is_valid
    assert STAGE_STATUSES[0] == "not_started"
    assert WORK_STATUSES[-1] == "cancelled"
    assert ARTIFACT_STATUSES[-1] == "retired"
    assert SOURCE_STATUSES[-1] == "unavailable"
    assert CRITERION_STATUSES[-1] == "not_applicable"


def test_snapshot_validation_rejects_dangling_state_and_produced_artifact_references() -> None:
    """A typo cannot make operational focus or declared work output ambiguous."""
    snapshot = snapshot_with()
    stage = snapshot.plan.stages[0]
    dangling_work = replace(stage.work_items[0], produces=("ART-MISSING",))
    changed_stage = replace(stage, work_items=(dangling_work,))
    changed_plan = replace(snapshot.plan, stages=(changed_stage, *snapshot.plan.stages[1:]))
    changed_state = replace(snapshot.state, focus_work_items=("TASK-MISSING",))

    report = validate_snapshot(replace(snapshot, plan=changed_plan, state=changed_state))

    assert [finding.code for finding in report.findings] == [
        "unknown_produced_artifact",
        "unknown_focus_work_item",
    ]


def test_snapshot_validation_reports_non_string_executor_without_crashing() -> None:
    """Malformed direct model input still yields a deterministic finding."""
    stage = snapshot_with().plan.stages[0]
    malformed_work = replace(stage.work_items[0], executor=None)  # type: ignore[arg-type]
    malformed_stage = replace(stage, work_items=(malformed_work,))
    snapshot = snapshot_with()
    malformed_plan = replace(snapshot.plan, stages=(malformed_stage, *snapshot.plan.stages[1:]))

    report = validate_snapshot(replace(snapshot, plan=malformed_plan))

    assert report.findings[0].code == "missing_executor"


def test_model_collections_are_normalized_to_immutable_tuples() -> None:
    """Direct construction cannot smuggle mutable lists into frozen records."""
    criterion = Criterion(id="EC-01", text="Brief exists", status="pending")
    artifact = Artifact(id="ART-VISION", kind="product_vision", path="vision.md", status="draft")
    source = Source(
        id="SRC-001", kind="business_document", title="Brief", origin="customer",
        received_at="2026-11-03", version="1", location="brief.md", status="registered",
    )
    work = WorkItem(
        id="TASK-001", title="Create vision", status="planned", executor="agent",
        depends_on=["TASK-000"], produces=["ART-VISION"],
        scope=Scope(artifacts=["ART-VISION"], paths=["docs/"]),
    )
    stage = Stage(
        id="STAGE-01", title=STAGE_DEFINITIONS[0][1], status="active",
        entry_criteria=[criterion], work_items=[work],
    )
    plan = ProjectPlan(schema_version=1, project_id="PROJECT-001", stages=[stage])
    state = ProjectState(
        schema_version=1, project_id="PROJECT-001", lifecycle_mode="initial-development",
        current_stage="STAGE-01", focus_work_items=["TASK-001"],
        next_actions=[NextAction("TASK-001", "continue")], updated_at="2026-11-03", updated_by="agent",
    )
    snapshot = ProjectSnapshot(plan=plan, state=state, artifacts=[artifact], sources=[source])

    assert isinstance(snapshot.plan.stages, tuple)
    assert isinstance(snapshot.plan.stages[0].work_items, tuple)
    assert isinstance(snapshot.plan.stages[0].work_items[0].depends_on, tuple)
    assert isinstance(snapshot.plan.stages[0].work_items[0].scope.artifacts, tuple)
    assert isinstance(snapshot.state.focus_work_items, tuple)
    assert isinstance(snapshot.state.next_actions, tuple)
    assert isinstance(snapshot.artifacts, tuple)
    assert isinstance(snapshot.sources, tuple)


def test_snapshot_validation_reports_malformed_scope_without_crashing() -> None:
    """A direct model with a non-Scope value yields a structured finding."""
    stage = snapshot_with().plan.stages[0]
    malformed_work = replace(stage.work_items[0], scope={"artifacts": ("ART-VISION",)})  # type: ignore[arg-type]
    malformed_stage = replace(stage, work_items=(malformed_work,))
    snapshot = snapshot_with()
    malformed_plan = replace(snapshot.plan, stages=(malformed_stage, *snapshot.plan.stages[1:]))

    report = validate_snapshot(replace(snapshot, plan=malformed_plan))

    assert report.findings[0].code == "invalid_scope"


@pytest.mark.parametrize("malformed_status", [["active"], {"value": "active"}])
def test_transition_validation_reports_wrong_type_status_without_crashing(malformed_status: object) -> None:
    """Unhashable or wrong-type statuses cannot escape as built-in exceptions."""
    before = snapshot_with()
    after = replace_status(before, "stage", malformed_status)  # type: ignore[arg-type]

    report = validate_transition(before, after)

    assert not report.is_valid
    assert report.findings[0].code == "invalid_status_or_kind"
