# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Lower aggregate pass-through, delegation and peer connections."""

from __future__ import annotations

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module

from . import observations as semantic_observations
from .connection_endpoints import HierarchicalEndpointResolver
from .errors import SemanticError
from .type_resolution import TypeResolver


def lower_top_aggregate(
    declaration: ast.ConnectionDecl,
    aggregate_protocol_endpoints: list[ir_module.AggregateProtocolEndpoint],
    resolver: HierarchicalEndpointResolver,
    type_resolver: TypeResolver,
    connections: list[ir_module.Connection],
) -> ir_module.AggregateProtocolConnection | None:
    source_top = next(
        (
            item
            for item in aggregate_protocol_endpoints
            if item.name == declaration.source
        ),
        None,
    )
    destination_top = next(
        (
            item
            for item in aggregate_protocol_endpoints
            if item.name == declaration.destination
        ),
        None,
    )
    if source_top is None and destination_top is None:
        return None
    if source_top is None or destination_top is None:
        raise SemanticError(
            "aggregate protocol pass-through requires two aggregate endpoints",
            primary=semantic_observations.declaration_origin(
                declaration.origin, "connection", resolver.expression_context
            ),
        )
    if source_top.protocol != destination_top.protocol:
        raise SemanticError(
            "aggregate protocol pass-through requires the same protocol",
            primary=semantic_observations.declaration_origin(
                declaration.origin, "connection", resolver.expression_context
            ),
        )
    if source_top.specialization_identity != destination_top.specialization_identity:
        raise SemanticError(
            "aggregate protocol pass-through specialization arguments do not match",
            primary=semantic_observations.declaration_origin(
                declaration.origin, "connection", resolver.expression_context
            ),
        )
    if source_top.role == destination_top.role:
        raise SemanticError(
            "aggregate protocol pass-through requires complementary roles",
            primary=semantic_observations.declaration_origin(
                declaration.origin, "connection", resolver.expression_context
            ),
        )
    if (
        declaration.buffer_depth
        or declaration.request_buffer_depth
        or declaration.response_buffer_depth
    ):
        raise SemanticError(
            "aggregate protocol pass-through does not accept buffering",
            primary=semantic_observations.declaration_origin(
                declaration.origin, "connection", resolver.expression_context
            ),
        )
    if declaration.adapter is not None:
        raise SemanticError(
            "aggregate protocol pass-through does not accept adapters",
            primary=semantic_observations.declaration_origin(
                declaration.origin, "connection", resolver.expression_context
            ),
        )
    source_members = {item.name: item for item in source_top.members}
    destination_members = {item.name: item for item in destination_top.members}
    if set(source_members) != set(destination_members):
        raise SemanticError(
            "aggregate protocol pass-through member sets do not match",
            primary=semantic_observations.declaration_origin(
                declaration.origin, "connection", resolver.expression_context
            ),
        )
    aggregate_crossing = (
        ir_cdc.Crossing(
            ir_cdc.CrossingKind(declaration.crossing.kind.value),
            (
                type_resolver._eval_storage_depth(
                    declaration.crossing.depth,
                    kind="async_fifo",
                    name=(f"{declaration.source}->{declaration.destination}"),
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
                "aggregate protocol crossings currently support only async_fifo",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        depth = aggregate_crossing.depth
        if depth is None or depth < 4 or depth & (depth - 1):
            raise SemanticError(
                "aggregate async_fifo depth must be a power of two and at least 4",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if len(source_members) != 1:
            raise SemanticError(
                "aggregate async_fifo crossing requires exactly one ready/valid member",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
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
                f"aggregate protocol member '{member_name}' does not match",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if member.protocol not in {
            ir_interfaces.InterfaceProtocol.WIRE,
            ir_interfaces.InterfaceProtocol.READY_VALID,
            ir_interfaces.InterfaceProtocol.CREDIT,
        }:
            raise SemanticError(
                f"aggregate protocol member '{member_name}' cannot yet use top-level pass-through",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if (
            aggregate_crossing is not None
            and member.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
        ):
            raise SemanticError(
                "aggregate async_fifo crossing requires exactly one ready/valid member",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        source_port = resolver.symbols[f"{source_top.name}__{member_name}"]
        destination_port = resolver.symbols[f"{destination_top.name}__{member_name}"]
        if source_port.direction is not ir_module.PortDirection.INPUT:
            raise SemanticError(
                f"aggregate pass-through source '{source_top.name}' has "
                f"the wrong role for member '{member_name}'",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if destination_port.direction is not ir_module.PortDirection.OUTPUT:
            raise SemanticError(
                f"aggregate pass-through destination "
                f"'{destination_top.name}' has the wrong role for member "
                f"'{member_name}'",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if aggregate_crossing is None and source_port.domain != destination_port.domain:
            raise SemanticError(
                "aggregate protocol pass-through endpoints must share a clock domain",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if (
            aggregate_crossing is not None
            and source_port.domain == destination_port.domain
        ):
            raise SemanticError(
                "aggregate async_fifo crossing requires different domains",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        connections.append(
            ir_module.Connection(
                source_port,
                destination_port,
                crossing=aggregate_crossing,
            )
        )
    return ir_module.AggregateProtocolConnection(
        declaration.source,
        declaration.destination,
        source_top.protocol,
        source_top.specialization_identity,
        crossing=aggregate_crossing,
    )


def lower_child_aggregate(
    declaration: ast.ConnectionDecl,
    source_parts: list[str],
    destination_parts: list[str],
    source_child: ir_module.Module | None,
    destination_child: ir_module.Module | None,
    aggregate_protocol_endpoints: list[ir_module.AggregateProtocolEndpoint],
    resolver: HierarchicalEndpointResolver,
    hierarchical_connections: list[ir_module.HierarchicalConnection],
) -> ir_module.AggregateProtocolConnection | None:
    source_aggregate = (
        next(
            (
                item
                for item in source_child.aggregate_protocol_endpoints
                if item.name == source_parts[1]
            ),
            None,
        )
        if source_child is not None and len(source_parts) == 2
        else None
    )
    destination_aggregate = (
        next(
            (
                item
                for item in destination_child.aggregate_protocol_endpoints
                if item.name == destination_parts[1]
            ),
            None,
        )
        if destination_child is not None and len(destination_parts) == 2
        else None
    )
    top_aggregate = (
        next(
            (
                item
                for item in aggregate_protocol_endpoints
                if item.name == source_parts[0]
            ),
            None,
        )
        if len(source_parts) == 1
        else None
    )
    # A same-role top/child connection is a delegation regardless of its
    # source spelling.  The physical direction of every member is fixed
    # by the protocol roles, not by which endpoint the author wrote first.
    # Normalize it so all downstream consumers see the existing top-to-
    # child delegation shape.
    reverse_delegation = (
        source_aggregate is not None
        and len(destination_parts) == 1
        and next(
            (
                item
                for item in aggregate_protocol_endpoints
                if item.name == destination_parts[0]
            ),
            None,
        )
        is not None
    )
    if reverse_delegation:
        top_aggregate = next(
            item
            for item in aggregate_protocol_endpoints
            if item.name == destination_parts[0]
        )
        destination_aggregate = source_aggregate
    if top_aggregate is not None and destination_aggregate is not None:
        if top_aggregate.protocol != destination_aggregate.protocol:
            raise SemanticError(
                "top aggregate delegation requires the same protocol",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if (
            top_aggregate.specialization_identity
            != destination_aggregate.specialization_identity
        ):
            raise SemanticError(
                "top aggregate delegation specialization arguments do not match",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if top_aggregate.role != destination_aggregate.role:
            raise SemanticError(
                "top aggregate delegation requires the same role",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if (
            declaration.buffer_depth
            or declaration.adapter is not None
            or declaration.crossing is not None
        ):
            raise SemanticError(
                "top aggregate delegation does not accept buffering, adapters, or crossings",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        source_members = {member.name: member for member in top_aggregate.members}
        destination_members = {
            member.name: member for member in destination_aggregate.members
        }
        if set(source_members) != set(destination_members):
            raise SemanticError(
                "top aggregate delegation member sets do not match",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        for name, member in source_members.items():
            other = destination_members[name]
            if (
                member.protocol,
                member.payload_type,
                member.source_role,
                member.sink_role,
            ) != (
                other.protocol,
                other.payload_type,
                other.source_role,
                other.sink_role,
            ):
                raise SemanticError(
                    f"top aggregate delegation member '{name}' does not match",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", resolver.expression_context
                    ),
                )
            if member.domain != other.domain:
                raise SemanticError(
                    f"top aggregate delegation member '{name}' must share a clock domain",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", resolver.expression_context
                    ),
                )
        normalized_top = (
            declaration.destination if reverse_delegation else declaration.source
        )
        normalized_child = (
            declaration.source if reverse_delegation else declaration.destination
        )
        return ir_module.AggregateProtocolConnection(
            normalized_top,
            normalized_child,
            top_aggregate.protocol,
            top_aggregate.specialization_identity,
            delegation=True,
        )
    if source_aggregate is not None or destination_aggregate is not None:
        if source_aggregate is None or destination_aggregate is None:
            raise SemanticError(
                "aggregate protocol connections require two aggregate endpoints",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if source_aggregate.protocol != destination_aggregate.protocol:
            raise SemanticError(
                "aggregate protocol connections require the same protocol",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if (
            source_aggregate.specialization_identity
            != destination_aggregate.specialization_identity
        ):
            raise SemanticError(
                "aggregate protocol specialization arguments do not match",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if (
            declaration.buffer_depth
            or declaration.adapter is not None
            or declaration.crossing is not None
        ):
            raise SemanticError(
                "aggregate protocol connections do not accept buffering, adapters, or crossings",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        source_members = {member.name: member for member in source_aggregate.members}
        destination_members = {
            member.name: member for member in destination_aggregate.members
        }
        if set(source_members) != set(destination_members):
            raise SemanticError(
                "aggregate protocol member sets do not match",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        for member_name, member in source_members.items():
            other = destination_members[member_name]
            if (
                member.protocol is not other.protocol
                or member.payload_type != other.payload_type
            ):
                raise SemanticError(
                    f"aggregate protocol member '{member_name}' does not match",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", resolver.expression_context
                    ),
                )
            if (
                member.source_role == source_aggregate.role
                and member.sink_role == destination_aggregate.role
            ):
                source_path = f"{source_parts[0]}.{source_parts[1]}__{member_name}"
                destination_path = (
                    f"{destination_parts[0]}.{destination_parts[1]}__{member_name}"
                )
            elif (
                member.source_role == destination_aggregate.role
                and member.sink_role == source_aggregate.role
            ):
                source_path = (
                    f"{destination_parts[0]}.{destination_parts[1]}__{member_name}"
                )
                destination_path = f"{source_parts[0]}.{source_parts[1]}__{member_name}"
            else:
                raise SemanticError(
                    f"aggregate protocol member '{member_name}' has incompatible roles",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", resolver.expression_context
                    ),
                )
            source_endpoint = resolver.protocol(source_path, source=True)
            destination_endpoint = resolver.protocol(destination_path, source=False)
            if (
                source_endpoint.protocol is not destination_endpoint.protocol
                or source_endpoint.payload_type != destination_endpoint.payload_type
            ):
                raise SemanticError(
                    f"aggregate protocol member '{member_name}' endpoint mismatch",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", resolver.expression_context
                    ),
                )
            if source_endpoint.domain != destination_endpoint.domain:
                raise SemanticError(
                    "aggregate protocol endpoints must share a clock domain",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", resolver.expression_context
                    ),
                )
            hierarchical_connections.append(
                ir_module.HierarchicalConnection(source_endpoint, destination_endpoint)
            )
        return ir_module.AggregateProtocolConnection(
            declaration.source,
            declaration.destination,
            source_aggregate.protocol,
            source_aggregate.specialization_identity,
        )
    return None
