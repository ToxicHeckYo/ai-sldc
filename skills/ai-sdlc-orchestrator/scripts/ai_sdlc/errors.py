"""Typed errors shared by portable orchestrator modules."""


class OrchestratorError(Exception):
    """Base error raised by the portable orchestration core."""


class ValidationError(OrchestratorError):
    """Raised when an artifact or transition violates a validation rule."""


class PolicyError(OrchestratorError):
    """Raised when work violates a governing lifecycle policy."""
