"""Fixture contract tests for the first real external provider adapters."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "ai-sdlc-orchestrator"
FIXTURES = Path(__file__).parent / "fixtures" / "providers"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.adapters import (  # noqa: E402
    build_dispatch,
    load_provider_adapter,
    normalize_provider_output,
)
from ai_sdlc.errors import ValidationError  # noqa: E402
from ai_sdlc.providers import _load_catalog  # noqa: E402
from ai_sdlc.runtime import ContractResult, ProviderResult  # noqa: E402


BASE_REVISION = "0123456789abcdef0123456789abcdef01234567"
IMPLEMENTATION_CAPABILITIES = {
    "planning.implementation",
    "implementation.tdd",
    "implementation.execute",
    "quality.code-review",
    "quality.verify",
}


def fixture(provider: str, name: str):
    return yaml.safe_load((FIXTURES / provider / name).read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pinned_sources_are_immutable_and_checksum_specific() -> None:
    requirements = load_provider_adapter("requirements-analysis")
    frontend = load_provider_adapter("frontend-standard")

    assert requirements.revision == "e02ec7e226a6e4f8419fd3b88a1d8e472d421b32"
    assert requirements.integrity == "sha256:87a6688bf03f9175ff56c9dd9331250ef814cf5c098ce305c9966210d209e69a"
    assert requirements.source_files[0]["path"] == "skills/tech/development/architecture/requirements-analysis/SKILL.md"
    assert requirements.source_files[0]["url"].endswith("/e02ec7e226a6e4f8419fd3b88a1d8e472d421b32/skills/tech/development/architecture/requirements-analysis/SKILL.md")
    assert frontend.revision == "9e5b44c6f3e33d3c0cd762f3dcb6aa15ce1a4968"
    assert frontend.integrity == "sha256:cc17e83ba02fc8883ccdaca5486fd4dac44b28f42340cf3fce1ab3c81a36f3c8"
    assert {item["sha256"] for item in frontend.source_files} == {
        "cc17e83ba02fc8883ccdaca5486fd4dac44b28f42340cf3fce1ab3c81a36f3c8",
        "0b0b89494d889c4cd91c178c4ebf2d0749bb497687bc8753a5711b5a9c9c6267",
        "1575e1af4a95f12f70b4ee6a6adce8160953d93ea17dc2611b90883ccc3ad3b8",
    }
    assert requirements.evaluated_at == frontend.evaluated_at == "2026-11-03"


def test_all_enabled_implementation_catalog_candidates_are_dispatchable_adapters() -> None:
    candidates = {
        provider_id: entry
        for provider_id, entry in _load_catalog().items()
        if entry.get("enabled") and IMPLEMENTATION_CAPABILITIES.intersection(entry.get("capabilities", ()))
    }

    assert candidates
    for provider_id, entry in candidates.items():
        adapter = load_provider_adapter(provider_id)
        assert adapter.provider_id == provider_id
        assert adapter.capability in IMPLEMENTATION_CAPABILITIES
        assert adapter.revision == entry["revision"]
        assert adapter.integrity == entry["integrity"]


def test_requirements_dispatch_preserves_formal_specification_boundary() -> None:
    request = build_dispatch(
        "requirements-analysis",
        fixture("requirements-analysis", "input.yaml"),
        base_revision=BASE_REVISION,
    )

    assert request.capability == "requirements.discovery"
    assert request.contract_version == 1
    assert request.writable_scope == ("staging/requirements",)
    assert request.inputs["provider"]["id"] == "requirements-analysis"
    assert request.inputs["provider"]["revision"] == "e02ec7e226a6e4f8419fd3b88a1d8e472d421b32"
    assert request.inputs["base_revision"] == BASE_REVISION
    assert request.base_revision == BASE_REVISION
    assert request.inputs["governance"]["output_role"] == "input_to_formal_specification"
    assert request.inputs["governance"]["cannot_substitute"] == (
        "formal_srs",
        "formal_acceptance_criteria",
    )
    assert request.inputs["governance"]["surface"] == ("assumptions", "open_questions")
    assert (SKILL_ROOT / request.task_template).is_file()


@pytest.mark.parametrize(
    ("fixture_name", "expected_status"),
    (("raw-complete.yaml", "completed"), ("raw-partial.yaml", "partial")),
)
def test_requirements_output_normalizes_full_and_partial_results(
    fixture_name: str,
    expected_status: str,
) -> None:
    adapted = normalize_provider_output(
        "requirements-analysis",
        fixture("requirements-analysis", fixture_name),
        base_revision=BASE_REVISION,
    )

    assert isinstance(adapted.provider_result, ProviderResult)
    assert isinstance(adapted.contract_result, ContractResult)
    assert adapted.provider_result.provider == adapted.contract_result.provider == "requirements-analysis"
    assert adapted.provider_result.base_revision == adapted.contract_result.base_revision == BASE_REVISION
    assert adapted.contract_result.status == expected_status
    assert adapted.contract_result.sections["assumptions"]
    assert adapted.contract_result.sections["open_questions"]
    assert adapted.contract_result.evidence["source-register"]
    if expected_status == "partial":
        assert adapted.contract_result.unresolved == ("Who approves the retention period?",)


@pytest.mark.parametrize("provider", ("requirements-analysis", "frontend-standard"))
def test_missing_or_malformed_evidence_fails_closed(provider: str) -> None:
    raw = fixture(provider, "raw-missing-evidence.yaml")

    with pytest.raises(ValidationError, match="evidence"):
        normalize_provider_output(provider, raw, base_revision=BASE_REVISION)

    malformed = fixture(provider, "raw-complete.yaml")
    malformed["evidence"] = ["not", "a", "mapping"]
    with pytest.raises(ValidationError, match="evidence"):
        normalize_provider_output(provider, malformed, base_revision=BASE_REVISION)


@pytest.mark.parametrize("provider", ("requirements-analysis", "frontend-standard"))
def test_raw_result_requires_exact_dispatch_base_revision(provider: str) -> None:
    raw = fixture(provider, "raw-complete.yaml")
    raw["base_revision"] = BASE_REVISION
    without_revision = dict(raw)
    without_revision.pop("base_revision")

    with pytest.raises(ValidationError, match="base_revision"):
        normalize_provider_output(provider, without_revision, base_revision=BASE_REVISION)

    mismatched = dict(raw)
    mismatched["base_revision"] = "f" * 40
    with pytest.raises(ValidationError, match="base_revision"):
        normalize_provider_output(provider, mismatched, base_revision=BASE_REVISION)


def test_frontend_dispatch_is_restricted_to_frontend_staging() -> None:
    request = build_dispatch(
        "frontend-standard",
        fixture("frontend-standard", "input.yaml"),
        base_revision=BASE_REVISION,
    )

    assert request.capability == "frontend.delivery"
    assert request.writable_scope == ("staging/frontend",)
    assert request.inputs["provider"]["id"] == "frontend-standard"
    assert request.inputs["governance"]["protected_paths"] == (
        "docs/project-control",
        "docs/artifact-register.yaml",
        "docs/product-definition/sources/source-register.yaml",
        "PROJECT_STATUS.md",
    )
    assert request.inputs["governance"]["forbidden_actions"] == (
        "lifecycle_transition",
        "project_state_write",
        "control_file_write",
        "push",
        "merge",
        "deploy",
    )
    assert (SKILL_ROOT / request.task_template).is_file()


@pytest.mark.parametrize(
    ("fixture_name", "expected_status"),
    (("raw-complete.yaml", "completed"), ("raw-partial.yaml", "partial")),
)
def test_frontend_output_returns_delivery_and_verification_evidence(
    fixture_name: str,
    expected_status: str,
) -> None:
    adapted = normalize_provider_output(
        "frontend-standard",
        fixture("frontend-standard", fixture_name),
        base_revision=BASE_REVISION,
    )

    assert adapted.contract_result.status == expected_status
    assert adapted.contract_result.provider == "frontend-standard"
    assert adapted.contract_result.base_revision == BASE_REVISION
    assert adapted.contract_result.changed_paths == ("staging/frontend/src/Button.tsx",)
    assert set(adapted.contract_result.evidence) == {"tests", "build", "accessibility", "security"}
    assert adapted.contract_result.evidence["tests"]["exit_code"] == 0
    assert adapted.contract_result.sections["unverified"]


@pytest.mark.parametrize(
    ("evidence_name", "field", "bad_value"),
    (
        ("tests", "exit_code", 1),
        ("build", "exit_code", 2),
        ("accessibility", "status", "partial"),
        ("security", "status", "failed"),
        ("tests", "command", "   "),
        ("build", "output_sha256", "sha256:not-a-digest"),
        ("build", "output_sha256", "sha256:" + "f" * 64),
    ),
)
def test_completed_frontend_result_requires_successful_verification_evidence(
    evidence_name: str,
    field: str,
    bad_value,
) -> None:
    raw = fixture("frontend-standard", "raw-complete.yaml")
    raw["base_revision"] = BASE_REVISION
    for evidence in raw["evidence"].values():
        evidence.setdefault("output_sha256", "sha256:" + "1" * 64)
    raw["evidence"][evidence_name][field] = bad_value

    with pytest.raises(ValidationError, match="evidence|completed|raw output"):
        normalize_provider_output("frontend-standard", raw, base_revision=BASE_REVISION)


@pytest.mark.parametrize("provider", ("requirements-analysis", "frontend-standard"))
def test_raw_output_rejects_unknown_top_level_and_nested_fields(provider: str) -> None:
    raw = fixture(provider, "raw-complete.yaml")
    raw["base_revision"] = BASE_REVISION
    if provider == "frontend-standard":
        for evidence in raw["evidence"].values():
            evidence.setdefault("output_sha256", "sha256:" + "1" * 64)

    top_level = yaml.safe_load(yaml.safe_dump(raw))
    top_level["unknown_control_write"] = {"docs/project-control/project-state.yaml": "tampered"}
    with pytest.raises(ValidationError, match="unknown_control_write|raw output"):
        normalize_provider_output(provider, top_level, base_revision=BASE_REVISION)

    nested = yaml.safe_load(yaml.safe_dump(raw))
    nested["provider"]["unknown"] = "retained payload"
    with pytest.raises(ValidationError, match="unknown|raw output"):
        normalize_provider_output(provider, nested, base_revision=BASE_REVISION)


@pytest.mark.parametrize(
    ("provider", "files_key"),
    (("requirements-analysis", "files"), ("frontend-standard", "changes")),
)
def test_raw_output_schema_rejects_path_smuggling(provider: str, files_key: str) -> None:
    raw = fixture(provider, "raw-complete.yaml")
    raw["base_revision"] = BASE_REVISION
    if provider == "frontend-standard":
        for evidence in raw["evidence"].values():
            evidence.setdefault("output_sha256", "sha256:" + "1" * 64)
    raw[files_key] = {"staging/frontend/../../docs/project-control/project-state.yaml": "tampered"}

    with pytest.raises(ValidationError, match="raw output|writable scope|protected"):
        normalize_provider_output(provider, raw, base_revision=BASE_REVISION)


@pytest.mark.parametrize(
    ("provider", "status_key"),
    (("requirements-analysis", "outcome"), ("frontend-standard", "result")),
)
def test_provider_failure_status_is_normalized(provider: str, status_key: str) -> None:
    raw = fixture(provider, "raw-complete.yaml")
    raw[status_key] = "failure"
    raw["files" if provider == "requirements-analysis" else "changes"] = {}

    adapted = normalize_provider_output(provider, raw, base_revision=BASE_REVISION)

    assert adapted.contract_result.status == "failed"
    assert not adapted.contract_result.files


@pytest.mark.parametrize(
    ("provider", "status_key"),
    (("requirements-analysis", "outcome"), ("frontend-standard", "result")),
)
def test_failed_provider_output_cannot_carry_writes(provider: str, status_key: str) -> None:
    raw = fixture(provider, "raw-complete.yaml")
    raw[status_key] = "failure"

    with pytest.raises(ValidationError, match="failed.*file changes"):
        normalize_provider_output(provider, raw, base_revision=BASE_REVISION)


def test_frontend_adapter_rejects_lifecycle_and_project_control_writes(tmp_path: Path) -> None:
    canonical = tmp_path / "docs" / "project-control" / "project-state.yaml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("current_stage: STAGE-09\n", encoding="utf-8")
    before = canonical.read_bytes()
    raw = fixture("frontend-standard", "raw-complete.yaml")
    raw["changes"] = {"docs/project-control/project-state.yaml": "current_stage: STAGE-10\n"}
    raw["lifecycle_changes"] = ["complete STAGE-09"]

    with pytest.raises(ValidationError, match="raw output|lifecycle|writable scope|protected"):
        normalize_provider_output("frontend-standard", raw, base_revision=BASE_REVISION)

    assert canonical.read_bytes() == before


@pytest.mark.parametrize("provider", ("requirements-analysis", "frontend-standard"))
def test_recorded_fixture_trace_replays_exact_normalization(provider: str) -> None:
    trace = fixture(provider, "trace.yaml")
    assert trace["invocation_mode"] == "recorded-fixture"
    assert trace["limitation"] == "live provider invocation unavailable: no provider runtime/API is installed"
    assert trace["provider"]["revision"] == load_provider_adapter(provider).revision
    input_path = ROOT / trace["input_fixture"]
    raw_path = ROOT / trace["expected_raw_output_fixture"]
    result_path = ROOT / trace["normalization"]["expected_result_fixture"]
    assert trace["input_sha256"] == sha256(input_path)
    assert trace["expected_raw_output_sha256"] == sha256(raw_path)
    assert trace["normalization"]["expected_result_sha256"] == sha256(result_path)

    command = list(trace["normalization"]["command"])
    completed = subprocess.run(
        [sys.executable, *command[1:]],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == json.loads(
        result_path.read_text(encoding="utf-8")
    )
