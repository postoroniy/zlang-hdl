# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned direct connection lowering and protocol-cycle validation."""

from __future__ import annotations


from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import interfaces as ir_interfaces
from zlang.ir.traversal import expression_children
from zlang.ir.types import BitType
from zlang.source import SourceOrigin

from .errors import SemanticError
from .module_validation import reject_dependency_cycles


def render_assignment_target(
    target: ir_module.Port | ir_module.RequestResponseInterface,
    signal: ir_interfaces.InterfaceSignal | None,
    channel: ir_interfaces.RequestResponseChannel | None = None,
) -> str:
    if channel is not None and signal is not None:
        return f"{target.name}.{channel.value}.{signal.value}"
    return target.name if signal is None else f"{target.name}.{signal.value}"

class ConnectionAnalyzer:
    """Lower one compiler-resolved direct protocol connection."""

    @staticmethod
    def expand_direct(
        connection: ir_module.Connection,
    ) -> tuple[ir_module.Assignment, ...]:
        source = connection.source
        destination = connection.destination
        if source.protocol is ir_interfaces.InterfaceProtocol.WIRE:
            return (
                ir_module.Assignment(
                    destination,
                    ir_expr.InputRef(source.name, source.type),
                ),
            )
        if source.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            return (
                ir_module.Assignment(
                    destination,
                    ir_expr.ReadyValidRef(
                        source.name, ir_interfaces.ReadyValidSignal.PAYLOAD, source.type
                    ),
                    ir_interfaces.ReadyValidSignal.PAYLOAD,
                ),
                ir_module.Assignment(
                    destination,
                    ir_expr.ReadyValidRef(
                        source.name, ir_interfaces.ReadyValidSignal.VALID, BitType()
                    ),
                    ir_interfaces.ReadyValidSignal.VALID,
                ),
                ir_module.Assignment(
                    source,
                    ir_expr.ReadyValidRef(
                        destination.name, ir_interfaces.ReadyValidSignal.READY, BitType()
                    ),
                    ir_interfaces.ReadyValidSignal.READY,
                ),
            )
        return (
            ir_module.Assignment(
                destination,
                ir_expr.CreditRef(source.name, ir_interfaces.CreditSignal.PAYLOAD, source.type),
                ir_interfaces.CreditSignal.PAYLOAD,
            ),
            ir_module.Assignment(
                destination,
                ir_expr.CreditRef(source.name, ir_interfaces.CreditSignal.SEND, BitType()),
                ir_interfaces.CreditSignal.SEND,
            ),
            ir_module.Assignment(
                source,
                ir_expr.CreditRef(destination.name, ir_interfaces.CreditSignal.RETURN, BitType()),
                ir_interfaces.CreditSignal.RETURN,
            ),
        )

    @staticmethod
    def output_keys(
        connection: ir_module.Connection,
    ) -> tuple[tuple[str, None, ir_interfaces.InterfaceSignal | None], ...]:
        """Return every externally visible field owned by a connection."""

        source = connection.source
        destination = connection.destination
        if source.protocol is ir_interfaces.InterfaceProtocol.WIRE:
            return ((destination.name, None, None),)
        if source.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            source_signal: ir_interfaces.InterfaceSignal = ir_interfaces.ReadyValidSignal.READY
        else:
            source_signal = ir_interfaces.CreditSignal.RETURN
        if destination.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            destination_signals: tuple[ir_interfaces.InterfaceSignal, ...] = (
                ir_interfaces.ReadyValidSignal.PAYLOAD,
                ir_interfaces.ReadyValidSignal.VALID,
            )
        else:
            destination_signals = (ir_interfaces.CreditSignal.PAYLOAD, ir_interfaces.CreditSignal.SEND)
        return (
            (source.name, None, source_signal),
            *((destination.name, None, signal) for signal in destination_signals),
        )


class OutputConnectivityValidator:
    """Require every public output field to have one authoritative driver."""

    def validate(
        self,
        outputs: dict[str, ir_module.Port],
        protocols: dict[str, ir_module.Port],
        request_responses: tuple[ir_module.RequestResponseInterface, ...],
        assigned: set[
            tuple[
                str,
                ir_interfaces.RequestResponseChannel | None,
                ir_interfaces.InterfaceSignal | None,
            ]
        ],
        origins: dict[str, SourceOrigin | None],
    ) -> None:
        required: list[
            tuple[
                ir_module.Port | ir_module.RequestResponseInterface,
                ir_interfaces.RequestResponseChannel | None,
                ir_interfaces.InterfaceSignal | None,
            ]
        ] = [(port, None, None) for port in outputs.values()]
        for port in protocols.values():
            required.extend(self._protocol_outputs(port))
        for interface in request_responses:
            required.extend(self._request_response_outputs(interface))
        missing = next(
            (
                item for item in required
                if (item[0].name, item[1], item[2]) not in assigned
            ),
            None,
        )
        if missing is None:
            return
        target, channel, signal = missing
        raise SemanticError(
            f"output '{render_assignment_target(target, signal, channel)}' "
            "has no assignment",
            primary=origins.get(target.name),
        )

    @staticmethod
    def _protocol_outputs(
        port: ir_module.Port,
    ) -> tuple[
        tuple[ir_module.Port, None, ir_interfaces.InterfaceSignal], ...
    ]:
        if port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
            signals = (
                (ir_interfaces.ReadyValidSignal.READY,)
                if port.direction is ir_module.PortDirection.INPUT
                else (
                    ir_interfaces.ReadyValidSignal.PAYLOAD,
                    ir_interfaces.ReadyValidSignal.VALID,
                )
            )
        elif port.protocol is ir_interfaces.InterfaceProtocol.PACKET:
            signals = (
                (ir_interfaces.PacketSignal.READY,)
                if port.direction is ir_module.PortDirection.INPUT
                else (
                    ir_interfaces.PacketSignal.PAYLOAD,
                    ir_interfaces.PacketSignal.VALID,
                    ir_interfaces.PacketSignal.LAST,
                )
            )
        elif port.protocol is ir_interfaces.InterfaceProtocol.CREDIT:
            signals = (
                (ir_interfaces.CreditSignal.RETURN,)
                if port.direction is ir_module.PortDirection.INPUT
                else (
                    ir_interfaces.CreditSignal.PAYLOAD,
                    ir_interfaces.CreditSignal.SEND,
                )
            )
        else:
            signals = (
                (
                    ir_interfaces.VirtualChannelCreditSignal.RETURN,
                    ir_interfaces.VirtualChannelCreditSignal.RETURN_VC,
                )
                if port.direction is ir_module.PortDirection.INPUT
                else (
                    ir_interfaces.VirtualChannelCreditSignal.PAYLOAD,
                    ir_interfaces.VirtualChannelCreditSignal.VC,
                    ir_interfaces.VirtualChannelCreditSignal.SEND,
                )
            )
        return tuple((port, None, signal) for signal in signals)

    @staticmethod
    def _request_response_outputs(
        interface: ir_module.RequestResponseInterface,
    ) -> tuple[
        tuple[
            ir_module.RequestResponseInterface,
            ir_interfaces.RequestResponseChannel,
            ir_interfaces.ReadyValidSignal,
        ], ...
    ]:
        request = ir_interfaces.RequestResponseChannel.REQUEST
        response = ir_interfaces.RequestResponseChannel.RESPONSE
        if interface.role is ir_interfaces.RequestResponseRole.REQUESTER:
            fields = (
                (request, ir_interfaces.ReadyValidSignal.PAYLOAD),
                (request, ir_interfaces.ReadyValidSignal.VALID),
                (response, ir_interfaces.ReadyValidSignal.READY),
            )
        else:
            fields = (
                (request, ir_interfaces.ReadyValidSignal.READY),
                (response, ir_interfaces.ReadyValidSignal.PAYLOAD),
                (response, ir_interfaces.ReadyValidSignal.VALID),
            )
        return tuple((interface, channel, signal) for channel, signal in fields)

class ProtocolDependencyValidator:
    """Reject current-cycle cycles between driven protocol fields."""

    def validate(
        self,
        assignments: tuple[ir_module.Assignment, ...],
    ) -> None:
        driven = {
            (assignment.target.name, assignment.channel, assignment.signal): assignment
            for assignment in assignments
            if assignment.signal is not None
        }
        dependencies = {
            target: self._dependencies(assignment.expression) & driven.keys()
            for target, assignment in driven.items()
        }
        def describe_interface_cycle(
            cycle: tuple[
                tuple[str, ir_interfaces.RequestResponseChannel | None, ir_interfaces.InterfaceSignal], ...
            ],
        ) -> str:
            protocols = {
                "request_response" if channel is not None else type(signal)
                for _, channel, signal in cycle
            }
            if protocols == {ir_interfaces.ReadyValidSignal}:
                return "ready/valid"
            if protocols == {ir_interfaces.CreditSignal}:
                return "credit"
            if protocols == {"request_response"}:
                return "request/response"
            return "interface"

        reject_dependency_cycles(
            dependencies,
            render_node=lambda target: (
                f"{target[0]}.{target[1].value}.{target[2].value}"
                if target[1] is not None
                else f"{target[0]}.{target[2].value}"
            ),
            description=describe_interface_cycle,
            stable_sort=False,
        )

    def _dependencies(
        self,
        expression: ir_expr.Expression,
    ) -> set[
        tuple[str, ir_interfaces.RequestResponseChannel | None, ir_interfaces.InterfaceSignal]
    ]:
        dependencies: set[
            tuple[str, ir_interfaces.RequestResponseChannel | None, ir_interfaces.InterfaceSignal]
        ] = set()
        stack = [expression]
        while stack:
            current = stack.pop()
            if isinstance(current, ir_expr.ReadyValidRef):
                signals = (
                    (ir_interfaces.ReadyValidSignal.VALID, ir_interfaces.ReadyValidSignal.READY)
                    if current.signal is ir_interfaces.ReadyValidSignal.TRANSFER
                    else (current.signal,)
                )
                dependencies.update(
                    (current.interface, None, signal) for signal in signals
                )
                continue
            if isinstance(current, ir_expr.CreditRef):
                signal = (
                    ir_interfaces.CreditSignal.SEND
                    if current.signal is ir_interfaces.CreditSignal.TRANSFER
                    else current.signal
                )
                dependencies.add((current.interface, None, signal))
                continue
            if isinstance(current, ir_expr.PacketRef):
                signals = (
                    (ir_interfaces.PacketSignal.VALID, ir_interfaces.PacketSignal.READY)
                    if current.signal is ir_interfaces.PacketSignal.TRANSFER
                    else (current.signal,)
                )
                dependencies.update(
                    (current.interface, None, signal) for signal in signals
                )
                continue
            if isinstance(current, ir_expr.VirtualChannelCreditRef):
                signal = (
                    ir_interfaces.VirtualChannelCreditSignal.SEND
                    if current.signal is ir_interfaces.VirtualChannelCreditSignal.TRANSFER
                    else current.signal
                )
                dependencies.add((current.interface, None, signal))
                continue
            if isinstance(current, ir_expr.RequestResponseRef):
                signals = (
                    (ir_interfaces.ReadyValidSignal.VALID, ir_interfaces.ReadyValidSignal.READY)
                    if current.signal is ir_interfaces.ReadyValidSignal.TRANSFER
                    else (current.signal,)
                )
                dependencies.update(
                    (current.interface, current.channel, signal)
                    for signal in signals
                )
                continue

            if isinstance(current, ir_expr.FunctionalRegion):
                children = (
                    *(value for table in current.tables for value in table.values),
                    *(value for _, value in current.captures),
                )
            elif isinstance(current, ir_expr.Dot):
                children = (current.left, current.right)
            elif isinstance(current, ir_expr.Reduce):
                children = (current.collection,)
            else:
                children = expression_children(current)
            stack.extend(reversed(children))
        return dependencies
