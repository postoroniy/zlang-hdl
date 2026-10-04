# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Protocol adapter simulation lowering."""

from __future__ import annotations

import hashlib

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.ir import verification as ir_verification
from zlang.ir.storage import Fifo, FifoSignal
from zlang.simulation_primitives import bit_binary as _bit_binary
from zlang.simulation_primitives import bit_not as _bit_not


import zlang.simulation_protocol_shared as protocol_shared

class _ProtocolFieldExpressionLowerer(
    protocol_shared._MappedProtocolExpressionLowerer[tuple[str, str, str]],
):

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if isinstance(value, expr.ReadyValidRef):
            signal = (
                ir_interfaces.ReadyValidSignal.VALID
                if value.signal is ir_interfaces.ReadyValidSignal.TRANSFER
                else value.signal
            )
            result = self._values.get(("ready_valid", value.interface, signal.value))
            if result is None:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"adapter reference names unknown ready/valid field "
                    f"'{value.interface}.{signal.value}'"
                )
            if value.signal is ir_interfaces.ReadyValidSignal.TRANSFER:
                ready = self._values.get(
                    ("ready_valid", value.interface, ir_interfaces.ReadyValidSignal.READY.value)
                )
                if ready is None:
                    raise protocol_shared.ProtocolSimulationLoweringError(
                        f"adapter transfer has no ready field for '{value.interface}'"
                    )
                return _bit_binary(expr.BinaryOperator.BIT_AND, result, ready)
            return result
        if isinstance(value, expr.CreditRef):
            signal = (
                ir_interfaces.CreditSignal.SEND
                if value.signal is ir_interfaces.CreditSignal.TRANSFER
                else value.signal
            )
            result = self._values.get(("credit", value.interface, signal.value))
            if result is None:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"adapter reference names unknown credit field "
                    f"'{value.interface}.{signal.value}'"
                )
            return result
        return None


def _adapter_scalar_ports(
    module: ir_module.Module,
) -> protocol_shared.ScalarPortInventory:
    scalar = protocol_shared.ScalarPortInventory.retained_wires(module)
    for port in module.ports:
        if port.protocol is ir_interfaces.InterfaceProtocol.WIRE:
            continue
        if port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            name_for = ir_interfaces.ready_valid_field_name
        elif port.protocol is ir_interfaces.InterfaceProtocol.CREDIT:
            name_for = protocol_shared.credit_field_name
        else:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"adapter does not support protocol '{port.protocol.value}'"
            )
        scalar.add_protocol_endpoint(port, name_for)
    return scalar


def lower_adapter_module(module: ir_module.Module) -> ir_module.Module:
    """Erase one exact ready/valid-credit adapter to counters or FIFO state."""

    if len(module.connections) != 1:
        raise protocol_shared.ProtocolSimulationLoweringError(
            "primitive protocol adapter lowering requires exactly one connection"
        )
    if (
        module.registers
        or module.next_assignments
        or module.rules
        or module.fifos
        or module.memories
        or module.roms
        or module.csr_blocks
        or module.elaborated_instances
        or module.instances
        or module.children
    ):
        raise protocol_shared.ProtocolSimulationLoweringError(
            "primitive protocol adapter lowering requires one closed adapter"
        )
    connection = module.connections[0]
    source = connection.source
    destination = connection.destination
    if connection.crossing is not None or connection.adapter is None:
        raise protocol_shared.ProtocolSimulationLoweringError(
            "protocol adapter lowering requires one same-domain typed adapter"
        )
    if source.domain != destination.domain:
        raise protocol_shared.ProtocolSimulationLoweringError(
            "protocol adapter endpoints must share one clock domain"
        )
    domain = source.domain or module.clock
    reset = next(
        (
            candidate.reset
            for candidate in module.clock_domains
            if candidate.clock == domain
        ),
        module.reset if module.clock == domain else None,
    )
    if domain is None or reset is None:
        raise protocol_shared.ProtocolSimulationLoweringError(
            "protocol adapter requires one exact clock/reset domain"
        )
    scalar = _adapter_scalar_ports(module)
    reset_deasserted = _bit_not(expr.InputRef(f"$reset:{reset}", ir_types.BitType()))
    values: dict[tuple[str, str, str], expr.Expression] = {}

    def scalar_input(port: ir_module.Port, field: str, type_) -> expr.InputRef:
        name = (
            ir_interfaces.ready_valid_field_name(port.name, field)
            if port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID
            else protocol_shared.credit_field_name(port.name, field)
        )
        return expr.InputRef(name, type_)

    registers = list(module.registers)
    next_assignments = list(module.next_assignments)
    fifos = list(module.fifos)
    runtime_scopes: list[ir_verification.VerificationScope] = []
    internal_outputs: list[tuple[ir_module.Port, expr.Expression]] = []

    if connection.adapter is ir_interfaces.ConnectionAdapter.READY_VALID_TO_CREDIT:
        if (
            source.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or source.direction is not ir_module.PortDirection.INPUT
            or destination.protocol is not ir_interfaces.InterfaceProtocol.CREDIT
            or destination.direction is not ir_module.PortDirection.OUTPUT
            or destination.capacity is None
            or connection.buffer_depth
        ):
            raise protocol_shared.ProtocolSimulationLoweringError(
                "invalid typed ready/valid-to-credit adapter"
            )
        count_type = ir_types.UIntType(max(1, destination.capacity.bit_length()))
        count_register = ir_module.Register(
            protocol_shared.credit_state_name(destination.name, "credits"),
            count_type,
            expr.Constant(destination.capacity, count_type),
            domain,
        )
        registers.append(count_register)
        count = expr.RegisterRef(count_register.name, count_type)
        payload = scalar_input(source, "payload", source.type)
        valid = scalar_input(source, "valid", ir_types.BitType())
        returned = scalar_input(destination, "return", ir_types.BitType())
        available = expr.Binary(
            expr.BinaryOperator.NOT_EQUAL,
            count,
            expr.Constant(0, count_type),
            count_type,
            ir_types.BitType(),
        )
        ready = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            reset_deasserted,
            available,
        )
        sent = _bit_binary(expr.BinaryOperator.BIT_AND, valid, ready)
        values.update({
            ("ready_valid", source.name, "payload"): payload,
            ("ready_valid", source.name, "valid"): valid,
            ("ready_valid", source.name, "ready"): ready,
            ("credit", destination.name, "payload"): payload,
            ("credit", destination.name, "send"): sent,
            ("credit", destination.name, "return"): returned,
            ("credit", destination.name, "credits"): count,
        })
        transition = protocol_shared.credit_counter_transition(
            count, sent, returned, destination.capacity
        )
        next_assignments.append(
            ir_module.NextAssignment(count_register, transition.updated)
        )
        violation = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            transition.returned_without_send,
            transition.at_capacity,
        )
        runtime_scopes.append(
            protocol_shared._runtime_protocol_scope(
                module=module,
                port=destination,
                conditions=((
                    f"rv_to_credit adapter '{source.name}->{destination.name}' "
                    "received a return at maximum credits",
                    _bit_not(violation),
                ),),
            )
        )
    elif connection.adapter is ir_interfaces.ConnectionAdapter.CREDIT_TO_READY_VALID:
        if (
            source.protocol is not ir_interfaces.InterfaceProtocol.CREDIT
            or source.direction is not ir_module.PortDirection.INPUT
            or destination.protocol is not ir_interfaces.InterfaceProtocol.READY_VALID
            or destination.direction is not ir_module.PortDirection.OUTPUT
            or source.capacity is None
            or connection.buffer_depth != source.capacity
        ):
            raise protocol_shared.ProtocolSimulationLoweringError(
                "invalid typed credit-to-ready/valid adapter"
            )
        identity = hashlib.sha256(
            (
                f"{module.name}|{source.name}|{destination.name}|"
                f"{connection.buffer_depth}|credit-to-rv"
            ).encode("utf-8")
        ).hexdigest()[:20]
        fifo_name = f"$zlang_protocol_adapter_{identity}"
        payload = scalar_input(source, "payload", source.type)
        sent = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            reset_deasserted,
            scalar_input(source, "send", ir_types.BitType()),
        )
        ready = scalar_input(destination, "ready", ir_types.BitType())
        accepted = expr.FifoRef(fifo_name, FifoSignal.READY, ir_types.BitType())
        push = _bit_binary(expr.BinaryOperator.BIT_AND, sent, accepted)
        fifo = Fifo(
            fifo_name,
            source.type,
            connection.buffer_depth,
            payload,
            push,
            ready,
            domain=domain,
        )
        fifos.append(fifo)
        valid = expr.FifoRef(fifo_name, FifoSignal.VALID, ir_types.BitType())
        returned = _bit_binary(expr.BinaryOperator.BIT_AND, valid, ready)
        values.update({
            ("credit", source.name, "payload"): payload,
            ("credit", source.name, "send"): sent,
            ("credit", source.name, "return"): returned,
            ("ready_valid", destination.name, "payload"): expr.FifoRef(
                fifo_name, FifoSignal.FRONT, source.type
            ),
            ("ready_valid", destination.name, "valid"): valid,
            ("ready_valid", destination.name, "ready"): ready,
        })
        overflow_name = f"{fifo_name}:overflow"
        overflow_port = scalar.add(
            ir_module.PortDirection.OUTPUT,
            overflow_name,
            ir_types.BitType(),
            domain,
        )
        overflow = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            sent,
            _bit_not(accepted),
        )
        internal_outputs.append((overflow_port, overflow))
        runtime_scopes.append(
            protocol_shared._runtime_protocol_scope(
                module=module,
                port=source,
                conditions=((
                    f"credit_to_rv adapter '{source.name}->{destination.name}' "
                    "received a transfer without credit",
                    _bit_not(expr.InputRef(overflow_name, ir_types.BitType())),
                ),),
            )
        )
    else:
        raise protocol_shared.ProtocolSimulationLoweringError(
            f"unsupported protocol adapter '{connection.adapter.value}'"
        )

    lowerer = _ProtocolFieldExpressionLowerer(values)
    assignments = protocol_shared._retained_wire_assignments(module, lowerer)
    for (protocol, endpoint, field), value in values.items():
        name = (
            ir_interfaces.ready_valid_field_name(endpoint, field)
            if protocol == "ready_valid"
            else protocol_shared.credit_field_name(endpoint, field)
        )
        target = scalar.by_name.get(name)
        if target is not None and target.direction is ir_module.PortDirection.OUTPUT:
            assignments.append(ir_module.Assignment(target, value))
    assignments.extend(
        ir_module.Assignment(target, value) for target, value in internal_outputs
    )
    if connection.adapter is ir_interfaces.ConnectionAdapter.READY_VALID_TO_CREDIT:
        credits_name = protocol_shared.credit_field_name(destination.name, ir_interfaces.CreditSignal.CREDITS)
        credits_port = scalar.add(
            ir_module.PortDirection.OUTPUT,
            credits_name,
            registers[-1].type,
            destination.domain,
        )
        assignments.append(
            ir_module.Assignment(
                credits_port,
                values[("credit", destination.name, "credits")],
            )
        )

    return protocol_shared._finalize_protocol_module(
        module,
        lowerer,
        ports=tuple(scalar.ports),
        assignments=tuple(assignments),
        rewritten={
            "registers": tuple(lowerer.value(tuple(registers))),
            "next_assignments": tuple(lowerer.value(tuple(next_assignments))),
            "fifos": tuple(lowerer.value(tuple(fifos))),
            "verification_scopes": (
                *lowerer.value(module.verification_scopes),
                *runtime_scopes,
            ),
        },
    )
