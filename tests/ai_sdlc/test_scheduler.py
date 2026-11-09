"""Focused tests for DAG validation and deterministic safe batches."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "ai-sdlc-orchestrator" / "scripts"))

from ai_sdlc.model import (  # noqa: E402
    STAGE_DEFINITIONS,
    Artifact,
    ProjectPlan,
    ProjectSnapshot,
    ProjectState,
    Scope,
    Source,
    Stage,
    WorkItem,
)
from ai_sdlc.scheduler import (  # noqa: E402
    build_safe_batch,
    conflicts,
    ready_work,
    validate_dag,
)


def work(item_id: str, *, status: str = "ready", deps: tuple[str, ...] = (), paths: tuple[str, ...] = (), artifacts: tuple[str, ...] = ()) -> WorkItem:
    return WorkItem(item_id, item_id, status, "executor", deps, artifacts, Scope(artifacts, paths))


def test_validate_dag_reports_self_dependency_and_arbitrary_cycle_deterministically() -> None:
    items = (work("B", deps=("C",)), work("A", deps=("A", "B")), work("C", deps=("A",)))

    report = validate_dag(items)

    assert not report.is_valid
    assert [finding.code for finding in report.findings] == ["self_dependency", "dependency_cycle"]
    assert report.findings[0].path == "work_items[A].depends_on"
    assert "self-dependency" in report.findings[0].message
    assert "A" in report.findings[1].message and "B" in report.findings[1].message
    assert report == validate_dag(tuple(reversed(items)))


def test_validate_dag_reports_duplicate_and_unknown_dependencies() -> None:
    report = validate_dag((work("A", deps=("MISSING",)), work("A")))

    assert {finding.code for finding in report.findings} == {"duplicate_id", "unknown_work_dependency"}


def test_ready_work_requires_ready_status_and_done_dependencies() -> None:
    items = (
        work("A", status="done"),
        work("B", deps=("A",)),
        work("C", status="in_progress"),
        work("D", deps=("C",)),
    )
    stages = tuple(Stage(stage_id, title, "not_started", work_items=items if index == 0 else ()) for index, (stage_id, title) in enumerate(STAGE_DEFINITIONS))
    snapshot = ProjectSnapshot(
        ProjectPlan(1, "P", stages),
        ProjectState(1, "P", "initial-development", "STAGE-01", (), (), "2026-11-03", "test"),
        (Artifact("ART", "product_vision", "docs/a.md", "draft"),),
        (Source("SRC", "business_document", "brief", "customer", "2026-11-03", "1", "brief.md", "registered"),),
    )

    assert [item.id for item in ready_work(snapshot)] == ["B"]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (work("A", paths=("src",)), work("B", paths=("src/module.py",))),
        (work("A", paths=(r"C:\Repo\SRC\a.py",)), work("B", paths=("c:/repo/src/a.py",))),
        (work("A", artifacts=("ART-1",)), work("B", artifacts=("ART-1",))),
    ],
)
def test_conflict_graph_is_conservative_for_paths_and_artifacts(left: WorkItem, right: WorkItem) -> None:
    assert conflicts(left, right)


def test_root_path_conflicts_with_descendant() -> None:
    assert conflicts(work("A", paths=("/",)), work("B", paths=("/foo",)))


def test_conflict_graph_rejects_shared_unresolved_decision() -> None:
    left = SimpleNamespace(id="A", scope=Scope(), unresolved_decisions=("DEC-1",))
    right = SimpleNamespace(id="B", scope=Scope(), unresolved_decisions=("DEC-1",))

    assert conflicts(left, right)


def test_safe_batch_is_deterministic_and_has_no_pairwise_conflicts() -> None:
    items = (
        work("C", paths=("docs/c.md",)),
        work("A", paths=("src/a.py",)),
        work("B", paths=("src/b.py",)),
    )

    batch = build_safe_batch(items, capacity=2)

    assert [item.id for item in batch.items] == ["A", "B"]
    assert all(not conflicts(left, right) for index, left in enumerate(batch.items) for right in batch.items[index + 1 :])
    assert batch == build_safe_batch(tuple(reversed(items)), capacity=2)


def test_property_no_pair_in_any_generated_batch_conflicts() -> None:
    items = tuple(work(f"TASK-{index:02d}", paths=(f"src/{index}/file.py",)) for index in range(12))

    for capacity in range(1, 6):
        batch = build_safe_batch(items, capacity)
        assert all(not conflicts(left, right) for index, left in enumerate(batch.items) for right in batch.items[index + 1 :])


def test_safe_batch_rejects_invalid_capacity() -> None:
    with pytest.raises(ValueError):
        build_safe_batch((work("A"),), capacity=0)


def test_parallel_batch_exposes_required_integration_work() -> None:
    branches = (work("A", paths=("src/a.py",)), work("B", paths=("src/b.py",)))
    integration = work("INTEGRATE", status="planned", deps=("A", "B"), paths=("integration",))

    batch = build_safe_batch((*branches, integration), capacity=2)

    assert [item.id for item in batch.items] == ["A", "B"]
    assert batch.integration_work == integration
    assert set(batch.integration_work.depends_on) == {item.id for item in batch.items}


def test_ready_fan_in_is_integration_work_even_without_integration_name() -> None:
    branches = (work("A", paths=("src/a.py",)), work("B", paths=("src/b.py",)))
    merge = work("MERGE", deps=("A", "B"), paths=("merge",))

    batch = build_safe_batch((*branches, merge), capacity=3)

    assert [item.id for item in batch.items] == ["A", "B"]
    assert batch.integration_work == merge


def test_batch_completion_requires_integration_completion() -> None:
    branches = (work("A"), work("B"))
    integration = work("MERGE", status="planned", deps=("A", "B"))
    batch = build_safe_batch((*branches, integration), capacity=2)

    assert not batch.is_complete(("A", "B"))
    assert batch.is_complete(("A", "B", "MERGE"))


def test_batch_excludes_ready_work_with_unfinished_dependency() -> None:
    waiting = work("WAIT", deps=("UPSTREAM",))

    batch = build_safe_batch((waiting, work("UPSTREAM")), capacity=2)

    assert [item.id for item in batch.items] == ["UPSTREAM"]


def test_plain_batch_input_without_dependency_evidence_fails_closed() -> None:
    waiting = work("WAIT", deps=("UPSTREAM",))

    batch = build_safe_batch((waiting,), capacity=1)

    assert batch.items == ()


def test_fan_in_is_schedulable_on_next_snapshot_tick() -> None:
    branches = (work("A"), work("B"))
    merge = work("MERGE", deps=("A", "B"))

    def snapshot(items: tuple[WorkItem, ...]) -> ProjectSnapshot:
        stages = tuple(Stage(stage_id, title, "not_started", work_items=items if index == 0 else ()) for index, (stage_id, title) in enumerate(STAGE_DEFINITIONS))
        return ProjectSnapshot(
            ProjectPlan(1, "P", stages),
            ProjectState(1, "P", "initial-development", "STAGE-01", (), (), "2026-11-03", "test"),
            (),
            (),
        )

    first_ready = ready_work(snapshot((*branches, merge)))
    first_batch = build_safe_batch(first_ready, capacity=2)
    assert {item.id for item in first_batch.items} == {"A", "B"}

    second_ready = ready_work(snapshot((work("A", status="done"), work("B", status="done"), merge)))
    second_batch = build_safe_batch(second_ready, capacity=1)
    assert [item.id for item in second_batch.items] == ["MERGE"]
    assert second_batch.integration_work is None


def test_empty_batch_is_never_complete() -> None:
    assert not build_safe_batch((), capacity=1).is_complete(())


def test_explicit_integration_marker_is_schedulable_when_standalone() -> None:
    integration = WorkItem("INT", "Integration", "ready", "executor")
    stages = tuple(Stage(stage_id, title, "not_started", work_items=(integration,) if index == 0 else ()) for index, (stage_id, title) in enumerate(STAGE_DEFINITIONS))
    snapshot = ProjectSnapshot(
        ProjectPlan(1, "P", stages),
        ProjectState(1, "P", "initial-development", "STAGE-01", (), (), "2026-11-03", "test"),
        (),
        (),
    )

    ready = ready_work(snapshot)

    assert [item.id for item in build_safe_batch(ready, capacity=1).items] == ["INT"]
