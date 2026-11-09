"""Readiness calculation contract tests."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "skills" / "ai-sdlc-orchestrator" / "scripts"))

from ai_sdlc.errors import ValidationError
from ai_sdlc.model import Criterion
from ai_sdlc.readiness import calculate_readiness


def test_readiness_counts_only_verified_mandatory_applicable_criteria() -> None:
    criteria = (
        Criterion("C-01", "verified", "verified"),
        Criterion("C-02", "pending", "pending"),
        Criterion("C-03", "review", "in_review"),
        Criterion("C-04", "excluded", "not_applicable", reason="Not required for this stage"),
        Criterion("C-05", "optional", "pending", mandatory=False),
    )

    readiness = calculate_readiness(criteria)

    assert readiness.verified == 1
    assert readiness.applicable == 3
    assert readiness.not_applicable == 1
    assert readiness.percentage == pytest.approx(33.3333333333)


def test_in_review_does_not_increase_readiness() -> None:
    criteria = (
        Criterion("C-01", "verified", "verified"),
        Criterion("C-02", "review", "in_review"),
    )

    assert calculate_readiness(criteria).percentage == pytest.approx(50)


def test_not_applicable_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="reason"):
        calculate_readiness((Criterion("C-01", "excluded", "not_applicable"),))


def test_empty_criteria_has_no_readiness_percentage() -> None:
    assert calculate_readiness(()).percentage is None


def test_all_mandatory_not_applicable_criteria_have_undefined_readiness() -> None:
    criteria = (
        Criterion("C-01", "excluded one", "not_applicable", reason="Not in scope"),
        Criterion("C-02", "excluded two", "not_applicable", reason="Not in scope"),
    )

    readiness = calculate_readiness(criteria)

    assert readiness.applicable == 0
    assert readiness.not_applicable == 2
    assert readiness.percentage is None
