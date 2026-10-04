# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Public protocol-value access for one native simulation instance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from zlang.ir.interfaces import (
    CreditSignal,
    InterfaceProtocol,
    PacketSignal,
    ReadyValidSignal,
    RequestResponseChannel,
    RequestResponseRole,
    VirtualChannelCreditSignal,
    ready_valid_field_name,
)
from zlang.ir.module import Port, PortDirection, RequestResponseInterface
from zlang.ir.types import BitType, HardwareType, UIntType, VecType
from zlang import simulation_protocol_shared as protocol_shared
from zlang.simulation_protocol_shared import (
    credit_field_name,
    packet_field_name,
    request_response_field_name,
    vc_credit_field_name,
)
from zlang.simulation_values import SimulationRuntimeError

if TYPE_CHECKING:
    from zlang.sim import Simulator

_ProtocolSignal = (
    ReadyValidSignal | CreditSignal | VirtualChannelCreditSignal | PacketSignal
)


class SimulationProtocolAccess:
    """Own protocol field shapes and typed public get/set projection."""

    def __init__(self, simulator: Simulator) -> None:
        self._simulator = simulator

    @staticmethod
    def input_fields(port: Port) -> tuple[tuple[str, HardwareType], ...]:
        try:
            fields = protocol_shared.protocol_scalar_fields(port)
        except protocol_shared.ProtocolSimulationLoweringError as error:
            raise SimulationRuntimeError(str(error)) from error
        return tuple(
            (field.signal.value, field.type)
            for field in fields
            if field.external
        )

    @staticmethod
    def field_name(port: Port, field: str) -> str:
        if port.protocol is InterfaceProtocol.READY_VALID:
            return ready_valid_field_name(port.name, field)
        if port.protocol is InterfaceProtocol.CREDIT:
            return credit_field_name(port.name, field)
        if port.protocol is InterfaceProtocol.VC_CREDIT:
            return vc_credit_field_name(port.name, field)
        if port.protocol is InterfaceProtocol.PACKET:
            return packet_field_name(port.name, field)
        raise SimulationRuntimeError(
            f"public protocol '{port.protocol.value}' is not supported"
        )

    def set_port(self, port: Port, value: object) -> None:
        for field, internal, type_, field_value in self.port_updates(port, value):
            try:
                self._simulator._set_scalar(internal, type_, field_value)
            except SimulationRuntimeError as error:
                raise SimulationRuntimeError(
                    f"{port.name}.{field}: {error}"
                ) from error

    def port_updates(
        self,
        port: Port,
        value: object,
        *,
        label: str | None = None,
    ) -> tuple[tuple[str, str, HardwareType, object], ...]:
        if label is None:
            label = (
                "ready/valid"
                if port.protocol is InterfaceProtocol.READY_VALID
                else port.protocol.value
            )
        if not isinstance(value, Mapping):
            raise SimulationRuntimeError(
                f"{label} input '{port.name}' must be a field mapping"
            )
        fields = self.input_fields(port)
        expected = {name for name, _ in fields}
        if set(value) != expected:
            rendered = ", ".join(sorted(expected))
            raise SimulationRuntimeError(
                f"{label} input '{port.name}' requires fields: {rendered}"
            )
        return tuple(
            (field, self.field_name(port, field), type_, value[field])
            for field, type_ in fields
        )

    def get_port(self, port: Port) -> dict[str, object]:
        return self.decode_port(port, self._simulator._get_scalar)

    def decode_port(
        self,
        port: Port,
        read: Callable[[str, HardwareType], object],
    ) -> dict[str, object]:
        handlers = {
            InterfaceProtocol.READY_VALID: self._get_ready_valid,
            InterfaceProtocol.CREDIT: self._get_credit,
            InterfaceProtocol.VC_CREDIT: self._get_vc_credit,
            InterfaceProtocol.PACKET: self._get_packet,
        }
        handler = handlers.get(port.protocol)
        if handler is None:
            raise SimulationRuntimeError(
                f"public protocol '{port.protocol.value}' is not supported"
            )
        return handler(port, read)

    def _port_reader(
        self,
        port: Port,
        read: Callable[[str, HardwareType], object],
    ) -> Callable[[_ProtocolSignal], object]:
        """Bind scalar field names and types to one public endpoint reader."""

        try:
            fields = {
                field.signal: field.type
                for field in protocol_shared.protocol_scalar_fields(port)
            }
        except protocol_shared.ProtocolSimulationLoweringError as error:
            raise SimulationRuntimeError(str(error)) from error

        def scalar(signal: _ProtocolSignal) -> object:
            return read(
                self.field_name(port, signal.value),
                fields[signal],
            )

        return scalar

    def _get_ready_valid(
        self,
        port: Port,
        read: Callable[[str, HardwareType], object],
    ) -> dict[str, object]:
        scalar = self._port_reader(port, read)
        valid = int(scalar(ReadyValidSignal.VALID))
        ready = int(scalar(ReadyValidSignal.READY))
        transfer = int(bool(valid) and bool(ready))
        if port.direction is PortDirection.INPUT:
            return {"ready": ready, "transfer": transfer}
        return {
            "payload": scalar(ReadyValidSignal.PAYLOAD),
            "valid": valid,
            "transfer": transfer,
        }

    def _get_credit(
        self,
        port: Port,
        read: Callable[[str, HardwareType], object],
    ) -> dict[str, object]:
        scalar = self._port_reader(port, read)
        sent = int(scalar(CreditSignal.SEND))
        if port.direction is PortDirection.INPUT:
            return {"return": int(scalar(CreditSignal.RETURN)), "transfer": sent}
        if port.capacity is None:
            raise SimulationRuntimeError(
                f"credit endpoint '{port.name}' has no capacity"
            )
        count_type = UIntType(max(1, port.capacity.bit_length()))
        return {
            "payload": scalar(CreditSignal.PAYLOAD),
            "send": sent,
            "transfer": sent,
            "credits": int(
                read(credit_field_name(port.name, CreditSignal.CREDITS), count_type)
            ),
        }

    def _get_vc_credit(
        self,
        port: Port,
        read: Callable[[str, HardwareType], object],
    ) -> dict[str, object]:
        if (
            port.capacity is None
            or port.virtual_channels is None
            or port.virtual_channels < 1
        ):
            raise SimulationRuntimeError(
                f"VC-credit endpoint '{port.name}' has incomplete bounds"
            )
        count_type = UIntType(max(1, port.capacity.bit_length()))
        counts_type = VecType(port.virtual_channels, count_type)
        scalar = self._port_reader(port, read)
        sent = int(scalar(VirtualChannelCreditSignal.SEND))
        if port.direction is PortDirection.INPUT:
            occupancy = read(
                vc_credit_field_name(port.name, "occupancy"),
                counts_type,
            )
            return {
                "return": int(scalar(VirtualChannelCreditSignal.RETURN)),
                "return_vc": scalar(VirtualChannelCreditSignal.RETURN_VC),
                "transfer": sent,
                "occupancy": tuple(occupancy),
            }
        credits = read(
            vc_credit_field_name(port.name, VirtualChannelCreditSignal.CREDITS),
            counts_type,
        )
        return {
            "payload": scalar(VirtualChannelCreditSignal.PAYLOAD),
            "vc": scalar(VirtualChannelCreditSignal.VC),
            "send": sent,
            "transfer": sent,
            "credits": tuple(credits),
        }

    def _get_packet(
        self,
        port: Port,
        read: Callable[[str, HardwareType], object],
    ) -> dict[str, object]:
        scalar = self._port_reader(port, read)
        valid = int(scalar(PacketSignal.VALID))
        ready = int(scalar(PacketSignal.READY))
        transfer = int(bool(valid) and bool(ready))
        if port.direction is PortDirection.INPUT:
            return {"ready": ready, "transfer": transfer}
        grant_valid = int(
            read(packet_field_name(port.name, "grant_valid"), BitType())
        )
        owner_count = sum(
            candidate.protocol is InterfaceProtocol.PACKET
            and candidate.direction is PortDirection.INPUT
            for candidate in self._simulator.program.module.ports
        )
        owner_type = UIntType(max(1, (owner_count - 1).bit_length()))
        grant = read(packet_field_name(port.name, "grant"), owner_type)
        return {
            "payload": scalar(PacketSignal.PAYLOAD),
            "valid": valid,
            "last": int(scalar(PacketSignal.LAST)),
            "transfer": transfer,
            "grant": grant if grant_valid else None,
        }

    @staticmethod
    def trace_fields(port: Port) -> tuple[str, ...]:
        try:
            fields = tuple(
                field.signal.value
                for field in protocol_shared.protocol_scalar_fields(port)
            )
        except protocol_shared.ProtocolSimulationLoweringError as error:
            raise SimulationRuntimeError(str(error)) from error
        if port.protocol is InterfaceProtocol.CREDIT:
            return fields + (
                ("credits",) if port.direction is PortDirection.OUTPUT else ()
            )
        if port.protocol is InterfaceProtocol.VC_CREDIT:
            return fields + (
                ("credits",)
                if port.direction is PortDirection.OUTPUT
                else ("occupancy",)
            )
        if (
            port.protocol is InterfaceProtocol.PACKET
            and port.direction is PortDirection.OUTPUT
        ):
            return fields + ("grant", "grant_valid")
        return fields

    @staticmethod
    def request_response_input_fields(
        interface: RequestResponseInterface,
    ) -> tuple[tuple[RequestResponseChannel, str, HardwareType], ...]:
        return tuple(
            (field.channel, field.signal.value, field.type)
            for field in protocol_shared.request_response_scalar_fields(interface)
            if field.external
        )

    def set_request_response(
        self,
        interface: RequestResponseInterface,
        value: object,
    ) -> None:
        for internal, type_, field_value in self.request_response_updates(
            interface,
            value,
        ):
            self._simulator._set_scalar(internal, type_, field_value)

    def request_response_updates(
        self,
        interface: RequestResponseInterface,
        value: object,
    ) -> tuple[tuple[str, HardwareType, object], ...]:
        if not isinstance(value, Mapping) or set(value) != {"request", "response"}:
            raise SimulationRuntimeError(
                f"request/response input '{interface.name}' requires request "
                "and response mappings"
            )
        expected: dict[str, dict[str, HardwareType]] = {
            "request": {},
            "response": {},
        }
        for channel, field, type_ in self.request_response_input_fields(interface):
            expected[channel.value][field] = type_
        updates: list[tuple[str, HardwareType, object]] = []
        for channel_name, fields in expected.items():
            supplied = value[channel_name]
            if not isinstance(supplied, Mapping) or set(supplied) != set(fields):
                rendered = ", ".join(sorted(fields))
                raise SimulationRuntimeError(
                    f"request/response input '{interface.name}.{channel_name}' "
                    f"requires fields: {rendered}"
                )
            for field, type_ in fields.items():
                updates.append((
                    request_response_field_name(
                        interface.name,
                        channel_name,
                        field,
                    ),
                    type_,
                    supplied[field],
                ))
        return tuple(updates)

    def get_request_response(
        self,
        interface: RequestResponseInterface,
    ) -> dict[str, object]:
        return self.decode_request_response(interface, self._simulator._get_scalar)

    def decode_request_response(
        self,
        interface: RequestResponseInterface,
        read: Callable[[str, HardwareType], object],
    ) -> dict[str, object]:
        def scalar(
            channel: RequestResponseChannel,
            field: str,
            type_: HardwareType,
        ) -> object:
            return read(
                request_response_field_name(interface.name, channel, field),
                type_,
            )

        request_valid = int(
            scalar(RequestResponseChannel.REQUEST, "valid", BitType())
        )
        request_ready = int(
            scalar(RequestResponseChannel.REQUEST, "ready", BitType())
        )
        response_valid = int(
            scalar(RequestResponseChannel.RESPONSE, "valid", BitType())
        )
        response_ready = int(
            scalar(RequestResponseChannel.RESPONSE, "ready", BitType())
        )
        request_transfer = int(bool(request_valid) and bool(request_ready))
        response_transfer = int(bool(response_valid) and bool(response_ready))
        count_type = UIntType(max(1, interface.max_outstanding.bit_length()))
        outstanding = int(
            read(
                request_response_field_name(
                    interface.name,
                    "ledger",
                    "outstanding",
                ),
                count_type,
            )
        )
        if interface.role is RequestResponseRole.REQUESTER:
            return {
                "request": {
                    "payload": scalar(
                        RequestResponseChannel.REQUEST,
                        "payload",
                        interface.request_type,
                    ),
                    "valid": request_valid,
                    "transfer": request_transfer,
                },
                "response": {
                    "ready": response_ready,
                    "transfer": response_transfer,
                },
                "outstanding": outstanding,
            }
        return {
            "request": {
                "ready": request_ready,
                "transfer": request_transfer,
            },
            "response": {
                "payload": scalar(
                    RequestResponseChannel.RESPONSE,
                    "payload",
                    interface.response_type,
                ),
                "valid": response_valid,
                "transfer": response_transfer,
            },
            "outstanding": outstanding,
        }

    @staticmethod
    def request_response_trace_names(
        interface: RequestResponseInterface,
    ) -> tuple[tuple[str, str], ...]:
        fields = tuple(
            (
                request_response_field_name(
                    interface.name, field.channel, field.signal
                ),
                f"{interface.name}.{field.channel.value}.{field.signal.value}",
            )
            for field in protocol_shared.request_response_scalar_fields(interface)
        )
        return (
            *fields,
            (
                request_response_field_name(
                    interface.name,
                    "ledger",
                    "outstanding",
                ),
                f"{interface.name}.outstanding",
            ),
        )


__all__ = ["SimulationProtocolAccess"]
