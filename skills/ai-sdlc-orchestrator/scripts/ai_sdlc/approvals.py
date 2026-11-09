"""Exact-payload human approval records and fail-closed verification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from .validation import ValidationFinding, ValidationReport


_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_DECISIONS = frozenset({"approved", "rejected", "returned"})


@dataclass(frozen=True, slots=True)
class Approval:
    """Immutable, asserted human decision for one gate and one fingerprint.

    ``role_assurance`` deliberately records an assertion rather than claiming
    IAM verification.  A record is evidence for its exact gate/fingerprint
    context; it is not a bearer authorization for another preview.
    """

    gate: str
    actor: str
    role: str
    fingerprint: str
    decision: str = "approved"
    role_assurance: str = "asserted"

    def __post_init__(self) -> None:
        for name in ("gate", "actor", "role", "fingerprint", "decision", "role_assurance"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"approval {name} must be a non-empty string")
        if not _FINGERPRINT_RE.fullmatch(self.fingerprint):
            raise ValueError("approval fingerprint must be a lowercase SHA-256 hex digest")
        if self.decision not in _DECISIONS:
            raise ValueError(f"unsupported approval decision: {self.decision!r}")
        if self.role_assurance != "asserted":
            raise ValueError("approval role_assurance must be 'asserted'")


def fingerprint_payload(payload: bytes) -> str:
    """Return the deterministic SHA-256 digest of the exact payload bytes."""
    if not isinstance(payload, bytes):
        raise TypeError("approval payload must be bytes")
    return hashlib.sha256(payload).hexdigest()


def record_approval(
    gate: str,
    actor: str,
    role: str,
    fingerprint: str,
    *,
    decision: str = "approved",
) -> Approval:
    """Create an immutable approval with explicitly asserted role assurance."""
    return Approval(
        gate=gate,
        actor=actor,
        role=role,
        fingerprint=fingerprint,
        decision=decision,
        role_assurance="asserted",
    )


def verify_approval(
    approval: Approval,
    payload: bytes,
    required_role: str,
    required_gate: str,
) -> ValidationReport:
    """Verify approval evidence against the exact current payload.

    The function is intentionally pure: re-checking the same immutable
    approval in the same gate/payload context is deterministic and valid.
    Reuse for another payload, gate, or role fails closed as stale/context
    mismatch; application layers own any separate one-time application ledger.
    """
    findings: list[ValidationFinding] = []
    if not isinstance(approval, Approval):
        return ValidationReport((_finding("invalid_approval", "approval", "approval must be an Approval record"),))
    if not isinstance(payload, bytes):
        findings.append(_finding("invalid_payload", "payload", "approval payload must be bytes"))
    if not isinstance(required_role, str) or not required_role.strip():
        findings.append(_finding("invalid_required_role", "required_role", "required role must be a non-empty string"))
    if not isinstance(required_gate, str) or not required_gate.strip():
        findings.append(_finding("invalid_required_gate", "required_gate", "required gate must be a non-empty string"))
    elif approval.gate != required_gate:
        findings.append(_finding("approval_gate_mismatch", "approval.gate", "approval gate does not match the requested gate"))
    if approval.role != required_role:
        findings.append(_finding("approval_role_mismatch", "approval.role", "approval role does not satisfy the required role"))
    if approval.role_assurance != "asserted":
        findings.append(_finding("unassured_role", "approval.role_assurance", "approval role assurance is not asserted"))
    if approval.decision != "approved":
        findings.append(_finding("approval_rejected", "approval.decision", "approval decision is not approved"))
    if isinstance(payload, bytes) and approval.fingerprint != fingerprint_payload(payload):
        findings.append(_finding("approval_fingerprint_mismatch", "approval.fingerprint", "approval is stale for the exact payload"))
    return ValidationReport(tuple(findings))


def _finding(code: str, path: str, message: str) -> ValidationFinding:
    return ValidationFinding(code=code, path=path, message=message)
