"""Backend-independent target/resource architecture records.

The records deliberately contain no vendor-specific enum.  Concrete resource
names and physical bindings arrive from parsed ZLang standard-library source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256

from zlang.ir.expressions import Expression
from zlang.ir.scheduled import ScheduledValueGraph


@dataclass(frozen=True)
class ResourcePort:
    name: str
    direction: str
    signedness: str
    width: int


@dataclass(frozen=True)
class ResourceRegisterSite:
    name: str
    minimum: int
    maximum: int


@dataclass(frozen=True)
class PipelineSite:
    name: str
    semantic_location: str
    latency_delta: int
    initiation_interval: int
    resource_local: bool
    estimated_delay_ps: int


@dataclass(frozen=True)
class PipelineConfiguration:
    name: str
    sites: tuple[str, ...]
    latency: int
    initiation_interval: int
    physical_settings: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class PhysicalBinding:
    backend: str
    emitter: str
    primitive: str | None
    pipeline_site_map: tuple[tuple[str, str], ...]
    dedicated_edge_map: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class ResourceConstraint:
    name: str
    relation: str
    value: int | str


@dataclass(frozen=True)
class ResourceInventory:
    resource_definition_identity: str
    available: int


@dataclass(frozen=True)
class TimingEvidence:
    name: str
    value_ps: int
    provenance: str  # estimated, synthesis_measured, or route_measured


@dataclass(frozen=True)
class ResourceDedicatedLink:
    name: str
    source_port: str
    destination_port: str
    width: int
    placement_relation: str
    latency: int = 0
    fabric_fallback: bool = False


@dataclass(frozen=True)
class ResourceDefinition:
    identity: str
    name: str
    source_path: str
    source_hash: str
    dependency_hashes: tuple[tuple[str, str], ...]
    ports: tuple[ResourcePort, ...]
    operation: str
    limits: tuple[tuple[str, int], ...]
    register_sites: tuple[ResourceRegisterSite, ...]
    dedicated_links: tuple[ResourceDedicatedLink, ...]
    backend_bindings: tuple[tuple[str, str], ...]
    resource_class: str = "generic"
    capabilities: tuple[tuple[str, str], ...] = ()
    pipeline_sites: tuple[PipelineSite, ...] = ()
    pipeline_configurations: tuple[PipelineConfiguration, ...] = ()
    physical_bindings: tuple[PhysicalBinding, ...] = ()
    source_origin: object | None = field(default=None, compare=False)

    def limit(self, name: str) -> int:
        try:
            return dict(self.limits)[name]
        except KeyError as error:
            raise ValueError(f"resource '{self.identity}' has no '{name}' limit") from error

    def pipeline_configuration(self, name: str) -> PipelineConfiguration:
        matches = tuple(item for item in self.pipeline_configurations if item.name == name)
        if len(matches) != 1:
            raise ValueError(f"resource '{self.identity}' has no unique pipeline configuration '{name}'")
        return matches[0]


@dataclass(frozen=True)
class TargetFamilyDefinition:
    identity: str
    name: str
    source_path: str
    source_hash: str
    dependency_hashes: tuple[tuple[str, str], ...]
    resource_identities: tuple[str, ...]
    source_origin: object | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TargetInstance:
    identity: str
    name: str
    part: str
    family_identity: str
    source_path: str
    source_hash: str
    dependency_hashes: tuple[tuple[str, str], ...]
    inventory: tuple[tuple[str, int], ...]
    dedicated_capacities: tuple[tuple[str, str, int], ...]
    source_origin: object | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ArchitectureTemplate:
    identity: str
    name: str
    source_path: str
    source_hash: str
    dependency_hashes: tuple[tuple[str, str], ...]
    operation: str
    resource_name: str
    resource_count: int
    latency: int
    initiation_interval: int
    register_configuration: tuple[tuple[str, int], ...]
    pipeline_configuration: str | None = None
    dedicated_link: str | None = None
    source_origin: object | None = field(default=None, compare=False)


@dataclass(frozen=True)
class SemanticPortMapping:
    resource_port: str
    semantic_identity: str
    expression: Expression | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ResourceInstance:
    identity: str
    resource_definition_identity: str
    operation: str
    configuration: tuple[tuple[str, int | str], ...]
    semantic_mappings: tuple[SemanticPortMapping, ...]


@dataclass(frozen=True)
class DedicatedPhysicalEdge:
    identity: str
    kind: str
    source_instance: str
    source_port: str
    destination_instance: str
    destination_port: str
    width: int
    placement_relation: str
    latency: int = 0
    fabric_fallback: bool = False


@dataclass(frozen=True)
class TimingNode:
    identity: str
    kind: str
    implementation_node_identity: str
    semantic_identity: str | None = None
    resource_instance_identity: str | None = None
    pipeline_site_identity: str | None = None
    latency: int = 0
    estimated_delay_ps: int = 0
    source_origin: str | None = field(default=None, compare=False)
    target_identity: str | None = None


@dataclass(frozen=True)
class TimingEdge:
    identity: str
    kind: str
    source_node: str
    destination_node: str
    latency: int = 0
    estimated_delay_ps: int = 0
    dedicated_edge_identity: str | None = None


@dataclass(frozen=True)
class TimingCut:
    identity: str
    node_identity: str
    kind: str
    cycles: int
    resource_instance_identity: str | None = None
    pipeline_site_identity: str | None = None


@dataclass(frozen=True)
class ImplementationDelay:
    identity: str
    kind: str  # alignment or compensation
    source_node: str
    destination_node: str
    cycles: int
    width: int
    ff_cost: int
    semantic_identity: str | None = None
    source_origin: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TimingDAG:
    nodes: tuple[TimingNode, ...]
    edges: tuple[TimingEdge, ...]
    cuts: tuple[TimingCut, ...]
    alignment_delays: tuple[ImplementationDelay, ...] = ()
    compensation_delays: tuple[ImplementationDelay, ...] = ()
    output_latency: int = 0
    estimated_critical_delay_ps: int | None = None

    @property
    def identity(self) -> str:
        # Source provenance is diagnostic metadata, not implementation
        # semantics.  In particular, migrating a region from a compatibility
        # spelling to ``implement`` must not invalidate otherwise identical
        # QoR evidence.  Build the identity from the typed/timing shape and
        # intentionally omit source_origin fields.
        node_identity = tuple(
            (
                item.identity,
                item.kind,
                item.implementation_node_identity,
                item.semantic_identity,
                item.resource_instance_identity,
                item.pipeline_site_identity,
                item.latency,
                item.estimated_delay_ps,
                item.target_identity,
            )
            for item in self.nodes
        )
        delay_identity = tuple(
            (
                item.identity,
                item.kind,
                item.source_node,
                item.destination_node,
                item.cycles,
                item.width,
                item.ff_cost,
                item.semantic_identity,
            )
            for item in (*self.alignment_delays, *self.compensation_delays)
        )
        return sha256(repr((
            node_identity, self.edges, self.cuts, delay_identity,
            self.output_latency,
            self.estimated_critical_delay_ps,
        )).encode()).hexdigest()


@dataclass(frozen=True)
class ImplementationGraph:
    semantic_region_identity: str
    architecture_template_identity: str
    target_identity: str | None
    target_hash: str | None
    resource_definition_hashes: tuple[tuple[str, str], ...]
    resources: tuple[ResourceInstance, ...]
    dedicated_edges: tuple[DedicatedPhysicalEdge, ...]
    latency: int
    initiation_interval: int
    realization_backend: str = "backend_independent"
    latency_knowledge: str = "known"
    quantization: Expression | None = field(default=None, compare=False)
    source_origin: object | None = field(default=None, compare=False)
    legality_evidence: tuple[str, ...] = ()
    architecture_template_hash: str | None = None
    target_family_identity: str | None = None
    target_dependency_hashes: tuple[tuple[str, str], ...] = ()
    architecture_dependency_hashes: tuple[tuple[str, str], ...] = ()
    selection_policy: str = "generic"
    target_part: str | None = None
    pipeline_configuration_identity: str | None = None
    active_pipeline_sites: tuple[str, ...] = ()
    physical_binding_identities: tuple[str, ...] = ()
    timing_dag: TimingDAG | None = None
    policy_requirements: tuple[tuple[str, str, int], ...] = ()
    objective: str = "lut"
    selected_cost: tuple[tuple[str, int | float | None, str], ...] = ()
    evidence_identity: str | None = None
    scheduled_value_graph: ScheduledValueGraph | None = None

    @property
    def identity(self) -> str:
        values = (
            self.semantic_region_identity, self.architecture_template_identity,
            self.target_identity, self.target_hash, self.resource_definition_hashes,
            self.resources, self.dedicated_edges, self.latency,
            self.initiation_interval, self.legality_evidence,
            self.architecture_template_hash, self.target_family_identity,
            self.target_dependency_hashes, self.architecture_dependency_hashes,
            self.selection_policy,
            self.target_part,
            self.pipeline_configuration_identity, self.active_pipeline_sites,
            self.physical_binding_identities,
            self.timing_dag.identity if self.timing_dag is not None else None,
        )
        # Preserve accepted evidence identities for legacy graphs.  The new
        # scheduled graph participates only when it is actually present.
        if self.scheduled_value_graph is not None:
            values = (*values, self.scheduled_value_graph.identity)
        payload = repr(values)
        return sha256(payload.encode()).hexdigest()

    @property
    def is_generic(self) -> bool:
        return not self.resources


__all__ = [
    "ArchitectureTemplate", "DedicatedPhysicalEdge", "ImplementationGraph",
    "PhysicalBinding", "PipelineConfiguration", "PipelineSite",
    "ResourceConstraint", "ResourceInventory", "TimingEvidence",
    "ResourceDedicatedLink", "ResourceDefinition", "ResourceInstance",
    "ResourcePort", "ResourceRegisterSite", "SemanticPortMapping",
    "TargetFamilyDefinition", "TargetInstance", "TimingCut", "TimingDAG",
    "TimingEdge", "TimingNode", "ImplementationDelay",
]
