# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned direct connection lowering and protocol-cycle validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin, SourceSpan

from . import module_pipeline
from .errors import SemanticError
from . import observations as semantic_observations
from .connection_endpoints import HierarchicalEndpointResolver, endpoint_reference
from .aggregate_connections import lower_child_aggregate, lower_top_aggregate
from .request_response_connections import lower_request_response
from .context import ExpressionContext


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
    protocol_endpoints: tuple[ir_module.ProtocolEndpoint, ...]


def expand_connection_chains(
    module: ast.Module,
    instance_declarations: dict[str, ast.InstanceDecl],
    child_irs: dict[str, ir_module.Module],
    module_context: ExpressionContext,
) -> list[ast.ConnectionDecl]:
    declarations = list(module.connections)
    for chain in module.connection_chains:
        if len(chain.endpoints) < 3:
            raise SemanticError(
                "connection chain requires at least one intermediate instance",
                primary=semantic_observations.declaration_origin(
                    chain.origin, "connection chain", module_context
                ),
            )
        if any("[" in item for item in chain.endpoints):
            raise SemanticError(
                "instance arrays require explicit indexed connections; "
                "connection-chain array endpoints are not supported",
                primary=semantic_observations.declaration_origin(
                    chain.origin, "connection chain", module_context
                ),
            )
        current_source = chain.endpoints[0]
        current_origins = (
            chain.endpoint_name_origins[0]
            if chain.endpoint_name_origins else ()
        )
        for chain_index, instance_name in enumerate(chain.endpoints[1:-1], 1):
            if "." in instance_name or "[" in instance_name:
                raise SemanticError(
                    "connection-chain intermediates must be bare physical instance names",
                    primary=semantic_observations.declaration_origin(
                        chain.origin, "connection chain", module_context
                    ),
                )
            declaration = instance_declarations.get(instance_name)
            child = child_irs.get(instance_name)
            if declaration is None or child is None:
                raise SemanticError(
                    f"connection-chain intermediate '{instance_name}' is not an instance",
                    primary=semantic_observations.declaration_origin(
                        chain.origin, "connection chain", module_context
                    ),
                )
            if declaration.array_length is not None:
                raise SemanticError(
                    f"connection-chain intermediate '{instance_name}' cannot be an instance array",
                    primary=semantic_observations.declaration_origin(
                        chain.origin, "connection chain", module_context
                    ),
                )
            if child.module_signature is None:
                raise SemanticError(
                    f"connection-chain instance '{instance_name}' must conform to one named module interface",
                    primary=semantic_observations.declaration_origin(
                        chain.origin, "connection chain", module_context
                    ),
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
                    "one protocol input and one protocol output in its named interface",
                    primary=semantic_observations.declaration_origin(
                        chain.origin, "connection chain", module_context
                    ),
                )
            declarations.append(ast.ConnectionDecl(
                current_source,
                f"{instance_name}.{protocol_inputs[0].name}",
                source_name_origins=current_origins,
                destination_name_origins=(
                    chain.endpoint_name_origins[chain_index]
                    if chain.endpoint_name_origins else ()
                ),
                origin=chain.origin,
            ))
            current_source = f"{instance_name}.{protocol_outputs[0].name}"
            current_origins = (
                chain.endpoint_name_origins[chain_index]
                if chain.endpoint_name_origins else ()
            )
        declarations.append(ast.ConnectionDecl(
            current_source,
            chain.endpoints[-1],
            source_name_origins=current_origins,
            destination_name_origins=(
                chain.endpoint_name_origins[-1]
                if chain.endpoint_name_origins else ()
            ),
            origin=chain.origin,
        ))
    return declarations


class HierarchicalConnectionAnalyzer:
    """Own chain expansion and typed hierarchical protocol connections."""

    def analyze(
        self,
        preparation: module_pipeline.DeclarationPreparationProduct,
        hardware: module_pipeline.HardwareInterfaceProduct,
        state_storage: module_pipeline.StateStoragePreparationProduct,
        known_modules: dict[str, ast.Module],
        instances: Any,
        endpoint_resolver: HierarchicalEndpointResolver,
    ) -> HierarchicalConnectionProduct:
        module = preparation.module
        module_context = state_storage.expression_context
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
        assigned_outputs: set[tuple[str, object | None, object | None]] = set()
        concrete_hierarchical_endpoint = endpoint_resolver.concrete
        endpoint = endpoint_resolver.protocol
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
        hierarchical_connection_declarations = expand_connection_chains(
            module, instance_declarations, child_irs, module_context
        )

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
                symbol = state_storage.value_symbols.get(owner) or state_storage.symbols.get(owner)
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
            child_module = known_modules.get(declaration.module)
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
                aggregate = lower_top_aggregate(
                    declaration, aggregate_protocol_endpoints, endpoint_resolver,
                    preparation.type_resolver, connections,
                )
                if aggregate is not None:
                    aggregate_protocol_connections.append(aggregate)
                continue
            source_parts = declaration.source.split(".")
            destination_parts = declaration.destination.split(".")
            source_child = child_irs.get(source_parts[0]) if len(source_parts) == 2 else None
            destination_child = child_irs.get(destination_parts[0]) if len(destination_parts) == 2 else None
            aggregate = lower_child_aggregate(
                declaration, source_parts, destination_parts, source_child, destination_child,
                aggregate_protocol_endpoints, endpoint_resolver, hierarchical_connections,
            )
            if aggregate is not None:
                aggregate_protocol_connections.append(aggregate)
                continue
            request_response = lower_request_response(
                declaration, source_parts, destination_parts, source_child, destination_child,
                endpoint_resolver, hierarchical_connections, hardware.reset,
            )
            if request_response is not None:
                request_response_connections.append(request_response)
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
                raise SemanticError(
                    "hierarchical protocol connections require identical protocols",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", module_context
                    ),
                )
            if source.payload_type != destination.payload_type:
                raise SemanticError(
                    "hierarchical protocol payload types do not match",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", module_context
                    ),
                )
            if source.domain != destination.domain:
                raise SemanticError(
                    "hierarchical protocol endpoints must share a clock domain",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", module_context
                    ),
                )
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
                    "cannot use buffering, adapters, or crossings",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", module_context
                    ),
                )
            if declaration.adapter is not None:
                raise SemanticError(
                    "hierarchical protocol connections do not accept adapters",
                    primary=semantic_observations.declaration_origin(
                        declaration.origin, "connection", module_context
                    ),
                )
            if array_edge:
                source_key = (source.owner, source.name)
                destination_key = (destination.owner, destination.name)
                if source_key in array_rv_sources:
                    raise SemanticError(
                        f"ready/valid array endpoint '{source.owner}.{source.name}' has multiple consumers",
                        primary=semantic_observations.declaration_origin(
                            declaration.origin, "connection", module_context
                        ),
                    )
                if destination_key in array_rv_destinations:
                    raise SemanticError(
                        f"ready/valid array endpoint "
                        f"'{destination.owner}.{destination.name}' has multiple drivers",
                        primary=semantic_observations.declaration_origin(
                            declaration.origin, "connection", module_context
                        ),
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
            tuple(endpoint_resolver.protocol_endpoints),
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
            endpoint_reference(module.name, connection.source, ir_interfaces.ReadyValidSignal.VALID),
            endpoint_reference(module.name, connection.destination, ir_interfaces.ReadyValidSignal.READY),
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
