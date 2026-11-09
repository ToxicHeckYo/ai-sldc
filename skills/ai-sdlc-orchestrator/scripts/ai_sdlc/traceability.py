"""Deterministic trace graph derived from the canonical project snapshot.

The graph deliberately contains no second source or artifact registry.  Nodes are
materialised from :class:`ProjectSnapshot`; the small mapping below only describes
allowed *kind-to-kind* trace relationships from the development standard.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from .model import ARTIFACT_KINDS, SOURCE_KINDS, Artifact, ProjectSnapshot
from .validation import ValidationFinding, ValidationReport


@dataclass(frozen=True, slots=True)
class TraceNode:
    """A source or project-created artifact in a trace graph."""

    id: str
    category: str
    kind: str
    status: str | None = None
    path: str | None = None

    def __post_init__(self) -> None:
        # Accept the natural ``(id, category, kind)`` form while being forgiving
        # of callers that use ``(id, kind, category)``.
        categories = {"source", "artifact"}
        if self.category not in categories and self.kind in categories:
            category, kind = self.category, self.kind
            object.__setattr__(self, "category", kind)
            object.__setattr__(self, "kind", category)


@dataclass(frozen=True, slots=True)
class TraceEdge:
    """A directed, typed relation between two graph node IDs."""

    source: str
    target: str
    relation: str = "traces_to"

    @property
    def from_id(self) -> str:
        return self.source

    @property
    def to_id(self) -> str:
        return self.target


@dataclass(frozen=True, slots=True, init=False)
class TraceGraph:
    """Immutable graph with deterministic tuple-backed collections.

    ``edges`` and ``links`` are accepted as synonyms to keep the in-memory API
    convenient for YAML integrations, while ``edges`` remains the canonical
    attribute used by the validator.
    """

    nodes: tuple[TraceNode, ...]
    edges: tuple[TraceEdge, ...]

    def __init__(self, nodes: Sequence[TraceNode] = (), edges: Sequence[TraceEdge] | None = None, links: Sequence[TraceEdge] | None = None) -> None:
        if edges is not None and links is not None and tuple(edges) != tuple(links):
            raise ValueError("edges and links must describe the same trace links")
        selected = edges if edges is not None else links
        object.__setattr__(self, "nodes", tuple(nodes))
        object.__setattr__(self, "edges", tuple(selected or ()))

    @property
    def links(self) -> tuple[TraceEdge, ...]:
        return self.edges


# The standard's lifecycle is intentionally represented as mappings rather than
# another registry of records.  Edges are emitted only when both endpoint kinds
# are present in the snapshot.
_KIND_ORDER = (
    "product_vision", "product_goals", "system_scope", "requirements_analysis",
    "domain_model", "roles_and_processes", "entity_lifecycles", "gaps_and_conflicts",
    "assumptions", "open_questions", "business_requirements", "roles_and_access_matrix",
    "functional_requirements", "business_scenario", "business_scenario_catalog",
    "non_functional_requirements", "constraints", "acceptance_criteria", "domain_glossary",
    "technical_glossary", "technical_specification_manifest", "technical_specification_release",
    "c4_context", "c4_container", "adr", "c4_component", "c4_dynamic", "arc42_section",
    "sdd_specification", "architecture_baseline", "test_plan", "test_report",
    "deployment_package", "release_record", "operations_document",
)
_KIND_RANK = {kind: rank for rank, kind in enumerate(_KIND_ORDER)}

# Capability chains are deliberately short at early lifecycle points.  A product
# definition is useful before architecture and release artifacts exist.
CAPABILITY_REQUIRED_CHAINS: dict[str, tuple[str, ...]] = {
    "product_definition": ("source", "product_vision"),
    "requirements": ("source", "requirements_analysis", "functional_requirements", "acceptance_criteria"),
    "architecture": ("source", "functional_requirements", "c4_context", "c4_container", "architecture_baseline"),
    "baseline": ("source", "functional_requirements", "sdd_specification", "architecture_baseline"),
    "release": ("source", "technical_specification_release", "deployment_package", "release_record"),
    "source_to_release": ("source", "product_vision", "functional_requirements", "architecture_baseline", "sdd_specification", "test_report", "release_record"),
}
# Public aliases make the capability vocabulary discoverable without creating a
# second source/artifact registry.
CAPABILITY_CHAINS = CAPABILITY_REQUIRED_CHAINS
REQUIRED_CHAINS = CAPABILITY_REQUIRED_CHAINS


def build_trace_graph(snapshot: ProjectSnapshot) -> TraceGraph:
    """Build an immutable graph from canonical sources and artifacts.

    Source and artifact records remain separate nodes even when malformed input
    reuses an ID or path; validation can then report the defect instead of losing
    evidence by collapsing a dictionary key.
    """
    nodes: list[TraceNode] = [
        TraceNode(source.id, "source", source.kind, source.status, source.location)
        for source in snapshot.sources
    ]
    nodes.extend(
        TraceNode(artifact.id, "artifact", artifact.kind, artifact.status, artifact.path)
        for artifact in snapshot.artifacts
    )
    nodes.sort(key=lambda node: (0 if node.category == "source" else 1, node.id, node.kind, node.path or ""))

    artifacts = [node for node in nodes if node.category == "artifact"]
    present_kinds = {node.kind for node in artifacts}
    edges: list[TraceEdge] = []
    # Connect each artifact to later canonical kinds.  This preserves a path for
    # any explicitly requested chain while not inventing absent intermediate nodes.
    for left in artifacts:
        left_rank = _KIND_RANK.get(left.kind)
        if left_rank is None:
            continue
        for right in artifacts:
            right_rank = _KIND_RANK.get(right.kind)
            if right_rank is not None and right_rank > left_rank:
                edges.append(TraceEdge(left.id, right.id, "traces_to"))

    roots = [
        node for node in artifacts
        if node.kind not in present_kinds.intersection(_KIND_RANK)
        or not any(
            other.kind in _KIND_RANK and _KIND_RANK[other.kind] < _KIND_RANK.get(node.kind, -1)
            for other in artifacts
        )
    ]
    # Unknown kinds are left for validation and get no inferred relation.  Every
    # known root receives provenance from every registered source.
    roots = [node for node in roots if node.kind in _KIND_RANK]
    for source in (node for node in nodes if node.category == "source"):
        edges.extend(TraceEdge(source.id, root.id, "traces_to") for root in roots)

    edges.sort(key=lambda edge: (edge.source, edge.target, edge.relation))
    return TraceGraph(nodes=tuple(nodes), edges=tuple(edges))


def validate_traceability(graph: TraceGraph, required_chain: str | Sequence[str] | Mapping[str, Any] | None = None) -> ValidationReport:
    """Validate graph identity, vocabularies, references, and a required chain."""
    findings: list[ValidationFinding] = []
    seen: set[str] = set()
    source_nodes: list[TraceNode] = []
    artifact_nodes: list[TraceNode] = []

    for index, node in enumerate(graph.nodes):
        path = f"nodes[{index}]"
        if node.id in seen:
            findings.append(_finding("duplicate_trace_id", f"{path}.id", f"duplicate trace ID {node.id!r}"))
        seen.add(node.id)
        if node.category == "source":
            source_nodes.append(node)
            if node.kind not in SOURCE_KINDS:
                findings.append(_finding("unknown_source_kind", f"{path}.kind", f"source kind {node.kind!r} is not controlled by the standard"))
        elif node.category == "artifact":
            artifact_nodes.append(node)
            if node.kind not in ARTIFACT_KINDS:
                findings.append(_finding("unknown_artifact_kind", f"{path}.kind", f"artifact kind {node.kind!r} is not controlled by the standard"))
        else:
            findings.append(_finding("invalid_trace_category", f"{path}.category", "trace node category must be source or artifact"))

    source_ids = {node.id for node in source_nodes}
    source_locations = {node.path for node in source_nodes if node.path}
    for index, node in enumerate(artifact_nodes):
        path = f"nodes[{graph.nodes.index(node)}]"
        if node.kind in SOURCE_KINDS or node.id in source_ids or node.path in source_locations:
            findings.append(_finding("source_as_artifact", path, "source records must remain in the source registry, not the artifact registry"))
    paths: dict[str, str] = {}
    for index, node in enumerate(artifact_nodes):
        if node.path and node.path in paths and paths[node.path] != node.id:
            findings.append(_finding("duplicate_artifact_path", f"nodes[{graph.nodes.index(node)}].path", f"artifact path {node.path!r} is used by multiple IDs"))
        elif node.path:
            paths[node.path] = node.id

    for index, edge in enumerate(graph.edges):
        if edge.source not in seen or edge.target not in seen:
            findings.append(_finding("dangling_trace_link", f"edges[{index}]", f"trace link {edge.source!r} -> {edge.target!r} does not resolve"))
        if edge.source == edge.target:
            findings.append(_finding("self_trace_link", f"edges[{index}]", "trace link cannot point to the same node"))

    if isinstance(required_chain, Mapping) and "capability" in required_chain:
        capability = required_chain["capability"]
        if capability not in CAPABILITY_REQUIRED_CHAINS:
            findings.append(_finding("unknown_capability", "required_chain.capability", f"capability {capability!r} has no controlled required chain"))
    chain = _resolve_chain(required_chain)
    if chain:
        if not _chain_exists(graph, chain):
            missing = next((token for token in chain if not any(_matches(node, token) for node in graph.nodes)), chain[-1])
            findings.append(_finding("incomplete_trace_chain", "required_chain", f"required trace chain is incomplete at {missing!r}"))
    return ValidationReport(tuple(findings))


def _resolve_chain(required_chain: str | Sequence[str] | Mapping[str, Any] | None) -> tuple[str, ...]:
    if required_chain is None:
        return ()
    if isinstance(required_chain, str):
        if required_chain in CAPABILITY_REQUIRED_CHAINS:
            return CAPABILITY_REQUIRED_CHAINS[required_chain]
        return tuple(token.strip() for token in required_chain.replace("→", "->").split("->") if token.strip())
    if isinstance(required_chain, Mapping):
        value = required_chain.get("chain", required_chain.get("required_chain"))
        if value is None and required_chain.get("capability") in CAPABILITY_REQUIRED_CHAINS:
            value = CAPABILITY_REQUIRED_CHAINS[required_chain["capability"]]
        return _resolve_chain(value)
    return tuple(str(token) for token in required_chain)


def _chain_exists(graph: TraceGraph, chain: tuple[str, ...]) -> bool:
    links = {(edge.source, edge.target) for edge in graph.edges}
    candidates = [node for node in graph.nodes if _matches(node, chain[0])]
    for token in chain[1:]:
        # A required capability chain describes ordered provenance, not an
        # obligation that every adjacent pair have a materialized direct edge.
        # Providers may emit only local links (for example source -> analysis
        # and analysis -> formal requirements), while the canonical graph also
        # derives later-kind edges.  Walk transitively to the next required
        # node so the chain remains valid without inventing provider evidence.
        next_candidates: list[TraceNode] = []
        pending = list(candidates)
        visited = {node.id for node in candidates}
        while pending:
            previous = pending.pop()
            for node in graph.nodes:
                if (previous.id, node.id) not in links or node.id in visited:
                    continue
                visited.add(node.id)
                if _matches(node, token):
                    next_candidates.append(node)
                else:
                    pending.append(node)
        if not next_candidates:
            return False
        candidates = next_candidates
    return bool(candidates)


def _matches(node: TraceNode, token: str) -> bool:
    return token == node.id or token == node.kind or token == node.category


def _finding(code: str, path: str, message: str) -> ValidationFinding:
    return ValidationFinding(code=code, path=path, message=message)
