"""Evidence and classification invariants for the external provider catalog."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "skills" / "ai-sdlc-orchestrator" / "references" / "providers" / "catalog.yaml"
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CLASSIFICATIONS = {"preferred", "alternative", "conditional", "rejected"}
ACCEPTABLE_LICENSES = {"Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MIT", "MPL-2.0"}
REQUIRED_SHORTLIST = {
    "requirements-analysis",
    "create-prd",
    "anthropic-architecture",
    "wondelai-system-design",
    "superpowers-using-superpowers",
    "c4-modeling",
    "microsoft-code-review",
    "telemetry-foundations",
    "frontend-standard",
    "security-threat-modeling",
    "api-design",
    "data-design",
    "deployment-delivery",
    "operations-reliability",
}


def _catalog() -> dict[str, dict[str, object]]:
    document = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    return document["providers"]


def test_evaluated_external_entries_have_provenance_license_audit_and_controls() -> None:
    """A catalog recommendation is reviewable without trusting prose elsewhere."""
    providers = _catalog()
    evaluated = {
        provider_id: entry
        for provider_id, entry in providers.items()
        if entry.get("kind") == "external" and entry.get("classification")
    }

    assert evaluated
    for provider_id, entry in evaluated.items():
        assert entry["classification"] in CLASSIFICATIONS, provider_id
        assert entry.get("evaluated_at") == "2026-11-04", provider_id
        assert isinstance(entry.get("source"), str) and entry["source"], provider_id
        assert isinstance(entry.get("revision"), str) and entry["revision"], provider_id
        assert entry.get("source_files"), provider_id
        assert entry.get("license"), provider_id
        assert entry.get("license_evidence"), provider_id
        assert entry.get("audit_evidence"), provider_id
        assert entry.get("compatibility"), provider_id
        assert entry.get("capabilities"), provider_id
        assert entry.get("security_reasoning"), provider_id
        assert entry.get("supply_chain_reasoning"), provider_id


def test_preferred_external_entries_do_not_use_floating_revisions() -> None:
    """Preferred providers must be reproducible and cannot follow a branch."""
    providers = _catalog()
    for provider_id, entry in providers.items():
        if entry.get("kind") != "external" or entry.get("classification") != "preferred":
            continue
        source = str(entry["source"])
        revision = str(entry["revision"])
        if source.startswith("https://github.com/"):
            assert SHA.fullmatch(revision), f"{provider_id} uses non-immutable revision {revision!r}"
        assert revision.casefold() not in {"main", "master", "head", "latest", "trunk"}, provider_id


def _preferred_metadata_violations(provider_id: str, entry: dict[str, Any]) -> list[str]:
    """Return review findings for metadata that is required of preferred sources."""
    violations: list[str] = []
    source = entry.get("source")
    revision = entry.get("revision")
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        violations.append("revision must be an immutable 40-hex commit")
    if not isinstance(source, str) or not isinstance(revision, str) or f"/{revision}/" not in source:
        violations.append("source URL must contain the declared revision")

    license_name = entry.get("license")
    license_evidence = entry.get("license_evidence")
    if license_name not in ACCEPTABLE_LICENSES:
        violations.append("license must be known and acceptable")
    if not isinstance(license_evidence, str) or not license_evidence.strip() or "unknown" in license_evidence.casefold():
        violations.append("license evidence must be present and non-placeholder")
    elif isinstance(revision, str) and revision not in license_evidence:
        violations.append("license evidence must identify the inspected revision")

    source_files = entry.get("source_files")
    if not isinstance(source_files, list) or not source_files:
        violations.append("preferred source files must be listed")
    else:
        for index, source_file in enumerate(source_files):
            if not isinstance(source_file, dict):
                violations.append(f"source file {index} must be an object")
                continue
            path = source_file.get("path")
            url = source_file.get("url")
            digest = source_file.get("sha256")
            if not isinstance(path, str) or not path:
                violations.append(f"source file {index} path is missing")
            if not isinstance(digest, str) or not SHA256.fullmatch(digest):
                violations.append(f"source file {index} sha256 must be exactly 64 hex characters")
            if not isinstance(url, str) or not isinstance(revision, str) or f"/{revision}/" not in url:
                violations.append(f"source file {index} URL must contain the declared revision")
            elif isinstance(path, str) and not url.rstrip("/").endswith("/" + path.replace("\\", "/")):
                violations.append(f"source file {index} URL must end with its inspected path")

    audit_evidence = entry.get("audit_evidence")
    if not isinstance(audit_evidence, list) or not audit_evidence or not all(isinstance(item, str) and item.strip() for item in audit_evidence):
        violations.append("audit evidence must contain non-empty strings")
    else:
        audit_text = " ".join(audit_evidence).casefold()
        placeholders = ("unknown", "placeholder", "not independently", "not verifiable", "no public audit")
        if any(marker in audit_text for marker in placeholders):
            violations.append("audit evidence must not be placeholder or unknown text")
        source_reviews = [item.casefold() for item in audit_evidence if "source review:" in item.casefold()]
        if not source_reviews:
            violations.append("audit evidence must include source-review evidence")
        elif not isinstance(revision, str) or not any("inspected" in item and revision in item for item in source_reviews):
            violations.append("source-review evidence must identify the inspected revision")
        elif not isinstance(source_files, list) or not any(
            isinstance(source_file, dict)
            and isinstance(source_file.get("path"), str)
            and any(source_file["path"].casefold() in item for item in source_reviews)
            for source_file in source_files
        ):
            violations.append("source-review evidence must identify an inspected file")

    return [f"{provider_id}: {violation}" for violation in violations]


@pytest.mark.parametrize("provider_id", [
    "superpowers-using-superpowers-pinned",
    "superpowers-brainstorming-pinned",
    "superpowers-writing-plans",
    "superpowers-test-driven-development",
])
def test_preferred_external_entries_have_verified_metadata(provider_id: str) -> None:
    """Preferred entries require inspectable provenance rather than truthy fields."""
    entry = _catalog()[provider_id]
    violations = _preferred_metadata_violations(provider_id, entry)
    assert not violations, "preferred metadata violations: " + "; ".join(violations)


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        (lambda entry: entry.update(license="unknown"), "license must be known"),
        (lambda entry: entry.update(license_evidence="unknown"), "license evidence"),
        (lambda entry: entry["source_files"][0].update(sha256="0" * 63), "sha256"),
        (lambda entry: entry["source_files"][0].update(url=entry["source_files"][0]["url"].replace(entry["revision"], "main")), "URL must contain"),
        (lambda entry: entry["source_files"][0].update(url=entry["source_files"][0]["url"].rsplit("/", 1)[0] + "/other.md"), "URL must end"),
        (lambda entry: entry.update(audit_evidence=["placeholder audit evidence"]), "audit evidence must not"),
        (lambda entry: entry.update(audit_evidence=["https://audit.example/review (pass)"]), "source-review evidence"),
        (lambda entry: entry.update(audit_evidence=["Source review: pass"]), "source-review evidence must identify"),
    ],
    ids=["unknown-license", "missing-license-evidence", "short-file-hash", "unbound-revision", "unbound-path", "placeholder-audit", "missing-source-review", "weak-source-review"],
)
def test_preferred_metadata_mutations_are_rejected(mutation: Any, expected_fragment: str) -> None:
    """Each provenance mutation is rejected instead of passing truthiness checks."""
    entry = deepcopy(_catalog()["superpowers-using-superpowers-pinned"])
    mutation(entry)
    violations = _preferred_metadata_violations("mutated-preferred", entry)
    assert any(expected_fragment in violation for violation in violations), violations


def test_required_shortlist_is_classified() -> None:
    """The named shortlist and each capability gap have an explicit decision."""
    providers = _catalog()
    missing = sorted(REQUIRED_SHORTLIST.difference(providers))
    assert not missing, f"missing catalog decisions: {missing}"
    for provider_id in REQUIRED_SHORTLIST:
        entry = providers[provider_id]
        assert entry.get("classification") in CLASSIFICATIONS, provider_id
        assert entry.get("capabilities"), provider_id
