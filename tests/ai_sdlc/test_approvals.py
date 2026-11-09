"""RED/GREEN tests for exact-fingerprint human approval evidence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.approvals import Approval, fingerprint_payload, record_approval, verify_approval


def test_fingerprint_is_sha256_of_exact_bytes_and_is_deterministic() -> None:
    payload = b"preview\x00with exact bytes"

    first = fingerprint_payload(payload)

    assert first == fingerprint_payload(payload)
    assert len(first) == 64
    assert first == "f033720f6ac863d966378c2fe43f12f9e6b49393ce5a9d18818cd8840ea7476b"
    assert first != fingerprint_payload(payload + b"\n")


def test_recorded_approval_is_immutable_and_declares_asserted_role_assurance() -> None:
    approval = record_approval("release", "alice", "product-owner", "a" * 64)

    assert isinstance(approval, Approval)
    assert approval.gate == "release"
    assert approval.actor == "alice"
    assert approval.role == "product-owner"
    assert approval.fingerprint == "a" * 64
    assert approval.role_assurance == "asserted"
    with pytest.raises((AttributeError, TypeError)):
        approval.role = "system-architect"  # type: ignore[misc]


def test_approval_verifies_only_exact_payload_and_required_role() -> None:
    payload = b"release preview"
    approval = record_approval("release", "alice", "product-owner", fingerprint_payload(payload))

    assert verify_approval(approval, payload, "product-owner", "release").is_valid
    wrong_payload = verify_approval(approval, b"changed preview", "product-owner", "release")
    wrong_role = verify_approval(approval, payload, "system-architect", "release")

    assert not wrong_payload.is_valid
    assert wrong_payload.findings[0].code == "approval_fingerprint_mismatch"
    assert not wrong_role.is_valid
    assert wrong_role.findings[0].code == "approval_role_mismatch"


def test_rejected_approval_never_verifies() -> None:
    payload = b"release preview"
    approval = record_approval(
        "release", "alice", "product-owner", fingerprint_payload(payload), decision="rejected"
    )

    report = verify_approval(approval, payload, "product-owner", "release")

    assert not report.is_valid
    assert report.findings[0].code == "approval_rejected"


def test_reusing_approval_for_another_gate_is_rejected() -> None:
    payload = b"release preview"
    approval = record_approval("release", "alice", "product-owner", fingerprint_payload(payload))

    report = verify_approval(approval, payload, "product-owner", "acceptance")

    assert not report.is_valid
    assert report.findings[0].code == "approval_gate_mismatch"


def test_transition_validation_requires_external_approval_for_human_criterion() -> None:
    from tests.ai_sdlc.test_model import replace_status, snapshot_with
    from ai_sdlc.validation import validate_transition

    before = snapshot_with()
    criterion = replace(before.plan.stages[0].entry_criteria[0], approval_role="product-owner", status="in_review")
    before_stage = replace(before.plan.stages[0], entry_criteria=(criterion,))
    before = replace(before, plan=replace(before.plan, stages=(before_stage, *before.plan.stages[1:])))
    after = replace_status(before, "criterion", "verified")

    missing = validate_transition(before, after)

    assert not missing.is_valid
    assert missing.findings[0].code == "missing_approval"


def test_transition_validation_accepts_exact_external_approval() -> None:
    from tests.ai_sdlc.test_model import replace_status, snapshot_with
    from ai_sdlc.validation import validate_transition

    before = snapshot_with()
    criterion = replace(before.plan.stages[0].entry_criteria[0], approval_role="product-owner", status="in_review")
    before_stage = replace(before.plan.stages[0], entry_criteria=(criterion,))
    before = replace(before, plan=replace(before.plan, stages=(before_stage, *before.plan.stages[1:])))
    after = replace_status(before, "criterion", "verified")
    payload = b"criterion EC-01 preview"
    approval = record_approval("EC-01", "alice", "product-owner", fingerprint_payload(payload))

    report = validate_transition(before, after, approval=approval, payload=payload)

    assert report.is_valid


def test_transition_validation_rejects_stale_external_approval() -> None:
    from tests.ai_sdlc.test_model import replace_status, snapshot_with
    from ai_sdlc.validation import validate_transition

    before = snapshot_with()
    criterion = replace(before.plan.stages[0].entry_criteria[0], approval_role="product-owner", status="in_review")
    before_stage = replace(before.plan.stages[0], entry_criteria=(criterion,))
    before = replace(before, plan=replace(before.plan, stages=(before_stage, *before.plan.stages[1:])))
    after = replace_status(before, "criterion", "verified")
    approved_payload = b"criterion EC-01 preview"
    approval = record_approval("EC-01", "alice", "product-owner", fingerprint_payload(approved_payload))

    report = validate_transition(before, after, approval=approval, payload=b"changed preview")

    assert not report.is_valid
    assert report.findings[0].code == "approval_fingerprint_mismatch"


def test_human_role_removed_while_verifying_still_requires_before_approval() -> None:
    from tests.ai_sdlc.test_model import replace_status, snapshot_with
    from ai_sdlc.validation import validate_transition

    before = snapshot_with()
    criterion = replace(before.plan.stages[0].entry_criteria[0], approval_role="product-owner", status="in_review")
    before_stage = replace(before.plan.stages[0], entry_criteria=(criterion,))
    before = replace(before, plan=replace(before.plan, stages=(before_stage, *before.plan.stages[1:])))
    after = replace_status(before, "criterion", "verified")
    after_criterion = replace(after.plan.stages[0].entry_criteria[0], approval_role=None)
    after_stage = replace(after.plan.stages[0], entry_criteria=(after_criterion,))
    after = replace(after, plan=replace(after.plan, stages=(after_stage, *after.plan.stages[1:])))

    report = validate_transition(before, after)

    assert not report.is_valid
    assert report.findings[0].code == "missing_approval"


def test_newly_added_verified_human_criterion_requires_approval() -> None:
    from tests.ai_sdlc.test_model import snapshot_with
    from ai_sdlc.validation import validate_transition

    before = snapshot_with()
    after_stage = replace(
        before.plan.stages[0],
        entry_criteria=(
            *before.plan.stages[0].entry_criteria,
            replace(before.plan.stages[0].entry_criteria[0], id="EC-NEW", status="verified", approval_role="product-owner"),
        ),
    )
    after = replace(before, plan=replace(before.plan, stages=(after_stage, *before.plan.stages[1:])))

    report = validate_transition(before, after)

    assert not report.is_valid
    assert report.findings[0].code == "missing_approval"


def test_verify_approval_requires_gate_argument() -> None:
    payload = b"release preview"
    approval = record_approval("release", "alice", "product-owner", fingerprint_payload(payload))

    with pytest.raises(TypeError):
        verify_approval(approval, payload, "product-owner")  # type: ignore[call-arg]


def test_approval_schema_rejects_whitespace_only_identifiers() -> None:
    import json

    schema_path = SKILL_ROOT / "assets" / "schemas" / "approval.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    document = {
        "gate": " ",
        "actor": "\t",
        "role": "\n",
        "fingerprint": "a" * 64,
        "decision": "approved",
        "role_assurance": "asserted",
    }

    assert list(validator.iter_errors(document))


def test_already_verified_human_criterion_is_idempotent_without_reapproval() -> None:
    from tests.ai_sdlc.test_model import snapshot_with
    from ai_sdlc.validation import validate_transition

    before = snapshot_with()
    criterion = replace(before.plan.stages[0].entry_criteria[0], status="verified", approval_role="product-owner")
    stage = replace(before.plan.stages[0], entry_criteria=(criterion,))
    before = replace(before, plan=replace(before.plan, stages=(stage, *before.plan.stages[1:])))

    report = validate_transition(before, before)

    assert report.is_valid


def test_unrelated_transition_does_not_reverify_already_verified_human_criterion() -> None:
    from tests.ai_sdlc.test_model import replace_status, snapshot_with
    from ai_sdlc.validation import validate_transition

    before = snapshot_with()
    criterion = replace(before.plan.stages[0].entry_criteria[0], status="verified", approval_role="product-owner")
    stage = replace(before.plan.stages[0], entry_criteria=(criterion,))
    before = replace(before, plan=replace(before.plan, stages=(stage, *before.plan.stages[1:])))
    after = replace_status(before, "stage", "in_review")

    report = validate_transition(before, after)

    assert report.is_valid


@pytest.mark.parametrize("new_role", [None, "system-architect"])
def test_already_verified_human_criterion_rejects_role_mutation(new_role: str | None) -> None:
    from tests.ai_sdlc.test_model import snapshot_with
    from ai_sdlc.validation import validate_transition

    before = snapshot_with()
    criterion = replace(before.plan.stages[0].entry_criteria[0], status="verified", approval_role="product-owner")
    before_stage = replace(before.plan.stages[0], entry_criteria=(criterion,))
    before = replace(before, plan=replace(before.plan, stages=(before_stage, *before.plan.stages[1:])))
    after_criterion = replace(criterion, approval_role=new_role)
    after_stage = replace(before.plan.stages[0], entry_criteria=(after_criterion,))
    after = replace(before, plan=replace(before.plan, stages=(after_stage, *before.plan.stages[1:])))

    report = validate_transition(before, after)

    assert not report.is_valid
    assert report.findings[0].code == "approval_role_changed"
