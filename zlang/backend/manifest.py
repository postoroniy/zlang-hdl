"""Backend-published, artifact-bound signal manifests (M37)."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Any

from zlang.backend.companions import CompanionArtifact
from zlang.backend.manifest_codec import (
    binding_from_data,
    binding_to_data,
    canonical_json,
    decode_artifact_payload,
    origin_from_data as _origin_from_data,
    origin_to_data as _origin_to_data,
    validate_artifact_links,
)
from zlang.ir.equivalence import BindingMap, BindingSide, EquivalenceBinding, SignalRole, signedness
from zlang.ir.formal_observations import request_response_observation_id
from zlang.ir.physical_types import physical_width
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
    clock_domain_contract_identity,
)
from zlang.ir.module import Module, PortDirection
from zlang.ir.module import default_selected_ir_identity, dependency_context_identity
from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
from zlang.common import stable_digest
from zlang.ir.timing import (
    InstanceOutputTiming,
    ModuleTimingContract,
    OutputTiming,
    TimingKnowledge,
    ValueTiming,
)
from zlang.ir.types import HardwareType, TaggedUnionType
from zlang.source import SourceOrigin

MANIFEST_VERSION = 2
TOP_AGGREGATE_MANIFEST_VERSION = 3
RECURSIVE_MANIFEST_VERSION = 4
IMPLEMENTATION_MANIFEST_VERSION = 7
COMPANION_MANIFEST_VERSION = 5
TIMING_MANIFEST_VERSION = 7
DEPENDENCY_MANIFEST_VERSION = 8
MODULE_SIGNATURE_MANIFEST_VERSION = 9
PHYSICAL_DOMAIN_MANIFEST_VERSION = 10


@dataclass(frozen=True)
class PhysicalDomainManifest:
    """One exact typed clock/reset contract bound to public RTL ports.

    ``identity`` is backend independent: generated names and source provenance
    do not change the semantic physical-domain contract.  The enclosing
    BackendArtifact build identity additionally includes the concrete RTL
    module and paths so stale constraints cannot be reused after ABI changes.
    """

    identity: str
    clock: str
    reset: str
    rtl_module: str
    rtl_clock_path: str
    rtl_reset_path: str
    clock_edge: str
    reset_mode: str
    reset_polarity: str
    reset_release_mode: str
    reset_release_cycles: int
    power_up: str
    source_origin: SourceOrigin | str | None = None

    @property
    def contract_data(self) -> dict[str, object]:
        return {
            "schema": "zlang-physical-domain-contract-v1",
            "clock": self.clock,
            "reset": self.reset,
            "clock_edge": self.clock_edge,
            "reset_mode": self.reset_mode,
            "reset_polarity": self.reset_polarity,
            "reset_release_mode": self.reset_release_mode,
            "reset_release_cycles": self.reset_release_cycles,
            "power_up": self.power_up,
        }

    @property
    def build_data(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "rtl_module": self.rtl_module,
            "rtl_clock_path": self.rtl_clock_path,
            "rtl_reset_path": self.rtl_reset_path,
        }

    @classmethod
    def publish(
        cls,
        domain: ClockDomain,
        *,
        rtl_module: str,
        rtl_clock_path: str,
        rtl_reset_path: str,
    ) -> "PhysicalDomainManifest":
        result = cls(
            clock_domain_contract_identity(domain),
            domain.clock,
            domain.reset,
            rtl_module,
            rtl_clock_path,
            rtl_reset_path,
            domain.edge.value,
            domain.reset_mode.value,
            domain.reset_polarity.value,
            domain.reset_release_mode.value,
            domain.reset_release_cycles,
            domain.power_up.value,
            domain.source_origin,
        )
        result.validate()
        return result

    @classmethod
    def from_data(cls, data: object) -> "PhysicalDomainManifest":
        if not isinstance(data, dict):
            raise ValueError("backend physical domain must be an object")
        fields = {
            "schema", "identity", "clock", "reset", "rtl_module",
            "rtl_clock_path", "rtl_reset_path", "clock_edge", "reset_mode",
            "reset_polarity", "reset_release_mode", "reset_release_cycles",
            "power_up", "source_origin",
        }
        unknown = set(data) - fields
        if unknown:
            raise ValueError(
                "backend physical domain contains unsupported field(s): "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        required = fields - {"source_origin"}
        missing = required - set(data)
        if missing:
            raise ValueError(
                "backend physical domain is missing required field(s): "
                + ", ".join(sorted(missing))
            )
        if data.get("schema") != "zlang-physical-domain-manifest-v1":
            raise ValueError("unsupported backend physical domain schema")
        string_fields = (
            "identity", "clock", "reset", "rtl_module", "rtl_clock_path",
            "rtl_reset_path", "clock_edge", "reset_mode", "reset_polarity",
            "reset_release_mode", "power_up",
        )
        for field in string_fields:
            value = data[field]
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"backend physical domain {field} must be a non-empty string"
                )
        cycles = data["reset_release_cycles"]
        if isinstance(cycles, bool) or not isinstance(cycles, int):
            raise ValueError(
                "backend physical domain reset_release_cycles must be an integer"
            )
        result = cls(
            data["identity"],
            data["clock"],
            data["reset"],
            data["rtl_module"],
            data["rtl_clock_path"],
            data["rtl_reset_path"],
            data["clock_edge"],
            data["reset_mode"],
            data["reset_polarity"],
            data["reset_release_mode"],
            cycles,
            data["power_up"],
            _origin_from_data(data.get("source_origin")),
        )
        result.validate()
        return result

    def to_data(self) -> dict[str, object]:
        return {
            "schema": "zlang-physical-domain-manifest-v1",
            "identity": self.identity,
            "clock": self.clock,
            "reset": self.reset,
            "rtl_module": self.rtl_module,
            "rtl_clock_path": self.rtl_clock_path,
            "rtl_reset_path": self.rtl_reset_path,
            "clock_edge": self.clock_edge,
            "reset_mode": self.reset_mode,
            "reset_polarity": self.reset_polarity,
            "reset_release_mode": self.reset_release_mode,
            "reset_release_cycles": self.reset_release_cycles,
            "power_up": self.power_up,
            "source_origin": _origin_to_data(self.source_origin),
        }

    def validate(self) -> None:
        for field, value in (
            ("clock", self.clock),
            ("reset", self.reset),
            ("rtl_module", self.rtl_module),
            ("rtl_clock_path", self.rtl_clock_path),
            ("rtl_reset_path", self.rtl_reset_path),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"backend physical domain {field} must be a non-empty string"
                )
        try:
            domain = ClockDomain(
                self.clock,
                self.reset,
                ClockEdge(self.clock_edge),
                ResetMode(self.reset_mode),
                ResetPolarity(self.reset_polarity),
                PowerUpPolicy(self.power_up),
                reset_release_mode=ResetReleaseMode(self.reset_release_mode),
                reset_release_cycles=self.reset_release_cycles,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"backend physical domain contract is invalid: {error}"
            ) from error
        if self.identity != clock_domain_contract_identity(domain):
            raise ValueError(
                "backend physical domain identity does not match its contract"
            )


@dataclass(frozen=True)
class ModuleSignatureManifest:
    """Stable publication form of an applied named module interface.

    Keeping the canonical JSON payload here avoids reconstructing semantic type
    objects when reading a backend manifest.  The two identities are checked
    against that payload at both publication and deserialization boundaries.
    """

    nominal_identity: str
    applied_identity: str
    signature_json: str

    @property
    def signature_data(self) -> dict[str, object]:
        data = json.loads(self.signature_json)
        if not isinstance(data, dict):
            raise ValueError("backend module signature data must be an object")
        return data

    @classmethod
    def publish(cls, signature: object) -> "ModuleSignatureManifest":
        data = signature.to_data()
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
        result = cls(
            nominal_identity=str(signature.nominal_identity),
            applied_identity=str(signature.identity),
            signature_json=encoded,
        )
        result.validate()
        return result

    @classmethod
    def from_data(cls, data: object) -> "ModuleSignatureManifest":
        if not isinstance(data, dict):
            raise ValueError("backend module_signature must be an object")
        if data.get("schema") != "zlang-backend-module-signature-v1":
            raise ValueError("unsupported backend module signature schema")
        signature = data.get("signature")
        if not isinstance(signature, dict):
            raise ValueError("backend module signature payload must be an object")
        result = cls(
            nominal_identity=str(data.get("nominal_identity", "")),
            applied_identity=str(data.get("applied_identity", "")),
            signature_json=json.dumps(
                signature, sort_keys=True, separators=(",", ":")
            ),
        )
        result.validate()
        return result

    def to_data(self) -> dict[str, object]:
        return {
            "schema": "zlang-backend-module-signature-v1",
            "nominal_identity": self.nominal_identity,
            "applied_identity": self.applied_identity,
            "signature": self.signature_data,
        }

    def validate(self) -> None:
        data = self.signature_data
        if data.get("schema") != "zlang-module-signature-v1":
            raise ValueError("unsupported canonical module signature schema")
        declaration_identity = data.get("declaration_identity")
        if not isinstance(declaration_identity, str) or not declaration_identity:
            raise ValueError(
                "backend module signature declaration identity is missing"
            )
        expected_nominal = stable_digest(
            {
                "schema": "zlang-module-interface-v1",
                "declaration_identity": declaration_identity,
            }
        )
        if self.nominal_identity != expected_nominal:
            raise ValueError(
                "backend module signature nominal identity does not match its contents"
            )
        if self.applied_identity != stable_digest(data):
            raise ValueError(
                "backend module signature applied identity does not match its contents"
            )


@dataclass(frozen=True)
class ComponentManifest:
    identity: str
    module_name: str
    source_identity: str
    source_hash: str
    specialization_identity: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    semantic_state: tuple[str, ...] = ()
    children: tuple[str, ...] = ()
    clock_domain: str | None = None
    reset_domain: str | None = None
    aggregate_leaves: tuple[str, ...] = ()
    formal_observations: tuple[str, ...] = ()
    physical_domain_identity: str | None = None


@dataclass(frozen=True)
class InstanceManifest:
    instance_identity: str
    physical_instance_path: tuple[str, ...]
    source_instance_name: str
    source_instance_index: int | None
    module_definition_identity: str
    module_name: str
    source_identity: str
    source_hash: str
    specialization_identity: str
    clock_domain: str | None
    reset_domain: str | None
    source_origin: SourceOrigin | str | None
    component_identity: str
    children: tuple[str, ...] = ()
    physical_domain_identity: str | None = None


@dataclass(frozen=True)
class RecursiveBindingManifest:
    semantic_binding_id: str
    instance_identity: str
    local_semantic_id: str
    specialization_identity: str
    physical_instance_path: tuple[str, ...]
    object_kind: str
    canonical_type: str
    width: int
    signedness: str
    direction: str
    clock_domain: str | None
    reset_domain: str | None
    ownership: str | None
    source_origin: SourceOrigin | str | None
    aggregate_endpoint_id: str | None
    member_path: tuple[str, ...]
    leaf_semantic_id: str | None
    backend: str
    artifact_hash: str
    rtl_module: str | None = None
    rtl_path: tuple[str, ...] = ()
    signal_token: str | None = None
    formal_observation_token: str | None = None
    physical_available: bool = False
    implementation_state_id: str | None = None


@dataclass(frozen=True)
class FormalObservationManifest:
    semantic_binding_id: str
    observation_token: str | None
    width: int
    canonical_type: str
    clock_domain: str | None
    reset_domain: str | None
    artifact_hash: str
    physical_available: bool = False


@dataclass(frozen=True)
class ImplementationResourceManifest:
    identity: str
    definition_identity: str
    operation: str
    configuration: tuple[tuple[str, int | str], ...]
    semantic_mappings: tuple[tuple[str, str], ...]
    source_origin: SourceOrigin | str | None = None


@dataclass(frozen=True)
class ImplementationEdgeManifest:
    identity: str
    kind: str
    source_instance: str
    source_port: str
    destination_instance: str
    destination_port: str
    width: int
    placement_relation: str
    latency: int
    fabric_fallback: bool = False


@dataclass(frozen=True)
class ImplementationManifest:
    graph_identity: str
    semantic_region_identity: str
    architecture_template_identity: str
    target_identity: str | None
    target_hash: str | None
    resource_definition_hashes: tuple[tuple[str, str], ...]
    resources: tuple[ImplementationResourceManifest, ...]
    dedicated_edges: tuple[ImplementationEdgeManifest, ...]
    latency: int
    initiation_interval: int
    intended_resource_counts: tuple[tuple[str, int], ...]
    implementation_artifact_hash: str
    target_family_identity: str | None = None
    architecture_template_hash: str | None = None
    target_dependency_hashes: tuple[tuple[str, str], ...] = ()
    architecture_dependency_hashes: tuple[tuple[str, str], ...] = ()
    selection_policy: str = "generic"
    target_part: str | None = None
    pipeline_configuration_identity: str | None = None
    active_pipeline_sites: tuple[str, ...] = ()
    physical_binding_identities: tuple[str, ...] = ()
    emitted_resource_counts: tuple[tuple[str, int], ...] = ()
    measured_resource_counts: tuple[tuple[str, int], ...] = ()
    timing_evidence: tuple[tuple[str, str], ...] = ()
    policy_requirements: tuple[tuple[str, str, int], ...] = ()
    objective: str = "lut"
    selected_cost: tuple[tuple[str, int | float | None, str], ...] = ()
    timing_dag_identity: str | None = None
    timing_nodes: tuple[tuple[str, str, str, int, int], ...] = ()
    timing_edges: tuple[tuple[str, str, str, str, int, int, str | None], ...] = ()
    timing_cuts: tuple[tuple[str, str, str, int, str | None, str | None], ...] = ()
    alignment_delays: tuple[tuple[str, str, str, int, int, int], ...] = ()
    compensation_delays: tuple[tuple[str, str, str, int, int, int], ...] = ()
    evidence_identity: str | None = None
    realization_backend: str = "backend_independent"
    latency_knowledge: str = "known"

def _width(type_: HardwareType) -> int:
    return physical_width(type_)


def _canonical_type(type_: HardwareType) -> str:
    """Render a manifest type without erasing nominal enum identity."""
    # EnumType.__str__ includes the declaration identity and ordered members;
    # using the ordinary canonical spelling here retains both nominal identity
    # and the complete packed-domain definition in artifact hashes.
    if isinstance(type_, TaggedUnionType):
        variants = "|".join(
            variant.name
            + (
                "(" + ",".join(
                    f"{field.name}:{field.type}" for field in variant.fields
                ) + ")"
                if variant.fields else ""
            )
            for variant in type_.variants
        )
        return (
            f"union<{type_.name}:{variants}@{type_.declaration_identity}>"
        )
    return str(type_)


def _timing_record_to_data(port: str, timing: ValueTiming) -> dict[str, object]:
    return {
        "port": port,
        "knowledge": timing.knowledge.value,
        "latency": timing.latency,
        "reason": timing.reason,
    }


def _timing_record_from_data(data: dict[str, object]) -> ValueTiming:
    knowledge = TimingKnowledge(str(data["knowledge"]))
    if knowledge is TimingKnowledge.TIMELESS:
        return ValueTiming.timeless()
    if knowledge is TimingKnowledge.KNOWN:
        return ValueTiming.known(int(data["latency"]))
    return ValueTiming.unknown(str(data["reason"]))

@dataclass(frozen=True)
class BackendArtifact:
    backend: str
    module: str
    selected_ir_identity: str
    artifact_hash: str
    text: str
    bindings: tuple[EquivalenceBinding, ...]
    manifest_version: int = MANIFEST_VERSION
    components: tuple[ComponentManifest, ...] = ()
    instances: tuple[InstanceManifest, ...] = ()
    recursive_bindings: tuple[RecursiveBindingManifest, ...] = ()
    formal_observations: tuple[FormalObservationManifest, ...] = ()
    formal_artifact_hash: str | None = None
    library_dependencies: tuple[tuple[str, str], ...] = ()
    implementation: ImplementationManifest | None = None
    companions: tuple[CompanionArtifact, ...] = ()
    timing_contract: ModuleTimingContract | None = None
    output_timings: tuple[OutputTiming, ...] = ()
    instance_output_timings: tuple[InstanceOutputTiming, ...] = ()
    root_module_identity: DependencyModuleIdentity | None = None
    dependency_closure: DependencyClosure | None = None
    module_signature: ModuleSignatureManifest | None = None
    physical_domains: tuple[PhysicalDomainManifest, ...] = ()

    def binding_map(self) -> BindingMap:
        return BindingMap(self.bindings, self.manifest_version)

    @property
    def build_identity(self) -> str:
        """Identity of emitted RTL in its exact locked semantic context."""

        payload: dict[str, object] = {
            "schema": "zlang-backend-build-v1",
            "backend": self.backend,
            "module": self.module,
            "selected_ir_identity": self.selected_ir_identity,
            "artifact_hash": self.artifact_hash,
        }
        dependency_identity = dependency_context_identity(self)
        if dependency_identity is not None:
            payload["dependency_identity"] = dependency_identity
        if self.module_signature is not None:
            payload["module_signature_identity"] = (
                self.module_signature.applied_identity
            )
        if self.physical_domains:
            payload["physical_domains"] = [
                item.build_data
                for item in sorted(
                    self.physical_domains, key=lambda value: value.identity
                )
            ]
        return stable_digest(payload)

    def to_json(self) -> str:
        validate_artifact_links(self)
        entries = [binding_to_data(item) for item in self.bindings]
        payload: dict[str, Any] = {"manifest_version": self.manifest_version,
                           "backend": self.backend, "module": self.module,
                           "selected_ir_identity": self.selected_ir_identity,
                           "artifact_hash": self.artifact_hash,
                           "build_identity": self.build_identity,
                           "library_dependencies": [list(item) for item in self.library_dependencies],
                           "bindings": entries}
        if self.root_module_identity is not None:
            payload["root_module_identity"] = self.root_module_identity.to_data()
        if self.dependency_closure is not None:
            payload["dependency_closure"] = self.dependency_closure.to_data()
        if self.module_signature is not None:
            self.module_signature.validate()
            payload["module_signature"] = self.module_signature.to_data()
        if self.manifest_version >= PHYSICAL_DOMAIN_MANIFEST_VERSION:
            payload["physical_domains"] = [
                item.to_data()
                for item in sorted(
                    self.physical_domains, key=lambda value: value.identity
                )
            ]
        if (
            self.timing_contract is not None
            or self.output_timings
            or self.instance_output_timings
        ):
            contract = self.timing_contract
            payload["timing_contract"] = (
                {
                    "latency": contract.latency,
                    "initiation_interval": contract.initiation_interval,
                    "clock_domain": contract.clock_domain,
                    "reset_domain": contract.reset_domain,
                    "source_origin": _origin_to_data(contract.source_origin),
                }
                if contract is not None else None
            )
            payload["output_timings"] = [
                _timing_record_to_data(item.port, item.timing)
                for item in self.output_timings
            ]
            payload["instance_output_timings"] = [
                {
                    "instance": item.instance,
                    **_timing_record_to_data(item.port, item.timing),
                }
                for item in self.instance_output_timings
            ]
        if self.companions:
            payload["companions"] = [
                {
                    "logical_path": item.logical_path,
                    "file_hash": item.file_hash,
                    "semantic_id": item.semantic_id,
                    "object_kind": item.object_kind,
                    "canonical_type": item.canonical_type,
                    "word_width": item.word_width,
                    "depth": item.depth,
                    "read_latency": item.read_latency,
                    "initialization_identity": item.initialization_identity,
                    "dependency_identity": [list(value) for value in item.dependency_identity],
                    "evaluator_schema": item.evaluator_schema,
                    "content_hash": item.content_hash,
                    "source_origin": _origin_to_data(item.source_origin),
                }
                for item in self.companions
            ]
        if self.manifest_version >= RECURSIVE_MANIFEST_VERSION:
            payload.update({
                "formal_artifact_hash": self.formal_artifact_hash,
                "components": [
                    {"identity": item.identity, "module_name": item.module_name,
                     "source_identity": item.source_identity, "source_hash": item.source_hash,
                     "specialization_identity": item.specialization_identity,
                     "inputs": list(item.inputs), "outputs": list(item.outputs),
                     "semantic_state": list(item.semantic_state), "children": list(item.children),
                     "clock_domain": item.clock_domain, "reset_domain": item.reset_domain,
                     "aggregate_leaves": list(item.aggregate_leaves),
                     "formal_observations": list(item.formal_observations),
                     **(
                         {"physical_domain_identity": item.physical_domain_identity}
                         if self.manifest_version >= PHYSICAL_DOMAIN_MANIFEST_VERSION
                         else {}
                     )}
                    for item in self.components
                ],
                "instances": [
                    {"instance_identity": item.instance_identity,
                     "physical_instance_path": list(item.physical_instance_path),
                     "source_instance_name": item.source_instance_name,
                     "source_instance_index": item.source_instance_index,
                     "module_definition_identity": item.module_definition_identity,
                     "module_name": item.module_name,
                     "source_identity": item.source_identity, "source_hash": item.source_hash,
                     "specialization_identity": item.specialization_identity,
                     "clock_domain": item.clock_domain, "reset_domain": item.reset_domain,
                     "source_origin": _origin_to_data(item.source_origin),
                     "component_identity": item.component_identity,
                     "children": list(item.children),
                     **(
                         {"physical_domain_identity": item.physical_domain_identity}
                         if self.manifest_version >= PHYSICAL_DOMAIN_MANIFEST_VERSION
                         else {}
                     )}
                    for item in self.instances
                ],
                "recursive_bindings": [
                    {"semantic_binding_id": item.semantic_binding_id,
                     "instance_identity": item.instance_identity,
                     "local_semantic_id": item.local_semantic_id,
                     "specialization_identity": item.specialization_identity,
                     "physical_instance_path": list(item.physical_instance_path),
                     "object_kind": item.object_kind,
                     "canonical_type": item.canonical_type,
                     "width": item.width, "signedness": item.signedness,
                     "direction": item.direction,
                     "clock_domain": item.clock_domain, "reset_domain": item.reset_domain,
                     "ownership": item.ownership,
                     "source_origin": _origin_to_data(item.source_origin),
                     "aggregate_endpoint_id": item.aggregate_endpoint_id,
                     "member_path": list(item.member_path),
                     "leaf_semantic_id": item.leaf_semantic_id,
                     "backend": item.backend, "artifact_hash": item.artifact_hash,
                     "rtl_module": item.rtl_module, "rtl_path": list(item.rtl_path),
                     "signal_token": item.signal_token,
                     "formal_observation_token": item.formal_observation_token,
                     "physical_available": item.physical_available,
                     "implementation_state_id": item.implementation_state_id}
                    for item in self.recursive_bindings
                ],
                "formal_observations": [
                    {"semantic_binding_id": item.semantic_binding_id,
                     "observation_token": item.observation_token,
                     "width": item.width, "canonical_type": item.canonical_type,
                     "clock_domain": item.clock_domain, "reset_domain": item.reset_domain,
                     "artifact_hash": item.artifact_hash,
                     "physical_available": item.physical_available}
                    for item in self.formal_observations
                ],
            })
        if self.implementation is not None:
            implementation = self.implementation
            payload["implementation"] = {
                "graph_identity": implementation.graph_identity,
                "semantic_region_identity": implementation.semantic_region_identity,
                "architecture_template_identity": implementation.architecture_template_identity,
                "target_identity": implementation.target_identity,
                "target_hash": implementation.target_hash,
                "target_family_identity": implementation.target_family_identity,
                "architecture_template_hash": implementation.architecture_template_hash,
                "target_dependency_hashes": [list(item) for item in implementation.target_dependency_hashes],
                "architecture_dependency_hashes": [list(item) for item in implementation.architecture_dependency_hashes],
                "selection_policy": implementation.selection_policy,
                "target_part": implementation.target_part,
                "pipeline_configuration_identity": implementation.pipeline_configuration_identity,
                "active_pipeline_sites": list(implementation.active_pipeline_sites),
                "physical_binding_identities": list(implementation.physical_binding_identities),
                "resource_definition_hashes": [list(item) for item in implementation.resource_definition_hashes],
                "resources": [{
                    "identity": item.identity,
                    "definition_identity": item.definition_identity,
                    "operation": item.operation,
                    "configuration": [list(value) for value in item.configuration],
                    "semantic_mappings": [list(value) for value in item.semantic_mappings],
                    "source_origin": _origin_to_data(item.source_origin),
                } for item in implementation.resources],
                "dedicated_edges": [{
                    "identity": item.identity, "kind": item.kind,
                    "source_instance": item.source_instance, "source_port": item.source_port,
                    "destination_instance": item.destination_instance,
                    "destination_port": item.destination_port, "width": item.width,
                    "placement_relation": item.placement_relation, "latency": item.latency,
                    "fabric_fallback": item.fabric_fallback,
                } for item in implementation.dedicated_edges],
                "latency": implementation.latency,
                "latency_knowledge": implementation.latency_knowledge,
                "initiation_interval": implementation.initiation_interval,
                "realization_backend": implementation.realization_backend,
                "intended_resource_counts": [list(item) for item in implementation.intended_resource_counts],
                "emitted_resource_counts": [list(item) for item in implementation.emitted_resource_counts],
                "measured_resource_counts": [list(item) for item in implementation.measured_resource_counts],
                "timing_evidence": [list(item) for item in implementation.timing_evidence],
                "policy_requirements": [list(item) for item in implementation.policy_requirements],
                "objective": implementation.objective,
                "selected_cost": [list(item) for item in implementation.selected_cost],
                "timing_dag_identity": implementation.timing_dag_identity,
                "timing_nodes": [list(item) for item in implementation.timing_nodes],
                "timing_edges": [list(item) for item in implementation.timing_edges],
                "timing_cuts": [list(item) for item in implementation.timing_cuts],
                "alignment_delays": [list(item) for item in implementation.alignment_delays],
                "compensation_delays": [list(item) for item in implementation.compensation_delays],
                "evidence_identity": implementation.evidence_identity,
                "implementation_artifact_hash": implementation.implementation_artifact_hash,
            }
        return canonical_json(payload)

    @classmethod
    def from_json(cls, payload: str | bytes | dict[str, object]) -> "BackendArtifact":
        """Read v2 and v3 manifests without reconstructing semantic paths."""
        data = decode_artifact_payload(
            payload,
            minimum_version=MANIFEST_VERSION,
            maximum_version=PHYSICAL_DOMAIN_MANIFEST_VERSION,
        )
        version = int(data["manifest_version"])
        bindings = [
            binding_from_data(item, manifest_version=version)
            for item in data["bindings"]
        ]
        components = tuple(ComponentManifest(
            item["identity"], item["module_name"], item["source_identity"], item["source_hash"],
            item["specialization_identity"], tuple(item.get("inputs", ())), tuple(item.get("outputs", ())),
            tuple(item.get("semantic_state", ())), tuple(item.get("children", ())),
            item.get("clock_domain"), item.get("reset_domain"),
            tuple(item.get("aggregate_leaves", ())), tuple(item.get("formal_observations", ())),
            item.get("physical_domain_identity"),
        ) for item in data.get("components", ()))
        instances = tuple(InstanceManifest(
            item["instance_identity"], tuple(item.get("physical_instance_path", ())),
            item["source_instance_name"], item.get("source_instance_index"),
            item["module_definition_identity"], item["module_name"], item["source_identity"],
            item["source_hash"], item["specialization_identity"], item.get("clock_domain"),
            item.get("reset_domain"), _origin_from_data(item.get("source_origin")),
            item["component_identity"],
            tuple(item.get("children", ())),
            item.get("physical_domain_identity"),
        ) for item in data.get("instances", ()))
        physical_domains = tuple(
            PhysicalDomainManifest.from_data(item)
            for item in data.get("physical_domains", ())
        )
        recursive_bindings = tuple(RecursiveBindingManifest(
            item["semantic_binding_id"], item["instance_identity"], item["local_semantic_id"],
            item["specialization_identity"], tuple(item.get("physical_instance_path", ())),
            item["object_kind"], item["canonical_type"], int(item["width"]), item["signedness"],
            item["direction"], item.get("clock_domain"), item.get("reset_domain"),
            item.get("ownership"), _origin_from_data(item.get("source_origin")),
            item.get("aggregate_endpoint_id"),
            tuple(item.get("member_path", ())), item.get("leaf_semantic_id"), item["backend"],
            item["artifact_hash"], item.get("rtl_module"), tuple(item.get("rtl_path", ())),
            item.get("signal_token"), item.get("formal_observation_token"),
            item.get("physical_available", bool(item.get("formal_observation_token"))),
            item.get("implementation_state_id"),
        ) for item in data.get("recursive_bindings", ()))
        observations = tuple(FormalObservationManifest(
            item["semantic_binding_id"], item["observation_token"], int(item["width"]),
            item["canonical_type"], item.get("clock_domain"), item.get("reset_domain"),
            item["artifact_hash"],
            item.get("physical_available", bool(item.get("observation_token"))),
        ) for item in data.get("formal_observations", ()))
        companions = tuple(
            CompanionArtifact(
                logical_path=item["logical_path"],
                file_hash=item["file_hash"],
                semantic_id=item["semantic_id"],
                object_kind=item.get("object_kind", "rom_image"),
                canonical_type=item["canonical_type"],
                word_width=int(item["word_width"]),
                depth=int(item["depth"]),
                read_latency=int(item["read_latency"]),
                initialization_identity=item["initialization_identity"],
                dependency_identity=tuple(
                    (str(value[0]), str(value[1]))
                    for value in item.get("dependency_identity", ())
                ),
                evaluator_schema=item["evaluator_schema"],
                content_hash=item["content_hash"],
                source_origin=_origin_from_data(item.get("source_origin")),
            )
            for item in data.get("companions", ())
        )
        raw_implementation = data.get("implementation")
        implementation = None
        if raw_implementation is not None:
            implementation = ImplementationManifest(
                raw_implementation["graph_identity"],
                raw_implementation["semantic_region_identity"],
                raw_implementation["architecture_template_identity"],
                raw_implementation.get("target_identity"), raw_implementation.get("target_hash"),
                tuple(tuple(item) for item in raw_implementation.get("resource_definition_hashes", ())),
                tuple(ImplementationResourceManifest(
                    item["identity"], item["definition_identity"], item["operation"],
                    tuple(tuple(value) for value in item.get("configuration", ())),
                    tuple(tuple(value) for value in item.get("semantic_mappings", ())),
                    _origin_from_data(item.get("source_origin")),
                ) for item in raw_implementation.get("resources", ())),
                tuple(ImplementationEdgeManifest(
                    item["identity"], item["kind"], item["source_instance"], item["source_port"],
                    item["destination_instance"], item["destination_port"], int(item["width"]),
                    item["placement_relation"], int(item.get("latency", 0)),
                    bool(item.get("fabric_fallback", False)),
                ) for item in raw_implementation.get("dedicated_edges", ())),
                int(raw_implementation["latency"]), int(raw_implementation["initiation_interval"]),
                tuple((str(item[0]), int(item[1])) for item in raw_implementation.get("intended_resource_counts", ())),
                raw_implementation["implementation_artifact_hash"],
                raw_implementation.get("target_family_identity"),
                raw_implementation.get("architecture_template_hash"),
                tuple(tuple(item) for item in raw_implementation.get("target_dependency_hashes", ())),
                tuple(tuple(item) for item in raw_implementation.get("architecture_dependency_hashes", ())),
                raw_implementation.get("selection_policy", "generic"),
                raw_implementation.get("target_part"),
                raw_implementation.get("pipeline_configuration_identity"),
                tuple(raw_implementation.get("active_pipeline_sites", ())),
                tuple(raw_implementation.get("physical_binding_identities", ())),
                tuple((str(item[0]), int(item[1])) for item in raw_implementation.get("emitted_resource_counts", ())),
                tuple((str(item[0]), int(item[1])) for item in raw_implementation.get("measured_resource_counts", ())),
                tuple((str(item[0]), str(item[1])) for item in raw_implementation.get("timing_evidence", ())),
                tuple((str(item[0]), str(item[1]), int(item[2])) for item in raw_implementation.get("policy_requirements", ())),
                raw_implementation.get("objective", "lut"),
                tuple((str(item[0]), item[1], str(item[2])) for item in raw_implementation.get("selected_cost", ())),
                raw_implementation.get("timing_dag_identity"),
                tuple((str(item[0]), str(item[1]), str(item[2]), int(item[3]), int(item[4])) for item in raw_implementation.get("timing_nodes", ())),
                tuple((str(item[0]), str(item[1]), str(item[2]), str(item[3]), int(item[4]), int(item[5]), item[6]) for item in raw_implementation.get("timing_edges", ())),
                tuple((str(item[0]), str(item[1]), str(item[2]), int(item[3]), item[4], item[5]) for item in raw_implementation.get("timing_cuts", ())),
                tuple((str(item[0]), str(item[1]), str(item[2]), int(item[3]), int(item[4]), int(item[5])) for item in raw_implementation.get("alignment_delays", ())),
                tuple((str(item[0]), str(item[1]), str(item[2]), int(item[3]), int(item[4]), int(item[5])) for item in raw_implementation.get("compensation_delays", ())),
                raw_implementation.get("evidence_identity"),
                raw_implementation.get("realization_backend", "backend_independent"),
                raw_implementation.get("latency_knowledge", "known"),
            )
        raw_contract = data.get("timing_contract")
        timing_contract = None
        if raw_contract is not None:
            timing_contract = ModuleTimingContract(
                int(raw_contract["latency"]),
                int(raw_contract["initiation_interval"]),
                raw_contract.get("clock_domain"),
                raw_contract.get("reset_domain"),
                _origin_from_data(raw_contract.get("source_origin")),
            )
        output_timings = tuple(
            OutputTiming(str(item["port"]), _timing_record_from_data(item))
            for item in data.get("output_timings", ())
        )
        instance_output_timings = tuple(
            InstanceOutputTiming(
                str(item["instance"]), str(item["port"]),
                _timing_record_from_data(item),
            )
            for item in data.get("instance_output_timings", ())
        )
        raw_root_module_identity = data.get("root_module_identity")
        if raw_root_module_identity is not None and not isinstance(
            raw_root_module_identity, dict
        ):
            raise ValueError("backend manifest root_module_identity must be an object")
        root_module_identity = (
            DependencyModuleIdentity.from_data(raw_root_module_identity)
            if isinstance(raw_root_module_identity, dict) else None
        )
        raw_dependency_closure = data.get("dependency_closure")
        if raw_dependency_closure is not None and not isinstance(
            raw_dependency_closure, dict
        ):
            raise ValueError("backend manifest dependency_closure must be an object")
        dependency_closure = (
            DependencyClosure.from_data(raw_dependency_closure)
            if isinstance(raw_dependency_closure, dict) else None
        )
        raw_module_signature = data.get("module_signature")
        if (
            raw_module_signature is not None
            and version < MODULE_SIGNATURE_MANIFEST_VERSION
        ):
            raise ValueError(
                "backend module signature requires manifest version "
                f"{MODULE_SIGNATURE_MANIFEST_VERSION} or newer"
            )
        module_signature = (
            ModuleSignatureManifest.from_data(raw_module_signature)
            if raw_module_signature is not None else None
        )
        artifact = cls(
            backend=data["backend"],
            module=data["module"],
            selected_ir_identity=data["selected_ir_identity"],
            artifact_hash=data["artifact_hash"],
            text="",
            bindings=tuple(bindings),
            manifest_version=version,
            components=components,
            instances=instances,
            recursive_bindings=recursive_bindings,
            formal_observations=observations,
            formal_artifact_hash=data.get("formal_artifact_hash"),
            library_dependencies=tuple(
                (str(item[0]), str(item[1]))
                for item in data.get("library_dependencies", ())
            ),
            implementation=implementation,
            companions=companions,
            timing_contract=timing_contract,
            output_timings=output_timings,
            instance_output_timings=instance_output_timings,
            root_module_identity=root_module_identity,
            dependency_closure=dependency_closure,
            module_signature=module_signature,
            physical_domains=physical_domains,
        )
        encoded_build_identity = data.get("build_identity")
        if (
            encoded_build_identity is not None
            and str(encoded_build_identity) != artifact.build_identity
        ):
            raise ValueError("backend manifest build identity does not match its contents")
        artifact.binding_map().validate()
        validate_artifact_links(artifact)
        return artifact

def publish_artifact(module: Module, text: str, *, backend: str,
                     selected_ir_identity: str | None = None,
                     rtl_names: dict[str, str] | None = None,
                     validated_physical_paths: frozenset[str] = frozenset(),
                     side: BindingSide = BindingSide.IMPLEMENTATION,
                     recursive_design: object | None = None,
                     formal_artifact_hash: str | None = None,
                     companions: tuple[CompanionArtifact, ...] = ()) -> BackendArtifact:
    selected_ir_identity = (
        selected_ir_identity or default_selected_ir_identity(module)
    )
    names = dict(rtl_names or {})
    top_abi = module.top_aggregate_abi
    publishes_physical_domains = len(module.clock_domains) > 1 or any(
        not domain.is_legacy_default for domain in module.clock_domains
    )
    artifact_version = max(
        DEPENDENCY_MANIFEST_VERSION if (
            module.root_module_identity is not None
            or module.dependency_closure is not None
        ) else MANIFEST_VERSION,
        COMPANION_MANIFEST_VERSION if companions else MANIFEST_VERSION,
        TIMING_MANIFEST_VERSION if (
            module.timing_contract is not None
            or module.output_timings
            or module.instance_output_timings
        ) else MANIFEST_VERSION,
        MODULE_SIGNATURE_MANIFEST_VERSION
        if module.module_signature is not None else MANIFEST_VERSION,
        PHYSICAL_DOMAIN_MANIFEST_VERSION
        if publishes_physical_domains else MANIFEST_VERSION,
        RECURSIVE_MANIFEST_VERSION if recursive_design is not None else
        TOP_AGGREGATE_MANIFEST_VERSION if top_abi.leaves else MANIFEST_VERSION
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    origins = {a.target.name: a.expression.origin for a in module.assignments
               if getattr(a.expression, "origin", None) is not None}
    bindings: list[EquivalenceBinding] = []
    for port in module.ports:
        if top_abi.leaves and "__" in port.name:
            continue
        semantic = f"port:{port.name}"
        forward_role = (
            SignalRole.INPUT
            if port.direction is PortDirection.INPUT
            else SignalRole.OUTPUT
        )
        reverse_role = (
            SignalRole.OUTPUT
            if forward_role is SignalRole.INPUT
            else SignalRole.INPUT
        )
        bindings.append(EquivalenceBinding(
            artifact_version, side, semantic, selected_ir_identity, module.name,
            names.get(semantic, port.name), _width(port.type), signedness(port.type),
            forward_role,
            port.domain or module.clock, module.reset, backend, digest, origins.get(port.name),
            physical_available=port.protocol is InterfaceProtocol.WIRE,
            canonical_type=_canonical_type(port.type)))
        fields: tuple[tuple[str, int, str, SignalRole], ...] = ()
        if port.protocol is InterfaceProtocol.READY_VALID:
            fields = (
                ("payload", _width(port.type), signedness(port.type), forward_role),
                ("valid", 1, "bit", forward_role),
                ("ready", 1, "bit", reverse_role),
            )
        elif port.protocol is InterfaceProtocol.CREDIT:
            fields = (
                ("payload", _width(port.type), signedness(port.type), forward_role),
                ("send", 1, "bit", forward_role),
                ("return", 1, "bit", reverse_role),
            )
        elif port.protocol is InterfaceProtocol.VC_CREDIT:
            vc_width = max(1, ((port.virtual_channels or 1) - 1).bit_length())
            fields = (
                ("payload", _width(port.type), signedness(port.type), forward_role),
                ("vc", vc_width, "unsigned", forward_role),
                ("send", 1, "bit", forward_role),
                ("return", 1, "bit", reverse_role),
                ("return_vc", vc_width, "unsigned", reverse_role),
            )
        elif port.protocol is InterfaceProtocol.PACKET:
            fields = (
                ("payload", _width(port.type), signedness(port.type), forward_role),
                ("valid", 1, "bit", forward_role),
                ("last", 1, "bit", forward_role),
                ("ready", 1, "bit", reverse_role),
            )
        for field, width, field_signedness, field_role in fields:
            field_id = f"{semantic}.{field}"
            bindings.append(EquivalenceBinding(
                artifact_version, side, field_id, selected_ir_identity,
                module.name, names.get(field_id, f"{port.name}_{field}"),
                width, field_signedness, field_role,
                port.domain or module.clock, module.reset, backend, digest,
                origins.get(port.name), canonical_type=(
                    _canonical_type(port.type) if field == "payload" else
                    f"uint<{width}>" if width > 1 else "bit"
                ),
            ))
    if module.clock:
        bindings.append(EquivalenceBinding(artifact_version, side, "clock", selected_ir_identity,
            module.name, names.get("clock", module.clock), 1, "bit", SignalRole.CLOCK,
            module.clock, module.reset, backend, digest))
    if module.reset:
        bindings.append(EquivalenceBinding(artifact_version, side, "reset", selected_ir_identity,
            module.name, names.get("reset", module.reset), 1, "bit", SignalRole.RESET,
            module.clock, module.reset, backend, digest))
    for endpoint in module.protocol_endpoints:
        channel_suffix = f".{endpoint.channel.value}" if endpoint.channel is not None else ""
        physical_suffix = f"_{endpoint.channel.value}" if endpoint.channel is not None else ""
        semantic = f"endpoint:{endpoint.owner}.{endpoint.name}{channel_suffix}"
        endpoint_role = (SignalRole.INPUT if endpoint.direction is PortDirection.INPUT
                         else SignalRole.OUTPUT)
        endpoint_rtl = (
            endpoint.name + physical_suffix
            if endpoint.owner == module.name
            else f"{endpoint.owner}_{endpoint.name}{physical_suffix}"
        )
        bindings.append(EquivalenceBinding(
            MANIFEST_VERSION, side, semantic, selected_ir_identity, module.name,
            names.get(semantic, endpoint_rtl),
            _width(endpoint.payload_type), signedness(endpoint.payload_type),
            endpoint_role, endpoint.domain or module.clock, module.reset, backend, digest,
            physical_available=endpoint.protocol is InterfaceProtocol.WIRE,
            canonical_type=_canonical_type(endpoint.payload_type),
        ))
        # Publish the physical bidirectional handshake components as separate
        # semantic bindings.  Their direction is the physical RTL direction,
        # not the transaction direction of the endpoint.
        for field, width, field_role in (
            ("payload", _width(endpoint.payload_type), endpoint_role),
            ("valid", 1, endpoint_role),
            ("ready", 1, SignalRole.OUTPUT if endpoint_role is SignalRole.INPUT else SignalRole.INPUT),
        ):
            field_id = f"{semantic}.{field}"
            bindings.append(EquivalenceBinding(
                MANIFEST_VERSION, side, field_id, selected_ir_identity, module.name,
                names.get(
                    field_id,
                    f"{endpoint.name}{physical_suffix}_{field}"
                    if endpoint.owner == module.name
                    else f"{endpoint.owner}_{endpoint.name}{physical_suffix}_{field}",
                ),
                width, signedness(endpoint.payload_type) if field == "payload" else "bit",
                field_role, endpoint.domain or module.clock, module.reset, backend, digest,
                canonical_type=(_canonical_type(endpoint.payload_type) if field == "payload" else "bit"),
            ))
    if top_abi.leaves:
        for aggregate in module.aggregate_protocol_endpoints:
            aggregate_id = f"aggregate:{module.name}.{aggregate.name}"
            bindings.append(EquivalenceBinding(
                artifact_version, side, aggregate_id, selected_ir_identity, module.name,
                names.get(aggregate_id, aggregate.name), 1, "bit", SignalRole.OUTPUT,
                aggregate.domain or module.clock, module.reset, backend, digest,
                aggregate_endpoint_id=aggregate_id,
                protocol_specialization_id=aggregate.specialization_identity,
                protocol_role=aggregate.role,
                signal_kind="aggregate",
                physical_available=False,
            ))
        for leaf in top_abi.leaves:
            bindings.append(EquivalenceBinding(
                artifact_version, side, leaf.leaf_semantic_id,
                selected_ir_identity, module.name,
                names.get(leaf.leaf_semantic_id, leaf.external_name),
                leaf.width, leaf.signedness,
                SignalRole.INPUT if leaf.direction is PortDirection.INPUT else SignalRole.OUTPUT,
                leaf.clock_domain, leaf.reset_domain, backend, digest, leaf.source_origin,
                leaf.aggregate_id, leaf.protocol_specialization_id, leaf.role,
                leaf.member_path, leaf.ownership, leaf.signal_kind,
                canonical_type=_canonical_type(leaf.canonical_type),
            ))
    else:
        for aggregate in module.aggregate_protocol_endpoints:
            aggregate_id = f"aggregate:{module.name}.{aggregate.name}"
            aggregate_width = sum(_width(member.payload_type) for member in aggregate.members)
            bindings.append(EquivalenceBinding(
                artifact_version, side, aggregate_id, selected_ir_identity, module.name,
                names.get(aggregate_id, aggregate.name), max(1, aggregate_width), "bit",
                SignalRole.OUTPUT,
                aggregate.domain or module.clock, module.reset, backend, digest,
                physical_available=False,
            ))
    for connection in module.hierarchical_connections:
        channel_depths = [("buffer", connection.buffer_depth)]
        if connection.request_buffer_depth:
            channel_depths.append(("request", connection.request_buffer_depth))
        if connection.response_buffer_depth:
            channel_depths.append(("response", connection.response_buffer_depth))
        for channel_kind, depth in channel_depths:
            if not depth:
                continue
            state_id = (f"fifo:{connection.source.owner}.{connection.source.name}->"
                        f"{connection.destination.owner}.{connection.destination.name}:"
                        f"{channel_kind}:state")
            bindings.append(EquivalenceBinding(
                MANIFEST_VERSION, side, state_id, selected_ir_identity, module.name,
                names.get(
                    state_id,
                    f"{connection.source.owner}_{connection.source.name}_"
                    f"{connection.destination.name}_{channel_kind}_buffer_count"
                    if connection.destination.owner == module.name
                    else f"{connection.source.owner}_{connection.destination.owner}_fifo_{channel_kind}_state",
                ),
                max(1, depth.bit_length()), "unsigned", SignalRole.OUTPUT,
                module.clock, module.reset, backend, digest,
            ))
    for descriptor in module.request_response_connections:
        tracker_id = request_response_observation_id(
            descriptor.semantic_id, "outstanding"
        )
        bindings.append(EquivalenceBinding(
            MANIFEST_VERSION, side, tracker_id, selected_ir_identity, module.name,
            names.get(
                tracker_id,
                f"rr_{descriptor.request.source.owner}_{descriptor.request.source.name}_"
                f"{descriptor.response.destination.owner}_outstanding",
            ),
            max(1, descriptor.max_outstanding.bit_length()), "unsigned",
            SignalRole.OUTPUT, module.clock, module.reset, backend, digest,
        ))
        # Cross-channel accounting identities are published even when a
        # direction is unbuffered (the backend then binds its occupancy to a
        # constant zero).  Consumers never need to reconstruct these names.
        tracker_name = (
            f"rr_{descriptor.request.source.owner}_{descriptor.request.source.name}_"
            f"{descriptor.response.destination.owner}_outstanding"
        )
        width = max(1, descriptor.max_outstanding.bit_length())
        for suffix in ("request_occupancy", "response_occupancy", "waiting_response"):
            if suffix == "waiting_response":
                # Waiting-response state is an implementation/equivalence
                # locator, not one of the frozen M35 RR observations.
                semantic_id = f"{descriptor.semantic_id}:{suffix}"
            else:
                semantic_id = request_response_observation_id(
                    descriptor.semantic_id, suffix
                )
            bindings.append(EquivalenceBinding(
                MANIFEST_VERSION, side, semantic_id, selected_ir_identity, module.name,
                names.get(semantic_id, f"{tracker_name}_{suffix}"), width, "unsigned",
                SignalRole.OUTPUT, module.clock, module.reset, backend, digest,
            ))
    for connection in module.connections:
        if connection.buffer_depth:
            state_id = f"fifo:{connection.source.name}->{connection.destination.name}:state"
            bindings.append(EquivalenceBinding(
                MANIFEST_VERSION, side, state_id, selected_ir_identity, module.name,
                names.get(state_id, f"{connection.source.name}_{connection.destination.name}_fifo_state"),
                max(1, connection.buffer_depth.bit_length()), "unsigned", SignalRole.OUTPUT,
                module.clock, module.reset, backend, digest,
            ))
    known = {item.semantic_signal_id for item in bindings}
    for semantic, rtl in sorted(names.items()):
        if semantic in known or semantic in {"clock", "reset"}:
            continue
        bindings.append(EquivalenceBinding(artifact_version, side, semantic, selected_ir_identity,
            module.name, rtl, 1, "bit", SignalRole.OUTPUT, module.clock, module.reset,
            backend, digest))

    # The public physical ABI is the shared truth for both backends.  Private
    # component roots may remain packed, but a split public root is not a real
    # top-level signal and must never be advertised as one.  Conversely every
    # typed leaf names the actual public RTL port, including user structs and
    # native unpacked vector arrays.
    physical_leaves = tuple(module.top_physical_abi.leaves)
    leaves_by_semantic = {
        leaf.leaf_semantic_id: leaf for leaf in physical_leaves
    }
    leaves_by_root: dict[str, list[object]] = {}
    for leaf in physical_leaves:
        leaves_by_root.setdefault(leaf.packed_root_semantic_id, []).append(leaf)
    split_roots = {
        root
        for root, root_leaves in leaves_by_root.items()
        if not (
            len(root_leaves) == 1
            and root_leaves[0].leaf_semantic_id == root
            and root_leaves[0].external_name
            == root_leaves[0].packed_root_external_name
        )
    }
    rebound: list[EquivalenceBinding] = []
    rebound_ids: set[str] = set()
    for binding in bindings:
        leaf = leaves_by_semantic.get(binding.semantic_signal_id)
        if leaf is not None:
            binding = replace(
                binding,
                map_version=artifact_version,
                rtl_module=module.name,
                rtl_path=names.get(leaf.leaf_semantic_id, leaf.external_name),
                width=leaf.width,
                signedness=leaf.signedness,
                role=(
                    SignalRole.INPUT
                    if leaf.direction is PortDirection.INPUT
                    else SignalRole.OUTPUT
                ) if leaf.signal_kind not in {"clock", "reset"} else (
                    SignalRole.CLOCK
                    if leaf.signal_kind == "clock" else SignalRole.RESET
                ),
                clock_domain=leaf.clock_domain,
                reset_domain=leaf.reset_domain,
                source_origin=leaf.source_origin or binding.source_origin,
                aggregate_endpoint_id=(
                    leaf.aggregate_id if leaf.category == "aggregate" else None
                ),
                protocol_specialization_id=leaf.protocol_specialization_id,
                protocol_role=leaf.role,
                member_path=leaf.member_path,
                ownership=leaf.ownership,
                signal_kind=leaf.signal_kind,
                physical_available=True,
                canonical_type=_canonical_type(leaf.canonical_type),
            )
        elif binding.semantic_signal_id in split_roots:
            binding = replace(binding, rtl_path="", physical_available=False)
        rebound.append(binding)
        rebound_ids.add(binding.semantic_signal_id)
    for leaf in physical_leaves:
        if leaf.leaf_semantic_id in rebound_ids:
            continue
        rebound.append(EquivalenceBinding(
            artifact_version,
            side,
            leaf.leaf_semantic_id,
            selected_ir_identity,
            module.name,
            names.get(leaf.leaf_semantic_id, leaf.external_name),
            leaf.width,
            leaf.signedness,
            (
                SignalRole.CLOCK if leaf.signal_kind == "clock" else
                SignalRole.RESET if leaf.signal_kind == "reset" else
                SignalRole.INPUT if leaf.direction is PortDirection.INPUT else
                SignalRole.OUTPUT
            ),
            leaf.clock_domain,
            leaf.reset_domain,
            backend,
            digest,
            leaf.source_origin,
            leaf.aggregate_id if leaf.category == "aggregate" else None,
            leaf.protocol_specialization_id,
            leaf.role,
            leaf.member_path,
            leaf.ownership,
            leaf.signal_kind,
            True,
            _canonical_type(leaf.canonical_type),
        ))
        rebound_ids.add(leaf.leaf_semantic_id)
    bindings = rebound

    # Never publish a physical locator merely because a semantic object exists.
    # A backend may leave a semantic binding available while explicitly marking
    # its physical observation unavailable until formal instrumentation exists.
    validated: list[EquivalenceBinding] = []
    for binding in bindings:
        token = binding.rtl_path.rsplit(".", 1)[-1] if binding.rtl_path else ""
        available = bool(token) and (
            binding.rtl_path in validated_physical_paths
            or re.search(
                rf"(?<![A-Za-z0-9_$]){re.escape(token)}(?![A-Za-z0-9_$])",
                text,
            )
        )
        validated.append(replace(
            binding,
            physical_available=binding.physical_available and bool(available),
            rtl_path=binding.rtl_path if available else "",
        ))
    bindings = validated
    physical_domains: tuple[PhysicalDomainManifest, ...] = ()
    physical_domain_by_names: dict[tuple[str, str], str] = {}
    if publishes_physical_domains:
        published: list[PhysicalDomainManifest] = []
        for domain in module.clock_domains:
            clock_bindings = tuple(
                item for item in bindings
                if item.role is SignalRole.CLOCK
                and item.clock_domain == domain.clock
                and item.reset_domain == domain.reset
            )
            reset_bindings = tuple(
                item for item in bindings
                if item.role is SignalRole.RESET
                and item.clock_domain == domain.clock
                and item.reset_domain == domain.reset
            )
            if len(clock_bindings) != 1 or len(reset_bindings) != 1:
                raise ValueError(
                    f"physical domain '{domain.clock}/{domain.reset}' requires "
                    "exactly one public clock and reset binding"
                )
            clock_binding = clock_bindings[0]
            reset_binding = reset_bindings[0]
            for label, binding in (
                ("clock", clock_binding), ("reset", reset_binding)
            ):
                if (
                    not binding.physical_available
                    or binding.width != 1
                    or binding.rtl_module != module.name
                    or not binding.rtl_path
                ):
                    raise ValueError(
                        f"physical domain {label} binding is not a validated "
                        "one-bit public RTL port"
                    )
            record = PhysicalDomainManifest.publish(
                domain,
                rtl_module=module.name,
                rtl_clock_path=clock_binding.rtl_path,
                rtl_reset_path=reset_binding.rtl_path,
            )
            key = (domain.clock, domain.reset)
            if key in physical_domain_by_names:
                raise ValueError(
                    f"duplicate physical domain '{domain.clock}/{domain.reset}'"
                )
            physical_domain_by_names[key] = record.identity
            published.append(record)
        physical_domains = tuple(
            sorted(published, key=lambda item: item.identity)
        )

    def physical_domain_identity(
        clock_domain: str | None, reset_domain: str | None,
    ) -> str | None:
        if clock_domain is None and reset_domain is None:
            return None
        if clock_domain is None or reset_domain is None:
            raise ValueError(
                "recursive sequential object has an incomplete physical domain"
            )
        if not publishes_physical_domains:
            return None
        identity = physical_domain_by_names.get((clock_domain, reset_domain))
        if identity is None:
            raise ValueError(
                "recursive sequential object references an unpublished physical "
                f"domain '{clock_domain}/{reset_domain}'"
            )
        return identity

    components: tuple[ComponentManifest, ...] = ()
    instances: tuple[InstanceManifest, ...] = ()
    recursive_bindings: tuple[RecursiveBindingManifest, ...] = ()
    observations: tuple[FormalObservationManifest, ...] = ()
    if recursive_design is not None:
        # Keep this adapter duck-typed so the backend manifest does not import
        # the semantic recursive-formal module and create an IR cycle.
        components = tuple(ComponentManifest(
            item.identity, item.module_name, item.source_identity, item.source_hash,
            item.specialization_identity, item.inputs, item.outputs, item.semantic_state,
            item.children, item.clock_domain, item.reset_domain,
            item.aggregate_leaves, item.formal_observations,
            physical_domain_identity(item.clock_domain, item.reset_domain),
        ) for item in recursive_design.components)
        instances = tuple(InstanceManifest(
            item.instance_identity, item.physical_instance_path, item.source_instance_name,
            item.source_instance_index, item.module_definition_identity, item.module_name,
            item.source_identity, item.source_hash, item.specialization_identity,
            item.clock_domain, item.reset_domain,
            item.source_origin,
            item.component_identity, item.children,
            physical_domain_identity(item.clock_domain, item.reset_domain),
        ) for item in recursive_design.instances)
        recursive_bindings = tuple(RecursiveBindingManifest(
            item.semantic_binding_id, item.ref.instance_identity, item.ref.local_semantic_id,
            item.specialization_identity, item.physical_instance_path, item.ref.object_kind,
            item.ref.canonical_type, item.width, item.signedness, item.direction,
            item.ref.clock_domain, item.ref.reset_domain, item.ownership,
            item.ref.source_origin,
            item.ref.aggregate_endpoint_id, item.ref.member_path, item.ref.leaf_semantic_id,
            backend, digest,
            item.locator.rtl_module if item.locator else None,
            item.locator.rtl_path if item.locator else (),
            item.locator.signal_token if item.locator else None,
            item.locator.formal_observation_token if item.locator else None,
            bool(item.locator and item.locator.formal_observation_token),
            item.ref.implementation_state_id,
        ) for item in recursive_design.bindings)
        observations = tuple(FormalObservationManifest(
            item.semantic_binding_id,
            item.locator.formal_observation_token if item.locator else None,
            item.width, item.ref.canonical_type,
            item.ref.clock_domain, item.ref.reset_domain, formal_artifact_hash or digest,
            bool(item.locator and item.locator.formal_observation_token),
        ) for index, item in enumerate(sorted(recursive_design.bindings, key=lambda value: value.semantic_binding_id)))
        if formal_artifact_hash is None:
            formal_artifact_hash = hashlib.sha256(
                (digest + "|" + "|".join(item.observation_token or "unavailable" for item in observations)).encode()
            ).hexdigest()
    artifact = BackendArtifact(
        backend=backend,
        module=module.name,
        selected_ir_identity=selected_ir_identity,
        artifact_hash=digest,
        text=text,
        bindings=tuple(bindings),
        manifest_version=artifact_version,
        components=components,
        instances=instances,
        recursive_bindings=recursive_bindings,
        formal_observations=observations,
        formal_artifact_hash=formal_artifact_hash,
        library_dependencies=module.library_dependencies,
        companions=companions,
        timing_contract=module.timing_contract,
        output_timings=module.output_timings,
        instance_output_timings=module.instance_output_timings,
        root_module_identity=module.root_module_identity,
        dependency_closure=module.dependency_closure,
        module_signature=(
            ModuleSignatureManifest.publish(module.module_signature)
            if module.module_signature is not None else None
        ),
        physical_domains=physical_domains,
    )
    artifact.binding_map().validate()
    validate_artifact_links(artifact)
    return artifact

__all__ = [
    "BackendArtifact", "ComponentManifest", "FormalObservationManifest",
    "ImplementationEdgeManifest", "ImplementationManifest",
    "ImplementationResourceManifest", "IMPLEMENTATION_MANIFEST_VERSION",
    "COMPANION_MANIFEST_VERSION", "TIMING_MANIFEST_VERSION",
    "DEPENDENCY_MANIFEST_VERSION", "MODULE_SIGNATURE_MANIFEST_VERSION",
    "PHYSICAL_DOMAIN_MANIFEST_VERSION", "PhysicalDomainManifest",
    "InstanceManifest", "MANIFEST_VERSION", "RECURSIVE_MANIFEST_VERSION",
    "ModuleSignatureManifest", "RecursiveBindingManifest",
    "TOP_AGGREGATE_MANIFEST_VERSION", "publish_artifact",
]
