# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Hierarchical protocol simulation lowering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import hashlib

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.ir.storage import Fifo, FifoSignal
from zlang.simulation_primitives import bit_binary as _bit_binary


import zlang.simulation_protocol_shared as protocol_shared
from zlang.simulation_ready_valid import lower_ready_valid_module
from zlang.simulation_credit import lower_credit_module
from zlang.simulation_request_response import lower_request_response_module


_StreamSource = Callable[
    [str, str, ir_interfaces.ReadyValidSignal, ir_types.HardwareType],
    expr.Expression,
]
_StreamDrive = Callable[
    [str, str, ir_interfaces.ReadyValidSignal, expr.Expression],
    None,
]


class _HierarchicalProtocolRouter:
    """Own scalar references and drives across one typed protocol hierarchy."""

    def __init__(
        self,
        module: ir_module.Module,
        protocol: ir_interfaces.InterfaceProtocol,
        label: str,
        assignments: list[ir_module.Assignment],
        bindings: list[ir_module.InstancePortBinding],
    ) -> None:
        self._module = module
        self._protocol = protocol
        self._label = label
        self._assignments = assignments
        self._bindings = bindings
        self._children = {
            (owner.instance.name, port.name): port
            for owner, child in zip(
                module.elaborated_instances,
                module.children,
                strict=True,
            )
            for port in child.ports
        }
        self._top = {port.name: port for port in module.ports}

    def _port(self, owner: str, name: str) -> ir_module.Port:
        try:
            port = (
                self._top[name]
                if owner == self._module.name
                else self._children[(owner, name)]
            )
        except KeyError as error:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"unknown {self._label} endpoint '{owner}.{name}'"
            ) from error
        if port.protocol is not self._protocol:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"hierarchical endpoint '{owner}.{name}' is not {self._label}"
            )
        return port

    def _field_name(self, endpoint: str, signal: object) -> str:
        if self._protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            assert isinstance(signal, ir_interfaces.ReadyValidSignal)
            return ir_interfaces.ready_valid_field_name(endpoint, signal)
        assert isinstance(signal, ir_interfaces.CreditSignal)
        return protocol_shared.credit_field_name(endpoint, signal)

    def source(
        self,
        owner: str,
        name: str,
        signal: ir_interfaces.ReadyValidSignal | ir_interfaces.CreditSignal,
        type_: ir_types.HardwareType,
    ) -> expr.Expression:
        self._port(owner, name)
        if owner != self._module.name:
            return expr.InstanceOutputRef(
                owner,
                self._field_name(name, signal),
                type_,
            )
        if self._protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            assert isinstance(signal, ir_interfaces.ReadyValidSignal)
            return expr.ReadyValidRef(name, signal, type_)
        assert isinstance(signal, ir_interfaces.CreditSignal)
        return expr.CreditRef(name, signal, type_)

    def drive(
        self,
        owner: str,
        name: str,
        signal: ir_interfaces.ReadyValidSignal | ir_interfaces.CreditSignal,
        value: expr.Expression,
    ) -> None:
        port = self._port(owner, name)
        if owner == self._module.name:
            self._assignments.append(ir_module.Assignment(port, value, signal))
        else:
            self._bindings.append(ir_module.InstancePortBinding(
                owner,
                self._field_name(name, signal),
                value,
            ))


def _route_ready_valid_stream(
    *,
    source_owner: str,
    source_name: str,
    destination_owner: str,
    destination_name: str,
    payload_type: ir_types.HardwareType,
    depth: int,
    domain: str | None,
    fifo_name: str,
    duplicate_error: str,
    fifos: list[Fifo],
    source_value: _StreamSource,
    drive: _StreamDrive,
) -> None:
    """Route one validated ready/valid edge through its optional FIFO."""

    if depth:
        if any(fifo.name == fifo_name for fifo in fifos):
            raise protocol_shared.ProtocolSimulationLoweringError(duplicate_error)
        destination_ready = source_value(
            destination_owner,
            destination_name,
            ir_interfaces.ReadyValidSignal.READY,
            ir_types.BitType(),
        )
        pop = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            destination_ready,
            expr.FifoRef(fifo_name, FifoSignal.VALID, ir_types.BitType()),
        )
        fifos.append(Fifo(
            fifo_name,
            payload_type,
            depth,
            source_value(
                source_owner,
                source_name,
                ir_interfaces.ReadyValidSignal.PAYLOAD,
                payload_type,
            ),
            source_value(
                source_owner,
                source_name,
                ir_interfaces.ReadyValidSignal.VALID,
                ir_types.BitType(),
            ),
            pop,
            domain=domain,
        ))
        payload = expr.FifoRef(fifo_name, FifoSignal.FRONT, payload_type)
        valid = expr.FifoRef(fifo_name, FifoSignal.VALID, ir_types.BitType())
        ready = expr.FifoRef(fifo_name, FifoSignal.READY, ir_types.BitType())
    else:
        payload = source_value(
            source_owner,
            source_name,
            ir_interfaces.ReadyValidSignal.PAYLOAD,
            payload_type,
        )
        valid = source_value(
            source_owner,
            source_name,
            ir_interfaces.ReadyValidSignal.VALID,
            ir_types.BitType(),
        )
        ready = source_value(
            destination_owner,
            destination_name,
            ir_interfaces.ReadyValidSignal.READY,
            ir_types.BitType(),
        )
    drive(
        destination_owner,
        destination_name,
        ir_interfaces.ReadyValidSignal.PAYLOAD,
        payload,
    )
    drive(
        destination_owner,
        destination_name,
        ir_interfaces.ReadyValidSignal.VALID,
        valid,
    )
    drive(
        source_owner,
        source_name,
        ir_interfaces.ReadyValidSignal.READY,
        ready,
    )

def lower_ready_valid_hierarchy(module: ir_module.Module) -> ir_module.Module:
    """Erase direct/buffered ready/valid hierarchy into primitive state."""

    if not (module.elaborated_instances or module.instances or module.children):
        return lower_ready_valid_module(module)
    if len(module.elaborated_instances) != len(module.children):
        raise protocol_shared.ProtocolSimulationLoweringError(
            f"module '{module.name}' has incomplete elaborated hierarchy"
        )
    lowered_children = tuple(
        lower_ready_valid_hierarchy(child) for child in module.children
    )
    assignments = list(module.assignments)
    bindings = list(module.instance_bindings)
    fifos = list(module.fifos)
    router = _HierarchicalProtocolRouter(
        module,
        ir_interfaces.InterfaceProtocol.READY_VALID,
        "ready/valid",
        assignments,
        bindings,
    )

    for connection in module.hierarchical_connections:
        if (
            connection.source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or connection.destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or connection.request_buffer_depth
            or connection.response_buffer_depth
            or connection.adapter is not None
            or connection.crossing is not None
        ):
            raise protocol_shared.ProtocolSimulationLoweringError(
                "primitive hierarchy supports only direct or buffered "
                "ready/valid connections"
            )
        source = connection.source
        destination = connection.destination
        identity = hashlib.sha256(
            (
                f"{module.name}|{source.owner}.{source.name}|"
                f"{destination.owner}.{destination.name}|"
                f"{connection.buffer_depth}|{source.payload_type}"
            ).encode("utf-8")
        ).hexdigest()[:20]
        _route_ready_valid_stream(
            source_owner=source.owner,
            source_name=source.name,
            destination_owner=destination.owner,
            destination_name=destination.name,
            payload_type=source.payload_type,
            depth=connection.buffer_depth,
            domain=source.domain,
            fifo_name=f"$zlang_protocol_buffer_{identity}",
            duplicate_error=(
                "hierarchical buffered connection has a duplicate physical "
                "FIFO identity"
            ),
            fifos=fifos,
            source_value=router.source,
            drive=router.drive,
        )

    prepared = replace(
        module,
        assignments=tuple(assignments),
        instance_bindings=tuple(bindings),
        fifos=tuple(fifos),
        children=lowered_children,
        hierarchical_connections=(),
        protocol_endpoints=(),
    )
    return lower_ready_valid_module(prepared)


def lower_request_response_hierarchy(module: ir_module.Module) -> ir_module.Module:
    """Erase hierarchical request/response links before primitive planning.

    A request/response connection is already represented by semantic analysis
    as two exact ready/valid channel edges.  This lowering keeps that ownership
    in the compiler: child interfaces become scalar ports, the two channel
    directions become ordinary instance bindings, and optional directional
    buffers become compiler-owned FIFOs.  Neither hierarchy nor transaction
    protocol concepts cross the SimulationPlan boundary.
    """

    if not (module.elaborated_instances or module.instances or module.children):
        return lower_request_response_module(module)
    if len(module.elaborated_instances) != len(module.children):
        raise protocol_shared.ProtocolSimulationLoweringError(
            f"module '{module.name}' has incomplete elaborated hierarchy"
        )

    lowered_children = tuple(
        lower_request_response_hierarchy(child) for child in module.children
    )
    if not module.request_response_connections:
        return replace(module, children=lowered_children)

    child_names = {item.instance.name for item in module.elaborated_instances}
    child_interfaces = {
        (owner.instance.name, interface.name): interface
        for owner, child in zip(
            module.elaborated_instances,
            module.children,
            strict=True,
        )
        for interface in child.request_responses
    }
    assignments = list(module.assignments)
    bindings = list(module.instance_bindings)
    fifos = list(module.fifos)

    def interface_for(owner: str, name: str) -> ir_module.RequestResponseInterface:
        try:
            return child_interfaces[(owner, name)]
        except KeyError as error:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"unknown request/response endpoint '{owner}.{name}'"
            ) from error

    def source_value(
        owner: str,
        name: str,
        channel: ir_interfaces.RequestResponseChannel,
        signal: ir_interfaces.ReadyValidSignal,
        type_,
    ) -> expr.Expression:
        if owner not in child_names:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "hierarchical request/response endpoints must name physical "
                f"children, found '{owner}.{name}'"
            )
        interface_for(owner, name)
        return expr.InstanceOutputRef(
            owner,
            protocol_shared.request_response_field_name(name, channel, signal),
            type_,
        )

    def drive(
        owner: str,
        name: str,
        channel: ir_interfaces.RequestResponseChannel,
        signal: ir_interfaces.ReadyValidSignal,
        value: expr.Expression,
    ) -> None:
        if owner not in child_names:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "hierarchical request/response endpoints must name physical "
                f"children, found '{owner}.{name}'"
            )
        interface_for(owner, name)
        bindings.append(
            ir_module.InstancePortBinding(
                owner,
                protocol_shared.request_response_field_name(name, channel, signal),
                value,
            )
        )

    consumed_edges = []
    for connection in module.request_response_connections:
        if connection.ordering is not ir_interfaces.RequestResponseOrdering.IN_ORDER:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "hierarchical request/response lowering currently requires "
                "ordering in_order"
            )
        for channel, edge, payload_type, depth in (
            (
                ir_interfaces.RequestResponseChannel.REQUEST,
                connection.request,
                connection.request_type,
                connection.request.request_buffer_depth,
            ),
            (
                ir_interfaces.RequestResponseChannel.RESPONSE,
                connection.response,
                connection.response_type,
                connection.response.response_buffer_depth,
            ),
        ):
            if (
                edge.source.channel is not channel
                or edge.destination.channel is not channel
                or edge.source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
                or edge.destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
                or edge.source.payload_type != payload_type
                or edge.destination.payload_type != payload_type
                or edge.buffer_depth
                or edge.adapter is not None
                or edge.crossing is not None
            ):
                raise protocol_shared.ProtocolSimulationLoweringError(
                    "request/response hierarchy contains a malformed channel edge"
                )
            consumed_edges.append(edge)
            source = edge.source
            destination = edge.destination
            identity = hashlib.sha256(
                (
                    f"{connection.semantic_id}|{channel.value}|{depth}|"
                    f"{payload_type}"
                ).encode("utf-8")
            ).hexdigest()[:20]
            _route_ready_valid_stream(
                source_owner=source.owner,
                source_name=source.name,
                destination_owner=destination.owner,
                destination_name=destination.name,
                payload_type=payload_type,
                depth=depth,
                domain=connection.clock_domain or source.domain,
                fifo_name=f"$zlang_request_response_buffer_{identity}",
                duplicate_error=(
                    "hierarchical request/response buffer has a duplicate "
                    "physical FIFO identity"
                ),
                fifos=fifos,
                source_value=lambda owner, name, signal, type_: source_value(
                    owner, name, channel, signal, type_
                ),
                drive=lambda owner, name, signal, value: drive(
                    owner, name, channel, signal, value
                ),
            )

    missing = tuple(edge for edge in consumed_edges if edge not in module.hierarchical_connections)
    if missing:
        raise protocol_shared.ProtocolSimulationLoweringError(
            "request/response descriptor is not backed by exact hierarchy edges"
        )
    remaining_edges = tuple(
        edge
        for edge in module.hierarchical_connections
        if edge not in consumed_edges
    )
    if any(
        edge.source.channel is not None or edge.destination.channel is not None
        for edge in remaining_edges
    ):
        raise protocol_shared.ProtocolSimulationLoweringError(
            "orphan request/response channel edge remains after lowering"
        )

    prepared = replace(
        module,
        assignments=tuple(assignments),
        instance_bindings=tuple(bindings),
        fifos=tuple(fifos),
        children=lowered_children,
        hierarchical_connections=remaining_edges,
        request_response_connections=(),
        protocol_endpoints=tuple(
            endpoint
            for endpoint in module.protocol_endpoints
            if endpoint.channel is None
        ),
    )
    if prepared.request_responses:
        return lower_request_response_module(prepared)
    return prepared


def lower_credit_hierarchy(module: ir_module.Module) -> ir_module.Module:
    """Erase direct hierarchical credit links into scalar child bindings."""

    if not (module.elaborated_instances or module.instances or module.children):
        return lower_credit_module(module)
    if len(module.elaborated_instances) != len(module.children):
        raise protocol_shared.ProtocolSimulationLoweringError(
            f"module '{module.name}' has incomplete elaborated hierarchy"
        )

    lowered_children = tuple(
        lower_credit_hierarchy(child) for child in module.children
    )
    credit_edges = tuple(
        edge
        for edge in module.hierarchical_connections
        if (
            edge.source.protocol is ir_interfaces.InterfaceProtocol.CREDIT
            or edge.destination.protocol is ir_interfaces.InterfaceProtocol.CREDIT
        )
    )
    if not credit_edges:
        return replace(module, children=lowered_children)

    assignments = list(module.assignments)
    bindings = list(module.instance_bindings)
    router = _HierarchicalProtocolRouter(
        module,
        ir_interfaces.InterfaceProtocol.CREDIT,
        "credit",
        assignments,
        bindings,
    )

    for edge in credit_edges:
        if (
            edge.source.protocol is not ir_interfaces.InterfaceProtocol.CREDIT
            or edge.destination.protocol is not ir_interfaces.InterfaceProtocol.CREDIT
            or edge.source.payload_type != edge.destination.payload_type
            or edge.source.capacity != edge.destination.capacity
            or edge.buffer_depth
            or edge.request_buffer_depth
            or edge.response_buffer_depth
            or edge.adapter is not None
            or edge.crossing is not None
            or edge.source.channel is not None
            or edge.destination.channel is not None
        ):
            raise protocol_shared.ProtocolSimulationLoweringError(
                "hierarchical credit connection is not an exact direct link"
            )
        source = edge.source
        destination = edge.destination
        for signal, type_ in (
            (ir_interfaces.CreditSignal.PAYLOAD, source.payload_type),
            (ir_interfaces.CreditSignal.SEND, ir_types.BitType()),
        ):
            router.drive(
                destination.owner,
                destination.name,
                signal,
                router.source(source.owner, source.name, signal, type_),
            )
        router.drive(
            source.owner,
            source.name,
            ir_interfaces.CreditSignal.RETURN,
            router.source(
                destination.owner,
                destination.name,
                ir_interfaces.CreditSignal.RETURN,
                ir_types.BitType(),
            ),
        )

    prepared = replace(
        module,
        assignments=tuple(assignments),
        instance_bindings=tuple(bindings),
        children=lowered_children,
        hierarchical_connections=tuple(
            edge
            for edge in module.hierarchical_connections
            if edge not in credit_edges
        ),
        protocol_endpoints=tuple(
            endpoint
            for endpoint in module.protocol_endpoints
            if endpoint.protocol is not ir_interfaces.InterfaceProtocol.CREDIT
        ),
    )
    if any(
        port.protocol is ir_interfaces.InterfaceProtocol.CREDIT for port in prepared.ports
    ):
        return lower_credit_module(prepared)
    return prepared


def lower_aggregate_protocol_hierarchy(
    module: ir_module.Module,
    *,
    _physical_child: bool = False,
) -> ir_module.Module:
    """Expand aggregate protocol composition into ordinary member edges.

    Semantic analysis owns the aggregate schema and exact role direction.  A
    non-delegating connection already carries one typed physical edge per
    member.  Same-role top delegation is expanded here from the validated
    schema.  Scalar wire members become ordinary instance bindings; protocol
    members continue through their existing family-specific lowerers.
    """

    if not (module.elaborated_instances or module.instances or module.children):
        if module.aggregate_protocol_connections:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "aggregate protocol connection has no physical hierarchy"
            )
        if _physical_child and module.aggregate_protocol_endpoints:
            return replace(module, aggregate_protocol_endpoints=())
        return module
    if len(module.elaborated_instances) != len(module.children):
        raise protocol_shared.ProtocolSimulationLoweringError(
            f"module '{module.name}' has incomplete elaborated hierarchy"
        )

    lowered_children = tuple(
        lower_aggregate_protocol_hierarchy(child, _physical_child=True)
        for child in module.children
    )
    if not module.aggregate_protocol_connections:
        return replace(
            module,
            children=lowered_children,
            aggregate_protocol_endpoints=(
                () if _physical_child else module.aggregate_protocol_endpoints
            ),
        )

    children = {
        item.instance.name: child
        for item, child in zip(
            module.elaborated_instances,
            module.children,
            strict=True,
        )
    }
    top_aggregates = {
        endpoint.name: endpoint
        for endpoint in module.aggregate_protocol_endpoints
    }
    child_aggregates = {
        (owner, endpoint.name): endpoint
        for owner, child in children.items()
        for endpoint in child.aggregate_protocol_endpoints
    }
    top_ports = {port.name: port for port in module.ports}
    child_ports = {
        (owner, port.name): port
        for owner, child in children.items()
        for port in child.ports
    }
    edges = list(module.hierarchical_connections)

    def endpoint(
        owner: str,
        port: ir_module.Port,
    ) -> ir_module.ProtocolEndpoint:
        return ir_module.ProtocolEndpoint(
            owner,
            port.name,
            port.direction,
            port.protocol,
            port.type,
            port.capacity,
            port.domain,
        )

    def aggregate_members(
        aggregate: ir_module.AggregateProtocolEndpoint,
    ) -> dict[str, object]:
        result = {member.name: member for member in aggregate.members}
        if len(result) != len(aggregate.members):
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"aggregate endpoint '{aggregate.name}' has duplicate members"
            )
        return result

    for connection in module.aggregate_protocol_connections:
        if not connection.delegation:
            if connection.crossing is not None:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    "aggregate protocol crossing must be lowered by CDC before "
                    "primitive simulation"
                )
            if "." not in connection.source or "." not in connection.destination:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    "aggregate composition must select two child endpoints"
                )
            source_owner, source_name = connection.source.split(".", 1)
            destination_owner, destination_name = connection.destination.split(
                ".", 1
            )
            try:
                source_aggregate = child_aggregates[(source_owner, source_name)]
                destination_aggregate = child_aggregates[
                    (destination_owner, destination_name)
                ]
            except KeyError as error:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    "aggregate composition names an unknown child endpoint"
                ) from error
            if (
                source_aggregate.protocol != connection.protocol
                or destination_aggregate.protocol != connection.protocol
                or source_aggregate.specialization_identity
                != connection.specialization_identity
                or destination_aggregate.specialization_identity
                != connection.specialization_identity
            ):
                raise protocol_shared.ProtocolSimulationLoweringError(
                    "aggregate composition metadata disagrees with its endpoints"
                )
            source_members = aggregate_members(source_aggregate)
            destination_members = aggregate_members(destination_aggregate)
            if source_members != destination_members:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    "aggregate composition member schemas do not match"
                )
            for member_name, member in source_members.items():
                if (
                    member.source_role == source_aggregate.role
                    and member.sink_role == destination_aggregate.role
                ):
                    physical_source = (source_owner, source_name)
                    physical_destination = (destination_owner, destination_name)
                elif (
                    member.source_role == destination_aggregate.role
                    and member.sink_role == source_aggregate.role
                ):
                    physical_source = (destination_owner, destination_name)
                    physical_destination = (source_owner, source_name)
                else:
                    raise protocol_shared.ProtocolSimulationLoweringError(
                        f"aggregate member '{member_name}' has incompatible roles"
                    )
                expected_source = (
                    physical_source[0],
                    f"{physical_source[1]}__{member_name}",
                )
                expected_destination = (
                    physical_destination[0],
                    f"{physical_destination[1]}__{member_name}",
                )
                matches = tuple(
                    edge
                    for edge in edges
                    if (
                        (edge.source.owner, edge.source.name) == expected_source
                        and (
                            edge.destination.owner,
                            edge.destination.name,
                        )
                        == expected_destination
                        and edge.source.protocol is member.protocol
                        and edge.destination.protocol is member.protocol
                        and edge.source.payload_type == member.payload_type
                        and edge.destination.payload_type == member.payload_type
                    )
                )
                if len(matches) != 1:
                    raise protocol_shared.ProtocolSimulationLoweringError(
                        f"aggregate member '{member_name}' does not have one "
                        "exact physical edge"
                    )
            continue
        if connection.crossing is not None:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "aggregate protocol delegation cannot carry a crossing"
            )
        if "." not in connection.destination or "." in connection.source:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "aggregate delegation must connect one top endpoint to one child"
            )
        child_owner, child_name = connection.destination.split(".", 1)
        try:
            top = top_aggregates[connection.source]
            child = child_aggregates[(child_owner, child_name)]
        except KeyError as error:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "aggregate delegation names an unknown endpoint"
            ) from error
        if (
            top.protocol != connection.protocol
            or child.protocol != connection.protocol
            or top.specialization_identity != connection.specialization_identity
            or child.specialization_identity != connection.specialization_identity
            or top.role != child.role
        ):
            raise protocol_shared.ProtocolSimulationLoweringError(
                "aggregate delegation metadata disagrees with its endpoints"
            )
        top_members = aggregate_members(top)
        child_members = aggregate_members(child)
        if top_members != child_members:
            raise protocol_shared.ProtocolSimulationLoweringError(
                "aggregate delegation member schemas do not match"
            )
        for member_name in top_members:
            top_port_name = f"{top.name}__{member_name}"
            child_port_name = f"{child.name}__{member_name}"
            try:
                top_port = top_ports[top_port_name]
                child_port = child_ports[(child_owner, child_port_name)]
            except KeyError as error:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    "aggregate delegation has an incomplete physical member"
                ) from error
            if (
                top_port.protocol is not child_port.protocol
                or top_port.type != child_port.type
                or top_port.capacity != child_port.capacity
                or top_port.direction is not child_port.direction
            ):
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"aggregate member '{member_name}' physical ports disagree"
                )
            if top_port.direction is ir_module.PortDirection.INPUT:
                source = endpoint(module.name, top_port)
                destination = endpoint(child_owner, child_port)
            else:
                source = endpoint(child_owner, child_port)
                destination = endpoint(module.name, top_port)
            edges.append(ir_module.HierarchicalConnection(source, destination))

    assignments = list(module.assignments)
    bindings = list(module.instance_bindings)
    wire_edges = tuple(
        edge
        for edge in edges
        if (
            edge.source.protocol is ir_interfaces.InterfaceProtocol.WIRE
            or edge.destination.protocol is ir_interfaces.InterfaceProtocol.WIRE
        )
    )
    for edge in wire_edges:
        if (
            edge.source.protocol is not ir_interfaces.InterfaceProtocol.WIRE
            or edge.destination.protocol is not ir_interfaces.InterfaceProtocol.WIRE
            or edge.source.payload_type != edge.destination.payload_type
            or edge.buffer_depth
            or edge.request_buffer_depth
            or edge.response_buffer_depth
            or edge.adapter is not None
            or edge.crossing is not None
            or edge.source.channel is not None
            or edge.destination.channel is not None
        ):
            raise protocol_shared.ProtocolSimulationLoweringError(
                "aggregate scalar member is not an exact direct wire"
            )
        if edge.source.owner == module.name:
            try:
                source_value: expr.Expression = expr.InputRef(
                    edge.source.name,
                    top_ports[edge.source.name].type,
                )
            except KeyError as error:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"unknown aggregate top input '{edge.source.name}'"
                ) from error
        else:
            source_value = expr.InstanceOutputRef(
                edge.source.owner,
                edge.source.name,
                edge.source.payload_type,
            )
        if edge.destination.owner == module.name:
            try:
                target = top_ports[edge.destination.name]
            except KeyError as error:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"unknown aggregate top output '{edge.destination.name}'"
                ) from error
            assignments.append(ir_module.Assignment(target, source_value))
        else:
            bindings.append(
                ir_module.InstancePortBinding(
                    edge.destination.owner,
                    edge.destination.name,
                    source_value,
                )
            )

    return replace(
        module,
        assignments=tuple(assignments),
        instance_bindings=tuple(bindings),
        children=lowered_children,
        hierarchical_connections=tuple(
            edge for edge in edges if edge not in wire_edges
        ),
        aggregate_protocol_connections=(),
        aggregate_protocol_endpoints=(),
        protocol_endpoints=tuple(
            endpoint
            for endpoint in module.protocol_endpoints
            if endpoint.protocol is not ir_interfaces.InterfaceProtocol.WIRE
        ),
    )


def lower_protocol_hierarchy(module: ir_module.Module) -> ir_module.Module:
    """Run hierarchy protocol erasure in its one authoritative order."""

    if _has_aggregate_protocol_hierarchy(module):
        module = lower_aggregate_protocol_hierarchy(module, _physical_child=True)
    if _has_request_response_hierarchy(module):
        module = lower_request_response_hierarchy(module)
    if _has_hierarchical_protocol(module, ir_interfaces.InterfaceProtocol.CREDIT):
        module = lower_credit_hierarchy(module)
    if _has_protocol_surface(module):
        module = lower_ready_valid_hierarchy(module)
    return module


def _has_request_response_hierarchy(module: ir_module.Module) -> bool:
    return bool(
        module.request_response_connections
        or any(_has_request_response_hierarchy(child) for child in module.children)
    )


def _has_hierarchical_protocol(
    module: ir_module.Module,
    protocol: ir_interfaces.InterfaceProtocol,
) -> bool:
    return bool(
        any(
            edge.source.protocol is protocol
            or edge.destination.protocol is protocol
            for edge in module.hierarchical_connections
        )
        or any(
            _has_hierarchical_protocol(child, protocol)
            for child in module.children
        )
    )


def _has_aggregate_protocol_hierarchy(module: ir_module.Module) -> bool:
    return bool(
        module.aggregate_protocol_connections
        or module.aggregate_protocol_endpoints
        or any(
            _has_aggregate_protocol_hierarchy(child) for child in module.children
        )
    )


def _has_protocol_surface(module: ir_module.Module) -> bool:
    return bool(
        any(port.protocol is not ir_interfaces.InterfaceProtocol.WIRE for port in module.ports)
        or module.hierarchical_connections
        or any(_has_protocol_surface(child) for child in module.children)
    )
