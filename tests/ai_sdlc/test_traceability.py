"""Contract tests for canonical registries and derived traceability."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.model import Artifact, ProjectSnapshot, Source  # noqa: E402
from ai_sdlc.traceability import (  # noqa: E402
    TraceEdge,
    TraceGraph,
    TraceNode,
    build_trace_graph,
    validate_traceability,
)
from ai_sdlc.validation import validate_snapshot  # noqa: E402
from test_model import snapshot_with  # noqa: E402


def _snapshot(*, artifacts=None, sources=None) -> ProjectSnapshot:
    base = snapshot_with()
    return replace(
        base,
        artifacts=tuple(artifacts if artifacts is not None else base.artifacts),
        sources=tuple(sources if sources is not None else base.sources),
    )


def test_graph_is_derived_from_separate_source_and_artifact_registries() -> None:
    graph = build_trace_graph(snapshot_with())

    assert graph.nodes[0].id == "SRC-001"
    assert graph.nodes[0].category == "source"
    assert graph.nodes[-1].id == "ART-VISION"
    assert graph.nodes[-1].category == "artifact"
    assert isinstance(graph.nodes, tuple)
    assert isinstance(graph.edges, tuple)


def test_duplicate_ids_are_reported_deterministically() -> None:
    snapshot = snapshot_with()
    duplicate = replace(snapshot.artifacts[0], kind="product_goals")
    graph = build_trace_graph(replace(snapshot, artifacts=(snapshot.artifacts[0], duplicate)))

    report = validate_traceability(graph, ("source", "product_vision"))

    assert report.findings[0].code == "duplicate_trace_id"


def test_dangling_trace_links_are_rejected() -> None:
    graph = TraceGraph(
        nodes=(TraceNode("SRC-001", "source", "business_document"),),
        edges=(TraceEdge("SRC-001", "ART-MISSING", "traces_to"),),
    )

    report = validate_traceability(graph, ("source",))

    assert any(finding.code == "dangling_trace_link" for finding in report.findings)


def test_source_kind_cannot_be_registered_as_an_artifact() -> None:
    artifact = Artifact("ART-BRIEF", "business_document", "brief.md", "draft")
    graph = build_trace_graph(_snapshot(artifacts=(artifact,)))

    report = validate_traceability(graph, ("source",))

    assert any(finding.code == "source_as_artifact" for finding in report.findings)


def test_unknown_kind_is_rejected_even_when_model_was_constructed_directly() -> None:
    artifact = Artifact("ART-UNKNOWN", "new_kind", "unknown.md", "draft")
    graph = build_trace_graph(_snapshot(artifacts=(artifact,)))

    report = validate_traceability(graph, ("source",))

    assert any(finding.code == "unknown_artifact_kind" for finding in report.findings)


def test_unknown_source_kind_is_rejected_even_when_model_was_constructed_directly() -> None:
    source = replace(snapshot_with().sources[0], kind="product_vision")
    graph = build_trace_graph(_snapshot(sources=(source,)))

    report = validate_traceability(graph, ("source",))

    assert any(finding.code == "unknown_source_kind" for finding in report.findings)


def test_unknown_capability_mapping_is_a_validation_finding() -> None:
    report = validate_traceability(TraceGraph(), {"capability": "typo"})

    assert not report.is_valid
    assert any(finding.code == "unknown_capability" for finding in report.findings)


def test_traceability_schema_rejects_unknown_and_cross_registry_kinds() -> None:
    schema_path = SKILL_ROOT / "assets" / "schemas" / "traceability.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    valid = {
        "nodes": [{"id": "SRC-001", "category": "source", "kind": "business_document"}],
        "edges": [],
    }
    source_with_artifact_kind = {
        "nodes": [{"id": "SRC-001", "category": "source", "kind": "product_vision"}],
        "edges": [],
    }
    artifact_with_source_kind = {
        "nodes": [{"id": "ART-001", "category": "artifact", "kind": "business_document"}],
        "edges": [],
    }
    unknown_kind = {
        "nodes": [{"id": "ART-001", "category": "artifact", "kind": "future_kind"}],
        "edges": [],
    }

    assert not list(validator.iter_errors(valid))
    assert list(validator.iter_errors(source_with_artifact_kind))
    assert list(validator.iter_errors(artifact_with_source_kind))
    assert list(validator.iter_errors(unknown_kind))


def test_required_chain_can_fail_before_release_exists() -> None:
    graph = build_trace_graph(snapshot_with())

    report = validate_traceability(
        graph,
        ("source", "product_vision", "functional_requirements", "release_record"),
    )

    assert not report.is_valid
    assert any(finding.code == "incomplete_trace_chain" for finding in report.findings)


def test_capability_specific_early_chain_does_not_require_release() -> None:
    graph = build_trace_graph(snapshot_with())

    report = validate_traceability(graph, "product_definition")

    assert report.is_valid


def test_artifact_paths_are_unique_across_ids() -> None:
    snapshot = snapshot_with()
    duplicate = Artifact("ART-OTHER", "product_goals", snapshot.artifacts[0].path, "draft")

    report = validate_snapshot(replace(snapshot, artifacts=(snapshot.artifacts[0], duplicate)))

    assert not report.is_valid
    assert report.findings[0].code == "duplicate_artifact_path"


def test_artifact_paths_with_distinct_values_are_accepted() -> None:
    snapshot = snapshot_with()
    other = Artifact("ART-OTHER", "product_goals", "goals.md", "draft")

    report = validate_snapshot(replace(snapshot, artifacts=(snapshot.artifacts[0], other)))

    assert report.is_valid


def test_graph_validation_reports_a_valid_release_chain() -> None:
    artifacts = (
        Artifact("ART-VISION", "product_vision", "vision.md", "approved"),
        Artifact("ART-FR", "functional_requirements", "requirements.md", "approved"),
        Artifact("ART-RELEASE", "release_record", "release.md", "approved"),
    )
    graph = build_trace_graph(_snapshot(artifacts=artifacts))

    report = validate_traceability(
        graph,
        ("source", "product_vision", "functional_requirements", "release_record"),
    )

    assert report.is_valid
