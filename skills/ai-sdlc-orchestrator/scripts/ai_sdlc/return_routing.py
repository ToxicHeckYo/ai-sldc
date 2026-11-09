"""Provider-neutral, fail-closed routing for the standard's R1--R27 returns.

The mapping is coordinator-owned data aligned with section 8 of the pinned
standard.  Providers may report a defect, but only this module can resolve a
marker into a target capability and a non-success lifecycle transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class ReturnRoute:
    marker: str
    normative_step: str
    capability: str


@dataclass(frozen=True, slots=True)
class ReturnTransition:
    marker: str
    source_step: Any
    source_status: str
    target_step: str
    capability: str
    status: str
    reason: str
    promoted: bool
    promotion_status: str

    @property
    def normative_step(self) -> str:
        return self.target_step

    @property
    def target_capability(self) -> str:
        return self.capability

    def as_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "source_step": self.source_step,
            "source_status": self.source_status,
            "target_step": self.target_step,
            "normative_step": self.target_step,
            "capability": self.capability,
            "target_capability": self.capability,
            "status": self.status,
            "reason": self.reason,
            "promoted": self.promoted,
            "promotion_status": self.promotion_status,
        }


# Exact target text from the pinned standard's return table, paired with the
# shipped capability that can execute the target step.
RETURN_ROUTES: dict[str, ReturnRoute] = {
    "R1": ReturnRoute("R1", "7 — C1", "architecture.c4"),
    "R2": ReturnRoute("R2", "5 — Бизнес-сценарии и критерии приёмки", "requirements.scenarios"),
    "R3": ReturnRoute("R3", "8 — C2", "architecture.c4"),
    "R4": ReturnRoute("R4", "10 — Уточнение нормативных требований", "requirements.formalize"),
    "R5": ReturnRoute("R5", "8 — C2", "architecture.c4"),
    "R6": ReturnRoute("R6", "12 — Архитектурная проработка", "architecture.decision"),
    "R7": ReturnRoute("R7", "12 — Архитектурная проработка", "architecture.decision"),
    "R8": ReturnRoute("R8", "8 — C2", "architecture.c4"),
    "R9": ReturnRoute("R9", "13 — Сценарная детализация", "architecture.sdd"),
    "R10": ReturnRoute("R10", "10 — Уточнение нормативных требований", "requirements.formalize"),
    "R11": ReturnRoute("R11", "17 — Модульные и компонентные тесты", "implementation.tdd"),
    "R12": ReturnRoute("R12", "18 — Реализация", "implementation.execute"),
    "R13": ReturnRoute("R13", "34 — Specification Delta", "change.implementation"),
    "R14": ReturnRoute("R14", "12 — Архитектурная проработка", "architecture.decision"),
    "R15": ReturnRoute("R15", "20 — Подготовка развертывания", "delivery.prepare"),
    "R16": ReturnRoute("R16", "18 — Реализация", "implementation.execute"),
    "R17": ReturnRoute("R17", "34 — Specification Delta", "change.implementation"),
    "R18": ReturnRoute("R18", "10 — Уточнение нормативных требований", "requirements.formalize"),
    "R19": ReturnRoute("R19", "20 — Подготовка развертывания", "delivery.prepare"),
    "R20": ReturnRoute("R20", "15 — Детальное планирование реализации", "planning.implementation"),
    "R21": ReturnRoute("R21", "10 — Уточнение нормативных требований", "requirements.formalize"),
    "R22": ReturnRoute("R22", "7 или 8 — C1/C2", "architecture.c4"),
    "R23": ReturnRoute("R23", "12 — Архитектурная проработка", "architecture.decision"),
    "R24": ReturnRoute("R24", "13 — Сценарная детализация", "architecture.sdd"),
    "R25": ReturnRoute("R25", "1 — Регистрация и проверка источников", "requirements.discovery"),
    "R26": ReturnRoute("R26", "2, 3 или 4 — Видение, предметный анализ или требования", "requirements.discovery"),
    "R27": ReturnRoute("R27", "13 — SDD-спецификация", "architecture.sdd"),
}


def _source_fields(source: Mapping[str, Any] | Any) -> tuple[Any, str]:
    if isinstance(source, Mapping):
        step = source.get("current_step", source.get("step", source.get("current_stage")))
        status = source.get("status")
    else:
        step = getattr(source, "current_step", getattr(source, "step", getattr(source, "current_stage", None)))
        status = getattr(source, "status", None)
    if step is None or not isinstance(status, str) or not status.strip():
        raise ValidationError("return routing source must declare current step and status")
    return step, status.strip()


def _validate_target(route: ReturnRoute, target: str | None) -> None:
    if not route.normative_step.strip() or "." not in route.capability or not route.capability.strip():
        raise ValidationError(f"return marker {route.marker} has an invalid target")
    if target is not None and target != route.normative_step:
        raise ValidationError(f"return marker {route.marker} target does not match normative target")


def route_return(
    source: Mapping[str, Any] | Any,
    marker: str,
    reason: str,
    *,
    target: str | None = None,
) -> ReturnTransition:
    """Resolve a return marker into a deterministic non-success transition."""
    if not isinstance(marker, str) or marker not in RETURN_ROUTES:
        raise ValidationError(f"unknown return marker: {marker!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("return routing reason must be non-empty")
    source_step, source_status = _source_fields(source)
    route = RETURN_ROUTES[marker]
    _validate_target(route, target)
    return ReturnTransition(
        marker=marker,
        source_step=source_step,
        source_status=source_status,
        target_step=route.normative_step,
        capability=route.capability,
        status="blocked",
        reason=reason.strip(),
        promoted=False,
        promotion_status="not_promoted",
    )


def apply_return(
    source: Mapping[str, Any],
    marker: str,
    reason: str,
    *,
    target: str | None = None,
) -> dict[str, Any]:
    """Apply a validated return to an operational-state mapping copy."""
    transition = route_return(source, marker, reason, target=target)
    if isinstance(source, Mapping):
        result = dict(source)
    elif hasattr(source, "__dict__"):
        result = dict(vars(source))
    else:
        raise ValidationError("return routing source must be a mapping for apply")
    result.update(
        {
            "current_step": transition.target_step,
            "target_step": transition.target_step,
            "capability": transition.capability,
            "status": transition.status,
            "reason": transition.reason,
            "return_marker": transition.marker,
            "promoted": transition.promoted,
            "promotion_status": transition.promotion_status,
        }
    )
    return result


__all__ = ("RETURN_ROUTES", "ReturnRoute", "ReturnTransition", "apply_return", "route_return")
