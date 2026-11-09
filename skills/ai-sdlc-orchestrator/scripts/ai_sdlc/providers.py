"""Deterministic provider catalog and fail-closed lock resolution."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator

from .contracts import CapabilityContract, load_contract
from .errors import ValidationError
from .io import sha256_file
from .validation import ValidationFinding, ValidationReport


_INTEGRITY = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


@dataclass(frozen=True, slots=True)
class ProviderLock:
    schema_version: int
    capabilities: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_value(cls, value: "ProviderLock | Mapping[str, Any] | Path") -> "ProviderLock":
        document = _read_document(value)
        if isinstance(document, cls):
            return document
        if not isinstance(document, Mapping):
            raise ValidationError("provider lock must contain an object")
        capabilities = document.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise ValidationError("provider lock capabilities must be an object")
        return cls(document.get("schema_version", 0), {str(key): item for key, item in capabilities.items()})


@dataclass(frozen=True, slots=True)
class ProviderResolution:
    status: str
    capability: str
    contract_version: int
    provider: str | None = None
    kind: str | None = None
    source: str | None = None
    revision: str | None = None
    integrity: str | None = None
    from_lock: bool = False
    used_fallback: bool = False
    reason: str | None = None

    @property
    def provider_id(self) -> str | None:
        """Stable descriptive alias for callers that use provider_id terminology."""
        return self.provider


def verify_provider_lock(lock: ProviderLock | Mapping[str, Any] | Path) -> ValidationReport:
    """Validate lock structure and verify every locally verifiable checksum."""
    findings: list[ValidationFinding] = []
    try:
        value = _read_document(lock)
    except ValidationError as error:
        return ValidationReport((ValidationFinding("invalid_lock", "$", str(error)),))
    schema_root = Path(__file__).resolve().parents[2] / "assets" / "schemas"
    try:
        schema = json.loads((schema_root / "capability-lock.schema.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return ValidationReport((ValidationFinding("invalid_lock_schema", "$", str(error)),))
    document = value if isinstance(value, Mapping) else {"schema_version": value.schema_version, "capabilities": value.capabilities}
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda item: (list(item.path), item.message))
    for error in errors:
        pointer = ".".join(str(part) for part in error.path) or "$"
        code = "invalid_checksum" if "integrity" in error.path else "schema_validation"
        findings.append(ValidationFinding(code, pointer, error.message))
    if findings:
        return ValidationReport(tuple(findings))
    for capability_id, entry in sorted(document["capabilities"].items()):
        integrity = entry["integrity"]
        match = _INTEGRITY.fullmatch(integrity)
        if not match:
            findings.append(ValidationFinding("invalid_checksum", f"capabilities.{capability_id}.integrity", "integrity must be sha256 followed by 64 hexadecimal characters"))
            continue
        source = entry["source"]
        source_path = _local_source(source)
        if source_path is not None:
            if not source_path.is_file():
                findings.append(ValidationFinding("missing_provider_source", f"capabilities.{capability_id}.source", f"provider source does not exist: {source}"))
            elif sha256_file(source_path).lower() != match.group(1).lower():
                findings.append(ValidationFinding("checksum_mismatch", f"capabilities.{capability_id}.integrity", "provider source checksum does not match lock"))
    return ValidationReport(tuple(findings))


def resolve_provider(contract: CapabilityContract, lock: ProviderLock | Mapping[str, Any] | Path | None, environment: Mapping[str, Any] | None = None) -> ProviderResolution:
    """Resolve a provider without installing or fetching anything dynamically."""
    _validate_public_contract(contract)
    environment = dict(environment or {})
    lock_value: ProviderLock | None = None
    lock_entry: Mapping[str, Any] | None = None
    if lock is not None:
        lock_value = ProviderLock.from_value(lock)
        report = verify_provider_lock(lock_value)
        if not report.is_valid:
            finding = report.findings[0]
            raise ValidationError(f"provider lock {finding.code}: {finding.message}")
        lock_entry = lock_value.capabilities.get(contract.id)
        if lock_entry is not None and lock_entry.get("contract_version") != contract.version:
            raise ValidationError(f"provider lock contract version mismatch for {contract.id}")

    applicable = environment.get("applicable", True)
    if applicable is False and contract.missing_provider_policy == "skip_if_not_applicable":
        return ProviderResolution("skipped", contract.id, contract.version, reason="capability is not applicable")

    catalog = _load_catalog()
    if lock_entry is not None:
        if lock_entry.get("status") != "enabled":
            return ProviderResolution("stopped", contract.id, contract.version, reason="locked provider is disabled")
        provider = _select_locked(catalog, contract.id, lock_entry)
        return _resolution(contract, lock_entry, provider, from_lock=True)

    unavailable = set(_as_strings(environment.get("unavailable_providers", ())))
    available = environment.get("available_providers")
    allowed = None if available is None else set(_as_strings(available))
    candidates = _catalog_candidates(catalog, contract.id, allowed, unavailable, fallback=False)
    if candidates:
        return _resolution(contract, _entry_for_provider(candidates[0]), candidates[0], from_lock=False)

    policy = contract.missing_provider_policy
    if policy == "fallback":
        fallback = _catalog_candidates(catalog, contract.id, allowed, unavailable, fallback=True)
        if fallback:
            return _resolution(contract, _entry_for_provider(fallback[0]), fallback[0], from_lock=False, used_fallback=True)
    if policy == "require_risk_decision":
        decision = environment.get("risk_decision")
        if str(decision).lower() in {"approved", "approve", "allow", "allowed", "true"} or decision is True:
            fallback = _catalog_candidates(catalog, contract.id, allowed, unavailable, fallback=True)
            if fallback:
                return _resolution(contract, _entry_for_provider(fallback[0]), fallback[0], from_lock=False, used_fallback=True)
        return ProviderResolution("risk_decision_required", contract.id, contract.version, reason="provider requires an explicit risk decision")
    if policy == "skip_if_not_applicable" and applicable is False:
        return ProviderResolution("skipped", contract.id, contract.version, reason="capability is not applicable")
    return ProviderResolution("stopped", contract.id, contract.version, reason="no applicable provider is available")


def _load_catalog() -> dict[str, dict[str, Any]]:
    path = Path(__file__).resolve().parents[2] / "references" / "providers" / "catalog.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValidationError("provider catalog cannot be read") from error
    if not isinstance(document, Mapping) or document.get("schema_version") != 1 or not isinstance(document.get("providers"), Mapping):
        raise ValidationError("provider catalog has an unsupported schema")
    return {str(key): dict(value) for key, value in document["providers"].items() if isinstance(value, Mapping)}


def _catalog_candidates(catalog: Mapping[str, Mapping[str, Any]], capability: str, allowed: set[str] | None, unavailable: set[str], fallback: bool) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for provider_id, entry in catalog.items():
        capabilities = entry.get("capabilities", ())
        if capability not in capabilities or bool(entry.get("fallback", False)) != fallback or not bool(entry.get("enabled", True)):
            continue
        if provider_id in unavailable or (allowed is not None and provider_id not in allowed):
            continue
        candidate = dict(entry)
        candidate["provider"] = provider_id
        values.append(candidate)
    values.sort(key=lambda entry: (int(entry.get("priority", 1000)), str(entry["provider"])))
    return values


def _select_locked(catalog: Mapping[str, Mapping[str, Any]], capability: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    provider_id = entry["provider"]
    catalog_entry = catalog.get(provider_id)
    if catalog_entry is None:
        raise ValidationError(f"locked provider is not present in local catalog: {provider_id}")
    if capability not in catalog_entry.get("capabilities", ()):
        raise ValidationError(f"locked provider does not advertise capability {capability}: {provider_id}")
    if catalog_entry.get("integrity") != entry["integrity"] or catalog_entry.get("source") != entry["source"] or catalog_entry.get("revision") != entry["revision"]:
        raise ValidationError(f"locked provider metadata does not match catalog: {provider_id}")
    selected = dict(catalog_entry)
    selected["provider"] = provider_id
    return selected


def _validate_public_contract(contract: CapabilityContract) -> None:
    if not isinstance(contract, CapabilityContract):
        raise ValidationError("provider resolver requires a CapabilityContract")
    try:
        load_contract(contract.id, contract.version)
    except ValidationError as error:
        raise ValidationError(f"unsupported capability contract version for {contract.id}: {contract.version}") from error


def _entry_for_provider(provider: Mapping[str, Any]) -> dict[str, Any]:
    return {"provider": provider["provider"], "source": provider["source"], "revision": provider["revision"], "integrity": provider["integrity"]}


def _resolution(contract: CapabilityContract, entry: Mapping[str, Any], provider: Mapping[str, Any], *, from_lock: bool, used_fallback: bool = False) -> ProviderResolution:
    return ProviderResolution("resolved", contract.id, contract.version, provider=entry["provider"], kind=provider.get("kind"), source=entry["source"], revision=entry["revision"], integrity=entry["integrity"], from_lock=from_lock, used_fallback=used_fallback)


def _read_document(value: ProviderLock | Mapping[str, Any] | Path) -> Any:
    if isinstance(value, ProviderLock) or isinstance(value, Mapping):
        return value
    if isinstance(value, Path):
        try:
            parsed = yaml.safe_load(value.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
            raise ValidationError(f"provider lock cannot be read: {value}") from error
        return parsed
    raise ValidationError("provider lock must be a mapping, Path, or ProviderLock")


def _local_source(source: str) -> Path | None:
    if re.match(r"^[A-Za-z]:[\\/]", source):
        return Path(source)
    parsed = urlparse(source)
    if parsed.scheme:
        return None
    return Path(source)


def _as_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in value)
    return ()
