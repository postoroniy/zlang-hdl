# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Lower paired request/response edges and their buffer and reset contracts."""

from __future__ import annotations

from zlang.ast import nodes as ast
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module

from . import observations as semantic_observations
from .connection_endpoints import HierarchicalEndpointResolver
from .errors import SemanticError


def lower_request_response(
    declaration: ast.ConnectionDecl,
    source_parts: list[str],
    destination_parts: list[str],
    source_child: ir_module.Module | None,
    destination_child: ir_module.Module | None,
    resolver: HierarchicalEndpointResolver,
    hierarchical_connections: list[ir_module.HierarchicalConnection],
    reset: str | None,
) -> ir_module.RequestResponseConnection | None:
    source_rr = (
        next(
            (
                item
                for item in source_child.request_responses
                if item.name == source_parts[1]
            ),
            None,
        )
        if source_child is not None
        else None
    )
    destination_rr = (
        next(
            (
                item
                for item in destination_child.request_responses
                if item.name == destination_parts[1]
            ),
            None,
        )
        if destination_child is not None
        else None
    )
    if source_rr is not None or destination_rr is not None:
        if source_rr is None or destination_rr is None:
            raise SemanticError(
                "request/response hierarchy connections require two request/response endpoints",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if source_rr.request_type != destination_rr.request_type:
            raise SemanticError(
                "request/response request payload types do not match",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if source_rr.response_type != destination_rr.response_type:
            raise SemanticError(
                "request/response response payload types do not match",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if source_rr.max_outstanding != destination_rr.max_outstanding:
            raise SemanticError(
                "request/response max_outstanding values do not match",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if source_rr.ordering is not destination_rr.ordering:
            raise SemanticError(
                "request/response ordering contracts do not match",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if (
            source_rr.max_outstanding <= 0
            or source_rr.ordering is not ir_interfaces.RequestResponseOrdering.IN_ORDER
        ):
            raise SemanticError(
                "hierarchical request/response currently supports only "
                "positive max_outstanding with ordering in_order",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        if declaration.buffer_depth:
            raise SemanticError(
                "generic 'buffer' is ambiguous on request/response connections; "
                "use request_buffer and/or response_buffer",
                primary=semantic_observations.declaration_origin(
                    declaration.origin, "connection", resolver.expression_context
                ),
            )
        rr_edges: dict[
            ir_interfaces.RequestResponseChannel, ir_module.HierarchicalConnection
        ] = {}
        for channel in (
            ir_interfaces.RequestResponseChannel.REQUEST,
            ir_interfaces.RequestResponseChannel.RESPONSE,
        ):
            if channel is ir_interfaces.RequestResponseChannel.REQUEST:
                source_path, destination_path = (
                    declaration.source,
                    declaration.destination,
                )
            else:
                # The response travels in the opposite physical direction
                # while remaining part of the same logical connection.
                source_path, destination_path = (
                    declaration.destination,
                    declaration.source,
                )
            source_endpoint = resolver.request_response(
                source_path, source=True, channel=channel
            )
            destination_endpoint = resolver.request_response(
                destination_path, source=False, channel=channel
            )
            if source_endpoint.domain != destination_endpoint.domain:
                raise SemanticError(
                    "hierarchical request/response endpoints must share a clock domain",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", resolver.expression_context
                    ),
                )
            if any(
                edge.source.owner == source_endpoint.owner
                and edge.source.name == source_endpoint.name
                and edge.source.channel is channel
                for edge in hierarchical_connections
            ):
                raise SemanticError(
                    f"request/response endpoint '{source_endpoint.owner}.{source_endpoint.name}' "
                    f"is connected more than once on {channel.value}",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", resolver.expression_context
                    ),
                )
            edge = ir_module.HierarchicalConnection(
                source_endpoint,
                destination_endpoint,
                request_buffer_depth=(
                    declaration.request_buffer_depth
                    if channel is ir_interfaces.RequestResponseChannel.REQUEST
                    else 0
                ),
                response_buffer_depth=(
                    declaration.response_buffer_depth
                    if channel is ir_interfaces.RequestResponseChannel.RESPONSE
                    else 0
                ),
            )
            hierarchical_connections.append(edge)
            rr_edges[channel] = edge
        return ir_module.RequestResponseConnection(
            semantic_id=(
                f"rr:{resolver.module.name}:{declaration.source}->{declaration.destination}"
            ),
            request=rr_edges[ir_interfaces.RequestResponseChannel.REQUEST],
            response=rr_edges[ir_interfaces.RequestResponseChannel.RESPONSE],
            request_type=source_rr.request_type,
            response_type=source_rr.response_type,
            max_outstanding=source_rr.max_outstanding,
            ordering=source_rr.ordering,
            requester=source_parts[0]
            if source_rr.role is ir_interfaces.RequestResponseRole.REQUESTER
            else destination_parts[0],
            responder=destination_parts[0]
            if source_rr.role is ir_interfaces.RequestResponseRole.REQUESTER
            else source_parts[0],
            clock_domain=rr_edges[
                ir_interfaces.RequestResponseChannel.REQUEST
            ].source.domain,
            reset_domain=reset,
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
    return None
