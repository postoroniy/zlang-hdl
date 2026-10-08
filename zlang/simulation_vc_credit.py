# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Virtual-channel credit protocol simulation lowering."""

from __future__ import annotations

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.simulation_rewrite import SimulationExpressionRewriter
from zlang.simulation_primitives import bit_binary as _bit_binary


import zlang.simulation_protocol_shared as protocol_shared

class _VirtualChannelCreditExpressionLowerer(SimulationExpressionRewriter):
    def __init__(
        self,
        module: ir_module.Module,
        ports: dict[str, ir_module.Port],
        producers: dict[
            tuple[str, ir_interfaces.VirtualChannelCreditSignal], expr.Expression
        ],
        count_registers: dict[str, tuple[ir_module.Register, ...]],
    ) -> None:
        super().__init__()
        self._module = module
        self._ports = ports
        self._producers = producers
        self._count_registers = count_registers
        self._active: set[tuple[str, ir_interfaces.VirtualChannelCreditSignal]] = set()

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if isinstance(value, expr.VirtualChannelCreditRef):
            return self.signal(value.interface, value.signal, value.origin)
        return None

    @staticmethod
    def channel_type(port: ir_module.Port) -> ir_types.UIntType:
        return protocol_shared.vc_credit_channel_type(port)

    def _channel_match(
        self,
        selector: expr.Expression,
        port: ir_module.Port,
        channel: int,
    ) -> expr.Expression:
        type_ = self.channel_type(port)
        return expr.Binary(
            expr.BinaryOperator.EQUAL,
            selector,
            expr.Constant(channel, type_),
            type_,
            ir_types.BitType(),
        )

    def _selected_count(
        self,
        port: ir_module.Port,
        selector: expr.Expression,
    ) -> expr.Expression:
        registers = self._count_registers[port.name]
        selected: expr.Expression = expr.RegisterRef(
            registers[0].name,
            registers[0].type,
        )
        for channel, register in enumerate(registers[1:], 1):
            selected = expr.Mux(
                self._channel_match(selector, port, channel),
                expr.RegisterRef(register.name, register.type),
                selected,
                register.type,
            )
        return selected

    def _count_vector(self, port: ir_module.Port) -> expr.Expression:
        registers = self._count_registers[port.name]
        return expr.VectorConcat(
            tuple(
                expr.RegisterRef(register.name, register.type)
                for register in registers
            ),
            ir_types.VecType(len(registers), registers[0].type),
        )

    def signal(
        self,
        endpoint: str,
        signal: ir_interfaces.VirtualChannelCreditSignal,
        origin=None,
    ) -> expr.Expression:
        port = protocol_shared._protocol_port(self._ports, endpoint, "VC-credit")
        if signal is ir_interfaces.VirtualChannelCreditSignal.TRANSFER:
            signal = ir_interfaces.VirtualChannelCreditSignal.SEND
        if signal is ir_interfaces.VirtualChannelCreditSignal.CREDITS:
            if port.direction is not ir_module.PortDirection.OUTPUT:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"receiver VC-credit endpoint '{endpoint}' has no sender credits"
                )
            return self._count_vector(port)

        if protocol_shared.protocol_scalar_field(port, signal).external:
            if signal is ir_interfaces.VirtualChannelCreditSignal.PAYLOAD:
                type_ = port.type
            elif signal in {
                ir_interfaces.VirtualChannelCreditSignal.VC,
                ir_interfaces.VirtualChannelCreditSignal.RETURN_VC,
            }:
                type_ = self.channel_type(port)
            else:
                type_ = ir_types.BitType()
            value: expr.Expression = expr.InputRef(
                protocol_shared.vc_credit_field_name(endpoint, signal),
                type_,
                origin=origin,
            )
            if (
                port.direction is ir_module.PortDirection.INPUT
                and signal is ir_interfaces.VirtualChannelCreditSignal.SEND
            ):
                value = _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    protocol_shared._reset_deasserted(self._module, port, "VC-credit"),
                    value,
                )
            return value

        request = protocol_shared._producer_expression(
            self, self._producers, self._active, endpoint, signal, "VC-credit"
        )

        if (
            port.direction is ir_module.PortDirection.OUTPUT
            and signal is ir_interfaces.VirtualChannelCreditSignal.SEND
        ):
            selector = self.signal(
                endpoint,
                ir_interfaces.VirtualChannelCreditSignal.VC,
                origin,
            )
            count = self._selected_count(port, selector)
            available = expr.Binary(
                expr.BinaryOperator.NOT_EQUAL,
                count,
                expr.Constant(0, count.type),
                count.type,
                ir_types.BitType(),
                origin=origin,
            )
            return _bit_binary(
                expr.BinaryOperator.BIT_AND,
                protocol_shared._reset_deasserted(self._module, port, "VC-credit"),
                _bit_binary(expr.BinaryOperator.BIT_AND, request, available),
            )
        if (
            port.direction is ir_module.PortDirection.INPUT
            and signal is ir_interfaces.VirtualChannelCreditSignal.RETURN
        ):
            return _bit_binary(
                expr.BinaryOperator.BIT_AND,
                protocol_shared._reset_deasserted(self._module, port, "VC-credit"),
                request,
            )
        return request

def lower_vc_credit_module(module: ir_module.Module) -> ir_module.Module:
    """Erase bounded VC-credit endpoints into scalar fields and counters."""

    vc_ports = tuple(
        port
        for port in module.ports
        if port.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT
    )
    if not vc_ports:
        return module
    protocol_shared._validate_leaf_endpoint_module(
        module, ir_interfaces.InterfaceProtocol.VC_CREDIT, "VC-credit"
    )
    producers = protocol_shared._endpoint_producers(
        module,
        protocol=ir_interfaces.InterfaceProtocol.VC_CREDIT,
        signal_type=ir_interfaces.VirtualChannelCreditSignal,
        derived=frozenset({
            ir_interfaces.VirtualChannelCreditSignal.TRANSFER,
            ir_interfaces.VirtualChannelCreditSignal.CREDITS,
        }),
        label="VC-credit",
    )

    count_registers: dict[str, tuple[ir_module.Register, ...]] = {}
    for port in vc_ports:
        if port.virtual_channels is None or port.virtual_channels < 1:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"VC-credit endpoint '{port.name}' has no channel count"
            )
        type_, domain, kind, initial = protocol_shared.credit_counter_spec(
            module, port, "VC-credit"
        )
        count_registers[port.name] = tuple(
            ir_module.Register(
                protocol_shared.vc_credit_state_name(port.name, kind, channel),
                type_,
                expr.Constant(initial, type_),
                domain,
            )
            for channel in range(port.virtual_channels)
        )

    ports = {port.name: port for port in vc_ports}
    lowerer = _VirtualChannelCreditExpressionLowerer(
        module,
        ports,
        producers,
        count_registers,
    )
    scalar = protocol_shared.ScalarPortInventory.retained_wires(module)
    for port in vc_ports:
        scalar.add_protocol_endpoint(port, protocol_shared.vc_credit_field_name)
        count_field = (
            ir_interfaces.VirtualChannelCreditSignal.CREDITS.value
            if port.direction is ir_module.PortDirection.OUTPUT
            else "occupancy"
        )
        count_name = protocol_shared.vc_credit_field_name(port.name, count_field)
        scalar.add(
            ir_module.PortDirection.OUTPUT,
            count_name, lowerer._count_vector(port).type, port.domain,
        )

    assignments = protocol_shared._retained_wire_assignments(module, lowerer)
    for port in vc_ports:
        for field in protocol_shared.protocol_scalar_fields(port):
            if field.external:
                continue
            assignments.append(
                scalar.assignment(
                    protocol_shared.vc_credit_field_name(port.name, field.signal),
                    lowerer.signal(port.name, field.signal),
                )
            )
        count_field = (
            ir_interfaces.VirtualChannelCreditSignal.CREDITS.value
            if port.direction is ir_module.PortDirection.OUTPUT
            else "occupancy"
        )
        assignments.append(
            scalar.assignment(
                protocol_shared.vc_credit_field_name(port.name, count_field),
                lowerer._count_vector(port),
            )
        )

    next_assignments = list(lowerer.value(module.next_assignments))
    runtime_scopes = []
    for port in vc_ports:
        sent = lowerer.signal(port.name, ir_interfaces.VirtualChannelCreditSignal.SEND)
        returned = lowerer.signal(port.name, ir_interfaces.VirtualChannelCreditSignal.RETURN)
        vc = lowerer.signal(port.name, ir_interfaces.VirtualChannelCreditSignal.VC)
        return_vc = lowerer.signal(
            port.name,
            ir_interfaces.VirtualChannelCreditSignal.RETURN_VC,
        )
        conditions: list[tuple[str, expr.Expression]] = []
        for channel, register in enumerate(count_registers[port.name]):
            sent_here = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                sent,
                lowerer._channel_match(vc, port, channel),
            )
            returned_here = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                returned,
                lowerer._channel_match(return_vc, port, channel),
            )
            increment = (
                returned_here
                if port.direction is ir_module.PortDirection.OUTPUT
                else sent_here
            )
            decrement = (
                sent_here
                if port.direction is ir_module.PortDirection.OUTPUT
                else returned_here
            )
            update, counter_conditions = protocol_shared.credit_counter_update(
                register,
                sent_here,
                returned_here,
                port.capacity,
                port.direction,
                (
                    f"vc_credit interface '{port.name}' overflow on VC {channel}",
                    f"vc_credit interface '{port.name}' underflow on VC {channel}",
                    f"vc_credit interface '{port.name}' overflow on VC {channel}",
                ),
                increase=increment,
                decrease=decrement,
            )
            next_assignments.append(update)
            conditions.extend(counter_conditions)
        runtime_scopes.append(
            protocol_shared._runtime_protocol_scope(
                module=module,
                port=port,
                conditions=tuple(conditions),
            )
        )

    return protocol_shared._finalize_protocol_module(
        module,
        lowerer,
        ports=tuple(scalar.ports),
        assignments=tuple(assignments),
        rewritten={
            "registers": (
                *lowerer.value(module.registers),
                *(register for group in count_registers.values() for register in group),
            ),
            "next_assignments": tuple(next_assignments),
            "verification_scopes": (
                *lowerer.value(module.verification_scopes),
                *runtime_scopes,
            ),
        },
    )
