"""Deterministic human-readable projection of canonical project state."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from .errors import OrchestratorError, ValidationError
from .io import sha256_file
from .model import Criterion, ProjectSnapshot, Stage, WorkItem, load_project
from .readiness import Readiness, calculate_readiness
from .validation import validate_snapshot


STATUS_LABELS = {
    "not_started": "Не начато", "planning": "Планирование", "ready": "Готово",
    "active": "В работе", "in_review": "На проверке", "blocked": "Заблокировано", "completed": "Завершено",
    "planned": "Запланировано", "in_progress": "В работе", "review": "На проверке", "done": "Завершено",
    "needs_clarification": "Требует уточнения", "deferred": "Отложено", "cancelled": "Отменено",
    "draft": "Черновик", "approved": "Утверждено", "superseded": "Заменено", "retired": "Выведено из действия",
    "registered": "Зарегистрирован", "accepted": "Принят", "rejected": "Отклонён", "unavailable": "Недоступен",
    "pending": "Ожидает проверки", "verified": "Проверен", "not_applicable": "Неприменим",
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a read-only status projection check."""

    is_valid: bool
    message: str = ""
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.is_valid

    @property
    def valid(self) -> bool:
        return self.is_valid


def status_label(status: str) -> str:
    """Return the required Russian label while preserving canonical status."""
    return f"{STATUS_LABELS.get(status, status)} ({status})"


def _readiness_line(readiness: Readiness) -> str:
    return readiness.display()


def _criteria(lines: list[str], heading: str, criteria: tuple[Criterion, ...]) -> None:
    lines.append(f"### {heading}")
    if not criteria:
        lines.append("- Нет критериев")
        return
    for criterion in criteria:
        required = "обязательный" if criterion.mandatory else "необязательный"
        reason = f"; причина: {criterion.reason}" if criterion.reason else ""
        lines.append(f"- `{criterion.id}` — {criterion.text} — {status_label(criterion.status)} ({required}{reason})")


def _work_line(work: WorkItem) -> str:
    dependencies = ", ".join(f"`{item}`" for item in work.depends_on) or "нет"
    results = ", ".join(f"`{item}`" for item in work.produces) or "нет"
    return f"- `{work.id}` — {work.title}; исполнитель: `{work.executor}`; зависимости: {dependencies}; результаты: {results}; статус: {status_label(work.status)}"


def _stage_readiness(stage: Stage) -> tuple[Readiness, Readiness]:
    return calculate_readiness(stage.entry_criteria), calculate_readiness(stage.exit_criteria)


def render_project_status(
    snapshot: ProjectSnapshot,
    *,
    plan_sha256: str | None = None,
    state_sha256: str | None = None,
    combined_sha256: str | None = None,
) -> str:
    """Render a snapshot without reading clocks or mutating any files."""
    stages = snapshot.plan.stages
    current_index = next(index for index, stage in enumerate(stages) if stage.id == snapshot.state.current_stage)
    current = stages[current_index]
    next_stage = stages[current_index + 1] if current_index + 1 < len(stages) else None
    lines = [
        "# PROJECT STATUS",
        "",
        "> ⚠️ Этот файл сформирован автоматически. Не редактируйте вручную.",
        "> Источники: `docs/project-control/project-plan.yaml`, `docs/project-control/project-state.yaml`.",
    ]
    if plan_sha256 is not None and state_sha256 is not None and combined_sha256 is not None:
        lines.extend([
            f"> SHA-256 `project-plan.yaml`: `{plan_sha256}`",
            f"> SHA-256 `project-state.yaml`: `{state_sha256}`",
            f"> Общий SHA-256: `{combined_sha256}`",
        ])
    lines.extend([
        "",
        "## Состояние проекта",
        f"- Проект: `{snapshot.plan.project_id}`",
        f"- Режим жизненного цикла: `{snapshot.state.lifecycle_mode}`",
        f"- Текущий этап: `{current.id}` — {current.title}; статус: {status_label(current.status)}",
        f"- Baseline: `{snapshot.state.current_baseline or 'не указан'}`",
        f"- Активное изменение: `{snapshot.state.active_change or 'нет'}`",
        f"- Актуально на: {snapshot.state.updated_at}",
        "",
        "## Рабочий фокус",
    ])
    if snapshot.state.focus_work_items:
        lines.extend(f"- `{item}`" for item in snapshot.state.focus_work_items)
    else:
        lines.append("- Нет")
    lines.extend(["", "## Ближайшие действия"])
    if snapshot.state.next_actions:
        lines.extend(f"- `{action.work_item}` — {action.action}" for action in snapshot.state.next_actions)
    else:
        lines.append("- Нет")

    lines.extend(["", "## Готовность этапов"])
    for stage in stages:
        entry, exit_ = _stage_readiness(stage)
        lines.append(f"- `{stage.id}` — {stage.title}; статус: {status_label(stage.status)}; входная: {_readiness_line(entry)}; выходная: {_readiness_line(exit_)}")

    lines.extend(["", "## Работы"])
    for stage in stages:
        if not stage.work_items:
            continue
        lines.append(f"### `{stage.id}` — {stage.title}")
        lines.extend(_work_line(work) for work in stage.work_items)

    lines.extend(["", "## Блокировки"])
    blockers = [f"- Этап `{stage.id}` — {stage.title}" for stage in stages if stage.status == "blocked"]
    blockers.extend(f"- Работа `{stage.id}/{work.id}` — {work.title}" for stage in stages for work in stage.work_items if work.status == "blocked")
    lines.extend(blockers or ["- Нет"])

    lines.extend(["", "## Артефакты"])
    if snapshot.artifacts:
        lines.extend(f"- `{artifact.id}` — `{artifact.path}`; вид: `{artifact.kind}`; статус: {status_label(artifact.status)}" for artifact in snapshot.artifacts)
    else:
        lines.append("- Нет")
    lines.extend(["", "## Источники"])
    if snapshot.sources:
        lines.extend(f"- `{source.id}` — {source.title}; расположение: `{source.location}`; статус: {status_label(source.status)}" for source in snapshot.sources)
    else:
        lines.append("- Нет")

    lines.extend(["", "## Критерии"])
    for stage in stages:
        if not stage.entry_criteria and not stage.exit_criteria:
            continue
        lines.append(f"### `{stage.id}` — {stage.title}")
        _criteria(lines, "Входные", stage.entry_criteria)
        _criteria(lines, "Выходные", stage.exit_criteria)

    if next_stage is not None:
        next_entry, _ = _stage_readiness(next_stage)
        lines.extend(["", f"Следующий этап: `{next_stage.id}` — {next_stage.title}; входная готовность: {_readiness_line(next_entry)}"])
    return "\n".join(lines) + "\n"


def _source_fingerprint(root: Path) -> tuple[str, str, str]:
    plan_path = root / "docs/project-control/project-plan.yaml"
    state_path = root / "docs/project-control/project-state.yaml"
    plan_sha = sha256_file(plan_path)
    state_sha = sha256_file(state_path)
    combined = hashlib.sha256(f"{plan_sha}\n{state_sha}\n".encode("utf-8")).hexdigest()
    return plan_sha, state_sha, combined


def _load_validated_project(root: Path) -> ProjectSnapshot:
    snapshot = load_project(root)
    report = validate_snapshot(snapshot)
    if not report.is_valid:
        finding = report.findings[0]
        raise ValidationError(f"{finding.code}: {finding.message}")
    return snapshot


def generate_status(root: Path) -> Path:
    """Generate the root projection and return its path."""
    root = root.resolve()
    snapshot = _load_validated_project(root)
    plan_sha, state_sha, combined = _source_fingerprint(root)
    rendered = render_project_status(snapshot, plan_sha256=plan_sha, state_sha256=state_sha, combined_sha256=combined)
    output = root / "PROJECT_STATUS.md"
    output.write_bytes(rendered.encode("utf-8"))
    return output


def check_status(root: Path) -> CheckResult:
    """Build the expected projection in memory and compare bytes read-only."""
    root = root.resolve()
    output = root / "PROJECT_STATUS.md"
    try:
        snapshot = _load_validated_project(root)
        plan_sha, state_sha, combined = _source_fingerprint(root)
        expected = render_project_status(snapshot, plan_sha256=plan_sha, state_sha256=state_sha, combined_sha256=combined).encode("utf-8")
        actual = output.read_bytes()
    except (OSError, UnicodeError, OrchestratorError, StopIteration) as error:
        return CheckResult(False, str(error), output)
    if actual != expected:
        return CheckResult(False, "PROJECT_STATUS.md is missing, stale, or manually modified", output)
    return CheckResult(True, "PROJECT_STATUS.md is current", output)
