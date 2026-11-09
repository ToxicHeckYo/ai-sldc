"""Declarative mappings for pinned external provider results."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .contracts import CapabilityContract, load_contract
from .errors import ValidationError
from .integration import normalize_result
from .runtime import ContractResult, DispatchRequest, ProviderResult
from .providers import _load_catalog


_REVISION = re.compile(r"^[0-9a-f]{40}$")
_MISSING = object()


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    """Validated immutable view of one shipped declarative adapter."""

    provider_id: str
    source: str
    revision: str
    integrity: str
    evaluated_at: str
    source_files: tuple[Mapping[str, str], ...]
    capability: str
    contract_version: int
    raw_output_schema: str
    dispatch: Mapping[str, Any]
    result_mapping: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_files", tuple(MappingProxyType(dict(item)) for item in self.source_files))
        object.__setattr__(self, "dispatch", MappingProxyType(dict(self.dispatch)))
        object.__setattr__(self, "result_mapping", MappingProxyType(dict(self.result_mapping)))


@dataclass(frozen=True, slots=True)
class AdaptedProviderResult:
    """Both Task 10 result shapes produced by one provider normalization."""

    provider_result: ProviderResult
    contract_result: ContractResult


def load_provider_adapter(provider_id: str, root: Path | None = None) -> ProviderAdapter:
    """Load and schema-check one shipped adapter without fetching its provider."""
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValidationError("provider adapter id must be a non-empty string")
    skill_root = (root or Path(__file__).resolve().parents[2]).resolve()
    path = skill_root / "references" / "providers" / "adapters" / f"{provider_id}.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema = json.loads((skill_root / "assets" / "schemas" / "provider-adapter.schema.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValidationError(f"unknown provider adapter: {provider_id}") from error
    except (OSError, UnicodeDecodeError, yaml.YAMLError, json.JSONDecodeError) as error:
        raise ValidationError(f"provider adapter cannot be read: {provider_id}") from error
    if not isinstance(document, Mapping):
        raise ValidationError(f"provider adapter must contain an object: {provider_id}")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: (list(error.path), error.message),
    )
    if errors:
        error = errors[0]
        pointer = ".".join(str(part) for part in error.path) or "$"
        raise ValidationError(f"invalid provider adapter at {pointer}: {error.message}")
    provider = document["provider"]
    if provider["id"] != provider_id:
        raise ValidationError(f"provider adapter identity mismatch: {provider_id}")
    catalog_entry = _load_catalog().get(provider_id)
    if catalog_entry is None:
        raise ValidationError(f"provider adapter is absent from the local catalog: {provider_id}")
    for field in ("kind", "source", "revision", "integrity"):
        if catalog_entry.get(field) != provider.get(field):
            raise ValidationError(f"provider adapter metadata does not match catalog: {provider_id}")
    first_source = provider["source_files"][0]
    if provider["integrity"] != f"sha256:{first_source['sha256']}":
        raise ValidationError(f"provider adapter integrity does not identify its primary source: {provider_id}")
    capability = document["capability"]
    if capability["id"] not in catalog_entry.get("capabilities", ()):
        raise ValidationError(f"provider adapter capability is absent from catalog: {provider_id}")
    contract = load_contract(capability["id"], capability["contract_version"], skill_root)
    _validate_adapter_contract(document, contract, skill_root)
    return ProviderAdapter(
        provider_id=provider["id"],
        source=provider["source"],
        revision=provider["revision"],
        integrity=provider["integrity"],
        evaluated_at=str(provider["evaluated_at"]),
        source_files=tuple(provider["source_files"]),
        capability=capability["id"],
        contract_version=capability["contract_version"],
        raw_output_schema=document["raw_output_schema"],
        dispatch=document["dispatch"],
        result_mapping=document["result_mapping"],
    )


def build_dispatch(
    provider_id: str,
    inputs: Mapping[str, Any],
    *,
    base_revision: str,
    root: Path | None = None,
) -> DispatchRequest:
    """Build a Task 10 dispatch with pinned provider identity and governance."""
    adapter = load_provider_adapter(provider_id, root)
    contract = load_contract(adapter.capability, adapter.contract_version, root)
    if not isinstance(inputs, Mapping):
        raise ValidationError("provider dispatch inputs must be an object")
    missing = [name for name in contract.dispatch["required_inputs"] if name not in inputs]
    if missing:
        raise ValidationError(f"provider dispatch is missing required inputs: {', '.join(missing)}")
    if not isinstance(base_revision, str) or not _REVISION.fullmatch(base_revision):
        raise ValidationError("provider dispatch base_revision must be a 40-character Git SHA")
    task_template = str(adapter.dispatch["task_template"])
    skill_root = (root or Path(__file__).resolve().parents[2]).resolve()
    if not (skill_root / task_template).is_file():
        raise ValidationError(f"provider task template is missing: {task_template}")
    request_inputs = dict(inputs)
    request_inputs["provider"] = {
        "id": adapter.provider_id,
        "source": adapter.source,
        "revision": adapter.revision,
        "integrity": adapter.integrity,
    }
    request_inputs["base_revision"] = base_revision
    request_inputs["governance"] = dict(adapter.dispatch["governance"])
    return DispatchRequest(
        capability=adapter.capability,
        contract_version=adapter.contract_version,
        inputs=request_inputs,
        readable_scope=tuple(adapter.dispatch["readable_scope"]),
        writable_scope=tuple(adapter.dispatch["writable_scope"]),
        task_template=task_template,
        base_revision=base_revision,
    )


def normalize_provider_output(
    provider_id: str,
    raw_output: Mapping[str, Any],
    *,
    base_revision: str,
    root: Path | None = None,
) -> AdaptedProviderResult:
    """Apply declarative mappings and reject ambiguous evidence or writes."""
    adapter = load_provider_adapter(provider_id, root)
    contract = load_contract(adapter.capability, adapter.contract_version, root)
    if not isinstance(raw_output, Mapping):
        raise ValidationError("provider raw output must be an object")
    if not isinstance(base_revision, str) or not _REVISION.fullmatch(base_revision):
        raise ValidationError("provider result base_revision must be a 40-character Git SHA")
    skill_root = (root or Path(__file__).resolve().parents[2]).resolve()
    _validate_raw_output(adapter, raw_output, skill_root)
    identity = _path(raw_output, "provider")
    if not isinstance(identity, Mapping) or identity.get("id") != adapter.provider_id or identity.get("revision") != adapter.revision:
        raise ValidationError("provider raw output identity or revision does not match the pinned adapter")
    reported_base = raw_output.get("base_revision")
    if reported_base != base_revision:
        raise ValidationError("provider raw output base_revision does not match the dispatch revision")
    mapping = adapter.result_mapping
    status_config = mapping["status"]
    raw_status = _path(raw_output, status_config["path"])
    status = status_config["values"].get(raw_status)
    if status is None:
        raise ValidationError(f"provider returned an unsupported status: {raw_status!r}")
    for forbidden in mapping["forbidden_nonempty_paths"]:
        value = _path(raw_output, forbidden, default=None)
        if value not in (None, "", (), [], {}):
            raise ValidationError(f"provider returned forbidden lifecycle or control change data: {forbidden}")
    evidence_root = raw_output.get("evidence")
    if not isinstance(evidence_root, Mapping):
        raise ValidationError("provider evidence must be an object")
    evidence: dict[str, Any] = {}
    for name, rule in mapping["evidence"].items():
        value = _path(raw_output, rule["path"], default=_MISSING)
        _validate_evidence(name, value, rule)
        evidence[name] = value
    if status == "completed":
        for name, rule in mapping.get("completed_evidence", {}).items():
            value = _path(raw_output, rule["path"])
            if value not in rule["accepted"]:
                raise ValidationError(
                    f"completed provider evidence is not successful: {name} has {value!r}"
                )
    sections = {name: _path(raw_output, source, default=None) for name, source in mapping["sections"].items()}
    files = _path(raw_output, mapping["files_path"], default={})
    if not isinstance(files, Mapping):
        raise ValidationError("provider file changes must be an object")
    normalized_files = {str(path): value for path, value in files.items()}
    if status in {"blocked", "failed"} and normalized_files:
        raise ValidationError(f"{status} provider result cannot include file changes")
    _validate_changed_paths(normalized_files, adapter)
    produced = _as_tuple(_path(raw_output, mapping["produced_path"], default=()))
    unresolved = _as_tuple(_path(raw_output, mapping["unresolved_path"], default=()))
    _validate_implementation_semantics(adapter.capability, status, sections, evidence, normalized_files)
    provider_result = ProviderResult(
        capability=adapter.capability,
        status=status,
        sections=sections,
        evidence=evidence,
        files=normalized_files,
        changed_paths=tuple(normalized_files),
        produced=produced,
        unresolved=unresolved,
        payload=raw_output,
        provider=adapter.provider_id,
        base_revision=base_revision,
    )
    return AdaptedProviderResult(provider_result, normalize_result(provider_result, contract))


def adapted_result_document(result: AdaptedProviderResult) -> dict[str, Any]:
    """Return a stable JSON-serializable trace representation."""
    def view(value: ProviderResult | ContractResult) -> dict[str, Any]:
        return {
            "capability": value.capability,
            "status": value.status,
            "sections": _thaw(value.sections),
            "evidence": _thaw(value.evidence),
            "files": _thaw(value.files),
            "changed_paths": list(value.changed_paths),
            "produced": list(value.produced),
            "unresolved": list(value.unresolved),
            "provider": value.provider,
            "base_revision": value.base_revision,
        }
    return {"provider_result": view(result.provider_result), "contract_result": view(result.contract_result)}


def _validate_adapter_contract(document: Mapping[str, Any], contract: CapabilityContract, root: Path) -> None:
    dispatch = document["dispatch"]
    if tuple(dispatch["writable_scope"]) != tuple(contract.dispatch["writable_scope"]):
        raise ValidationError("provider adapter writable scope must exactly match its capability contract")
    if tuple(dispatch["readable_scope"]) != tuple(contract.dispatch["readable_scope"]):
        raise ValidationError("provider adapter readable scope must exactly match its capability contract")
    if dispatch["task_template"] != contract.dispatch["task_template"]:
        raise ValidationError("provider adapter task template must match its capability contract")
    if not (root / dispatch["task_template"]).is_file():
        raise ValidationError(f"provider task template is missing: {dispatch['task_template']}")
    raw_schema_path = root / document["raw_output_schema"]
    try:
        raw_schema = json.loads(raw_schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(raw_schema)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SchemaError) as error:
        raise ValidationError(f"provider raw output schema is unavailable or invalid: {raw_schema_path}") from error
    mapped_sections = set(document["result_mapping"]["sections"])
    if not set(contract.result["required_sections"]).issubset(mapped_sections):
        raise ValidationError("provider adapter does not map every required result section")
    mapped_evidence = set(document["result_mapping"]["evidence"])
    if not set(contract.result["required_evidence"]).issubset(mapped_evidence):
        raise ValidationError("provider adapter does not map every required evidence field")


def _validate_raw_output(adapter: ProviderAdapter, raw_output: Mapping[str, Any], root: Path) -> None:
    try:
        schema = json.loads((root / adapter.raw_output_schema).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"provider raw output schema is unavailable: {adapter.raw_output_schema}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(_thaw(raw_output)),
        key=lambda error: (list(error.path), error.message),
    )
    if errors:
        error = errors[0]
        pointer = ".".join(str(part) for part in error.path) or "$"
        raise ValidationError(f"provider raw output schema violation at {pointer}: {error.message}")


def _validate_evidence(name: str, value: Any, rule: Mapping[str, Any]) -> None:
    if value is _MISSING or value in (None, "", (), [], {}):
        raise ValidationError(f"provider evidence is missing: {name}")
    expected = rule["type"]
    valid = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, (list, tuple)),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
    }[expected]
    if not valid:
        raise ValidationError(f"provider evidence is malformed: {name} must be {expected}")
    if isinstance(value, Mapping):
        missing = [field for field in rule.get("required_fields", ()) if value.get(field) in (None, "", (), [], {})]
        if missing:
            raise ValidationError(f"provider evidence is malformed: {name} misses {', '.join(missing)}")
        for digest_field, content_field in rule.get("sha256_fields", {}).items():
            content = value.get(content_field)
            if not isinstance(content, str):
                raise ValidationError(
                    f"provider evidence is malformed: {name}.{content_field} must be text"
                )
            expected = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            if value.get(digest_field) != expected:
                raise ValidationError(
                    f"provider evidence hash is invalid: {name}.{digest_field}"
                )
    if isinstance(value, (list, tuple)):
        if any(not isinstance(item, Mapping) for item in value):
            raise ValidationError(f"provider evidence is malformed: {name} items must be objects")
        required = tuple(rule.get("item_required_fields", ()))
        for item in value:
            missing = [field for field in required if item.get(field) in (None, "", (), [], {})]
            if missing:
                raise ValidationError(f"provider evidence is malformed: {name} item misses {', '.join(missing)}")


def _validate_changed_paths(files: Mapping[str, Any], adapter: ProviderAdapter) -> None:
    scopes = tuple(str(value).replace("\\", "/").strip("/").casefold() for value in adapter.dispatch["writable_scope"])
    governance = adapter.dispatch["governance"]
    protected = tuple(str(value).replace("\\", "/").strip("/").casefold() for value in governance.get("protected_paths", ()))
    for raw_path in files:
        normalized = str(raw_path).replace("\\", "/")
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or not normalized or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValidationError(f"provider changed path is outside writable scope: {raw_path}")
        folded = "/".join(pure.parts).casefold()
        if any(folded == value or folded.startswith(value + "/") for value in protected):
            raise ValidationError(f"provider changed path targets a protected project path: {raw_path}")
        if not any(folded == scope or folded.startswith(scope + "/") for scope in scopes):
            raise ValidationError(f"provider changed path is outside writable scope: {raw_path}")


def _validate_implementation_semantics(
    capability: str,
    status: str,
    sections: Mapping[str, Any],
    evidence: Mapping[str, Any],
    files: Mapping[str, Any],
) -> None:
    """Fail closed on implementation quality claims that shape alone cannot prove."""
    if status != "completed":
        return
    text = lambda value: str(value or "").casefold()
    if capability == "implementation.tdd":
        failing = text(evidence.get("failing-test"))
        passing = text(evidence.get("passing-test"))
        green = text(sections.get("green"))
        if "command:" not in failing or "output:" not in failing:
            raise ValidationError("TDD RED evidence must include the failing command and output")
        if not re.search(r"\b(fail(?:ed|ure)?|error)\b", failing):
            raise ValidationError("TDD RED evidence must record an actual test failure")
        if "command:" not in passing or "exit_code:0" not in passing or not re.search(r"\bpass(?:ed)?\b", passing) or "pass" not in green:
            raise ValidationError("TDD GREEN evidence must include a passing command with exit_code:0")
    elif capability == "implementation.execute":
        if not files:
            raise ValidationError("completed integration must include changed paths")
        if "exit_code:0" not in text(evidence.get("execution")) or "exit_code:0" not in text(evidence.get("integration")):
            raise ValidationError("integration execution evidence must report exit_code:0")
    elif capability == "quality.code-review":
        if text(sections.get("decision")) != "approved":
            raise ValidationError("code review must be approved before checkpoint")
        findings = text(sections.get("findings")).strip(" .;")
        if not re.fullmatch(r"(?:no blocking findings|blocking findings?\s*[:=]\s*(?:none|0|false)|(?:none|0|false))", findings):
            raise ValidationError("code review contains blocking findings")
    elif capability == "quality.verify":
        for name in ("tests", "build"):
            if "exit_code:0" not in text(evidence.get(name)):
                raise ValidationError(f"verification evidence {name} must report exit_code:0")
        if text(sections.get("result")).strip() not in {"passed", "pass", "success", "successful"}:
            raise ValidationError("verification result must be successful")
        if text(evidence.get("security")).strip() not in {"passed", "pass", "ok", "success", "successful"}:
            raise ValidationError("verification security evidence must be passed")


def _path(value: Mapping[str, Any], dotted: str, *, default: Any = _MISSING) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            if default is _MISSING:
                raise ValidationError(f"provider raw output is missing mapped field: {dotted}")
            return default
        current = current[part]
    return current


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    raise ValidationError("provider produced/unresolved values must be arrays")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_thaw(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


__all__ = (
    "AdaptedProviderResult", "ProviderAdapter", "adapted_result_document",
    "build_dispatch", "load_provider_adapter", "normalize_provider_output",
)
