"""Contract and provider resolution tests for Task 7."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest
import yaml
from jsonschema import Draft202012Validator


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.errors import ValidationError
from ai_sdlc.contracts import CapabilityContract, load_contract
from ai_sdlc.providers import (
    ProviderLock,
    ProviderResolution,
    resolve_provider,
    verify_provider_lock,
)


def test_versioned_contract_has_all_three_sections() -> None:
    """A contract is not loadable unless dispatch/result/integration are present."""
    contract = load_contract("capability.resolve", 1)

    assert contract.id == "capability.resolve"
    assert contract.version == 1
    assert contract.dispatch
    assert contract.result
    assert contract.integration


def test_unknown_or_incompatible_contract_version_is_rejected() -> None:
    """A caller cannot silently receive a different contract version."""
    with pytest.raises(ValidationError, match="version"):
        load_contract("capability.resolve", 2)


def test_lock_takes_precedence_over_catalog_recommendation() -> None:
    """The project lock selects the provider even when catalog ordering differs."""
    contract = load_contract("capability.resolve", 1)
    lock = ProviderLock(
        schema_version=1,
        capabilities={
            contract.id: {
                "contract_version": 1,
                "provider": "builtin-capability-resolver",
                "source": "builtin://capability-resolver",
                "revision": "1",
                "integrity": "sha256:" + "0" * 64,
                "status": "enabled",
            }
        },
    )

    resolution = resolve_provider(contract, lock, {"available_providers": []})

    assert resolution.status == "resolved"
    assert resolution.provider == "builtin-capability-resolver"
    assert resolution.from_lock is True


def test_tampered_lock_fails_closed() -> None:
    """A lock with a changed checksum is never treated as a usable provider."""
    contract = load_contract("capability.resolve", 1)
    lock = ProviderLock(
        schema_version=1,
        capabilities={
            contract.id: {
                "contract_version": 1,
                "provider": "builtin-capability-resolver",
                "source": "builtin://capability-resolver",
                "revision": "1",
                "integrity": "sha256:not-a-checksum",
                "status": "enabled",
            }
        },
    )

    report = verify_provider_lock(lock)

    assert not report.is_valid
    assert report.findings[0].code == "invalid_checksum"
    with pytest.raises(ValidationError, match="checksum"):
        resolve_provider(contract, lock, {})


def test_resolver_applies_missing_provider_policies_deterministically() -> None:
    """Stop, fallback, skip, and risk gates have explicit stable outcomes."""
    base = load_contract("capability.resolve", 1)
    unavailable = {"unavailable_providers": ["builtin-capability-resolver", "external-capability-resolver"]}

    stopped = resolve_provider(base, None, unavailable)
    assert stopped.status == "stopped"

    fallback = replace(base, missing_provider_policy="fallback")
    fallback_result = resolve_provider(fallback, None, unavailable)
    assert fallback_result.status == "resolved"
    assert fallback_result.used_fallback is True

    skipped = replace(base, missing_provider_policy="skip_if_not_applicable")
    skipped_result = resolve_provider(skipped, None, {"applicable": False})
    assert skipped_result.status == "skipped"

    gated = replace(base, missing_provider_policy="require_risk_decision")
    gated_result = resolve_provider(gated, None, unavailable)
    assert gated_result.status == "risk_decision_required"
    approved = resolve_provider(gated, None, {"unavailable_providers": ["builtin-capability-resolver", "external-capability-resolver"], "risk_decision": "approved"})
    assert approved.status == "resolved"

    assert resolve_provider(fallback, None, unavailable) == fallback_result


def test_builtin_and_external_providers_use_the_same_resolution_data_model() -> None:
    """Provider origin is data, not a separate resolution engine."""
    contract = load_contract("capability.resolve", 1)
    builtin = resolve_provider(contract, None, {"available_providers": ["builtin-capability-resolver"]})
    external = resolve_provider(contract, None, {"available_providers": ["external-capability-resolver"]})

    assert isinstance(builtin, ProviderResolution)
    assert isinstance(external, ProviderResolution)
    assert builtin.provider != external.provider
    assert builtin.kind in {"builtin", "external"}
    assert external.kind in {"builtin", "external"}


def test_default_c4_resolution_prefers_pinned_external_provider() -> None:
    resolution = resolve_provider(load_contract("architecture.c4", 1), None, {})

    assert resolution.provider == "c4-modeling"
    assert resolution.kind == "external"


def test_c4_v1_retains_its_original_required_evidence() -> None:
    assert tuple(load_contract("architecture.c4", 1).result["required_evidence"]) == ("requirements",)


def test_external_adapter_cannot_use_numeric_builtin_revision() -> None:
    schema = json.loads((SKILL_ROOT / "assets/schemas/provider-adapter.schema.json").read_text(encoding="utf-8"))
    document = yaml.safe_load((SKILL_ROOT / "references/providers/adapters/requirements-analysis.yaml").read_text(encoding="utf-8"))
    document["provider"]["revision"] = "1"

    assert list(Draft202012Validator(schema).iter_errors(document))


def _locked_entry(*, provider: str, source: str, revision: str, integrity: str) -> ProviderLock:
    """Build a lock fixture with metadata copied from the shipped catalog."""
    return ProviderLock(
        schema_version=1,
        capabilities={
            "capability.resolve": {
                "contract_version": 1,
                "provider": provider,
                "source": source,
                "revision": revision,
                "integrity": integrity,
                "status": "enabled",
            }
        },
    )


def test_locked_provider_must_advertise_the_requested_capability() -> None:
    """A valid lock cannot route a capability through an unrelated provider."""
    contract = load_contract("capability.resolve", 1)
    lock = _locked_entry(
        provider="superpowers-brainstorming",
        source="https://github.com/obra/superpowers",
        revision="7.0.1",
        integrity="sha256:" + "3" * 64,
    )

    with pytest.raises(ValidationError, match="capability"):
        resolve_provider(contract, lock, {})


def test_skip_if_not_applicable_is_checked_before_locked_resolution() -> None:
    """An inapplicable capability remains skipped even when a lock exists."""
    contract = replace(load_contract("capability.resolve", 1), missing_provider_policy="skip_if_not_applicable")
    lock = _locked_entry(
        provider="builtin-capability-resolver",
        source="builtin://capability-resolver",
        revision="1",
        integrity="sha256:" + "0" * 64,
    )

    resolution = resolve_provider(contract, lock, {"applicable": False})

    assert resolution.status == "skipped"


def test_skip_if_not_applicable_still_fails_closed_on_tampered_lock() -> None:
    """Applicability skips provider selection, not lock integrity validation."""
    contract = replace(load_contract("capability.resolve", 1), missing_provider_policy="skip_if_not_applicable")
    lock = _locked_entry(
        provider="builtin-capability-resolver",
        source="builtin://capability-resolver",
        revision="1",
        integrity="sha256:not-a-checksum",
    )

    with pytest.raises(ValidationError, match="checksum"):
        resolve_provider(contract, lock, {"applicable": False})


def test_public_resolver_rejects_a_manually_constructed_unknown_contract_version() -> None:
    """The public resolver does not trust a hand-built unsupported contract version."""
    contract = replace(load_contract("capability.resolve", 1), version=99)

    with pytest.raises(ValidationError, match="version"):
        resolve_provider(contract, None, {})


def test_contract_schema_requires_parallelism_and_runtime_requires_section_fields() -> None:
    """Schema and immutable runtime validation reject the same missing contract data."""
    schema = json.loads((SKILL_ROOT / "assets/schemas/capability-contract.schema.json").read_text(encoding="utf-8"))
    document = json.loads(json.dumps({
        "id": "x",
        "version": 1,
        "dispatch": {
            "required_inputs": [], "optional_inputs": [], "task_template": "x",
            "readable_scope": [], "writable_scope": [], "forbidden_actions": [],
        },
        "result": {"required_sections": [], "required_evidence": [], "statuses": ["completed"]},
        "integration": {"mappings": [], "structural_checks": [], "semantic_checks": [], "human_approvals": []},
        "missing_provider_policy": "stop",
    }))
    assert list(Draft202012Validator(schema).iter_errors(document))

    with pytest.raises(ValidationError, match="parallelism"):
        CapabilityContract(
            id="x", version=1, dispatch=document["dispatch"], result=document["result"],
            integration=document["integration"], parallelism={}, missing_provider_policy="stop",
        )


def test_valid_but_mismatched_checksum_fails_against_catalog_metadata() -> None:
    """A syntactically valid changed digest cannot replace catalog integrity."""
    contract = load_contract("capability.resolve", 1)
    lock = _locked_entry(
        provider="builtin-capability-resolver",
        source="builtin://capability-resolver",
        revision="1",
        integrity="sha256:" + "f" * 64,
    )

    assert verify_provider_lock(lock).is_valid
    with pytest.raises(ValidationError, match="metadata"):
        resolve_provider(contract, lock, {})
