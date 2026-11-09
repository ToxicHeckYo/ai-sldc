"""Versioned capability contracts loaded only from the shipped package."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator

from .errors import ValidationError


_POLICIES = {"stop", "fallback", "skip_if_not_applicable", "require_risk_decision"}
_SECTION_FIELDS = {
    "dispatch": ("required_inputs", "optional_inputs", "task_template", "readable_scope", "writable_scope", "forbidden_actions"),
    "result": ("required_sections", "required_evidence", "statuses"),
    "integration": ("mappings", "structural_checks", "semantic_checks", "human_approvals"),
    "parallelism": ("allowed", "partition_by", "integration_capability"),
}
_LIST_FIELDS = {
    "required_inputs", "optional_inputs", "readable_scope", "writable_scope", "forbidden_actions",
    "required_sections", "required_evidence", "statuses", "mappings", "structural_checks",
    "semantic_checks", "human_approvals", "partition_by",
}


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    """Immutable runtime view of one versioned capability contract."""

    id: str
    version: int
    dispatch: Mapping[str, Any]
    result: Mapping[str, Any]
    integration: Mapping[str, Any]
    parallelism: Mapping[str, Any]
    missing_provider_policy: str
    applicable_when: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValidationError("capability contract id and positive version are required")
        if not self.dispatch or not self.result or not self.integration or not self.parallelism:
            raise ValidationError("capability contract requires dispatch, result, integration, and parallelism sections")
        if self.missing_provider_policy not in _POLICIES:
            raise ValidationError(f"unknown missing provider policy: {self.missing_provider_policy}")
        for name in ("dispatch", "result", "integration", "parallelism"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValidationError(f"contract section {name} must be an object")
            missing = [field for field in _SECTION_FIELDS[name] if field not in value]
            if missing:
                raise ValidationError(f"contract section {name} is missing required fields: {', '.join(missing)}")
            for field in _LIST_FIELDS.intersection(value):
                if not isinstance(value[field], (list, tuple)) or any(not isinstance(item, str) for item in value[field]):
                    raise ValidationError(f"contract field {name}.{field} must be a list of strings")
            if name == "dispatch" and not isinstance(value["task_template"], str):
                raise ValidationError("contract field dispatch.task_template must be a string")
            if name == "result" and not set(value["statuses"]).issubset({"completed", "partial", "blocked", "failed"}):
                raise ValidationError("contract field result.statuses contains an unsupported status")
            if name == "parallelism" and value["allowed"] not in {"never", "conditional", "always"}:
                raise ValidationError("contract field parallelism.allowed is unsupported")
            if name == "parallelism" and not isinstance(value["integration_capability"], str):
                raise ValidationError("contract field parallelism.integration_capability must be a string")
            if name == "integration" and "human_approval_roles" in value:
                roles = value["human_approval_roles"]
                if not isinstance(roles, Mapping) or any(
                    not isinstance(gate, str) or not gate.strip() or not isinstance(role, str) or not role.strip()
                    for gate, role in roles.items()
                ):
                    raise ValidationError("contract field integration.human_approval_roles must map non-empty gate names to roles")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        object.__setattr__(self, "applicable_when", tuple(self.applicable_when))
        _validate_contract_shape(self)


def load_contract(capability_id: str, version: int, root: Path | None = None) -> CapabilityContract:
    """Load exactly the requested contract version from local package data."""
    if not isinstance(capability_id, str) or not capability_id.strip():
        raise ValidationError("capability id must be a non-empty string")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValidationError(f"unsupported capability contract version: {version!r}")
    skill_root = (root or Path(__file__).resolve().parents[2]).resolve()
    contract_path = skill_root / "references" / "contracts" / f"{capability_id}.yaml"
    if not contract_path.is_file():
        raise ValidationError(f"unknown capability contract: {capability_id}@{version}")
    try:
        document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValidationError(f"capability contract cannot be read: {contract_path}") from error
    if not isinstance(document, dict):
        raise ValidationError(f"capability contract must contain an object: {contract_path}")
    schema_path = skill_root / "assets" / "schemas" / "capability-contract.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("capability contract schema is unavailable") from error
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: (list(error.path), error.message))
    if errors:
        error = errors[0]
        pointer = ".".join(str(part) for part in error.path) or "$"
        raise ValidationError(f"invalid capability contract at {pointer}: {error.message}")
    if document.get("id") != capability_id or document.get("version") != version:
        raise ValidationError(
            f"incompatible capability contract version for {capability_id}: requested {version}, found {document.get('version')!r}"
        )
    return CapabilityContract(
        id=document["id"], version=document["version"], dispatch=document["dispatch"], result=document["result"],
        integration=document["integration"], parallelism=document["parallelism"],
        missing_provider_policy=document["missing_provider_policy"], applicable_when=tuple(document.get("applicable_when", ())),
    )


def _validate_contract_shape(contract: CapabilityContract) -> None:
    """Apply the shipped JSON Schema to direct runtime construction as well."""
    schema_path = Path(__file__).resolve().parents[2] / "assets" / "schemas" / "capability-contract.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError("capability contract schema is unavailable") from error
    document = {
        "id": contract.id,
        "version": contract.version,
        "dispatch": dict(contract.dispatch),
        "result": dict(contract.result),
        "integration": dict(contract.integration),
        "parallelism": dict(contract.parallelism),
        "missing_provider_policy": contract.missing_provider_policy,
        "applicable_when": list(contract.applicable_when),
    }
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: (list(error.path), error.message))
    if errors:
        error = errors[0]
        pointer = ".".join(str(part) for part in error.path) or "$"
        raise ValidationError(f"invalid capability contract at {pointer}: {error.message}")
