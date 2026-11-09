# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Publish typed child protocol outputs and exact read-only input observations."""

from __future__ import annotations

from typing import TYPE_CHECKING
from dataclasses import replace

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from .errors import SemanticError

if TYPE_CHECKING:
    from .instances import InstanceAnalysisContext
    from .context import ExpressionContext
    from .hierarchical_connections import HierarchicalConnectionProduct


def _protocol_names(child: ir_module.Module, physical: str) -> tuple[str, ...]:
    return (physical, *(
        f"{aggregate.name}.{member.name}"
        for aggregate in child.aggregate_protocol_endpoints
        for member in aggregate.members
        if physical == f"{aggregate.name}__{member.name}"
    ))

def publish_child_protocol_outputs(
    base: InstanceAnalysisContext,
    physical_name: str,
    child: ir_module.Module,
) -> None:
    for port in child.ports:
        if port.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID:
            continue
        logical_bases = _protocol_names(child, port.name)
        for logical_base in logical_bases:
            base.expression_context.scope.instance_protocol_ports[
                (physical_name, logical_base)
            ] = port
        outgoing = (
            (
                ir_interfaces.ReadyValidSignal.PAYLOAD,
                ir_interfaces.ReadyValidSignal.VALID,
            )
            if port.direction is ir_module.PortDirection.OUTPUT
            else (ir_interfaces.ReadyValidSignal.READY,)
        )
        for signal in outgoing:
            scalar_name = ir_interfaces.ready_valid_field_name(port.name, signal)
            signal_type = (
                port.type
                if signal is ir_interfaces.ReadyValidSignal.PAYLOAD
                else ir_types.BitType()
            )
            projection = (scalar_name, signal_type, port.domain)
            for logical_base in logical_bases:
                key = (physical_name, f"{logical_base}.{signal.value}")
                base.expression_context.scope.instance_protocol_outputs[key] = projection

def publish_child_input_observations(
    module: ast.Module,
    children: dict[str, ir_module.Module],
    connections: HierarchicalConnectionProduct,
    context: ExpressionContext,
) -> None:
    """Expose the existing driver of a child input without inventing an output.

    Direct typed edges and delegations have exact endpoint-side expressions.
    Explicit scalar bindings retain their AST until the observing local is
    checked by the ordinary expression owner. Buffered/CDC edges deliberately
    do not alias the producer's pre-buffer value to the consumer's input.
    """
    from . import observations

    bit = ir_types.BitType()
    wired_inputs: set[tuple[str, str]] = set()

    def value(endpoint, field):
        type_ = endpoint.payload_type if field is ir_interfaces.ReadyValidSignal.PAYLOAD else bit
        if endpoint.owner == module.name:
            return ir_expr.ReadyValidRef(endpoint.name, field, type_)
        return ir_expr.InstanceOutputRef(
            endpoint.owner, ir_interfaces.ready_valid_field_name(endpoint.name, field),
            type_, domain=endpoint.domain,
        )

    def publish(owner, physical, field, expression):
        if owner == module.name:
            return
        for logical in _protocol_names(children[owner], physical):
            context.scope.instance_protocol_inputs[(owner, f"{logical}.{field.value}")] = expression

    for edge in connections.hierarchical_connections:
        if edge.source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID:
            continue
        for endpoint, incoming in (
            (edge.source, (ir_interfaces.ReadyValidSignal.READY,)),
            (edge.destination, (ir_interfaces.ReadyValidSignal.PAYLOAD, ir_interfaces.ReadyValidSignal.VALID)),
        ):
            if endpoint.owner != module.name:
                for name in _protocol_names(children[endpoint.owner], endpoint.name):
                    wired_inputs.update((endpoint.owner, f"{name}.{field.value}") for field in incoming)
        if (edge.buffer_depth or edge.request_buffer_depth
                or edge.response_buffer_depth or edge.adapter is not None
                or edge.crossing is not None):
            continue
        for field in (ir_interfaces.ReadyValidSignal.PAYLOAD, ir_interfaces.ReadyValidSignal.VALID):
            publish(edge.destination.owner, edge.destination.name, field, value(edge.source, field))
        publish(edge.source.owner, edge.source.name, ir_interfaces.ReadyValidSignal.READY,
                value(edge.destination, ir_interfaces.ReadyValidSignal.READY))

    for edge in connections.aggregate_protocol_connections:
        if not edge.delegation:
            continue
        owner, aggregate = edge.destination.split(".", 1)
        child = children[owner]
        endpoint = next(item for item in child.aggregate_protocol_endpoints if item.name == aggregate)
        for member in endpoint.members:
            if member.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID:
                continue
            physical = f"{aggregate}__{member.name}"
            port = next(item for item in child.ports if item.name == physical)
            incoming = ((ir_interfaces.ReadyValidSignal.READY,)
                        if port.direction is ir_module.PortDirection.OUTPUT else
                        (ir_interfaces.ReadyValidSignal.PAYLOAD, ir_interfaces.ReadyValidSignal.VALID))
            for field in incoming:
                type_ = port.type if field is ir_interfaces.ReadyValidSignal.PAYLOAD else bit
                publish(owner, physical, field, ir_expr.ReadyValidRef(
                    f"{edge.source}__{member.name}", field, type_,
                ))

    assignments = (*module.assignments, *(
        replace(binding, target=f"{declaration.name}.{binding.target}")
        for declaration in module.instances for binding in declaration.bindings
    ))
    for assignment in assignments:
        parts = assignment.target.split(".")
        if len(parts) < 3:
            continue
        owner, logical, field = parts[0], ".".join(parts[1:-1]), parts[-1]
        port = context.scope.instance_protocol_ports.get((owner, logical))
        if port is None:
            continue
        incoming = ({"ready"} if port.direction is ir_module.PortDirection.OUTPUT
                    else {"payload", "valid"})
        if field not in incoming:
            continue  # The assignment owner diagnoses writes to child-owned fields.
        key = (owner, f"{logical}.{field}")
        wired = key in wired_inputs or key in context.scope.instance_protocol_inputs
        if wired or key in context.scope.instance_protocol_bindings:
            conflict = (
                "both a binding and a hierarchical connection (multiple drivers)"
                if wired else "multiple drivers"
            )
            raise SemanticError(
                f"child protocol signal '{assignment.target}' has {conflict}",
                primary=observations.declaration_origin(
                    assignment.name_origin or assignment.origin,
                    f"assignment {assignment.target}", context,
                ),
            )
        type_ = port.type if field == "payload" else bit
        # Logical and physical spellings denote the very same immutable driver.
        for name in _protocol_names(children[owner], port.name):
            context.scope.instance_protocol_bindings[(owner, f"{name}.{field}")] = (
                assignment.expression, type_,
            )

def resolve_child_protocol_input(
    key: tuple[str, str],
    inputs: dict[str, object],
    context: ExpressionContext,
    *,
    primary: SourceOrigin | None = None,
) -> ir_expr.Expression:
    """Type one explicit driver once; observations reuse its exact state IDs."""
    cached = context.scope.instance_protocol_inputs.get(key)
    if cached is not None:
        return cached
    owner, path = key
    logical, _, signal = path.rpartition(".")
    port = context.scope.instance_protocol_ports[(owner, logical)]
    marker = f"child-input:{owner}.{port.name}.{signal}"
    if marker in context.scope.resolution_stack:
        raise SemanticError(
            f"cyclic child input observation '{owner}.{path}'", primary=primary,
        )
    syntax, type_ = context.scope.instance_protocol_bindings[key]
    bound_context = context.with_scope(
        resolution_stack=(*context.scope.resolution_stack, marker),
    )
    try:
        bound = context.expressions.check_typed_boundary(
            syntax, inputs, type_, bound_context,
        )
    finally:
        context.advance_delay_allocator(bound_context.scope.next_delay_instance)
    if bound.type != type_:
        raise SemanticError(
            f"binding '{owner}.{path}' has type {bound.type}, expected {type_}",
            primary=primary,
        )
    for (other_owner, name), other_port in context.scope.instance_protocol_ports.items():
        if other_owner == owner and other_port is port:
            context.scope.instance_protocol_inputs[(owner, f"{name}.{signal}")] = bound
    return bound
