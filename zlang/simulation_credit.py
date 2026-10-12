# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Credit protocol simulation lowering."""

from __future__ import annotations

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.simulation_rewrite import SimulationExpressionRewriter
from zlang.simulation_primitives import bit_binary as _bit_binary


import zlang.simulation_protocol_shared as protocol_shared

class _CreditExpressionLowerer(SimulationExpressionRewriter):
    def __init__(
        self,
        module: ir_module.Module,
        ports: dict[str, ir_module.Port],
        producers: dict[tuple[str, ir_interfaces.CreditSignal], expr.Expression],
        count_registers: dict[str, ir_module.Register],
    ) -> None:
        super().__init__()
        self._module = module
        self._ports = ports
        self._producers = producers
        self._count_registers = count_registers
        self._active: set[tuple[str, ir_interfaces.CreditSignal]] = set()

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if isinstance(value, expr.CreditRef):
            return self.signal(value.interface, value.signal, value.origin)
        return None

    def signal(
        self,
        endpoint: str,
        signal: ir_interfaces.CreditSignal,
        origin=None,
    ) -> expr.Expression:
        port = protocol_shared._protocol_port(self._ports, endpoint, "credit")
        if signal is ir_interfaces.CreditSignal.TRANSFER:
            signal = ir_interfaces.CreditSignal.SEND
        if signal is ir_interfaces.CreditSignal.CREDITS:
            if port.direction is not ir_module.PortDirection.OUTPUT:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"receiver credit endpoint '{endpoint}' has no sender credits"
                )
            register = self._count_registers[endpoint]
            return expr.RegisterRef(register.name, register.type, origin=origin)

        if protocol_shared.protocol_scalar_field(port, signal).external:
            value: expr.Expression = expr.InputRef(
                protocol_shared.credit_field_name(endpoint, signal),
                port.type if signal is ir_interfaces.CreditSignal.PAYLOAD else ir_types.BitType(),
                origin=origin,
            )
            if (
                port.direction is ir_module.PortDirection.INPUT
                and signal is ir_interfaces.CreditSignal.SEND
            ):
                value = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    protocol_shared._reset_deasserted(self._module, port, "credit"),
                    value,
                )
            return value

        request = protocol_shared._producer_expression(
            self, self._producers, self._active, endpoint, signal, "credit"
        )

        if port.direction is ir_module.PortDirection.OUTPUT and signal is ir_interfaces.CreditSignal.SEND:
            register = self._count_registers[endpoint]
            count = expr.RegisterRef(register.name, register.type, origin=origin)
            available = expr.Binary(
                expr.BinaryOperator.NOT_EQUAL,
                count,
                expr.Constant(0, register.type),
                register.type,
                ir_types.BitType(),
                origin=origin,
            )
            return _bit_binary(
                expr.BinaryOperator.BIT_AND,
                protocol_shared._reset_deasserted(self._module, port, "credit"),
                _bit_binary(expr.BinaryOperator.BIT_AND, request, available),
            )
        if port.direction is ir_module.PortDirection.INPUT and signal is ir_interfaces.CreditSignal.RETURN:
            return _bit_binary(
                expr.BinaryOperator.BIT_AND,
                protocol_shared._reset_deasserted(self._module, port, "credit"),
                request,
            )
        return request

def lower_credit_module(module: ir_module.Module) -> ir_module.Module:
    """Erase bounded credit endpoints into scalar fields and counter state."""

    credit_ports = tuple(
        port for port in module.ports if port.protocol is ir_interfaces.InterfaceProtocol.CREDIT
    )
    if not credit_ports:
        return module
    protocol_shared._validate_leaf_endpoint_module(
        module, ir_interfaces.InterfaceProtocol.CREDIT, "credit endpoint"
    )
    producers = protocol_shared._endpoint_producers(
        module,
        protocol=ir_interfaces.InterfaceProtocol.CREDIT,
        signal_type=ir_interfaces.CreditSignal,
        derived=frozenset({
            ir_interfaces.CreditSignal.TRANSFER,
            ir_interfaces.CreditSignal.CREDITS,
        }),
        label="credit",
    )

    count_registers: dict[str, ir_module.Register] = {}
    for port in credit_ports:
        type_, domain, kind, initial = protocol_shared.credit_counter_spec(
            module, port, "credit"
        )
        count_registers[port.name] = ir_module.Register(
            protocol_shared.credit_state_name(port.name, kind),
            type_,
            expr.Constant(initial, type_),
            domain,
        )

    ports = {port.name: port for port in credit_ports}
    lowerer = _CreditExpressionLowerer(
        module,
        ports,
        producers,
        count_registers,
    )
    scalar = protocol_shared.ScalarPortInventory.retained_wires(module)
    for port in credit_ports:
        scalar.add_protocol_endpoint(port, protocol_shared.credit_field_name)
        if port.direction is ir_module.PortDirection.OUTPUT:
            credits_name = protocol_shared.credit_field_name(port.name, ir_interfaces.CreditSignal.CREDITS)
            scalar.add(
                ir_module.PortDirection.OUTPUT,
                credits_name, count_registers[port.name].type, port.domain,
            )

    assignments = protocol_shared._retained_wire_assignments(module, lowerer)
    for port in credit_ports:
        for field in protocol_shared.protocol_scalar_fields(port):
            if field.external:
                continue
            assignments.append(
                scalar.assignment(
                    protocol_shared.credit_field_name(port.name, field.signal),
                    lowerer.signal(port.name, field.signal),
                )
            )
        if port.direction is not ir_module.PortDirection.OUTPUT:
            continue
        assignments.append(
            scalar.assignment(
                protocol_shared.credit_field_name(
                    port.name, ir_interfaces.CreditSignal.CREDITS
                ),
                lowerer.signal(port.name, ir_interfaces.CreditSignal.CREDITS),
            )
        )

    next_assignments = list(lowerer.value(module.next_assignments))
    runtime_scopes = []
    for port in credit_ports:
        register = count_registers[port.name]
        sent = lowerer.signal(port.name, ir_interfaces.CreditSignal.SEND)
        returned = lowerer.signal(port.name, ir_interfaces.CreditSignal.RETURN)
        update, conditions = protocol_shared.credit_counter_update(
            register,
            sent,
            returned,
            port.capacity,
            port.direction,
            (
                f"credit interface '{port.name}' overflow: return at maximum credits",
                f"credit interface '{port.name}' underflow: return with no "
                "outstanding transfer",
                f"credit interface '{port.name}' overflow: transfer at "
                "maximum occupancy",
            ),
        )
        next_assignments.append(update)
        runtime_scopes.append(
            protocol_shared._runtime_protocol_scope(module=module, port=port, conditions=conditions)
        )

    return protocol_shared._finalize_protocol_module(
        module,
        lowerer,
        ports=tuple(scalar.ports),
        assignments=tuple(assignments),
        rewritten={
            "registers": (*lowerer.value(module.registers), *count_registers.values()),
            "next_assignments": tuple(next_assignments),
            "verification_scopes": (
                *lowerer.value(module.verification_scopes),
                *runtime_scopes,
            ),
        },
    )
