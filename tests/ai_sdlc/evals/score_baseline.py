"""Deterministic scorer for the pre-skill baseline transcripts.

The scenario manifest is JSON syntax in a ``.yaml`` file.  JSON is a YAML
subset, which keeps this first, standalone evaluation runnable before the
package's YAML dependency is introduced in Task 2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest


@dataclass(frozen=True)
class ScoringRule:
    """A conservative text-only rule for one observable baseline failure."""

    id: str
    description: str
    classification: str
    kind: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class Scenario:
    """One pressure prompt and the restrictions used to score its transcript."""

    id: str
    title: str
    input: str
    pressure: str
    expected_prohibitions: tuple[str, ...]
    transcript: str
    scoring_rules: tuple[ScoringRule, ...]


@dataclass(frozen=True)
class TargetFailure:
    """Affirmative raw-text evidence of prohibited behavior."""

    rule_id: str
    description: str
    evidence: str


@dataclass(frozen=True)
class RegressionControl:
    """Affirmative raw-text evidence of a compliant refusal or safe control."""

    rule_id: str
    description: str
    evidence: str


@dataclass(frozen=True)
class ScenarioScore:
    scenario_id: str
    transcript: Path
    target_failures: tuple[TargetFailure, ...]
    regression_controls: tuple[RegressionControl, ...]


def _as_text(value: Any, field: str, scenario_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"scenario {scenario_id!r} has no non-empty {field}")
    return value


def _as_strings(value: Any, field: str, scenario_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"scenario {scenario_id!r} has no non-empty {field}")
    return tuple(_as_text(item, field, scenario_id) for item in value)


def load_scenarios(path: Path) -> list[Scenario]:
    """Load and validate the reproducible evaluation scenario manifest."""
    document = json.loads(path.read_text(encoding="utf-8"))
    raw_scenarios = document.get("scenarios") if isinstance(document, dict) else None
    if not isinstance(raw_scenarios, list) or len(raw_scenarios) != 6:
        raise ValueError("scenarios manifest must contain exactly six scenarios")

    scenarios: list[Scenario] = []
    seen_ids: set[str] = set()
    for raw in raw_scenarios:
        if not isinstance(raw, dict):
            raise ValueError("each scenario must be an object")
        scenario_id = _as_text(raw.get("id"), "id", "<unknown>")
        if scenario_id in seen_ids:
            raise ValueError(f"duplicate scenario id: {scenario_id}")
        seen_ids.add(scenario_id)

        rules: list[ScoringRule] = []
        raw_rules = raw.get("scoring_rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError(f"scenario {scenario_id!r} has no scoring_rules")
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                raise ValueError(f"scenario {scenario_id!r} has invalid scoring rule")
            kind = _as_text(raw_rule.get("kind"), "kind", scenario_id)
            if kind != "match_any":
                raise ValueError(f"scenario {scenario_id!r} has unsupported rule kind {kind!r}")
            classification = _as_text(raw_rule.get("classification"), "classification", scenario_id)
            if classification not in {"target_failure", "regression_control"}:
                raise ValueError(f"scenario {scenario_id!r} has invalid rule classification")
            rules.append(
                ScoringRule(
                    id=_as_text(raw_rule.get("id"), "rule id", scenario_id),
                    description=_as_text(raw_rule.get("description"), "rule description", scenario_id),
                    classification=classification,
                    kind=kind,
                    terms=_as_strings(raw_rule.get("terms"), "rule terms", scenario_id),
                )
            )

        scenarios.append(
            Scenario(
                id=scenario_id,
                title=_as_text(raw.get("title"), "title", scenario_id),
                input=_as_text(raw.get("input"), "input", scenario_id),
                pressure=_as_text(raw.get("pressure"), "pressure", scenario_id),
                expected_prohibitions=_as_strings(
                    raw.get("expected_prohibitions"), "expected_prohibitions", scenario_id
                ),
                transcript=_as_text(raw.get("transcript"), "transcript", scenario_id),
                scoring_rules=tuple(rules),
            )
        )
    return scenarios


def _matched_evidence(rule: ScoringRule, text: str) -> str | None:
    normalized = text.casefold()
    matched = tuple(term for term in rule.terms if term.casefold() in normalized)
    return f"matched: {', '.join(matched)}" if matched else None


def _resolve_transcript_path(baseline_dir: Path, declared_path: str) -> Path:
    """Resolve only a manifest path genuinely contained by ``baseline_dir``."""
    path = Path(declared_path)
    root = baseline_dir.resolve()
    if path.is_absolute() or not path.parts or path.parts[0] != "baseline":
        raise ValueError(f"transcript path must be declared under baseline/: {declared_path}")
    transcript = (root.parent / path).resolve()
    if not transcript.is_relative_to(root):
        raise ValueError(f"transcript path must resolve under baseline/: {declared_path}")
    return transcript


def score_baseline(scenarios: list[Scenario], baseline_dir: Path) -> list[ScenarioScore]:
    """Return text-supported target failures for every supplied transcript."""
    scores: list[ScenarioScore] = []
    for scenario in scenarios:
        transcript = _resolve_transcript_path(baseline_dir, scenario.transcript)
        if not transcript.is_file():
            raise FileNotFoundError(f"missing baseline transcript for {scenario.id}: {transcript}")
        text = transcript.read_text(encoding="utf-8")
        observations = tuple(
            (rule, evidence)
            for rule in scenario.scoring_rules
            if (evidence := _matched_evidence(rule, text)) is not None
        )
        failures = tuple(
            TargetFailure(rule.id, rule.description, evidence)
            for rule, evidence in observations
            if rule.classification == "target_failure"
        )
        controls = tuple(
            RegressionControl(rule.id, rule.description, evidence)
            for rule, evidence in observations
            if rule.classification == "regression_control"
        )
        scores.append(ScenarioScore(scenario.id, transcript, failures, controls))
    return scores


def test_baseline_exhibits_target_failures() -> None:
    """Only affirmative unsafe behavior is a failure; refusals are controls."""
    scenarios = load_scenarios(Path(__file__).with_name("scenarios.yaml"))
    baseline_dir = Path(__file__).with_name("baseline")

    scores = score_baseline(scenarios, baseline_dir)
    unmatched_rule = replace(scenarios[0].scoring_rules[0], terms=("unmatched evidence",))
    inconclusive = score_baseline(
        [replace(scenarios[0], scoring_rules=(unmatched_rule,))], baseline_dir
    )[0]

    assert {score.scenario_id for score in scores} == {scenario.id for scenario in scenarios}
    assert all(not hasattr(scenario, "expected_outcome") for scenario in scenarios)
    assert not inconclusive.target_failures and not inconclusive.regression_controls
    assert any(score.target_failures for score in scores), "the suite needs one observed target failure"
    controls = [control for score in scores for control in score.regression_controls]
    assert controls and all(control.evidence for control in controls), "refusal controls need affirmative evidence"


def test_baseline_transcript_paths_must_stay_under_baseline() -> None:
    scenarios = load_scenarios(Path(__file__).with_name("scenarios.yaml"))
    unsafe_scenario = replace(scenarios[0], transcript="../baseline/new-project.md")

    with pytest.raises(ValueError, match="under baseline"):
        score_baseline([unsafe_scenario], Path(__file__).with_name("baseline"))
