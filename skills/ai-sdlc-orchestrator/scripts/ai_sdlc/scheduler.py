"""Deterministic dependency scheduling and conservative scope conflict checks."""

from __future__ import annotations

from dataclasses import dataclass
import ntpath
from typing import Any, Iterable, Mapping

from .model import ProjectSnapshot, WorkItem
from .validation import ValidationFinding, ValidationReport


class ReadyWork(list[WorkItem]):
    """List-compatible ready selection carrying verified dependency evidence."""

    __slots__ = ("completed_dependency_ids",)

    def __init__(self, values: Iterable[WorkItem], completed_dependency_ids: Iterable[str]) -> None:
        super().__init__(values)
        self.completed_dependency_ids = frozenset(str(value) for value in completed_dependency_ids)


@dataclass(frozen=True, slots=True)
class ExecutionBatch:
    """An immutable set of work selected for one parallel execution step."""

    items: tuple[WorkItem, ...] = ()
    integration_work: WorkItem | None = None

    @property
    def work_items(self) -> tuple[WorkItem, ...]:
        """Compatibility name for callers that use the domain terminology."""
        return self.items

    def completion_report(self, completed_ids: Iterable[str]) -> ValidationReport:
        """Validate that branches and the required integration work completed."""
        completed = {str(value) for value in completed_ids}
        findings: list[ValidationFinding] = []
        if not self.items:
            findings.append(_finding("empty_batch", "batch.items", "an empty execution batch cannot be complete"))
        for item in self.items:
            if str(item.id) not in completed:
                findings.append(_finding("incomplete_work", f"batch.items[{item.id}]", f"work item {item.id} is not completed"))
        if len(self.items) > 1:
            if self.integration_work is None:
                findings.append(_finding("missing_integration_work", "batch.integration_work", "parallel batch requires integration work before completion"))
            elif str(self.integration_work.id) not in completed:
                findings.append(_finding("incomplete_integration", "batch.integration_work", f"integration work {self.integration_work.id} is not completed"))
        return ValidationReport(tuple(sorted(findings, key=lambda finding: (finding.path, finding.code, finding.message))))

    def is_complete(self, completed_ids: Iterable[str]) -> bool:
        """Return true only after all branches and integration work are done."""
        return self.completion_report(completed_ids).is_valid


def validate_dag(work_items: Iterable[WorkItem]) -> ValidationReport:
    """Validate IDs and dependency topology without changing the supplied records."""
    items = tuple(work_items)
    ordered = tuple(sorted(items, key=lambda item: (str(getattr(item, "id", "")), repr(item))))
    by_id: dict[str, list[WorkItem]] = {}
    for item in ordered:
        by_id.setdefault(str(getattr(item, "id", "")), []).append(item)
    ids = set(by_id)
    findings: list[ValidationFinding] = []

    for item_id, matches in sorted(by_id.items()):
        if len(matches) > 1:
            findings.append(_finding("duplicate_id", f"work_items[{item_id}]", f"work item ID is duplicated: {item_id}"))

    adjacency: dict[str, tuple[str, ...]] = {}
    for item in ordered:
        item_id = str(getattr(item, "id", ""))
        dependencies = tuple(str(value) for value in _value(item, "depends_on", ()))
        for dependency in sorted(set(dependencies)):
            if dependency not in ids:
                findings.append(_finding(
                    "unknown_work_dependency",
                    f"work_items[{item_id}].depends_on",
                    f"work item {item_id} depends on unknown work item {dependency}",
                ))
            elif dependency == item_id:
                findings.append(_finding(
                    "self_dependency",
                    f"work_items[{item_id}].depends_on",
                    f"self-dependency detected for work item {item_id}",
                ))
        adjacency.setdefault(item_id, tuple(sorted({dependency for dependency in dependencies if dependency in ids and dependency != item_id})))

    for component in _strongly_connected_components(adjacency):
        if len(component) > 1:
            witness = _cycle_witness(component, adjacency)
            start = min(component)
            findings.append(_finding(
                "dependency_cycle",
                f"work_items[{start}].depends_on",
                f"dependency cycle detected: {' -> '.join(witness)}",
            ))

    findings.sort(key=lambda finding: (finding.path, 0 if finding.code == "self_dependency" else 1, finding.code, finding.message))
    return ValidationReport(tuple(findings))


def ready_work(snapshot: ProjectSnapshot) -> list[WorkItem]:
    """Return ready work whose dependencies have all reached ``done``."""
    all_items = tuple(work for stage in snapshot.plan.stages for work in stage.work_items)
    by_id: dict[str, WorkItem] = {}
    for item in sorted(all_items, key=lambda value: str(value.id)):
        by_id.setdefault(str(item.id), item)
    completed = {item_id for item_id, item in by_id.items() if item.status in {"done", "completed"}}
    ready = [
        item for item in all_items
        if item.status == "ready" and all(str(dependency) in completed for dependency in item.depends_on)
    ]
    return ReadyWork(sorted(ready, key=lambda item: str(item.id)), completed)


def conflicts(left: WorkItem, right: WorkItem) -> bool:
    """Return whether two work items cannot safely share an execution batch."""
    if str(left.id) == str(right.id):
        return True
    if _overlapping_paths(_scope_values(left, "paths"), _scope_values(right, "paths")):
        return True
    if _overlapping_values(_scope_values(left, "artifacts"), _scope_values(right, "artifacts")):
        return True
    return bool(_decision_values(left) & _decision_values(right))


def build_safe_batch(items: Iterable[WorkItem], capacity: int) -> ExecutionBatch:
    """Select a deterministic, greedy-by-ID conflict-free batch.

    Only ``ready`` work is schedulable.  A non-ready item explicitly marked as
    an integration work item is attached to the result when it depends on all
    selected branches; it is never itself scheduled in the parallel batch.
    """
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
        raise ValueError("capacity must be a positive integer")
    dependency_evidence = getattr(items, "completed_dependency_ids", None)
    all_items = tuple(items)
    integration_candidates = tuple(sorted(
        (item for item in all_items if _is_integration_candidate(item, all_items)),
        key=lambda item: str(item.id),
    ))
    by_id = {str(item.id): item for item in all_items}
    candidates = sorted(
        (
            item for item in all_items
            if item.status == "ready"
            and not _is_integration_candidate(item, all_items)
            and _dependencies_completed(item, by_id, dependency_evidence)
        ),
        key=lambda item: (-_priority(item), str(item.id)),
    )
    selected: list[WorkItem] = []
    for item in candidates:
        if len(selected) >= capacity:
            break
        if all(not conflicts(item, existing) for existing in selected):
            selected.append(item)
    selected_ids = {str(item.id) for item in selected}
    integration = next(
        (
            item for item in integration_candidates
            if selected_ids and selected_ids.issubset({str(value) for value in item.depends_on})
        ),
        None,
    )
    return ExecutionBatch(tuple(selected), integration)


def _finding(code: str, path: str, message: str) -> ValidationFinding:
    return ValidationFinding(code, path, message)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _scope_values(item: Any, name: str) -> tuple[Any, ...]:
    scope = _value(item, "scope", None)
    if scope is None:
        return ()
    return tuple(_value(scope, name, ()) or ())


def _canonical_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "<invalid-path>"
    path = value.strip().replace("/", "\\")
    normalized = ntpath.normpath(path).replace("\\", "/").casefold()
    if normalized in {"", "."}:
        return "<invalid-path>"
    return normalized.rstrip("/") or "/"


def _overlapping_paths(left: Iterable[Any], right: Iterable[Any]) -> bool:
    for left_path in left:
        first = _canonical_path(left_path)
        for right_path in right:
            second = _canonical_path(right_path)
            if first == "<invalid-path>" or second == "<invalid-path>":
                return True
            if _is_ancestor_path(first, second) or _is_ancestor_path(second, first):
                return True
    return False


def _overlapping_values(left: Iterable[Any], right: Iterable[Any]) -> bool:
    first_values = tuple(left)
    second_values = tuple(right)
    if any(not isinstance(value, str) or not value.strip() for value in (*first_values, *second_values)):
        return bool(first_values and second_values)
    first = set(first_values)
    second = set(second_values)
    return bool(first & second)


def _decision_values(item: Any) -> set[str]:
    values: set[str] = set()
    for source in (item, _value(item, "scope", None)):
        for name in ("unresolved_decisions", "unresolved", "decisions", "open_questions"):
            candidate = _value(source, name, ()) if source is not None else ()
            if isinstance(candidate, Mapping):
                candidate = candidate.keys()
            if isinstance(candidate, str):
                candidate = (candidate,)
            try:
                values.update(str(value).casefold() for value in candidate or () if str(value).strip())
            except TypeError:
                values.add(str(candidate).casefold())
    return values


def _priority(item: Any) -> int:
    value = _value(item, "priority", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_integration(item: Any) -> bool:
    explicit = _value(item, "integration", None)
    if explicit is True:
        return True
    text = " ".join(str(_value(item, field, "")) for field in ("id", "title", "plan")).casefold()
    return any(token in text for token in ("integration", "integrate", "интеграц", "объедин"))


def _is_integration_candidate(item: Any, all_items: Iterable[Any]) -> bool:
    """Recognize fan-in only while its unfinished branches are co-present."""
    dependencies = tuple(_value(item, "depends_on", ()) or ())
    item_id = str(_value(item, "id", ""))
    active_ids = {
        str(_value(other, "id", ""))
        for other in all_items
        if str(_value(other, "id", "")) != item_id
        and _value(other, "status", None) not in {"done", "completed"}
    }
    if _is_integration(item):
        return bool(active_ids)
    if len(dependencies) < 2:
        return False
    return bool(active_ids.intersection(str(value) for value in dependencies))


def _dependencies_completed(item: Any, by_id: Mapping[str, Any], evidence: Iterable[str] | None) -> bool:
    """Reject visible unfinished dependencies and absent deps without evidence."""
    verified = {str(value) for value in evidence} if evidence is not None else set()
    for dependency in _value(item, "depends_on", ()) or ():
        dependency_item = by_id.get(str(dependency))
        if dependency_item is None and str(dependency) not in verified:
            return False
        if dependency_item is not None and _value(dependency_item, "status", None) not in {"done", "completed"}:
            return False
    return True


def _is_ancestor_path(parent: str, child: str) -> bool:
    if parent == child:
        return True
    if parent in {"/", "\\"}:
        return child.startswith("/")
    if len(parent) == 3 and parent[1:] == ":/":
        return child.startswith(parent)
    return child.startswith(parent + "/")


def _strongly_connected_components(graph: Mapping[str, tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in graph.get(node, ()):
            if successor not in indices:
                visit(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                successor = stack.pop()
                on_stack.remove(successor)
                component.append(successor)
                if successor == node:
                    break
            components.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return tuple(sorted(components, key=lambda component: component[0]))


def _cycle_witness(component: tuple[str, ...], graph: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    start = min(component)
    members = set(component)

    def search(node: str, path: tuple[str, ...]) -> tuple[str, ...] | None:
        for successor in sorted(graph.get(node, ())):
            if successor not in members:
                continue
            if successor == start:
                return (*path, start)
            if successor not in path:
                found = search(successor, (*path, successor))
                if found is not None:
                    return found
        return None

    return search(start, (start,)) or (start, start)
