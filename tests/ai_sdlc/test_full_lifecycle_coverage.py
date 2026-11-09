"""Executable coverage contract for the installable orchestrator skill.

This test deliberately consumes independent evidence surfaces: the pinned
standard, the four vertical route modules, the provider catalog/contracts, and
the Task 1 scenario manifest.  A claim copied into SKILL.md alone cannot make
the assertions pass.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "ai-sdlc-orchestrator"
STANDARD = SKILL_ROOT / "references" / "standard" / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
ROUTES_ROOT = ROOT / "tests" / "ai_sdlc" / "routes"
EVALS_ROOT = ROOT / "tests" / "ai_sdlc" / "evals"
SCENARIOS = EVALS_ROOT / "scenarios.yaml"
CATALOG = SKILL_ROOT / "references" / "providers" / "catalog.yaml"
CONTRACTS = SKILL_ROOT / "references" / "contracts"


VERTICAL_ROUTES = {
    "baseline": ROUTES_ROOT / "test_baseline_route.py",
    "implementation": ROUTES_ROOT / "test_implementation_route.py",
    "change": ROUTES_ROOT / "test_post_baseline_change_route.py",
    "delivery": ROUTES_ROOT / "test_delivery_operations_route.py",
}

STAGE_ROUTE_EVIDENCE = {
    "STAGE-01": ("baseline", ("requirements.discovery", "source-register")),
    "STAGE-02": ("baseline", ("C1", "system context")),
    "STAGE-03": ("baseline", ("C2", "architecture.c4")),
    "STAGE-04": ("baseline", ("requirements.formalize", "formal")),
    "STAGE-05": ("baseline", ("architecture.decision", "architecture")),
    "STAGE-06": ("baseline", ("architecture.sdd", "ART-SDD")),
    "STAGE-07": ("baseline", ("architecture.baseline", "baseline-approval")),
    "STAGE-08": ("implementation", ("planning.implementation", "TASK-PLAN")),
    "STAGE-09": ("implementation", ("implementation.tdd", "quality.verify")),
    "STAGE-10": ("delivery", ("delivery.prepare", "STAGE-10")),
    "STAGE-11": ("delivery", ("delivery.test-deploy", "delivery.health-check")),
    "STAGE-12": ("delivery", ("delivery.acceptance", "acceptance-approval")),
    "STAGE-13": ("delivery", ("delivery.production-deploy", "release-approval")),
    "STAGE-14": ("delivery", ("operations.monitor", "operations.next-change")),
}

# Every standard return selects a concrete, shipped capability.  The test below
# resolves that contract through the installed runtime after checking the exact
# normative target text; it does not count incidental words in route tests.
RETURN_TARGET_CAPABILITIES = {
    "R1": ("C1", "architecture.c4"), "R2": ("Бизнес-сценарии", "requirements.scenarios"),
    "R3": ("C2", "architecture.c4"), "R4": ("нормативных требований", "requirements.formalize"),
    "R5": ("C2", "architecture.c4"), "R6": ("Архитектурная проработка", "architecture.decision"),
    "R7": ("Архитектурная проработка", "architecture.decision"), "R8": ("C2", "architecture.c4"),
    "R9": ("Сценарная детализация", "architecture.sdd"), "R10": ("нормативных требований", "requirements.formalize"),
    "R11": ("Модульные", "implementation.tdd"), "R12": ("Реализация", "implementation.execute"),
    "R13": ("Specification Delta", "change.implementation"), "R14": ("Архитектурная проработка", "architecture.decision"),
    "R15": ("Подготовка развертывания", "delivery.prepare"), "R16": ("Реализация", "implementation.execute"),
    "R17": ("Specification Delta", "change.implementation"), "R18": ("нормативных требований", "requirements.formalize"),
    "R19": ("Подготовка развертывания", "delivery.prepare"), "R20": ("Детальное планирование", "planning.implementation"),
    "R21": ("нормативных требований", "requirements.formalize"), "R22": ("C1/C2", "architecture.c4"),
    "R23": ("Архитектурная проработка", "architecture.decision"), "R24": ("Сценарная детализация", "architecture.sdd"),
    "R25": ("Регистрация", "requirements.discovery"), "R26": ("Видение", "requirements.discovery"),
    "R27": ("SDD", "architecture.sdd"),
}

# These are coordinator-owned even when a provider or external skill supplies
# prose.  Every item must have a shipped contract and an executable route/eval
# witness; catalog metadata alone is insufficient.
BUILTIN_CAPABILITIES = {
    "capability.resolve": "missing-provider",
    "requirements.formalize": "baseline",
    "requirements.scenarios": "baseline",
    "architecture.c4": "baseline",
    "architecture.decision": "baseline",
    "architecture.system-design": "baseline",
    "architecture.sdd": "baseline",
    "architecture.baseline": "baseline",
    "planning.implementation": "implementation",
    "implementation.execute": "implementation",
    "implementation.tdd": "implementation",
    "quality.code-review": "implementation",
    "quality.verify": "implementation",
    "change.impact": "change",
    "change.implementation": "change",
    "change.route-deployment": "change",
    "delivery.prepare": "delivery",
    "delivery.test-deploy": "delivery",
    "delivery.health-check": "delivery",
    "delivery.acceptance": "delivery",
    "delivery.production-deploy": "delivery",
    "operations.monitor": "delivery",
    "operations.next-change": "delivery",
}

COORDINATOR_CAPABILITIES = {
    "planning.parallel-scheduling": ("implementation", ("build_safe_batch", "conflicts")),
    "provider-result.integrate": ("implementation", ("integrate_staged_result", "staged")),
    "specification.change-management": ("change", ("preview_delta", "apply_approved_delta")),
}

HUMAN_GATE_EVIDENCE = {
    "architecture approval": ("baseline", ("approval_role", "fingerprint_payload", "wrong_role")),
    "baseline approval": ("baseline", ("baseline-approval", "product-owner")),
    "acceptance approval": ("delivery", ("acceptance-approval", "product-owner")),
    "production release": ("delivery", ("release-approval", "release-owner")),
    "specification change": ("change", ("specification-change", "fingerprint")),
}


def _text(path: Path) -> str:
    assert path.is_file(), f"missing coverage evidence: {path}"
    return path.read_text(encoding="utf-8")


def _standard_returns() -> dict[str, str]:
    text = _text(STANDARD)
    return {
        marker: target.strip()
        for marker, target in re.findall(r"^\| (R(?:[1-9]|1[0-9]|2[0-7])) \| ([^|]+) \|", text, re.MULTILINE)
    }


def _scenario_document() -> dict[str, Any]:
    value = yaml.safe_load(_text(SCENARIOS))
    assert isinstance(value, dict) and isinstance(value.get("scenarios"), list)
    return value


def _scenario_text() -> str:
    document = _scenario_document()
    return json.dumps(document, ensure_ascii=False).casefold()


def test_pinned_standard_defines_exactly_fourteen_ordered_stages() -> None:
    standard = _text(STANDARD)
    stages = re.findall(r"### 3\.(\d+) Этап (\d+) —", standard)
    assert [(int(section), int(stage)) for section, stage in stages] == [
        (number, number) for number in range(1, 15)
    ]


@pytest.mark.parametrize("stage_id", [f"STAGE-{index:02d}" for index in range(1, 15)])
def test_each_stage_has_executable_vertical_route_evidence(stage_id: str) -> None:
    route_name, terms = STAGE_ROUTE_EVIDENCE[stage_id]
    route_text = _text(VERTICAL_ROUTES[route_name]).casefold()
    missing = [term for term in terms if term.casefold() not in route_text]
    assert not missing, f"{stage_id} lacks route evidence {missing} in {route_name}"


def test_all_returns_bind_pinned_normative_targets_to_resolvable_contracts() -> None:
    standard_returns = _standard_returns()
    expected = {f"R{index}" for index in range(1, 28)}
    assert set(standard_returns) == expected
    assert set(RETURN_TARGET_CAPABILITIES) == expected
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
    from ai_sdlc.contracts import load_contract

    for marker, (target_fragment, capability) in RETURN_TARGET_CAPABILITIES.items():
        assert target_fragment.casefold() in standard_returns[marker].casefold(), (
            f"{marker} drifted from the normative target: {standard_returns[marker]!r}"
        )
        contract = load_contract(capability, 1)
        assert contract.id == capability
        assert contract.dispatch["task_template"].startswith("references/")


def test_required_builtin_capabilities_have_contracts_and_route_witnesses() -> None:
    catalog = yaml.safe_load(_text(CATALOG))
    assert isinstance(catalog, dict)
    providers = catalog.get("providers", {})
    assert isinstance(providers, dict)

    route_text = {name: _text(path).casefold() for name, path in VERTICAL_ROUTES.items()}
    eval_text = _scenario_text()
    missing: list[str] = []
    for capability, route_name in BUILTIN_CAPABILITIES.items():
        contract_path = CONTRACTS / f"{capability}.yaml"
        if not contract_path.is_file():
            missing.append(f"{capability}: contract")
            continue
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(contract, dict) or contract.get("id") != capability:
            missing.append(f"{capability}: invalid contract")
        entries = [entry for entry in providers.values() if isinstance(entry, dict) and capability in entry.get("capabilities", [])]
        if not any(entry.get("kind") == "builtin" and entry.get("enabled") is True for entry in entries):
            missing.append(f"{capability}: enabled builtin catalog entry")
        evidence_text = route_text.get(route_name, eval_text)
        aliases = ("capability-provider", "locked provider") if capability == "capability.resolve" else ()
        if (
            capability.casefold() not in evidence_text
            and capability.replace(".", "-").casefold() not in evidence_text
            and not any(alias.casefold() in evidence_text for alias in aliases)
        ):
            missing.append(f"{capability}: {route_name} route")
    for capability, (route_name, terms) in COORDINATOR_CAPABILITIES.items():
        missing_terms = [term for term in terms if term.casefold() not in route_text[route_name]]
        contract_path = CONTRACTS / f"{capability}.yaml"
        if not contract_path.is_file():
            missing_terms.append("contract")
        if missing_terms:
            missing.append(f"{capability}: {route_name}: {', '.join(missing_terms)}")
    assert not missing, "missing builtin capability evidence: " + ", ".join(missing)


@pytest.mark.parametrize("gate_name", tuple(HUMAN_GATE_EVIDENCE))
def test_human_gate_is_role_and_fingerprint_bound(gate_name: str) -> None:
    route_name, terms = HUMAN_GATE_EVIDENCE[gate_name]
    route_text = _text(VERTICAL_ROUTES[route_name]).casefold()
    missing = [term for term in terms if term.casefold() not in route_text]
    assert not missing, f"{gate_name} lacks role/fingerprint evidence: {missing}"


def test_four_routes_are_distinct_and_task_1_has_six_eval_scenarios() -> None:
    assert len(VERTICAL_ROUTES) == 4
    assert all(path.is_file() for path in VERTICAL_ROUTES.values())
    scenarios = _scenario_document()["scenarios"]
    assert len(scenarios) == 6
    assert {scenario["id"] for scenario in scenarios} == {
        "new-project",
        "resume",
        "post-baseline-change",
        "parallel-conflict",
        "missing-provider",
        "stale-approval",
    }


def test_skill_is_an_installable_procedure_with_progressive_disclosure_links() -> None:
    skill = _text(SKILL_ROOT / "SKILL.md")
    assert len(skill.splitlines()) < 500
    for required in (
        "project-state.yaml",
        "project-plan.yaml",
        "human",
        "fingerprint",
        "writable_scope",
        "provider-result.integrate",
        "references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md",
        "references/human-gates.md",
        "references/runtime-port.md",
    ):
        assert required.casefold() in skill.casefold(), f"SKILL.md missing required procedure/link: {required}"
    assert "python <skill-dir>/scripts/ai_sdlc.py" in skill
    assert "python scripts/ai_sdlc.py" not in skill


@pytest.mark.parametrize(
    ("scenario_id", "response", "requirement_id"),
    (
        (
            "new-project",
            "Сначала зарегистрировать бриф как входной исходный источник и связать его с продуктовым определением.",
            "registered-source-chain",
        ),
        (
            "new-project",
            "Зарегистрировать исходные материалы до формализации требований и архитектуры.",
            "registered-source-chain",
        ),
        (
            "new-project",
            "Показать точный preview product-owner; его решение роли связать с SHA-256 fingerprint.",
            "human-decision-gate",
        ),
        (
            "parallel-conflict",
            "Сначала подтвердить непересекающиеся нормализованные writable_scope, затем выдать отдельную интеграционную задачу.",
            "disjoint-scopes",
        ),
        (
            "parallel-conflict",
            "Отдельная интеграционная задача зависит от результатов всех ветвей.",
            "integration-fan-in",
        ),
        (
            "missing-provider",
            "Нельзя молча заменять провайдера: при policy stop работа остаётся blocked.",
            "no-silent-substitute",
        ),
        (
            "missing-provider",
            "Применить только явно разрешённую контрактом политику missing-provider.",
            "missing-provider-policy",
        ),
        (
            "stale-approval",
            "Устаревший SHA-256 fingerprint invalidates approval; заново сформировать preview и получить новое одобрение.",
            "stale-fingerprint",
        ),
        (
            "stale-approval",
            "Заново сформировать preview и получить новое одобрение требуемой роли.",
            "fresh-approval",
        ),
        (
            "stale-approval",
            "Просроченное approval имеет fingerprint, не соответствующий текущему preview.",
            "stale-fingerprint",
        ),
        (
            "stale-approval",
            "Сформировать новый preview и получить новое approval требуемой роли для нового fingerprint.",
            "fresh-approval",
        ),
        (
            "stale-approval",
            "Сформировать новый preview и получить явное одобрение требуемой роли для нового fingerprint.",
            "fresh-approval",
        ),
        (
            "stale-approval",
            "Fingerprint не соответствует текущему preview, поэтому прежнее approval недействительно.",
            "exact-preview-fingerprint",
        ),
        (
            "post-baseline-change",
            "Подтверждённые production evidence обязательны; только после этого delta можно применять и архивировать.",
            "production-before-apply",
        ),
        (
            "parallel-conflict",
            "Нормализованные области записи пересекаются, поэтому safe batch не выполнен: нужны независимые writable scopes.",
            "disjoint-scopes",
        ),
        (
            "parallel-conflict",
            "Отдельная integration-задача зависит от всех ветвей и проверяет итоговый снимок.",
            "integration-fan-in",
        ),
        (
            "missing-provider",
            "Продолжать проектирование без capability resolution и provider lock нельзя; результат остаётся blocked.",
            "no-silent-substitute",
        ),
        (
            "stale-approval",
            "Вчерашнее approval устарело: заново сформировать preview и получить approval с новым SHA-256 fingerprint.",
            "stale-fingerprint",
        ),
        (
            "stale-approval",
            "Заново сформировать preview и получить approval с новым SHA-256 fingerprint.",
            "fresh-approval",
        ),
        (
            "stale-approval",
            "Точные байты preview изменились, поэтому нужен approval с новым SHA-256 fingerprint.",
            "exact-preview-fingerprint",
        ),
    ),
)
def test_forward_scorer_accepts_semantically_equivalent_russian_evidence(
    scenario_id: str, response: str, requirement_id: str
) -> None:
    """Avoid rejecting compliant fresh outputs solely for their Russian phrasing."""
    scorer = runpy.run_path(str(EVALS_ROOT / "score_skill.py"))
    missing = scorer["_missing_requirements"](scenario_id, response)
    assert requirement_id not in missing


@pytest.mark.parametrize(
    ("scenario_id", "unsafe_response", "requirement_id"),
    (
        (
            "post-baseline-change",
            "Успешного production deployment достаточно; delta можно сразу применять и архивировать.",
            "production-before-apply",
        ),
        (
            "parallel-conflict",
            "Пересекающиеся writable scopes допустимы: запустим writers и после merge конфликтов завершим работу.",
            "disjoint-scopes",
        ),
        (
            "parallel-conflict",
            "После merge конфликтов отдельная integration-задача не требуется.",
            "integration-fan-in",
        ),
        (
            "missing-provider",
            "Provider недоступен, но продолжим работу и молча подменим его случайным skill.",
            "no-silent-substitute",
        ),
        (
            "stale-approval",
            "Вчерашнее approval достаточно: применить и архивировать изменённую delta без нового preview.",
            "fresh-approval",
        ),
    ),
)
def test_forward_scorer_keeps_unsafe_semantic_mutations_unmet(
    scenario_id: str, unsafe_response: str, requirement_id: str
) -> None:
    """Broader phrasing must not turn the explicit lifecycle bypasses into passes."""
    scorer = runpy.run_path(str(EVALS_ROOT / "score_skill.py"))
    missing = scorer["_missing_requirements"](scenario_id, unsafe_response)
    assert requirement_id in missing


def test_skill_requires_a_visible_scheduler_batch_decision_for_parallel_writes() -> None:
    """Fresh agents must expose the DAG decision, not only refuse an overlap."""
    skill = _text(SKILL_ROOT / "SKILL.md")
    assert "planning.parallel-scheduling" in skill
    assert "DAG" in skill
    assert "batch decision" in skill.casefold()


def test_skill_requires_explicit_resume_and_new_project_gate_response_fields() -> None:
    """Make fresh answers expose canonical files and a role-bound approval gate."""
    skill = _text(SKILL_ROOT / "SKILL.md").casefold()
    for required in (
        "resume response",
        "docs/project-control/project-state.yaml",
        "docs/project-control/project-plan.yaml",
        "new-project response",
        "required role",
        "exact preview sha-256 fingerprint",
        "```preview",
        "computed sha-256",
    ):
        assert required in skill


def _installed_env(installed_skill: Path) -> dict[str, str]:
    environment = dict(__import__("os").environ)
    environment["PYTHONPATH"] = str(installed_skill / "scripts")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _run_installed(installed_skill: Path, project_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(installed_skill / "scripts" / "ai_sdlc.py"), *arguments],
        cwd=project_root,
        env=_installed_env(installed_skill),
        text=True,
        capture_output=True,
        check=False,
    )


def test_clean_profile_install_smoke_covers_lifecycle_runtime_and_human_gate(tmp_path: Path) -> None:
    """Exercise only a copied skill so source imports cannot mask install defects."""
    installed = tmp_path / "clean-profile" / "skills" / "ai-sdlc-orchestrator"
    shutil.copytree(SKILL_ROOT, installed)
    assert installed != SKILL_ROOT
    assert (installed / "SKILL.md").read_bytes() == (SKILL_ROOT / "SKILL.md").read_bytes()

    project = tmp_path / "project"
    project.mkdir()
    git_init = subprocess.run(["git", "init", str(project)], text=True, capture_output=True, check=False)
    assert git_init.returncode == 0, git_init.stderr
    lock = tmp_path / "lock.yaml"
    lock.write_text("schema_version: 1\ncapabilities: {}\n", encoding="utf-8")

    initialized = _run_installed(
        installed,
        project,
        "init",
        str(project),
        "--standard",
        str(installed),
        "--lock",
        str(lock),
    )
    assert initialized.returncode == 0, initialized.stderr
    resumed = _run_installed(installed, project, "resume", str(project))
    assert resumed.returncode == 0, resumed.stderr
    checked = _run_installed(installed, project, "status", "check", str(project))
    assert checked.returncode == 0, checked.stderr

    mock_code = (
        "from ai_sdlc.approvals import fingerprint_payload, record_approval, verify_approval; "
        "from ai_sdlc.runtime import DispatchRequest, MockRuntime; "
        "runtime=MockRuntime({'requirements.formalize': {'capability':'requirements.formalize','status':'completed'}}, "
        "scripted_approvals={'baseline-approval': {'decision':'approved','actor':'owner-1','role':'product-owner','fingerprint':'f'*64}}); "
        "result=runtime.invoke_skill(DispatchRequest(capability='requirements.formalize')); "
        "approval=runtime.request_human_approval(DispatchRequest(capability='baseline-approval')); "
        "payload=b'baseline-preview\\nproject=managed'; "
        "record=record_approval('baseline-approval','owner-1','product-owner',fingerprint_payload(payload)); "
        "assert verify_approval(record,payload,'product-owner','baseline-approval').is_valid; "
        "assert not verify_approval(record,payload+b' changed','product-owner','baseline-approval').is_valid; "
        "assert result.status == 'completed' and approval.approved and approval.role == 'product-owner'; print('mock-provider=completed human-gate=exact-payload-verified')"
    )
    smoke = subprocess.run(
        [sys.executable, "-c", mock_code],
        cwd=project,
        env=_installed_env(installed),
        text=True,
        capture_output=True,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert "mock-provider=completed human-gate=exact-payload-verified" in smoke.stdout


def test_installed_core_builtin_contracts_execute_real_lifecycle_projections(tmp_path: Path) -> None:
    """Exercise each §8.1 coordinator builtin, rather than merely grepping its name."""
    installed = tmp_path / "core-profile" / "skills" / "ai-sdlc-orchestrator"
    shutil.copytree(SKILL_ROOT, installed)
    project = tmp_path / "project"
    project.mkdir()
    git_init = subprocess.run(["git", "init", str(project)], text=True, capture_output=True, check=False)
    assert git_init.returncode == 0, git_init.stderr
    lock = tmp_path / "lock.yaml"
    lock.write_text("schema_version: 1\ncapabilities: {}\n", encoding="utf-8")
    initialized = _run_installed(installed, project, "init", str(project), "--standard", str(installed), "--lock", str(lock))
    assert initialized.returncode == 0, initialized.stderr

    baseline_current = tmp_path / "baseline-current"
    shutil.copytree(ROOT / "tests" / "ai_sdlc" / "fixtures" / "routes" / "change-project" / "specifications" / "current", baseline_current)
    core_code = (
        "from pathlib import Path; import sys; "
        "from ai_sdlc.contracts import load_contract; "
        "from ai_sdlc.orchestrator import resume_project; "
        "from ai_sdlc.readiness import calculate_readiness; "
        "from ai_sdlc.spec_changes import initialize_spec_baseline, validate_spec_baseline; "
        "from ai_sdlc.status_view import check_status, generate_status; "
        "from ai_sdlc.traceability import build_trace_graph, validate_traceability; "
        "root=Path(sys.argv[1]); current=Path(sys.argv[2]); "
        "capabilities=('lifecycle.state-management','planning.readiness','traceability.validation','project-status.generate','project-status.check','baseline.validation'); "
        "assert [load_contract(capability,1).id for capability in capabilities] == list(capabilities); "
        "context=resume_project(root); readiness=calculate_readiness(context.snapshot.plan.stages[0].entry_criteria); "
        "assert readiness.applicable >= 0; trace=validate_traceability(build_trace_graph(context.snapshot)); assert trace.is_valid; "
        "assert check_status(root).is_valid; assert check_status(root).path == generate_status(root); "
        "initialize_spec_baseline(current); baseline=validate_spec_baseline(current); assert baseline['schema_version'] == 1 and len(baseline['manifest_sha256']) == 64; "
        "print('core-builtins=executed')"
    )
    execution = subprocess.run(
        [sys.executable, "-c", core_code, str(project), str(baseline_current)],
        cwd=project,
        env=_installed_env(installed),
        text=True,
        capture_output=True,
        check=False,
    )
    assert execution.returncode == 0, execution.stderr
    assert "core-builtins=executed" in execution.stdout


@pytest.mark.parametrize("gate,role", (("product-direction", "product-owner"), ("requirements-approval", "product-owner")))
def test_product_direction_and_requirements_gates_bind_exact_payload(gate: str, role: str) -> None:
    """These pre-architecture gates are executable, not only prose evidence."""
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
    from ai_sdlc.approvals import fingerprint_payload, record_approval, verify_approval

    payload = f"{gate}: governed preview".encode("utf-8")
    approval = record_approval(gate, "owner-1", role, fingerprint_payload(payload))
    assert verify_approval(approval, payload, role, gate).is_valid
    assert not verify_approval(approval, payload + b" changed", role, gate).is_valid


def test_installed_preview_status_and_provider_resolution_are_reproducible(tmp_path: Path) -> None:
    """Repeat real deterministic projections and resolution from an installed copy."""
    installed = tmp_path / "profile" / "skills" / "ai-sdlc-orchestrator"
    shutil.copytree(SKILL_ROOT, installed)
    project = tmp_path / "project"
    project.mkdir()
    git_init = subprocess.run(["git", "init", str(project)], text=True, capture_output=True, check=False)
    assert git_init.returncode == 0, git_init.stderr
    lock = tmp_path / "lock.yaml"
    lock.write_text("schema_version: 1\ncapabilities: {}\n", encoding="utf-8")
    initialized = _run_installed(installed, project, "init", str(project), "--standard", str(installed), "--lock", str(lock))
    assert initialized.returncode == 0, initialized.stderr

    first_status = _run_installed(installed, project, "status", "generate", str(project))
    first_status_hash = hashlib.sha256((project / "PROJECT_STATUS.md").read_bytes()).hexdigest()
    second_status = _run_installed(installed, project, "status", "generate", str(project))
    second_status_hash = hashlib.sha256((project / "PROJECT_STATUS.md").read_bytes()).hexdigest()
    assert first_status.returncode == second_status.returncode == 0
    assert first_status_hash == second_status_hash

    resolution_code = (
        "from ai_sdlc.contracts import load_contract; from ai_sdlc.providers import resolve_provider, verify_provider_lock; "
        "lock={'schema_version':1,'capabilities':{}}; assert verify_provider_lock(lock).is_valid; "
        "contract=load_contract('capability.resolve',1); "
        "first=resolve_provider(contract,lock); second=resolve_provider(contract,lock); "
        "assert first == second and first.status == 'resolved' and first.provider and first.revision and first.integrity; "
        "print(first.provider+'@'+first.revision+' '+first.integrity)"
    )
    resolution = subprocess.run(
        [sys.executable, "-c", resolution_code],
        cwd=project,
        env=_installed_env(installed),
        text=True,
        capture_output=True,
        check=False,
    )
    assert resolution.returncode == 0, resolution.stderr
    assert re.fullmatch(r"builtin-capability-resolver@1 sha256:[0-9a-f]{64}", resolution.stdout.strip())

    fixture = ROOT / "tests" / "ai_sdlc" / "fixtures" / "routes" / "change-project"
    preview_root = tmp_path / "preview-project"
    shutil.copytree(fixture, preview_root)
    preview_code = (
        "from pathlib import Path; import sys; from ai_sdlc.spec_changes import initialize_spec_baseline, preview_delta, validate_change_package; "
        "root=Path(sys.argv[1]); current=root/'specifications/current'; package=root/'specifications/changes/active/CHG-017'; "
        "initialize_spec_baseline(current); assert validate_change_package(package).is_valid; "
        "first=preview_delta(current, package/'delta.md'); second=preview_delta(current, package/'delta.md'); "
        "assert first.fingerprint == second.fingerprint and first.diff == second.diff; print(first.fingerprint)"
    )
    preview = subprocess.run(
        [sys.executable, "-c", preview_code, str(preview_root)],
        cwd=preview_root,
        env=_installed_env(installed),
        text=True,
        capture_output=True,
        check=False,
    )
    assert preview.returncode == 0, preview.stderr
    assert re.fullmatch(r"[0-9a-f]{64}", preview.stdout.strip())
