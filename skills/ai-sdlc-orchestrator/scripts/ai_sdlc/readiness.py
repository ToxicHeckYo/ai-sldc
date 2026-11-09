"""Deterministic readiness calculations for lifecycle criteria."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import ValidationError
from .model import Criterion


@dataclass(frozen=True, slots=True)
class Readiness:
    """A calculated readiness ratio and its auditable criterion counts."""

    verified: int
    applicable: int
    not_applicable: int
    percentage: float | None

    @property
    def percent(self) -> float | None:
        """Compatibility spelling for callers that use ``percent``."""
        return self.percentage

    @property
    def verified_count(self) -> int:
        return self.verified

    @property
    def applicable_count(self) -> int:
        return self.applicable

    @property
    def not_applicable_count(self) -> int:
        return self.not_applicable

    @property
    def excluded(self) -> int:
        return self.not_applicable

    def display(self) -> str:
        return "—" if self.percentage is None else f"{self.percentage:g} %"


def calculate_readiness(criteria: Iterable[Criterion]) -> Readiness:
    """Calculate verified mandatory criteria over mandatory applicable criteria.

    ``in_review`` and ``pending`` remain in the denominator but do not count as
    verified. A ``not_applicable`` criterion is removed from the denominator
    only when it carries a non-empty reason. An empty criterion set represents
    an undetailed stage and is rendered as an em dash rather than a percentage.
    """
    verified = 0
    applicable = 0
    not_applicable = 0
    for criterion in criteria:
        if not isinstance(criterion, Criterion):
            raise ValidationError("readiness criteria must be Criterion records")
        if criterion.status == "not_applicable":
            if not isinstance(criterion.reason, str) or not criterion.reason.strip():
                raise ValidationError(f"criterion {criterion.id!r} not_applicable status requires a reason")
            if criterion.mandatory:
                not_applicable += 1
            continue
        if criterion.status not in {"pending", "in_review", "verified"}:
            raise ValidationError(f"criterion {criterion.id!r} has unsupported status {criterion.status!r}")
        if not criterion.mandatory:
            continue
        applicable += 1
        if criterion.status == "verified":
            verified += 1
    percentage = None if applicable == 0 else verified / applicable * 100
    return Readiness(verified, applicable, not_applicable, percentage)
