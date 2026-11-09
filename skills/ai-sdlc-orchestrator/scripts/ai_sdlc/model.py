"""Immutable canonical records for the governed fourteen-stage lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ValidationError


STAGE_DEFINITIONS = (
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

STAGE_STATUSES = ("not_started", "planning", "ready", "active", "in_review", "blocked", "completed")
WORK_STATUSES = (
    "planned", "ready", "in_progress", "review", "done", "needs_clarification", "blocked", "deferred", "cancelled",
)
ARTIFACT_STATUSES = ("draft", "in_review", "approved", "superseded", "retired")
SOURCE_STATUSES = ("registered", "accepted", "superseded", "rejected", "unavailable")
CRITERION_STATUSES = ("pending", "in_review", "verified", "not_applicable")
ARTIFACT_KINDS = (
    "product_vision", "product_goals", "system_scope", "requirements_analysis", "domain_model", "roles_and_processes",
    "entity_lifecycles", "gaps_and_conflicts", "assumptions", "open_questions", "business_scenario", "business_scenario_catalog",
    "business_requirements", "roles_and_access_matrix", "functional_requirements", "non_functional_requirements", "constraints",
    "acceptance_criteria", "domain_glossary", "technical_glossary", "technical_specification_manifest", "technical_specification_release",
    "c4_context", "c4_container", "c4_component", "c4_dynamic", "arc42_section", "adr", "architecture_baseline",
    "sdd_specification", "test_plan", "test_report", "deployment_package", "release_record", "operations_document",
)
SOURCE_KINDS = (
    "contract", "technical_specification", "business_document", "meeting_protocol", "message", "data_export", "regulation", "external_reference",
)


def _immutable_tuple(value: Any) -> tuple[Any, ...]:
    """Normalize collection inputs so frozen records remain deeply immutable."""
    return () if value is None else tuple(value)


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    text: str
    status: str
    mandatory: bool = True
    reason: str | None = None
    approval_role: str | None = None
    approval_gate: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None


@dataclass(frozen=True, slots=True)
class Scope:
    artifacts: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", _immutable_tuple(self.artifacts))
        object.__setattr__(self, "paths", _immutable_tuple(self.paths))


@dataclass(frozen=True, slots=True)
class WorkItem:
    id: str
    title: str
    status: str
    executor: str
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    scope: Scope = field(default_factory=Scope)
    plan: str | None = None
    criterion_ids: tuple[str, ...] = ()
    criterion_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "depends_on", _immutable_tuple(self.depends_on))
        object.__setattr__(self, "produces", _immutable_tuple(self.produces))
        object.__setattr__(self, "criterion_ids", _immutable_tuple(self.criterion_ids))
        object.__setattr__(self, "criterion_evidence", _immutable_tuple(self.criterion_evidence))
        if self.scope is None:
            object.__setattr__(self, "scope", Scope())


@dataclass(frozen=True, slots=True)
class Stage:
    id: str
    title: str
    status: str
    depends_on: tuple[str, ...] = ()
    entry_criteria: tuple[Criterion, ...] = ()
    exit_criteria: tuple[Criterion, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    work_items: tuple[WorkItem, ...] = ()
    plan: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "depends_on", _immutable_tuple(self.depends_on))
        object.__setattr__(self, "entry_criteria", _immutable_tuple(self.entry_criteria))
        object.__setattr__(self, "exit_criteria", _immutable_tuple(self.exit_criteria))
        object.__setattr__(self, "required_artifacts", _immutable_tuple(self.required_artifacts))
        object.__setattr__(self, "work_items", _immutable_tuple(self.work_items))


@dataclass(frozen=True, slots=True)
class ProjectPlan:
    schema_version: int
    project_id: str
    stages: tuple[Stage, ...]
    planning_mode: str = "rolling_wave"

    def __post_init__(self) -> None:
        object.__setattr__(self, "stages", _immutable_tuple(self.stages))


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    kind: str
    path: str
    status: str


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    kind: str
    title: str
    origin: str
    received_at: str
    version: str
    location: str
    status: str
    scope: str | None = None
    checksum: str | None = None


@dataclass(frozen=True, slots=True)
class NextAction:
    work_item: str
    action: str


@dataclass(frozen=True, slots=True)
class ProjectState:
    schema_version: int
    project_id: str
    lifecycle_mode: str
    current_stage: str
    focus_work_items: tuple[str, ...]
    next_actions: tuple[NextAction, ...]
    updated_at: str
    updated_by: str
    active_change: str | None = None
    current_baseline: str | None = None
    last_approved_artifact: str | None = None
    deployment_package_identity: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "focus_work_items", _immutable_tuple(self.focus_work_items))
        object.__setattr__(self, "next_actions", _immutable_tuple(self.next_actions))


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    plan: ProjectPlan
    state: ProjectState
    artifacts: tuple[Artifact, ...]
    sources: tuple[Source, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", _immutable_tuple(self.artifacts))
        object.__setattr__(self, "sources", _immutable_tuple(self.sources))


def load_project(root: Path) -> ProjectSnapshot:
    """Load only the four canonical YAML sources into immutable records."""
    root = root.resolve()
    plan = _load_yaml(root / "docs/project-control/project-plan.yaml")
    state = _load_yaml(root / "docs/project-control/project-state.yaml")
    artifacts = _load_yaml(root / "docs/artifact-register.yaml")
    sources = _load_yaml(root / "docs/product-definition/sources/source-register.yaml")
    _validate_schemas(root, plan, state, artifacts, sources)
    return ProjectSnapshot(
        plan=_project_plan(plan),
        state=_project_state(state),
        artifacts=tuple(_artifact(value) for value in artifacts["artifacts"]),
        sources=tuple(_source(value) for value in sources["sources"]),
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"canonical YAML is missing: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValidationError(f"canonical YAML cannot be read: {path}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"canonical YAML must contain an object: {path}")
    return value


def _validate_schemas(root: Path, *documents: dict[str, Any]) -> None:
    from .validation import validate_documents

    report = validate_documents(root, *documents)
    if not report.is_valid:
        first = report.findings[0]
        raise ValidationError(f"{first.code}: {first.message}")


def _criterion(value: dict[str, Any]) -> Criterion:
    return Criterion(
        id=value["id"], text=value["text"], status=value["status"], mandatory=value.get("mandatory", True),
        reason=value.get("reason"), approval_role=value.get("approval_role"), approval_gate=value.get("approval_gate"), approved_by=value.get("approved_by"), approved_at=_text(value.get("approved_at")),
    )


def _scope(value: dict[str, Any] | None) -> Scope:
    value = value or {}
    return Scope(artifacts=tuple(value.get("artifacts", ())), paths=tuple(value.get("paths", ())))


def _work(value: dict[str, Any]) -> WorkItem:
    return WorkItem(
        id=value["id"], title=value["title"], status=value["status"], executor=value["executor"],
        depends_on=tuple(value.get("depends_on", ())), produces=tuple(value.get("produces", ())),
        criterion_ids=tuple(value.get("criterion_ids", ())), criterion_evidence=tuple(value.get("criterion_evidence", ())),
        scope=_scope(value.get("scope")), plan=value.get("plan"),
    )


def _stage(value: dict[str, Any]) -> Stage:
    return Stage(
        id=value["id"], title=value["title"], status=value["status"], depends_on=tuple(value.get("depends_on", ())),
        entry_criteria=tuple(_criterion(item) for item in value.get("entry_criteria", ())),
        exit_criteria=tuple(_criterion(item) for item in value.get("exit_criteria", ())),
        required_artifacts=tuple(value.get("required_artifacts", ())), work_items=tuple(_work(item) for item in value.get("work_items", ())), plan=value.get("plan"),
    )


def _project_plan(value: dict[str, Any]) -> ProjectPlan:
    return ProjectPlan(value["schema_version"], value["project_id"], tuple(_stage(stage) for stage in value["stages"]), value.get("planning_mode", "rolling_wave"))


def _project_state(value: dict[str, Any]) -> ProjectState:
    return ProjectState(
        schema_version=value["schema_version"], project_id=value["project_id"], lifecycle_mode=value["lifecycle_mode"], current_stage=value["current_stage"],
        focus_work_items=tuple(value["focus_work_items"]), next_actions=tuple(NextAction(item["work_item"], item["action"]) for item in value["next_actions"]),
        updated_at=_text(value["updated_at"]), updated_by=value["updated_by"], active_change=value.get("active_change"), current_baseline=value.get("current_baseline"), last_approved_artifact=value.get("last_approved_artifact"), deployment_package_identity=value.get("deployment_package_identity"),
    )


def _artifact(value: dict[str, Any]) -> Artifact:
    return Artifact(value["id"], value["kind"], value["path"], value["status"])


def _source(value: dict[str, Any]) -> Source:
    return Source(
        value["id"], value["kind"], value["title"], value["origin"], _text(value["received_at"]), _text(value["version"]), value["location"], value["status"], value.get("scope"), value.get("checksum"),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
