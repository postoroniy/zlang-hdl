"""Backend-neutral recursive M35 formal design.

This module deliberately stops at the semantic/elaborated boundary.  It does
does not inspect backend names or construct a hierarchical RTL path. Backends attach
their physical locators later through the v4 artifact manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from zlang.ir.formal import (
    Counterexample, FormalDesign, FormalProperty, FormalResult, FormalStatus,
    ProofMode, generate_properties, signal_bindings,
)
from zlang.ir.formal_observations import (
    fifo_observation_id,
    port_observation_id,
    recursive_observation_id,
    register_observation_id,
    request_response_observation_id,
    rule_fire_observation_id,
)
from zlang.ir.formal_predicates import ObservationRef, map_observations
from zlang.ir.equivalence import signedness as hardware_signedness
from zlang.ir import expressions as expr
from zlang.ir.csr import (
    csr_state_port_name, csr_write_hit_port_name, csr_write_value_port_name,
)
from zlang.ir.module import (
    Assignment,
    ElaboratedInstance,
    Module,
    Port,
    PortDirection,
    default_selected_ir_identity,
    dependency_context_identity,
)
from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
from zlang.source import SourceOrigin
from zlang.common import stable_digest, stable_json


RECURSIVE_FORMAL_SCHEMA = 2


def instrument_csr_observation_ports(
    module: Module,
) -> tuple[Module, dict[tuple[tuple[str, ...], str], str]]:
    """Build a formal-only recursive projection of typed CSR observations.

    Every component remains closed: a child observation becomes an explicit
    child output and is forwarded through each ancestor.  The transformation
    is applied only to the formal artifact, so the production component ABI
    and artifact hash are unchanged.  Keys retain the physical instance path
    separately from the backend-neutral CSR semantic identity.
    """
    children: list[Module] = []
    projected: dict[tuple[tuple[str, ...], str], str] = {}
    ports = list(module.ports)
    assignments = list(module.assignments)

    for index, elaborated in enumerate(module.elaborated_instances):
        child, child_observations = instrument_csr_observation_ports(
            module.children[index]
        )
        children.append(child)
        for (relative_path, semantic_id), child_port in sorted(
            child_observations.items(), key=lambda item: item[0]
        ):
            path = (elaborated.instance.name, *relative_path)
            output_name = "zlang_csr_observe_" + stable_digest(
                (path, semantic_id), length=16
            )
            child_output = next(port for port in child.outputs
                                if port.name == child_port)
            output = Port(
                PortDirection.OUTPUT, output_name, child_output.type,
                domain=module.clock,
            )
            ports.append(output)
            assignments.append(Assignment(
                output,
                expr.InstanceOutputRef(
                    elaborated.instance.name, child_port, child_output.type
                ),
            ))
            projected[(path, semantic_id)] = output_name

    # Modules without elaborated children still retain their ordinary child
    # list (normally empty).  A CSR bank publishes the canonical leaf ports
    # created by semantic lowering.
    if not module.elaborated_instances:
        children = list(module.children)
    for block in module.csr_blocks:
        for binding in block.state_bindings:
            projected[((), binding.semantic_state_id)] = csr_state_port_name(binding)
            projected[((), binding.write_hit_id)] = csr_write_hit_port_name(binding)
            projected[((), binding.write_value_id)] = csr_write_value_port_name(binding)

    return replace(
        module,
        ports=tuple(ports),
        assignments=tuple(assignments),
        children=tuple(children),
    ), projected


def _digest(value: object) -> str:
    return stable_digest(value, length=24)


def _type_text(type_: object) -> str:
    return repr(type_)


def _origin_text(origin: SourceOrigin | None) -> str | None:
    return origin.render() if origin is not None else None


@dataclass(frozen=True)
class BackendPhysicalLocator:
    backend: str
    artifact_hash: str
    rtl_module: str
    rtl_path: tuple[str, ...] = ()
    signal_token: str = ""
    formal_observation_token: str | None = None


@dataclass(frozen=True)
class FormalObjectRef:
    instance_identity: str
    local_semantic_id: str
    object_kind: str
    canonical_type: str
    clock_domain: str | None = None
    reset_domain: str | None = None
    source_origin: SourceOrigin | None = None
    aggregate_endpoint_id: str | None = None
    member_path: tuple[str, ...] = ()
    leaf_semantic_id: str | None = None
    implementation_state_id: str | None = None


@dataclass(frozen=True)
class RecursiveSignalBinding:
    ref: FormalObjectRef
    specialization_identity: str
    physical_instance_path: tuple[str, ...]
    width: int
    signedness: str
    direction: str
    ownership: str | None = None
    locator: BackendPhysicalLocator | None = None

    @property
    def semantic_binding_id(self) -> str:
        return recursive_observation_id(
            self.ref.instance_identity, self.ref.local_semantic_id
        )


@dataclass(frozen=True)
class ComponentContract:
    identity: str
    module_name: str
    source_identity: str
    source_hash: str
    specialization_identity: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    semantic_state: tuple[str, ...]
    children: tuple[str, ...]
    clock_domain: str | None
    reset_domain: str | None
    aggregate_leaves: tuple[str, ...]
    formal_observations: tuple[str, ...]


@dataclass(frozen=True)
class FormalInstanceNode:
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
    source_origin: SourceOrigin | None
    component_identity: str
    children: tuple[str, ...]


@dataclass(frozen=True)
class ConcreteFormalProperty:
    concrete_property_id: str
    source_property_id: str
    defining_module: str
    specialization_identity: str
    instance_identity: str
    physical_instance_path: tuple[str, ...]
    property: FormalProperty
    ownership: str
    object_refs: tuple[FormalObjectRef, ...]


@dataclass(frozen=True)
class RecursiveFormalResult:
    concrete_property_id: str
    source_property_id: str
    status: FormalStatus
    mode: ProofMode
    engine: str | None
    solver: str | None
    depth: int | None
    module_name: str
    specialization_identity: str
    instance_identity: str
    physical_instance_path: tuple[str, ...]
    source_origin: SourceOrigin | None = None
    object_values: tuple[tuple[str, str], ...] = ()
    counterexample: Counterexample | None = None
    artifact_hash: str | None = None
    reason: str | None = None

    @classmethod
    def from_formal_result(cls, property_: ConcreteFormalProperty,
                           result: FormalResult, *, artifact_hash: str | None = None,
                           object_values: tuple[tuple[str, str], ...] = ()) -> "RecursiveFormalResult":
        return cls(
            property_.concrete_property_id, property_.source_property_id,
            result.status, result.mode, result.engine, result.solver, result.depth,
            property_.defining_module, property_.specialization_identity,
            property_.instance_identity, property_.physical_instance_path,
            property_.property.source_origin, object_values, result.counterexample,
            artifact_hash, result.reason,
        )


@dataclass(frozen=True)
class RecursiveFormalDesign:
    schema_version: int
    root_selected_ir_identity: str
    root_source_identity: str
    root_source_hash: str
    root_instance_identity: str
    components: tuple[ComponentContract, ...]
    instances: tuple[FormalInstanceNode, ...]
    properties: tuple[ConcreteFormalProperty, ...]
    bindings: tuple[RecursiveSignalBinding, ...]
    required_observations: tuple[str, ...]
    root_module_identity: DependencyModuleIdentity | None = None
    dependency_closure: DependencyClosure | None = None

    @property
    def instance_paths(self) -> tuple[tuple[str, ...], ...]:
        return tuple(item.physical_instance_path for item in self.instances)

    def to_dict(self) -> dict[str, object]:
        def origin(value: SourceOrigin | None) -> str | None:
            return _origin_text(value)

        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "root_selected_ir_identity": self.root_selected_ir_identity,
            "root_source_identity": self.root_source_identity,
            "root_source_hash": self.root_source_hash,
            "root_instance_identity": self.root_instance_identity,
            "components": [
                {"identity": c.identity, "module_name": c.module_name,
                 "source_identity": c.source_identity, "source_hash": c.source_hash,
                 "specialization_identity": c.specialization_identity,
                 "inputs": list(c.inputs), "outputs": list(c.outputs),
                 "semantic_state": list(c.semantic_state), "children": list(c.children),
                 "clock_domain": c.clock_domain, "reset_domain": c.reset_domain,
                 "aggregate_leaves": list(c.aggregate_leaves),
                 "formal_observations": list(c.formal_observations)}
                for c in self.components
            ],
            "instances": [
                {"instance_identity": n.instance_identity,
                 "physical_instance_path": list(n.physical_instance_path),
                 "source_instance_name": n.source_instance_name,
                 "source_instance_index": n.source_instance_index,
                 "module_definition_identity": n.module_definition_identity,
                 "module_name": n.module_name, "source_identity": n.source_identity,
                 "source_hash": n.source_hash,
                 "specialization_identity": n.specialization_identity,
                 "clock_domain": n.clock_domain, "reset_domain": n.reset_domain,
                 "source_origin": origin(n.source_origin),
                 "component_identity": n.component_identity,
                 "children": list(n.children)}
                for n in self.instances
            ],
            "properties": [
                {"concrete_property_id": p.concrete_property_id,
                 "source_property_id": p.source_property_id,
                 "defining_module": p.defining_module,
                 "specialization_identity": p.specialization_identity,
                 "instance_identity": p.instance_identity,
                 "physical_instance_path": list(p.physical_instance_path),
                 "ownership": p.ownership,
                 "object_refs": [
                     {"instance_identity": r.instance_identity,
                      "local_semantic_id": r.local_semantic_id,
                      "object_kind": r.object_kind,
                      "canonical_type": r.canonical_type,
                      "clock_domain": r.clock_domain,
                      "reset_domain": r.reset_domain,
                      "source_origin": origin(r.source_origin),
                      "aggregate_endpoint_id": r.aggregate_endpoint_id,
                      "member_path": list(r.member_path),
                      "leaf_semantic_id": r.leaf_semantic_id,
                      "implementation_state_id": r.implementation_state_id}
                     for r in p.object_refs
                 ],
                 "expression": p.property.expression,
                 "predicate": (
                     p.property.predicate.to_data()
                     if p.property.predicate is not None else None
                 ),
                 "non_executable_reason": p.property.non_executable_reason,
                 "kind": p.property.kind.value,
                 "clock": p.property.clock,
                 "temporal_form": p.property.temporal_form.value}
                for p in self.properties
            ],
            "bindings": [
                {"semantic_binding_id": b.semantic_binding_id,
                 "instance_identity": b.ref.instance_identity,
                 "local_semantic_id": b.ref.local_semantic_id,
                 "object_kind": b.ref.object_kind,
                 "canonical_type": b.ref.canonical_type,
                 "clock_domain": b.ref.clock_domain,
                 "reset_domain": b.ref.reset_domain,
                 "source_origin": origin(b.ref.source_origin),
                 "aggregate_endpoint_id": b.ref.aggregate_endpoint_id,
                 "member_path": list(b.ref.member_path),
                 "leaf_semantic_id": b.ref.leaf_semantic_id,
                 "implementation_state_id": b.ref.implementation_state_id,
                 "specialization_identity": b.specialization_identity,
                 "physical_instance_path": list(b.physical_instance_path),
                 "width": b.width, "signedness": b.signedness,
                 "direction": b.direction, "ownership": b.ownership,
                 "locator": None if b.locator is None else {
                     "backend": b.locator.backend,
                     "artifact_hash": b.locator.artifact_hash,
                     "rtl_module": b.locator.rtl_module,
                     "rtl_path": list(b.locator.rtl_path),
                     "signal_token": b.locator.signal_token,
                     "formal_observation_token": b.locator.formal_observation_token,
                 }}
                for b in self.bindings
            ],
            "required_observations": list(self.required_observations),
        }
        if self.root_module_identity is not None:
            result["root_module_identity"] = self.root_module_identity.to_data()
        if self.dependency_closure is not None:
            result["dependency_closure"] = self.dependency_closure.to_data()
        return result

    def to_json(self) -> str:
        return stable_json(self.to_dict(), indent=2) + "\n"


def _module_source_key(module: Module) -> str:
    return (
        dependency_context_identity(module)
        or module.source_hash
        or module.source_identity
        or module.name
    )


def _module_source_identity(module: Module) -> str:
    return (
        module.root_module_identity.logical_path
        if module.root_module_identity is not None
        else module.source_identity or module.name
    )


def _module_source_hash(module: Module) -> str:
    return (
        module.root_module_identity.digest
        if module.root_module_identity is not None
        else module.source_hash or _digest(module.name)
    )


def _specialization_identity(module: Module, values: tuple[tuple[str, object], ...] = ()) -> str:
    return _digest({"module": module.name, "source": _module_source_key(module),
                    "parameters": list(values)})


def _instance_identity(parent: str, name: str, specialization: str, index: int | None) -> str:
    return _digest({"parent": parent, "name": name, "specialization": specialization, "index": index})


def _component(module: Module, specialization: str, children: tuple[str, ...]) -> ComponentContract:
    bindings = signal_bindings(module, include_rule_fire=True)
    state = tuple(
        item.semantic_signal_id for item in bindings
        if item.semantic_signal_id.startswith(("register:", "fifo:", "rr:", "port:", "csr-field:"))
        and item.direction == "internal"
    )
    aggregate = tuple(item.leaf_semantic_id for item in getattr(module.top_aggregate_abi, "leaves", ()))
    observations = tuple(sorted({item.semantic_signal_id for item in bindings}))
    identity = _digest({"module": module.name, "source": _module_source_key(module),
                        "specialization": specialization})
    return ComponentContract(
        identity, module.name, _module_source_identity(module),
        _module_source_hash(module), specialization,
        tuple(port_observation_id(p.name) for p in module.inputs),
        tuple(port_observation_id(p.name) for p in module.outputs), state, children,
        module.clock, module.reset, aggregate, observations,
    )


def _semantic_types(module: Module) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in module.ports:
        result[port_observation_id(item.name)] = _type_text(item.type)
        protocol = item.protocol.value
        if protocol == "ready_valid":
            result.update({
                port_observation_id(item.name, "payload"): _type_text(item.type),
                port_observation_id(item.name, "valid"): "bit",
                port_observation_id(item.name, "ready"): "bit",
            })
        elif protocol == "credit":
            state_name = (
                "credits"
                if item.direction is PortDirection.OUTPUT
                else "occupancy"
            )
            result.update({
                port_observation_id(item.name, "payload"): _type_text(item.type),
                port_observation_id(item.name, state_name):
                    f"uint<{max(1, (item.capacity or 1).bit_length())}>",
                port_observation_id(item.name, "send"): "bit",
                port_observation_id(item.name, "return"): "bit",
            })
        elif protocol == "packet":
            result.update({
                port_observation_id(item.name, "payload"): _type_text(item.type),
                port_observation_id(item.name, "valid"): "bit",
                port_observation_id(item.name, "ready"): "bit",
                port_observation_id(item.name, "last"): "bit",
            })
        elif protocol == "vc_credit":
            vc_width = max(1, ((item.virtual_channels or 1) - 1).bit_length())
            result.update({
                port_observation_id(item.name, "payload"): _type_text(item.type),
                port_observation_id(item.name, "vc"): f"uint<{vc_width}>",
                port_observation_id(item.name, "send"): "bit",
                port_observation_id(item.name, "return"): "bit",
                port_observation_id(item.name, "return_vc"): f"uint<{vc_width}>",
            })
    for interface in module.request_responses:
        result.update({
            port_observation_id(interface.name, "request.payload"):
                _type_text(interface.request_type),
            port_observation_id(interface.name, "request.valid"): "bit",
            port_observation_id(interface.name, "request.ready"): "bit",
            port_observation_id(interface.name, "response.payload"):
                _type_text(interface.response_type),
            port_observation_id(interface.name, "response.valid"): "bit",
            port_observation_id(interface.name, "response.ready"): "bit",
        })
    result.update({
        register_observation_id(item.name): _type_text(item.type)
        for item in module.registers
    })
    if module.resolved_transition is not None:
        result.update({
            rule_fire_observation_id(item.rule_name): "bit"
            for item in module.resolved_transition.action_groups
        })
    for fifo in module.fifos:
        result.update({
            fifo_observation_id(fifo.name, "count"):
                f"uint<{fifo.count_width}>",
            fifo_observation_id(fifo.name, "front"):
                _type_text(fifo.element_type),
        })
    for connection in module.request_response_connections:
        result.update({
            request_response_observation_id(connection.semantic_id, "outstanding"):
                f"uint<{max(1, connection.max_outstanding.bit_length())}>",
            request_response_observation_id(connection.semantic_id, "request_accept"):
                "bit",
            request_response_observation_id(connection.semantic_id, "response_consume"):
                "bit",
            request_response_observation_id(connection.semantic_id, "request_occupancy"):
                f"uint<{(max(1, connection.request.request_buffer_depth.bit_length()) if connection.request.request_buffer_depth else max(1, connection.max_outstanding.bit_length()))}>",
            request_response_observation_id(connection.semantic_id, "response_occupancy"):
                f"uint<{(max(1, connection.response.response_buffer_depth.bit_length()) if connection.response.response_buffer_depth else max(1, connection.max_outstanding.bit_length()))}>",
        })
    for block in module.csr_blocks:
        for state in block.state_bindings:
            result.update({
                state.semantic_state_id: _type_text(state.canonical_type),
                state.write_hit_id: "bit",
                state.write_value_id: _type_text(state.canonical_type),
            })
    return result


def _semantic_signedness(module: Module, design: FormalDesign) -> dict[str, str]:
    """Return canonical signedness for every published semantic observation.

    ``SignalBinding`` predates typed recursive observations and intentionally
    carries only a width.  Width is not enough to distinguish ``s8`` from
    ``u8`` (or ``bits<8>``), so recursive metadata must be recovered from the
    semantic hardware type and, for derived observations, the structured
    predicate leaves that define their exact formal type.
    """
    result: dict[str, str] = {
        "clock": "bit",
        "reset": "bit",
    }
    for port in module.ports:
        result[port_observation_id(port.name)] = hardware_signedness(port.type)
        if port.protocol.value == "ready_valid":
            result.update({
                port_observation_id(port.name, "valid"): "bit",
                port_observation_id(port.name, "ready"): "bit",
                port_observation_id(port.name, "payload"):
                    hardware_signedness(port.type),
            })
        elif port.protocol.value == "credit":
            state_name = (
                "credits"
                if port.direction is PortDirection.OUTPUT
                else "occupancy"
            )
            result.update({
                port_observation_id(port.name, "payload"):
                    hardware_signedness(port.type),
                port_observation_id(port.name, state_name): "unsigned",
                port_observation_id(port.name, "send"): "bit",
                port_observation_id(port.name, "return"): "bit",
            })
        elif port.protocol.value == "packet":
            result.update({
                port_observation_id(port.name, "payload"):
                    hardware_signedness(port.type),
                port_observation_id(port.name, "valid"): "bit",
                port_observation_id(port.name, "ready"): "bit",
                port_observation_id(port.name, "last"): "bit",
            })
        elif port.protocol.value == "vc_credit":
            result.update({
                port_observation_id(port.name, "payload"):
                    hardware_signedness(port.type),
                port_observation_id(port.name, "vc"): "unsigned",
                port_observation_id(port.name, "send"): "bit",
                port_observation_id(port.name, "return"): "bit",
                port_observation_id(port.name, "return_vc"): "unsigned",
            })
    for interface in module.request_responses:
        result.update({
            port_observation_id(interface.name, "request.payload"):
                hardware_signedness(interface.request_type),
            port_observation_id(interface.name, "request.valid"): "bit",
            port_observation_id(interface.name, "request.ready"): "bit",
            port_observation_id(interface.name, "response.payload"):
                hardware_signedness(interface.response_type),
            port_observation_id(interface.name, "response.valid"): "bit",
            port_observation_id(interface.name, "response.ready"): "bit",
        })
    for register in module.registers:
        result[register_observation_id(register.name)] = hardware_signedness(
            register.type
        )
    if module.resolved_transition is not None:
        result.update({
            rule_fire_observation_id(item.rule_name): "bit"
            for item in module.resolved_transition.action_groups
        })
    for fifo in module.fifos:
        result.update({
            fifo_observation_id(fifo.name, "count"): "unsigned",
            fifo_observation_id(fifo.name, "push"): "bit",
            fifo_observation_id(fifo.name, "pop"): "bit",
            fifo_observation_id(fifo.name, "empty"): "bit",
            fifo_observation_id(fifo.name, "full"): "bit",
            fifo_observation_id(fifo.name, "front"):
                hardware_signedness(fifo.element_type),
        })
    for connection in module.request_response_connections:
        result.update({
            request_response_observation_id(connection.semantic_id, "outstanding"):
                "unsigned",
            request_response_observation_id(connection.semantic_id, "request_accept"):
                "bit",
            request_response_observation_id(connection.semantic_id, "response_consume"):
                "bit",
            request_response_observation_id(connection.semantic_id, "request_occupancy"):
                "unsigned",
            request_response_observation_id(connection.semantic_id, "response_occupancy"):
                "unsigned",
        })
    for block in module.csr_blocks:
        for state in block.state_bindings:
            result.update({
                state.semantic_state_id: hardware_signedness(state.canonical_type),
                state.write_hit_id: "bit",
                state.write_value_id: hardware_signedness(state.canonical_type),
            })

    # Structured predicates are the source of truth for formal observations.
    # A protocol payload may intentionally be treated as opaque ``bits`` even
    # when its source payload type is numeric.  Require consistency between
    # predicates, then let that exact formal type override the source-level
    # storage interpretation.
    predicate_types: dict[str, str] = {}
    for prop in design.properties:
        if prop.predicate is None:
            continue
        for observation in prop.predicate.observations():
            observed = observation.signedness.value
            existing = predicate_types.get(observation.semantic_signal_id)
            if existing is not None and existing != observed:
                raise ValueError(
                    "inconsistent formal signedness for observation "
                    f"'{observation.semantic_signal_id}': {existing} versus {observed}"
                )
            predicate_types[observation.semantic_signal_id] = observed
    result.update(predicate_types)
    return result


def build_recursive_formal_design(module: Module, *, selected_ir_identity: str | None = None) -> RecursiveFormalDesign:
    """Instantiate M35 properties and semantic bindings for every physical instance."""
    selected = selected_ir_identity or default_selected_ir_identity(module)
    root_identity = _digest({"root": module.name, "source": _module_source_key(module)})
    components: list[ComponentContract] = []
    instances: list[FormalInstanceNode] = []
    properties: list[ConcreteFormalProperty] = []
    bindings: list[RecursiveSignalBinding] = []
    seen_components: set[str] = set()
    def walk(current: Module, parent_identity: str, path: tuple[str, ...],
             specialization: str, source_instance_name: str, index: int | None,
             inherited_origin: SourceOrigin | None = None) -> str:
        node_identity = root_identity if not path else _instance_identity(parent_identity, source_instance_name, specialization, index)
        children_nodes: list[str] = []
        design = generate_properties(current)
        child_components: list[str] = []
        for child_index, elaborated_child in enumerate(current.elaborated_instances):
            if child_index >= len(current.children):
                continue
            child_definition = current.children[child_index]
            if child_definition.name != elaborated_child.child_module:
                continue
            child_spec = elaborated_child.specialization_identity or _specialization_identity(
                child_definition,
                tuple((item.name, item.value) for item in elaborated_child.instance.specializations),
            )
            child_components.append(_component(child_definition, child_spec, ()).identity)
        component = _component(current, specialization, tuple(child_components))
        if component.identity not in seen_components:
            components.append(component)
            seen_components.add(component.identity)
        node = FormalInstanceNode(
            node_identity, path, source_instance_name, index,
            _digest({"module": current.name, "source": _module_source_key(current)}),
            current.name, _module_source_identity(current),
            _module_source_hash(current), specialization,
            current.clock, current.reset, inherited_origin,
            component.identity, (),
        )
        instances.append(node)
        local_binding_ids: dict[str, FormalObjectRef] = {}
        semantic_types = _semantic_types(current)
        semantic_signedness = _semantic_signedness(current, design)
        csr_implementation = {
            key: state.implementation_state_id
            for block in current.csr_blocks for state in block.state_bindings
            for key in (state.semantic_state_id, state.write_hit_id, state.write_value_id)
        }
        for binding in design.bindings:
            is_csr = binding.semantic_signal_id.startswith("csr-field:")
            ref = FormalObjectRef(
                node_identity, binding.semantic_signal_id,
                ("csr_field_state" if is_csr and binding.semantic_signal_id.endswith(":state")
                 else "csr_access_event" if is_csr else
                 "state" if binding.direction == "internal" else "port"),
                semantic_types.get(binding.semantic_signal_id, "bit"),
                binding.clock_domain, current.reset,
                binding.source_origin,
                implementation_state_id=csr_implementation.get(
                    binding.semantic_signal_id
                ),
            )
            local_binding_ids[binding.semantic_signal_id] = ref
            bindings.append(RecursiveSignalBinding(
                ref, specialization, path, binding.width,
                semantic_signedness.get(binding.semantic_signal_id, "bits"),
                binding.direction,
            ))
        for leaf in getattr(current.top_aggregate_abi, "leaves", ()):
            ref = FormalObjectRef(
                node_identity, leaf.leaf_semantic_id, "aggregate_leaf",
                _type_text(leaf.canonical_type), leaf.clock_domain,
                leaf.reset_domain, leaf.source_origin,
                leaf.aggregate_id, leaf.member_path, leaf.leaf_semantic_id,
            )
            local_binding_ids[leaf.leaf_semantic_id] = ref
            bindings.append(RecursiveSignalBinding(
                ref, specialization, path, leaf.width, leaf.signedness,
                leaf.direction.value, leaf.ownership,
            ))
        for prop in design.properties:
            resolved_refs: list[FormalObjectRef] = []
            unresolved: list[str] = []
            for signal in prop.relevant_signals:
                ref = local_binding_ids.get(signal)
                if ref is not None and ref not in resolved_refs:
                    resolved_refs.append(ref)
                elif ref is None:
                    unresolved.append(signal)
            refs = tuple(resolved_refs)
            concrete_id = _digest({"source_property": prop.id, "module": _module_source_key(current),
                                   "specialization": specialization, "instance": node_identity,
                                   "schema": RECURSIVE_FORMAL_SCHEMA})
            concrete_predicate = (
                map_observations(
                    prop.predicate,
                    lambda observation: ObservationRef(
                        recursive_observation_id(
                            node_identity, observation.semantic_signal_id
                        ),
                        observation.width,
                        observation.signedness,
                        observation.cycle,
                    ),
                )
                if prop.predicate is not None else None
            )
            unavailable = prop.non_executable_reason
            if unresolved and unavailable is None:
                unavailable = (
                    "semantic observations are not published: "
                    + ", ".join(sorted(unresolved))
                )
            concrete = FormalProperty(
                id=concrete_id, kind=prop.kind, clock=prop.clock,
                reset_condition=prop.reset_condition, expression=prop.expression,
                temporal_form=prop.temporal_form, ownership=prop.ownership,
                source_origin=prop.source_origin, generated_from=prop.generated_from,
                relevant_signals=(
                    concrete_predicate.observation_ids()
                    if concrete_predicate is not None else tuple(
                        recursive_observation_id(
                            ref.instance_identity, ref.local_semantic_id
                        )
                        for ref in refs
                    )
                ),
                antecedent=prop.antecedent, consequent=prop.consequent,
                min_delay=prop.min_delay, max_delay=prop.max_delay,
                predicate=concrete_predicate,
                non_executable_reason=unavailable,
            )
            properties.append(ConcreteFormalProperty(
                concrete_id, prop.id, current.name, specialization, node_identity,
                path, concrete, prop.ownership.value, refs,
            ))
        for child_position, elaborated in enumerate(current.elaborated_instances):
            if child_position >= len(current.children):
                continue
            child = current.children[child_position]
            if child.name != elaborated.child_module:
                continue
            child_spec = elaborated.specialization_identity or _specialization_identity(child, tuple(
                (item.name, item.value) for item in elaborated.instance.specializations
            ))
            array = elaborated.instance.array_length or 1
            for child_index in range(array):
                child_path = path + (elaborated.instance.name if array == 1 else f"{elaborated.instance.name}[{child_index}]",)
                children_nodes.append(walk(child, node_identity, child_path, child_spec,
                                            elaborated.instance.name, child_index if array > 1 else None))
        # Replace the provisional node with child references now that DFS has
        # produced them; list ordering remains deterministic pre-order.
        for idx, item in enumerate(instances):
            if item.instance_identity == node_identity:
                instances[idx] = FormalInstanceNode(
                    item.instance_identity, item.physical_instance_path,
                    item.source_instance_name, item.source_instance_index,
                    item.module_definition_identity, item.module_name,
                    item.source_identity, item.source_hash,
                    item.specialization_identity, item.clock_domain,
                    item.reset_domain, item.source_origin,
                    item.component_identity, tuple(children_nodes),
                )
                break
        return node_identity

    root_spec = _specialization_identity(module)
    actual_root_identity = walk(
        module, root_identity, (module.name,), root_spec, module.name, None
    )
    required = tuple(sorted({
        semantic_id
        for item in properties
        if item.property.non_executable_reason is None
        for semantic_id in item.property.relevant_signals
    }))
    return RecursiveFormalDesign(
        schema_version=RECURSIVE_FORMAL_SCHEMA,
        root_selected_ir_identity=selected,
        root_source_identity=_module_source_identity(module),
        root_source_hash=_module_source_hash(module),
        root_instance_identity=actual_root_identity,
        components=tuple(components),
        instances=tuple(instances),
        properties=tuple(properties),
        bindings=tuple(bindings),
        required_observations=required,
        root_module_identity=module.root_module_identity,
        dependency_closure=module.dependency_closure,
    )


def recursive_cache_key(design: RecursiveFormalDesign, *, top_artifact_hash: str,
                        formal_artifact_hash: str, harness_hash: str,
                        assumptions: Iterable[str] = (), mode: str = "bmc",
                        depth: int = 20, solver: str = "z3",
                        tool_versions: Iterable[tuple[str, str]] = ()) -> str:
    payload = {
        "schema": design.schema_version,
        "root": design.root_selected_ir_identity,
        "top_artifact": top_artifact_hash,
        "formal_artifact": formal_artifact_hash,
        "instances": [item.instance_identity for item in design.instances],
        "specializations": [item.specialization_identity for item in design.instances],
        "properties": [item.concrete_property_id for item in design.properties],
        "harness": harness_hash, "assumptions": sorted(assumptions),
        "mode": mode, "depth": depth, "solver": solver,
        "tool_versions": sorted(tool_versions),
    }
    dependency_identity = dependency_context_identity(design)
    if dependency_identity is not None:
        payload["dependency_identity"] = dependency_identity
    return stable_digest(payload)


__all__ = [
    "BackendPhysicalLocator", "ComponentContract", "ConcreteFormalProperty",
    "FormalInstanceNode", "FormalObjectRef", "RECURSIVE_FORMAL_SCHEMA",
    "RecursiveFormalDesign", "RecursiveSignalBinding", "build_recursive_formal_design",
    "recursive_cache_key",
]
