# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Ready/valid simulation lowering."""

from __future__ import annotations

from dataclasses import replace
import hashlib

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.ir.storage import Fifo, FifoSignal
from zlang.simulation_rewrite import SimulationExpressionRewriter
from zlang.simulation_primitives import bit_binary as _bit_binary


import zlang.simulation_protocol_shared as protocol_shared

class _ReadyValidExpressionLowerer(SimulationExpressionRewriter):
    def __init__(
        self,
        ports: dict[str, ir_module.Port],
        producers: dict[tuple[str, ir_interfaces.ReadyValidSignal], expr.Expression],
    ) -> None:
        super().__init__()
        self._ports = ports
        self._producers = producers
        self._active: set[tuple[str, ir_interfaces.ReadyValidSignal]] = set()

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if isinstance(value, expr.ReadyValidRef):
            return self._reference(value)
        return None

    def _reference(self, value: expr.ReadyValidRef) -> expr.Expression:
        port = protocol_shared._protocol_port(
            self._ports, value.interface, "ready/valid"
        )
        if value.signal is ir_interfaces.ReadyValidSignal.TRANSFER:
            valid = self._signal(port, ir_interfaces.ReadyValidSignal.VALID, value.origin)
            ready = self._signal(port, ir_interfaces.ReadyValidSignal.READY, value.origin)
            return expr.Binary(
                expr.BinaryOperator.BIT_AND,
                valid,
                ready,
                ir_types.BitType(),
                ir_types.BitType(),
                origin=value.origin,
            )
        return self._signal(port, value.signal, value.origin)

    def _signal(
        self,
        port: ir_module.Port,
        signal: ir_interfaces.ReadyValidSignal,
        origin,
    ) -> expr.Expression:
        field = protocol_shared.protocol_scalar_field(port, signal)
        if field.external:
            return expr.InputRef(
                ir_interfaces.ready_valid_field_name(port.name, signal),
                field.type,
                origin=origin,
            )
        return protocol_shared._producer_expression(
            self,
            self._producers,
            self._active,
            port.name,
            signal,
            "ready/valid",
        )


def lower_ready_valid_module(module: ir_module.Module) -> ir_module.Module:
    """Erase leaf ready/valid semantics into ordinary scalar wire semantics.

    The returned module is an internal compiler artifact.  Public API shape
    remains attached to the original module held by :class:`zlang.sim.Program`.
    """

    if module.elastic_pipeline_regions:
        from zlang.simulation_elastic import (
            ElasticSimulationLoweringError,
            lower_elastic_pipeline_module,
        )

        try:
            module = lower_elastic_pipeline_module(module)
        except ElasticSimulationLoweringError as error:
            raise protocol_shared.ProtocolSimulationLoweringError(str(error)) from error

    protocol_ports = tuple(
        port for port in module.ports if port.protocol is not ir_interfaces.InterfaceProtocol.WIRE
    )
    if not protocol_ports:
        return module
    unsupported = tuple(
        port
        for port in protocol_ports
        if port.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
    )
    if unsupported:
        names = ", ".join(sorted(port.name for port in unsupported))
        raise protocol_shared.ProtocolSimulationLoweringError(
            f"primitive simulation protocol lowering does not support: {names}"
        )
    if (
        module.request_responses
        or module.hierarchical_connections
        or module.aggregate_protocol_connections
        or module.request_response_connections
    ):
        raise protocol_shared.ProtocolSimulationLoweringError(
            "ready/valid endpoint lowering requires a leaf module without "
            "protocol connections or elastic regions"
        )

    expanded_assignments = list(module.assignments)
    expanded_fifos = list(module.fifos)
    existing_protocol_drivers = {
        (assignment.target.name, assignment.signal)
        for assignment in module.assignments
        if isinstance(assignment.target, ir_module.Port)
        and assignment.target.protocol is ir_interfaces.InterfaceProtocol.READY_VALID
        and isinstance(assignment.signal, ir_interfaces.ReadyValidSignal)
    }
    for connection in module.connections:
        if (
            connection.source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or connection.destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or connection.adapter is not None
            or connection.crossing is not None
        ):
            raise protocol_shared.ProtocolSimulationLoweringError(
                "primitive simulation supports direct and buffered "
                "ready/valid leaf connections; adapters require their "
                "protocol-specific lowering"
            )
        source = connection.source
        destination = connection.destination
        if connection.buffer_depth:
            identity = hashlib.sha256(
                (
                    f"{module.name}|{source.name}|{destination.name}|"
                    f"{connection.buffer_depth}|{source.type}"
                ).encode("utf-8")
            ).hexdigest()[:20]
            fifo_name = f"$zlang_protocol_buffer_{identity}"
            if any(fifo.name == fifo_name for fifo in expanded_fifos):
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"buffered connection '{source.name}->{destination.name}' "
                    "has a duplicate physical FIFO identity"
                )
            destination_ready = expr.ReadyValidRef(
                destination.name,
                ir_interfaces.ReadyValidSignal.READY,
                ir_types.BitType(),
            )
            pop = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                destination_ready,
                expr.FifoRef(fifo_name, FifoSignal.VALID, ir_types.BitType()),
            )
            expanded_fifos.append(
                Fifo(
                    fifo_name,
                    source.type,
                    connection.buffer_depth,
                    expr.ReadyValidRef(
                        source.name,
                        ir_interfaces.ReadyValidSignal.PAYLOAD,
                        source.type,
                    ),
                    expr.ReadyValidRef(
                        source.name,
                        ir_interfaces.ReadyValidSignal.VALID,
                        ir_types.BitType(),
                    ),
                    pop,
                    domain=source.domain,
                )
            )
            generated = (
                ir_module.Assignment(
                    destination,
                    expr.FifoRef(fifo_name, FifoSignal.FRONT, source.type),
                    ir_interfaces.ReadyValidSignal.PAYLOAD,
                ),
                ir_module.Assignment(
                    destination,
                    expr.FifoRef(fifo_name, FifoSignal.VALID, ir_types.BitType()),
                    ir_interfaces.ReadyValidSignal.VALID,
                ),
                ir_module.Assignment(
                    source,
                    expr.FifoRef(fifo_name, FifoSignal.READY, ir_types.BitType()),
                    ir_interfaces.ReadyValidSignal.READY,
                ),
            )
        else:
            generated = (
                ir_module.Assignment(
                    destination,
                    expr.ReadyValidRef(source.name, ir_interfaces.ReadyValidSignal.PAYLOAD, source.type),
                    ir_interfaces.ReadyValidSignal.PAYLOAD,
                ),
                ir_module.Assignment(
                    destination,
                    expr.ReadyValidRef(source.name, ir_interfaces.ReadyValidSignal.VALID, ir_types.BitType()),
                    ir_interfaces.ReadyValidSignal.VALID,
                ),
                ir_module.Assignment(
                    source,
                    expr.ReadyValidRef(destination.name, ir_interfaces.ReadyValidSignal.READY, ir_types.BitType()),
                    ir_interfaces.ReadyValidSignal.READY,
                ),
            )
        expanded_assignments.extend(
            assignment
            for assignment in generated
            if (assignment.target.name, assignment.signal)
            not in existing_protocol_drivers
        )

    ports = {port.name: port for port in protocol_ports}
    producers = protocol_shared._endpoint_producers(
        module,
        protocol=ir_interfaces.InterfaceProtocol.READY_VALID,
        signal_type=ir_interfaces.ReadyValidSignal,
        derived=frozenset({ir_interfaces.ReadyValidSignal.TRANSFER}),
        label="ready/valid",
        assignments=expanded_assignments,
        derived_error="ready/valid transfer is derived and cannot be assigned",
    )

    scalar = protocol_shared.ScalarPortInventory.retained_wires(module)
    for port in protocol_ports:
        scalar.add_protocol_endpoint(port, ir_interfaces.ready_valid_field_name)

    lowerer = _ReadyValidExpressionLowerer(ports, producers)
    assignments: list[ir_module.Assignment] = []
    for assignment in expanded_assignments:
        target = assignment.target
        if isinstance(target, ir_module.Port) and target.protocol is not ir_interfaces.InterfaceProtocol.WIRE:
            assert isinstance(assignment.signal, ir_interfaces.ReadyValidSignal)
            scalar_name = ir_interfaces.ready_valid_field_name(target.name, assignment.signal)
            assignments.append(
                ir_module.Assignment(
                    scalar.by_name[scalar_name],
                    lowerer.expression(assignment.expression),
                )
            )
        else:
            assignments.append(
                replace(
                    assignment,
                    expression=lowerer.expression(assignment.expression),
                )
            )

    return protocol_shared._finalize_protocol_module(
        module,
        ports=tuple(scalar.ports),
        assignments=tuple(assignments),
        lowerer=lowerer,
        rewritten={"fifos": lowerer.value(tuple(expanded_fifos))},
    )
