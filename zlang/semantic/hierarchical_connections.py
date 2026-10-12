# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned direct connection lowering and protocol-cycle validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin, SourceSpan

from . import module_pipeline
from .errors import SemanticError
from . import observations as semantic_observations
from .connection_endpoints import HierarchicalEndpointResolver


@dataclass(frozen=True)
class HierarchicalConnectionProduct:
    connections: tuple[ir_module.Connection, ...]
    hierarchical_connections: tuple[ir_module.HierarchicalConnection, ...]
    request_response_connections: tuple[ir_module.RequestResponseConnection, ...]
    aggregate_protocol_connections: tuple[ir_module.AggregateProtocolConnection, ...]
    instance_protocol_transfers: tuple[
        tuple[str, str, ir_expr.Expression], ...
    ]
    assigned_outputs: frozenset[tuple[str, object | None, object | None]]


class HierarchicalConnectionAnalyzer:
    """Own chain expansion and typed hierarchical protocol connections."""

    def analyze(
        self,
        preparation: module_pipeline.DeclarationPreparationProduct,
        hardware: module_pipeline.HardwareInterfaceProduct,
        state_storage: module_pipeline.StateStoragePreparationProduct,
        behavior: module_pipeline.ModuleBehaviorProduct,
        instances: Any,
        endpoint_resolver: HierarchicalEndpointResolver,
    ) -> HierarchicalConnectionProduct:
        module = preparation.module
        module_context = state_storage.expression_context
        symbols = state_storage.symbols
        child_irs = dict(instances.child_irs)
        aggregate_protocol_endpoints = list(
            state_storage.aggregate_protocol_endpoints
        )
        connections = list(hardware.connections)
        hierarchical_connections: list[ir_module.HierarchicalConnection] = []
        request_response_connections: list[
            ir_module.RequestResponseConnection
        ] = []
        aggregate_protocol_connections: list[
            ir_module.AggregateProtocolConnection
        ] = []
        assigned_outputs = set(behavior.assigned_outputs)
        concrete_hierarchical_endpoint = endpoint_resolver.concrete
        endpoint = endpoint_resolver.protocol
        request_response_endpoint = endpoint_resolver.request_response
        hierarchical_connection_declarations = list(module.connections)
        instance_declarations = {item.name: item for item in module.instances}
        for declaration in module.instances:
            semantic_observations.remember_definition_target(
                module_context,
                declaration,
                semantic_observations.declaration_origin(
                    declaration.name_origin or declaration.origin,
                    f"instance {declaration.name}",
                    module_context,
                ),
                name=declaration.name,
                kind="instance",
            )
        for chain in module.connection_chains:
            if len(chain.endpoints) < 3:
                raise SemanticError("connection chain requires at least one intermediate instance")
            if any("[" in item for item in chain.endpoints):
                raise SemanticError(
                    "instance arrays require explicit indexed connections; "
                    "connection-chain array endpoints are not supported"
                )
            current_source = chain.endpoints[0]
            current_origins = (
                chain.endpoint_name_origins[0]
                if chain.endpoint_name_origins else ()
            )
            for chain_index, instance_name in enumerate(chain.endpoints[1:-1], 1):
                if "." in instance_name or "[" in instance_name:
                    raise SemanticError(
                        "connection-chain intermediates must be bare physical instance names"
                    )
                declaration = instance_declarations.get(instance_name)
                child = child_irs.get(instance_name)
                if declaration is None or child is None:
                    raise SemanticError(
                        f"connection-chain intermediate '{instance_name}' is not an instance"
                    )
                if declaration.array_length is not None:
                    raise SemanticError(
                        f"connection-chain intermediate '{instance_name}' cannot be an instance array"
                    )
                if child.module_signature is None:
                    raise SemanticError(
                        f"connection-chain instance '{instance_name}' must conform to one named module interface"
                    )
                protocol_inputs = tuple(
                    port for port in child.ports
                    if port.direction is ir_module.PortDirection.INPUT
                    and port.protocol is not ir_interfaces.InterfaceProtocol.WIRE
                )
                protocol_outputs = tuple(
                    port for port in child.ports
                    if port.direction is ir_module.PortDirection.OUTPUT
                    and port.protocol is not ir_interfaces.InterfaceProtocol.WIRE
                )
                if len(protocol_inputs) != 1 or len(protocol_outputs) != 1:
                    raise SemanticError(
                        f"connection-chain instance '{instance_name}' requires exactly "
                        "one protocol input and one protocol output in its named interface"
                    )
                hierarchical_connection_declarations.append(ast.ConnectionDecl(
                    current_source,
                    f"{instance_name}.{protocol_inputs[0].name}",
                    source_name_origins=current_origins,
                    destination_name_origins=(
                        chain.endpoint_name_origins[chain_index]
                        if chain.endpoint_name_origins else ()
                    ),
                ))
                current_source = f"{instance_name}.{protocol_outputs[0].name}"
                current_origins = (
                    chain.endpoint_name_origins[chain_index]
                    if chain.endpoint_name_origins else ()
                )
            hierarchical_connection_declarations.append(ast.ConnectionDecl(
                current_source,
                chain.endpoints[-1],
                source_name_origins=current_origins,
                destination_name_origins=(
                    chain.endpoint_name_origins[-1]
                    if chain.endpoint_name_origins else ()
                ),
            ))

        hierarchical_connection_declarations = [
            replace(
                declaration,
                source=concrete_hierarchical_endpoint(declaration.source),
                destination=concrete_hierarchical_endpoint(declaration.destination),
            )
            for declaration in hierarchical_connection_declarations
        ]

        array_rv_sources: set[tuple[str, str]] = set()
        array_rv_destinations: set[tuple[str, str]] = set()

        def record_connection_endpoint_definitions(
            path: str,
            name_origins: tuple[SourceSpan | None, ...],
        ) -> None:
            """Publish exact semantic uses of names in one protocol endpoint."""

            if not name_origins:
                return
            parts = path.split(".")
            owner = parts[0].split("[", 1)[0]

            def occurrence(span: SourceSpan | None, name: str, kind: str) -> SourceOrigin | None:
                return semantic_observations.declaration_origin(
                    span,
                    f"{kind} {name}",
                    module_context,
                )

            if len(parts) == 1:
                symbol = state_storage.value_symbols.get(owner) or symbols.get(owner)
                if symbol is not None:
                    semantic_observations.record_definition(
                        module_context,
                        occurrence(name_origins[0], owner, "name"),
                        module_context.services.tooling.definition_targets.get(id(symbol)),
                        name=owner,
                        kind=semantic_observations.definition_kind(symbol),
                    )
                return

            declaration = instance_declarations.get(owner)
            if declaration is None:
                return
            semantic_observations.record_definition(
                module_context,
                occurrence(name_origins[0], owner, "name"),
                module_context.services.tooling.definition_targets.get(id(declaration)),
                name=owner,
                kind="instance",
            )
            if len(parts) != 2 or len(name_origins) < 2:
                return
            child_module = behavior.known_modules.get(declaration.module)
            if child_module is None:
                return
            member = parts[1]
            for port_declaration in child_module.ports:
                names = port_declaration.names or (port_declaration.name,)
                if member not in names:
                    continue
                index = names.index(member)
                target_span = (
                    port_declaration.name_origins[index]
                    if index < len(port_declaration.name_origins)
                    else port_declaration.origin
                )
                semantic_observations.record_definition(
                    module_context,
                    occurrence(name_origins[1], member, "name"),
                    semantic_observations.declaration_origin(
                        target_span,
                        f"port {member}",
                        module_context,
                        source_unit=child_module.source_identity,
                    ),
                    name=member,
                    kind="port",
                )
                return

        def array_physical_owner(owner: str) -> bool:
            match = re.fullmatch(
                r"(?P<array>[A-Za-z_][A-Za-z0-9_]*)\[[0-9]+\]", owner
            )
            return bool(
                match is not None
                and match.group("array") in module_context.scope.instance_arrays
            )

        for declaration in hierarchical_connection_declarations:
            record_connection_endpoint_definitions(
                declaration.source, declaration.source_name_origins
            )
            record_connection_endpoint_definitions(
                declaration.destination, declaration.destination_name_origins
            )
            if "." not in declaration.source and "." not in declaration.destination:
                source_top = next(
                    (
                        item for item in aggregate_protocol_endpoints
                        if item.name == declaration.source
                    ),
                    None,
                )
                destination_top = next(
                    (
                        item for item in aggregate_protocol_endpoints
                        if item.name == declaration.destination
                    ),
                    None,
                )
                if source_top is None and destination_top is None:
                    continue
                if source_top is None or destination_top is None:
                    raise SemanticError(
                        "aggregate protocol pass-through requires two aggregate endpoints"
                    )
                if source_top.protocol != destination_top.protocol:
                    raise SemanticError(
                        "aggregate protocol pass-through requires the same protocol"
                    )
                if (
                    source_top.specialization_identity
                    != destination_top.specialization_identity
                ):
                    raise SemanticError(
                        "aggregate protocol pass-through specialization arguments "
                        "do not match"
                    )
                if source_top.role == destination_top.role:
                    raise SemanticError(
                        "aggregate protocol pass-through requires complementary roles"
                    )
                if declaration.buffer_depth or declaration.request_buffer_depth \
                        or declaration.response_buffer_depth:
                    raise SemanticError(
                        "aggregate protocol pass-through does not accept buffering"
                    )
                if declaration.adapter is not None:
                    raise SemanticError(
                        "aggregate protocol pass-through does not accept adapters"
                    )
                source_members = {item.name: item for item in source_top.members}
                destination_members = {
                    item.name: item for item in destination_top.members
                }
                if set(source_members) != set(destination_members):
                    raise SemanticError(
                        "aggregate protocol pass-through member sets do not match"
                    )
                aggregate_crossing = (
                    ir_cdc.Crossing(
                        ir_cdc.CrossingKind(declaration.crossing.kind.value),
                        (
                            preparation.type_resolver._eval_storage_depth(
                                declaration.crossing.depth,
                                kind="async_fifo",
                                name=(
                                    f"{declaration.source}->{declaration.destination}"
                                ),
                            )
                            if declaration.crossing.depth is not None
                            else None
                        ),
                    )
                    if declaration.crossing is not None
                    else None
                )
                if aggregate_crossing is not None:
                    if aggregate_crossing.kind is not ir_cdc.CrossingKind.ASYNC_FIFO:
                        raise SemanticError(
                            "aggregate protocol crossings currently support only async_fifo"
                        )
                    depth = aggregate_crossing.depth
                    if depth is None or depth < 4 or depth & (depth - 1):
                        raise SemanticError(
                            "aggregate async_fifo depth must be a power of two and at least 4"
                        )
                    if len(source_members) != 1:
                        raise SemanticError(
                            "aggregate async_fifo crossing requires exactly one "
                            "ready/valid member"
                        )
                for member_name, member in source_members.items():
                    other = destination_members[member_name]
                    if (
                        member.protocol is not other.protocol
                        or member.payload_type != other.payload_type
                        or member.source_role != other.source_role
                        or member.sink_role != other.sink_role
                    ):
                        raise SemanticError(
                            f"aggregate protocol member '{member_name}' does not match"
                        )
                    if member.protocol not in {
                        ir_interfaces.InterfaceProtocol.WIRE,
                        ir_interfaces.InterfaceProtocol.READY_VALID,
                        ir_interfaces.InterfaceProtocol.CREDIT,
                    }:
                        raise SemanticError(
                            f"aggregate protocol member '{member_name}' cannot yet "
                            "use top-level pass-through"
                        )
                    if (
                        aggregate_crossing is not None
                        and member.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
                    ):
                        raise SemanticError(
                            "aggregate async_fifo crossing requires exactly one "
                            "ready/valid member"
                        )
                    source_port = symbols[f"{source_top.name}__{member_name}"]
                    destination_port = symbols[
                        f"{destination_top.name}__{member_name}"
                    ]
                    if source_port.direction is not ir_module.PortDirection.INPUT:
                        raise SemanticError(
                            f"aggregate pass-through source '{source_top.name}' has "
                            f"the wrong role for member '{member_name}'"
                        )
                    if destination_port.direction is not ir_module.PortDirection.OUTPUT:
                        raise SemanticError(
                            f"aggregate pass-through destination "
                            f"'{destination_top.name}' has the wrong role for member "
                            f"'{member_name}'"
                        )
                    if (
                        aggregate_crossing is None
                        and source_port.domain != destination_port.domain
                    ):
                        raise SemanticError(
                            "aggregate protocol pass-through endpoints must share "
                            "a clock domain"
                        )
                    if (
                        aggregate_crossing is not None
                        and source_port.domain == destination_port.domain
                    ):
                        raise SemanticError(
                            "aggregate async_fifo crossing requires different domains"
                        )
                    connections.append(
                        ir_module.Connection(
                            source_port,
                            destination_port,
                            crossing=aggregate_crossing,
                        )
                    )
                aggregate_protocol_connections.append(
                    ir_module.AggregateProtocolConnection(
                        declaration.source,
                        declaration.destination,
                        source_top.protocol,
                        source_top.specialization_identity,
                        crossing=aggregate_crossing,
                    )
                )
                continue
            source_parts = declaration.source.split(".")
            destination_parts = declaration.destination.split(".")
            source_child = child_irs.get(source_parts[0]) if len(source_parts) == 2 else None
            destination_child = child_irs.get(destination_parts[0]) if len(destination_parts) == 2 else None
            source_aggregate = (
                next((item for item in source_child.aggregate_protocol_endpoints if item.name == source_parts[1]), None)
                if source_child is not None and len(source_parts) == 2 else None
            )
            destination_aggregate = (
                next((item for item in destination_child.aggregate_protocol_endpoints if item.name == destination_parts[1]), None)
                if destination_child is not None and len(destination_parts) == 2 else None
            )
            top_aggregate = next(
                (item for item in aggregate_protocol_endpoints if item.name == source_parts[0]),
                None,
            ) if len(source_parts) == 1 else None
            # A same-role top/child connection is a delegation regardless of its
            # source spelling.  The physical direction of every member is fixed
            # by the protocol roles, not by which endpoint the author wrote first.
            # Normalize it so all downstream consumers see the existing top-to-
            # child delegation shape.
            reverse_delegation = (
                source_aggregate is not None
                and len(destination_parts) == 1
                and next(
                    (item for item in aggregate_protocol_endpoints
                     if item.name == destination_parts[0]),
                    None,
                ) is not None
            )
            if reverse_delegation:
                top_aggregate = next(
                    item for item in aggregate_protocol_endpoints
                    if item.name == destination_parts[0]
                )
                destination_aggregate = source_aggregate
            if top_aggregate is not None and destination_aggregate is not None:
                if top_aggregate.protocol != destination_aggregate.protocol:
                    raise SemanticError("top aggregate delegation requires the same protocol")
                if top_aggregate.specialization_identity != destination_aggregate.specialization_identity:
                    raise SemanticError("top aggregate delegation specialization arguments do not match")
                if top_aggregate.role != destination_aggregate.role:
                    raise SemanticError("top aggregate delegation requires the same role")
                if declaration.buffer_depth or declaration.adapter is not None or declaration.crossing is not None:
                    raise SemanticError("top aggregate delegation does not accept buffering, adapters, or crossings")
                source_members = {member.name: member for member in top_aggregate.members}
                destination_members = {member.name: member for member in destination_aggregate.members}
                if set(source_members) != set(destination_members):
                    raise SemanticError("top aggregate delegation member sets do not match")
                for name, member in source_members.items():
                    other = destination_members[name]
                    if (member.protocol, member.payload_type, member.source_role, member.sink_role) != (
                        other.protocol, other.payload_type, other.source_role, other.sink_role
                    ):
                        raise SemanticError(f"top aggregate delegation member '{name}' does not match")
                    if member.domain != other.domain:
                        raise SemanticError(
                            f"top aggregate delegation member '{name}' must share "
                            "a clock domain"
                        )
                normalized_top = (
                    declaration.destination if reverse_delegation
                    else declaration.source
                )
                normalized_child = (
                    declaration.source if reverse_delegation
                    else declaration.destination
                )
                aggregate_protocol_connections.append(
                    ir_module.AggregateProtocolConnection(
                        normalized_top,
                        normalized_child,
                        top_aggregate.protocol, top_aggregate.specialization_identity,
                        delegation=True,
                    )
                )
                continue
            if source_aggregate is not None or destination_aggregate is not None:
                if source_aggregate is None or destination_aggregate is None:
                    raise SemanticError("aggregate protocol connections require two aggregate endpoints")
                if source_aggregate.protocol != destination_aggregate.protocol:
                    raise SemanticError("aggregate protocol connections require the same protocol")
                if source_aggregate.specialization_identity != destination_aggregate.specialization_identity:
                    raise SemanticError("aggregate protocol specialization arguments do not match")
                if declaration.buffer_depth or declaration.adapter is not None or declaration.crossing is not None:
                    raise SemanticError("aggregate protocol connections do not accept buffering, adapters, or crossings")
                source_members = {member.name: member for member in source_aggregate.members}
                destination_members = {member.name: member for member in destination_aggregate.members}
                if set(source_members) != set(destination_members):
                    raise SemanticError("aggregate protocol member sets do not match")
                for member_name, member in source_members.items():
                    other = destination_members[member_name]
                    if member.protocol is not other.protocol or member.payload_type != other.payload_type:
                        raise SemanticError(f"aggregate protocol member '{member_name}' does not match")
                    if member.source_role == source_aggregate.role and member.sink_role == destination_aggregate.role:
                        source_path = f"{source_parts[0]}.{source_parts[1]}__{member_name}"
                        destination_path = f"{destination_parts[0]}.{destination_parts[1]}__{member_name}"
                    elif member.source_role == destination_aggregate.role and member.sink_role == source_aggregate.role:
                        source_path = f"{destination_parts[0]}.{destination_parts[1]}__{member_name}"
                        destination_path = f"{source_parts[0]}.{source_parts[1]}__{member_name}"
                    else:
                        raise SemanticError(f"aggregate protocol member '{member_name}' has incompatible roles")
                    source_endpoint = endpoint(source_path, source=True)
                    destination_endpoint = endpoint(destination_path, source=False)
                    if source_endpoint.protocol is not destination_endpoint.protocol or source_endpoint.payload_type != destination_endpoint.payload_type:
                        raise SemanticError(f"aggregate protocol member '{member_name}' endpoint mismatch")
                    if source_endpoint.domain != destination_endpoint.domain:
                        raise SemanticError("aggregate protocol endpoints must share a clock domain")
                    hierarchical_connections.append(ir_module.HierarchicalConnection(source_endpoint, destination_endpoint))
                aggregate_protocol_connections.append(
                    ir_module.AggregateProtocolConnection(
                        declaration.source, declaration.destination,
                        source_aggregate.protocol, source_aggregate.specialization_identity,
                    )
                )
                continue
            source_rr = (
                next((item for item in source_child.request_responses if item.name == source_parts[1]), None)
                if source_child is not None else None
            )
            destination_rr = (
                next((item for item in destination_child.request_responses if item.name == destination_parts[1]), None)
                if destination_child is not None else None
            )
            if source_rr is not None or destination_rr is not None:
                if source_rr is None or destination_rr is None:
                    raise SemanticError(
                        "request/response hierarchy connections require two "
                        "request/response endpoints"
                    )
                if source_rr.request_type != destination_rr.request_type:
                    raise SemanticError("request/response request payload types do not match")
                if source_rr.response_type != destination_rr.response_type:
                    raise SemanticError("request/response response payload types do not match")
                if source_rr.max_outstanding != destination_rr.max_outstanding:
                    raise SemanticError("request/response max_outstanding values do not match")
                if source_rr.ordering is not destination_rr.ordering:
                    raise SemanticError("request/response ordering contracts do not match")
                if source_rr.max_outstanding <= 0 or source_rr.ordering is not ir_interfaces.RequestResponseOrdering.IN_ORDER:
                    raise SemanticError(
                        "hierarchical request/response currently supports only "
                        "positive max_outstanding with ordering in_order"
                    )
                if declaration.buffer_depth:
                    raise SemanticError(
                        "generic 'buffer' is ambiguous on request/response connections; "
                        "use request_buffer and/or response_buffer"
                    )
                rr_edges: dict[ir_interfaces.RequestResponseChannel, ir_module.HierarchicalConnection] = {}
                for channel in (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.RequestResponseChannel.RESPONSE):
                    if channel is ir_interfaces.RequestResponseChannel.REQUEST:
                        source_path, destination_path = declaration.source, declaration.destination
                    else:
                        # The response travels in the opposite physical direction
                        # while remaining part of the same logical connection.
                        source_path, destination_path = declaration.destination, declaration.source
                    source_endpoint = request_response_endpoint(
                        source_path, source=True, channel=channel
                    )
                    destination_endpoint = request_response_endpoint(
                        destination_path, source=False, channel=channel
                    )
                    if source_endpoint.domain != destination_endpoint.domain:
                        raise SemanticError("hierarchical request/response endpoints must share a clock domain")
                    if any(
                        edge.source.owner == source_endpoint.owner
                        and edge.source.name == source_endpoint.name
                        and edge.source.channel is channel
                        for edge in hierarchical_connections
                    ):
                        raise SemanticError(
                            f"request/response endpoint '{source_endpoint.owner}.{source_endpoint.name}' "
                            f"is connected more than once on {channel.value}"
                        )
                    edge = ir_module.HierarchicalConnection(
                            source_endpoint,
                            destination_endpoint,
                            request_buffer_depth=(
                                declaration.request_buffer_depth
                                if channel is ir_interfaces.RequestResponseChannel.REQUEST else 0
                            ),
                            response_buffer_depth=(
                                declaration.response_buffer_depth
                                if channel is ir_interfaces.RequestResponseChannel.RESPONSE else 0
                            ),
                        )
                    hierarchical_connections.append(edge)
                    rr_edges[channel] = edge
                request_response_connections.append(
                    ir_module.RequestResponseConnection(
                        semantic_id=(
                            f"rr:{module.name}:{declaration.source}->{declaration.destination}"
                        ),
                        request=rr_edges[ir_interfaces.RequestResponseChannel.REQUEST],
                        response=rr_edges[ir_interfaces.RequestResponseChannel.RESPONSE],
                        request_type=source_rr.request_type,
                        response_type=source_rr.response_type,
                        max_outstanding=source_rr.max_outstanding,
                        ordering=source_rr.ordering,
                        requester=source_parts[0] if source_rr.role is ir_interfaces.RequestResponseRole.REQUESTER else destination_parts[0],
                        responder=destination_parts[0] if source_rr.role is ir_interfaces.RequestResponseRole.REQUESTER else source_parts[0],
                        clock_domain=rr_edges[ir_interfaces.RequestResponseChannel.REQUEST].source.domain,
                    reset_domain=hardware.reset,
                        reset_epoch_policy="synchronous_shared",
                        source_origin=next(
                            (
                                assignment.expression.origin
                                for assignment in source_child.assignments
                                if assignment.target.name == source_parts[1]
                                and assignment.expression.origin is not None
                            ),
                            None,
                        ),
                    )
                )
                continue
            source = endpoint(
                declaration.source,
                source=True,
                name_origins=declaration.source_name_origins,
            )
            destination = endpoint(
                declaration.destination,
                source=False,
                name_origins=declaration.destination_name_origins,
            )
            if source.protocol is not destination.protocol:
                raise SemanticError("hierarchical protocol connections require identical protocols")
            if source.payload_type != destination.payload_type:
                raise SemanticError("hierarchical protocol payload types do not match")
            if source.domain != destination.domain:
                raise SemanticError("hierarchical protocol endpoints must share a clock domain")
            array_edge = array_physical_owner(source.owner) or array_physical_owner(
                destination.owner
            )
            if array_edge and (
                declaration.buffer_depth
                or declaration.request_buffer_depth
                or declaration.response_buffer_depth
                or declaration.adapter is not None
                or declaration.crossing is not None
            ):
                raise SemanticError(
                    "ready/valid instance-array connections must be direct and "
                    "cannot use buffering, adapters, or crossings"
                )
            if declaration.adapter is not None:
                raise SemanticError("hierarchical protocol connections do not accept adapters")
            if array_edge:
                source_key = (source.owner, source.name)
                destination_key = (destination.owner, destination.name)
                if source_key in array_rv_sources:
                    raise SemanticError(
                        f"ready/valid array endpoint '{source.owner}.{source.name}' "
                        "has multiple consumers"
                    )
                if destination_key in array_rv_destinations:
                    raise SemanticError(
                        f"ready/valid array endpoint "
                        f"'{destination.owner}.{destination.name}' has multiple drivers"
                    )
                array_rv_sources.add(source_key)
                array_rv_destinations.add(destination_key)
            hierarchical_connections.append(
                ir_module.HierarchicalConnection(
                    source, destination, buffer_depth=declaration.buffer_depth
                )
            )
            # A hierarchical ready/valid link drives both directions of the
            # physical interface.  Forward payload/valid belong to the
            # destination; backward ready belongs to the source.  Count that
            # source-side ready as an assignment so top-level completeness checks
            # agree with the typed connection rather than requiring a duplicate
            # source assignment.
            if source.owner == module.name and source.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
                assigned_outputs.add((source.name, None, ir_interfaces.ReadyValidSignal.READY))
            if destination.owner == module.name:
                assigned_outputs.update(
                    {
                        (destination.name, None, ir_interfaces.ReadyValidSignal.PAYLOAD),
                        (destination.name, None, ir_interfaces.ReadyValidSignal.VALID),
                    }
                )

        for array, length in module_context.scope.instance_arrays.items():
            for index in range(length):
                owner = f"{array}[{index}]"
                child = child_irs[owner]
                if not child.ports or any(
                    port.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
                    for port in child.ports
                ):
                    continue
                for port in child.inputs:
                    if (owner, port.name) not in array_rv_destinations:
                        raise SemanticError(
                            f"ready/valid instance '{owner}' input '{port.name}' "
                            "has no compile-time indexed connection"
                        )
                for port in child.outputs:
                    if (owner, port.name) not in array_rv_sources:
                        raise SemanticError(
                            f"ready/valid instance '{owner}' output '{port.name}' "
                            "has no compile-time indexed connection"
                        )

        instance_protocol_transfers = _instance_protocol_transfer_projections(
            module,
            child_irs,
            tuple(hierarchical_connections),
            tuple(aggregate_protocol_connections),
            tuple(aggregate_protocol_endpoints),
        )
        return HierarchicalConnectionProduct(
            tuple(connections),
            tuple(hierarchical_connections),
            tuple(request_response_connections),
            tuple(aggregate_protocol_connections),
            instance_protocol_transfers,
            frozenset(assigned_outputs),
        )


def _instance_protocol_transfer_projections(
    module: ast.Module,
    child_irs: dict[str, ir_module.Module],
    connections: tuple[ir_module.HierarchicalConnection, ...],
    aggregate_connections: tuple[ir_module.AggregateProtocolConnection, ...],
    top_aggregates: tuple[ir_module.AggregateProtocolEndpoint, ...],
) -> tuple[tuple[str, str, ir_expr.Expression], ...]:
    """Build exact child ``transfer`` values from the typed connection graph.

    A child owns only one direction of a ready/valid endpoint, so ``transfer``
    cannot be represented by one child output projection.  Hierarchy analysis
    is the first stage that owns both the forward-valid and backward-ready
    paths; keep the derived value here rather than rediscovering connectivity
    in expression checking.
    """

    bit = ir_types.BitType()

    def signal(
        endpoint: ir_module.ProtocolEndpoint,
        field: ir_interfaces.ReadyValidSignal,
    ) -> ir_expr.Expression:
        if endpoint.owner == module.name:
            return ir_expr.ReadyValidRef(endpoint.name, field, bit)
        return ir_expr.InstanceOutputRef(
            endpoint.owner,
            ir_interfaces.ready_valid_field_name(endpoint.name, field),
            bit,
            domain=endpoint.domain,
        )

    def transfer(
        valid: ir_expr.Expression,
        ready: ir_expr.Expression,
    ) -> ir_expr.Expression:
        return ir_expr.Binary(
            ir_expr.BinaryOperator.BIT_AND,
            valid,
            ready,
            bit,
            bit,
        )

    def logical_names(owner: str, physical: str) -> tuple[str, ...]:
        child = child_irs[owner]
        names = [physical]
        names.extend(
            f"{aggregate.name}.{member.name}"
            for aggregate in child.aggregate_protocol_endpoints
            for member in aggregate.members
            if physical == f"{aggregate.name}__{member.name}"
        )
        return tuple(names)

    projections: dict[tuple[str, str], ir_expr.Expression] = {}
    for connection in connections:
        if (
            connection.source.protocol
            is not ir_interfaces.InterfaceProtocol.READY_VALID
            or connection.buffer_depth
            or connection.request_buffer_depth
            or connection.response_buffer_depth
            or connection.adapter is not None
            or connection.crossing is not None
        ):
            continue
        value = transfer(
            signal(connection.source, ir_interfaces.ReadyValidSignal.VALID),
            signal(connection.destination, ir_interfaces.ReadyValidSignal.READY),
        )
        for endpoint in (connection.source, connection.destination):
            if endpoint.owner == module.name:
                continue
            for logical in logical_names(endpoint.owner, endpoint.name):
                projections[(endpoint.owner, f"{logical}.transfer")] = value

    projections.update({
        (owner, path): value
        for owner, path, value in delegated_instance_transfer_projections(
            child_irs, aggregate_connections, top_aggregates
        )
    })

    return tuple(
        (owner, path, value)
        for (owner, path), value in sorted(projections.items())
    )


def delegated_instance_transfer_projections(
    child_irs: dict[str, ir_module.Module],
    aggregate_connections: tuple[ir_module.AggregateProtocolConnection, ...],
    top_aggregates: tuple[ir_module.AggregateProtocolEndpoint, ...],
) -> tuple[tuple[str, str, ir_expr.Expression], ...]:
    """Project delegated child transfers before rule guards are analyzed."""

    bit = ir_types.BitType()
    projections: dict[tuple[str, str], ir_expr.Expression] = {}
    for connection in aggregate_connections:
        if not connection.delegation or "." not in connection.destination:
            continue
        child_owner, child_aggregate_name = connection.destination.split(".", 1)
        child = child_irs[child_owner]
        child_aggregate = next(
            item
            for item in child.aggregate_protocol_endpoints
            if item.name == child_aggregate_name
        )
        top_aggregate = next(
            item
            for item in top_aggregates
            if item.name == connection.source
        )
        for member in child_aggregate.members:
            if member.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID:
                continue
            physical = f"{child_aggregate.name}__{member.name}"
            child_port = next(item for item in child.ports if item.name == physical)
            top_physical = f"{top_aggregate.name}__{member.name}"
            if child_port.direction is ir_module.PortDirection.OUTPUT:
                valid = ir_expr.InstanceOutputRef(
                    child_owner,
                    ir_interfaces.ready_valid_field_name(
                        physical, ir_interfaces.ReadyValidSignal.VALID
                    ),
                    bit,
                    domain=child_port.domain,
                )
                ready = ir_expr.ReadyValidRef(
                    top_physical, ir_interfaces.ReadyValidSignal.READY, bit
                )
            else:
                valid = ir_expr.ReadyValidRef(
                    top_physical, ir_interfaces.ReadyValidSignal.VALID, bit
                )
                ready = ir_expr.InstanceOutputRef(
                    child_owner,
                    ir_interfaces.ready_valid_field_name(
                        physical, ir_interfaces.ReadyValidSignal.READY
                    ),
                    bit,
                    domain=child_port.domain,
                )
            projections[
                (child_owner, f"{child_aggregate.name}.{member.name}.transfer")
            ] = ir_expr.Binary(
                ir_expr.BinaryOperator.BIT_AND,
                valid,
                ready,
                bit,
                bit,
            )

    return tuple(
        (owner, path, value)
        for (owner, path), value in sorted(projections.items())
    )


def predeclare_delegated_instance_transfer_projections(
    module: ast.Module,
    child_irs: dict[str, ir_module.Module],
    top_aggregates: tuple[ir_module.AggregateProtocolEndpoint, ...],
) -> tuple[tuple[str, str, ir_expr.Expression], ...]:
    """Expose only syntactically unambiguous delegation transfers early.

    Full role, specialization, and connection validation remains in
    :class:`HierarchicalConnectionAnalyzer`.  This early product exists solely
    because rule guards precede the finalized hierarchy product.
    """

    connections = tuple(
        ir_module.AggregateProtocolConnection(
            (
                declaration.destination
                if "." in declaration.source
                else declaration.source
            ),
            (
                declaration.source
                if "." in declaration.source
                else declaration.destination
            ),
            top.protocol,
            top.specialization_identity,
            delegation=True,
        )
        for declaration in module.connections
        for top in top_aggregates
        if (
            (declaration.source == top.name and "." in declaration.destination)
            or (declaration.destination == top.name and "." in declaration.source)
        )
    )
    return delegated_instance_transfer_projections(
        child_irs, connections, top_aggregates
    )
