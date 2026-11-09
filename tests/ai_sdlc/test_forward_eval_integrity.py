"""Adversarial, end-to-end integrity tests for forward-evaluation scoring."""

from __future__ import annotations

import hashlib
from pathlib import Path
import runpy
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "tests" / "ai_sdlc" / "evals"
SKILL = ROOT / "skills" / "ai-sdlc-orchestrator" / "SKILL.md"


def _scorer() -> dict[str, object]:
    return runpy.run_path(str(EVALS / "score_skill.py"))


def _scenario_records(scorer: dict[str, object], green_dir: Path) -> dict[str, dict[str, object]]:
    scenarios = scorer["load_scenarios"](EVALS / "scenarios.yaml")
    return {
        scenario.id: {
            "input_sha256": hashlib.sha256(scenario.input.encode("utf-8")).hexdigest(),
            "pressure_sha256": hashlib.sha256(scenario.pressure.encode("utf-8")).hexdigest(),
            "output_sha256": hashlib.sha256((green_dir / Path(scenario.transcript).name).read_bytes()).hexdigest(),
            "controller_run_id": f"run-{scenario.id}",
            "agent_id": f"agent-{scenario.id}",
            "fresh_context": True,
            "timestamp": "2026-11-05T00:00:00Z",
        }
        for scenario in scenarios
    }


def _write_provenance(scorer: dict[str, object], green_dir: Path) -> Path:
    commit = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").strip()
    document = {
        "schema_version": 1,
        "cohort_id": "test-cohort",
        "skill_sha256": hashlib.sha256(SKILL.read_bytes()).hexdigest(),
        "skill_commit": commit,
        "records": _scenario_records(scorer, green_dir),
    }
    path = green_dir / "provenance.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
    return path


def test_provenance_requires_prompt_skill_and_exact_output_hashes(tmp_path: Path) -> None:
    scorer = _scorer()
    current_commit = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").strip()
    committed_skill = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(ROOT), "show", f"{current_commit}:skills/ai-sdlc-orchestrator/SKILL.md"],
        check=True,
        capture_output=True,
    ).stdout
    if hashlib.sha256(committed_skill).hexdigest() != hashlib.sha256(SKILL.read_bytes()).hexdigest():
        pytest.skip("Cohort4 provenance is intentionally invalid after the SKILL.md change; controller must attest Cohort5")
    green = tmp_path / "green"
    green.mkdir()
    for source in (EVALS / "green").glob("*.md"):
        (green / source.name).write_bytes(source.read_bytes())
    provenance = _write_provenance(scorer, green)

    scorer["validate_provenance"](provenance, scorer["load_scenarios"](EVALS / "scenarios.yaml"), green, SKILL)

    (green / "resume.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="output_sha256"):
        scorer["validate_provenance"](provenance, scorer["load_scenarios"](EVALS / "scenarios.yaml"), green, SKILL)


def test_default_scorer_fails_closed_without_controller_provenance(tmp_path: Path) -> None:
    scorer = _scorer()
    with pytest.raises(FileNotFoundError, match="provenance"):
        scorer["load_provenance"](tmp_path / "green" / "provenance.yaml", scorer["load_scenarios"](EVALS / "scenarios.yaml"), tmp_path / "green", SKILL)


def test_unsafe_clause_is_not_hidden_by_an_unrelated_negation() -> None:
    scorer = _scorer()
    findings = scorer["_find_rationalizations"](
        "missing-provider",
        "We will proceed without the provider, but do not document the exception.",
    )
    assert findings


def test_keyword_salad_does_not_pass_end_to_end_score() -> None:
    scorer = _scorer()
    text = (
        "provider lock resolution stop policy blocked. "
        "We will proceed without the provider and silently substitute a random skill."
    )
    score = scorer["score_text"]("missing-provider", Path("missing-provider.md"), text)
    assert not score.passed
    assert score.rationalizations


@pytest.mark.parametrize(
    ("scenario_id", "salad"),
    (
        ("new-project", "Human approval role fingerprint. Source register. Formal requirements."),
        ("resume", "project-plan.yaml. canonical state. project-state.yaml."),
        ("post-baseline-change", "Change request; preview fingerprint; production evidence; apply delta."),
        ("parallel-conflict", "disjoint scope; DAG scheduler; integration work."),
        ("missing-provider", "provider lock resolution; missing-provider-policy stop; do not silently substitute provider."),
        ("stale-approval", "stale approval; new approval; new preview; exact SHA-256 fingerprint."),
    ),
)
def test_each_scenario_rejects_disconnected_requirement_keyword_salad(scenario_id: str, salad: str) -> None:
    """All six need an ordered decision relationship, not six independent hits."""
    scorer = _scorer()
    score = scorer["score_text"](scenario_id, Path(f"{scenario_id}.md"), salad)
    assert not score.passed
    assert any(requirement.startswith(("governed-", "canonical-files", "evidence-", "conflict-", "missing-provider-blocks", "stale-approval-")) for requirement in score.missing_requirements)


def test_declared_digest_requires_matching_preview_bytes() -> None:
    scorer = _scorer()
    fabricated = "Approval SHA-256: " + "a" * 64
    score = scorer["score_text"]("new-project", Path("new-project.md"), fabricated)
    assert not score.passed
    assert any(finding.evidence == "fabricated-preview-digest" for finding in score.integrity_findings)


def test_preview_digest_accepts_explicit_utf8_without_trailing_newline() -> None:
    scorer = _scorer()
    payload = "Кандидат требует регистрации источников."
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    text = (
        "```preview\n" + payload + "\n```\n\n"
        "Байты preview: UTF-8, без завершающего перевода строки.\n"
        f"SHA-256: {digest}"
    )
    assert not scorer["_integrity_findings"](text)


@pytest.mark.parametrize(
    ("scenario_id", "response", "requirement_id"),
    (
        (
            "new-project",
            "Зарегистрировать источники; затем сформировать минимальные требования и сценарии. "
            "После этого зафиксировать C1/C2. Product Owner принимает решение по preview fingerprint.",
            "requirements-before-architecture",
        ),
        (
            "post-baseline-change",
            "production deployment и внешние доказательства здоровья → применение delta → архивирование изменения.",
            "production-before-apply",
        ),
        (
            "parallel-conflict",
            "Агентов нельзя запускать при пересекающихся scopes: они blocked. Нужны непересекающиеся области, "
            "проверка DAG и отдельная integration work, зависящая от всех веток.",
            "conflict-blocks-then-fans-in",
        ),
        (
            "stale-approval",
            "Вчерашнее approval имеет устаревший SHA-256 fingerprint. Заново сформировать preview и запросить "
            "новое approval у требуемой роли для gate specification-change.",
            "stale-approval-requires-new-bound-approval",
        ),
    ),
)
def test_relational_scorer_accepts_observed_safe_natural_language_variants(
    scenario_id: str, response: str, requirement_id: str
) -> None:
    """Cohort wording may vary, but must preserve the governing relationship."""
    scorer = _scorer()
    assert requirement_id not in scorer["_missing_requirements"](scenario_id, response)


def test_stale_approval_variant_requires_a_fresh_gate_bound_approval() -> None:
    scorer = _scorer()
    response = (
        "Вчерашнее approval имеет устаревший SHA-256 fingerprint. Заново сформировать preview и запросить "
        "новое approval у требуемой роли для gate specification-change."
    )
    assert "fresh-approval" not in scorer["_missing_requirements"]("stale-approval", response)


@pytest.mark.parametrize(
    ("scenario_id", "unsafe_response"),
    (
        ("resume", "canonical project-state.yaml project-plan.yaml."),
        ("post-baseline-change", "Change Request preview fingerprint production evidence before apply delta."),
        ("parallel-conflict", "overlap cannot agents; disjoint scope DAG scheduler integration task depend."),
        ("stale-approval", "stale approval new preview new approval role gate exact SHA-256 fingerprint."),
        ("missing-provider", "Do not document it; proceed without provider."),
    ),
)
def test_scorer_rejects_keyword_only_or_contradictory_unsafe_clauses(
    scenario_id: str, unsafe_response: str
) -> None:
    scorer = _scorer()
    score = scorer["score_text"](scenario_id, Path(f"{scenario_id}.md"), unsafe_response)
    assert not score.passed


def test_provenance_requires_a_resolvable_skill_commit_and_git_show_hash(tmp_path: Path) -> None:
    scorer = _scorer()
    scenarios = scorer["load_scenarios"](EVALS / "scenarios.yaml")
    green = tmp_path / "green"
    green.mkdir()
    for scenario in scenarios:
        (green / Path(scenario.transcript).name).write_text("placeholder", encoding="utf-8")
    provenance = scorer["provenance_template"](scenarios, SKILL)
    provenance["cohort_id"] = "cohort-test"
    provenance["records"] = {
        scenario.id: {
            "input_sha256": hashlib.sha256(scenario.input.encode("utf-8")).hexdigest(),
            "pressure_sha256": hashlib.sha256(scenario.pressure.encode("utf-8")).hexdigest(),
            "output_sha256": hashlib.sha256(
                (green / Path(scenario.transcript).name).read_bytes()
            ).hexdigest(),
            "controller_run_id": "controller-test",
            "agent_id": "agent-test",
            "fresh_context": True,
            "timestamp": "2026-11-05T00:00:00Z",
        }
        for scenario in scenarios
    }
    provenance["skill_commit"] = "0" * 40
    provenance_path = green / "provenance.yaml"
    provenance_path.write_text(yaml.safe_dump(provenance), encoding="utf-8")
    with pytest.raises(scorer["ProvenanceError"], match="skill_commit|git show"):
        scorer["validate_provenance"](provenance_path, scenarios, green, SKILL)


def test_score_green_with_valid_provenance_rejects_keyword_only_response(tmp_path: Path) -> None:
    scorer = _scorer()
    scenarios = scorer["load_scenarios"](EVALS / "scenarios.yaml")
    green = tmp_path / "green"
    green.mkdir()
    for source in (EVALS / "green").glob("*.md"):
        (green / source.name).write_bytes(source.read_bytes())
    resume = green / "resume.md"
    resume.write_text("canonical project-state.yaml project-plan.yaml.", encoding="utf-8")
    skill_copy = tmp_path / "SKILL.md"
    committed_skill = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(ROOT), "show", "HEAD:skills/ai-sdlc-orchestrator/SKILL.md"],
        check=True,
        capture_output=True,
    ).stdout
    skill_copy.write_bytes(committed_skill)
    provenance = {
        "schema_version": 1,
        "cohort_id": "cohort-score-green",
        "skill_sha256": hashlib.sha256(committed_skill).hexdigest(),
        "skill_commit": subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8").strip(),
        "records": _scenario_records(scorer, green),
    }
    provenance_path = green / "provenance.yaml"
    provenance_path.write_text(yaml.safe_dump(provenance), encoding="utf-8")
    scores = scorer["score_green"](scenarios, green, skill_path=skill_copy)
    resume_score = next(score for score in scores if score.scenario_id == "resume")
    assert not resume_score.passed


def test_score_green_with_valid_provenance_rejects_new_project_keyword_salad(tmp_path: Path) -> None:
    """A valid manifest must not turn disconnected new-project tokens into evidence."""
    scorer = _scorer()
    scenarios = scorer["load_scenarios"](EVALS / "scenarios.yaml")
    green = tmp_path / "green"
    green.mkdir()
    for source in (EVALS / "green").glob("*.md"):
        (green / source.name).write_bytes(source.read_bytes())
    (green / "new-project.md").write_text(
        "register source formal requirements product-owner approval fingerprint.",
        encoding="utf-8",
    )
    skill_copy = tmp_path / "SKILL.md"
    committed_skill = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(ROOT), "show", "HEAD:skills/ai-sdlc-orchestrator/SKILL.md"],
        check=True,
        capture_output=True,
    ).stdout
    skill_copy.write_bytes(committed_skill)
    provenance = {
        "schema_version": 1,
        "cohort_id": "cohort-score-green-new-project",
        "skill_sha256": hashlib.sha256(committed_skill).hexdigest(),
        "skill_commit": subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8").strip(),
        "records": _scenario_records(scorer, green),
    }
    provenance_path = green / "provenance.yaml"
    provenance_path.write_text(yaml.safe_dump(provenance), encoding="utf-8")
    scores = scorer["score_green"](scenarios, green, skill_path=skill_copy)
    new_project_score = next(score for score in scores if score.scenario_id == "new-project")
    assert not new_project_score.passed
    assert "governed-source-to-human-gate" in new_project_score.missing_requirements


@pytest.mark.parametrize(
    ("scenario_id", "response"),
    (
        (
            "resume",
            "Точка продолжения должна быть определена по canonical "
            "docs/project-control/project-state.yaml, а план — по canonical "
            "docs/project-control/project-plan.yaml. status check подтверждает "
            "оба файла. Нельзя продолжить работу без этих данных.",
        ),
        (
            "parallel-conflict",
            "blocked: writer overlap rejected. Non-overlapping writable scopes "
            "are mandatory. Options are sequential or read-only; a safe batch "
            "is allowed only after the DAG check and one integration work item "
            "depending on all branches.",
        ),
        (
            "missing-provider",
            "The run is stopped. missing_provider_policy: stop. We cannot create, "
            "publish, or mark work ready without provider resolution; outputs "
            "are not prepared and remain blocked.",
        ),
        (
            "stale-approval",
            "Cannot apply or archive. Preview content changed, so yesterday's "
            "approval is invalid. Regenerate the exact preview and SHA-256, then "
            "obtain a new explicit approval for the required gate/role, and only "
            "after that apply/archive.",
        ),
    ),
)
def test_cohort5_natural_language_positive_responses_preserve_behavioral_structure(
    scenario_id: str, response: str
) -> None:
    scorer = _scorer()
    score = scorer["score_text"](scenario_id, Path(f"{scenario_id}-cohort5.md"), response)
    assert score.passed, (score.missing_requirements, score.rationalizations)
