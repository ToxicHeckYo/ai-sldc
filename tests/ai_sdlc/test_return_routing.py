from __future__ import annotations

import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).parents[2] / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.errors import ValidationError  # noqa: E402
from ai_sdlc.return_routing import RETURN_ROUTES, apply_return, route_return  # noqa: E402


EXPECTED = {
    "R1": ("7 — C1", "architecture.c4"), "R2": ("5 — Бизнес-сценарии и критерии приёмки", "requirements.scenarios"),
    "R3": ("8 — C2", "architecture.c4"), "R4": ("10 — Уточнение нормативных требований", "requirements.formalize"),
    "R5": ("8 — C2", "architecture.c4"), "R6": ("12 — Архитектурная проработка", "architecture.decision"),
    "R7": ("12 — Архитектурная проработка", "architecture.decision"), "R8": ("8 — C2", "architecture.c4"),
    "R9": ("13 — Сценарная детализация", "architecture.sdd"), "R10": ("10 — Уточнение нормативных требований", "requirements.formalize"),
    "R11": ("17 — Модульные и компонентные тесты", "implementation.tdd"), "R12": ("18 — Реализация", "implementation.execute"),
    "R13": ("34 — Specification Delta", "change.implementation"), "R14": ("12 — Архитектурная проработка", "architecture.decision"),
    "R15": ("20 — Подготовка развертывания", "delivery.prepare"), "R16": ("18 — Реализация", "implementation.execute"),
    "R17": ("34 — Specification Delta", "change.implementation"), "R18": ("10 — Уточнение нормативных требований", "requirements.formalize"),
    "R19": ("20 — Подготовка развертывания", "delivery.prepare"), "R20": ("15 — Детальное планирование реализации", "planning.implementation"),
    "R21": ("10 — Уточнение нормативных требований", "requirements.formalize"), "R22": ("7 или 8 — C1/C2", "architecture.c4"),
    "R23": ("12 — Архитектурная проработка", "architecture.decision"), "R24": ("13 — Сценарная детализация", "architecture.sdd"),
    "R25": ("1 — Регистрация и проверка источников", "requirements.discovery"), "R26": ("2, 3 или 4 — Видение, предметный анализ или требования", "requirements.discovery"),
    "R27": ("13 — SDD-спецификация", "architecture.sdd"),
}


@pytest.mark.parametrize("marker", tuple(EXPECTED))
def test_every_standard_return_routes_through_production_api(marker: str) -> None:
    source = {"current_step": "18", "status": "active", "capability": "implementation.execute"}
    reason = f"defect evidence for {marker}"
    transition = route_return(source, marker, reason)
    expected_step, expected_capability = EXPECTED[marker]
    assert transition.marker == marker
    assert transition.normative_step == expected_step
    assert transition.capability == expected_capability
    assert transition.status == "blocked"
    assert transition.reason == reason
    assert transition.promoted is False
    applied = apply_return(source, marker, reason)
    assert applied["current_step"] == expected_step
    assert applied["capability"] == expected_capability
    assert applied["status"] == "blocked"
    assert applied["reason"] == reason
    assert applied["promotion_status"] == "not_promoted"


def test_return_routing_fails_closed_for_unknown_marker_and_invalid_source() -> None:
    with pytest.raises(ValidationError, match="marker"):
        route_return({"current_step": "18", "status": "active"}, "R28", "reason")
    with pytest.raises(ValidationError, match="reason"):
        route_return({"current_step": "18", "status": "active"}, "R1", "")
    with pytest.raises(ValidationError, match="source"):
        route_return({"status": "active"}, "R1", "reason")
    assert set(RETURN_ROUTES) == {f"R{i}" for i in range(1, 28)}
