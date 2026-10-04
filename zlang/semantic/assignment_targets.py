# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Writable port and request/response assignment target resolution."""

from __future__ import annotations

from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types

from .errors import SemanticError


def resolve_port_target(
    target_text: str,
    ports: dict[str, ir_module.Port],
) -> tuple[ir_module.Port, ir_interfaces.InterfaceSignal | None, ir_types.HardwareType]:
    parts = target_text.split(".")
    port = ports.get(parts[0])
    if port is None:
        raise SemanticError(f"assignment target '{target_text}' is not a port")
    field = parts[1] if len(parts) == 2 else None

    if port.protocol is ir_interfaces.InterfaceProtocol.WIRE:
        if field is not None:
            raise SemanticError(
                f"wire interface '{port.name}' has no field '{field}'"
            )
        if port.direction is ir_module.PortDirection.INPUT:
            raise SemanticError(f"cannot assign to input '{target_text}'")
        return port, None, port.type

    if field is None:
        raise SemanticError(
            f"{port.protocol.value.replace('_', '/')} interface '{port.name}' "
            "must be assigned by field"
        )
    if port.protocol is ir_interfaces.InterfaceProtocol.READY_VALID:
        try:
            ready_valid_signal = ir_interfaces.ReadyValidSignal(field)
        except ValueError as error:
            raise SemanticError(
                f"ready/valid interface '{port.name}' has no field '{field}'"
            ) from error
        if ready_valid_signal is ir_interfaces.ReadyValidSignal.TRANSFER:
            raise SemanticError(
                f"ready/valid transfer '{port.name}.transfer' is read-only"
            )
        writable: set[ir_interfaces.InterfaceSignal] = (
            {ir_interfaces.ReadyValidSignal.READY}
            if port.direction is ir_module.PortDirection.INPUT
            else {ir_interfaces.ReadyValidSignal.PAYLOAD, ir_interfaces.ReadyValidSignal.VALID}
        )
        if ready_valid_signal not in writable:
            raise SemanticError(
                f"cannot drive incoming ready/valid field '{port.name}.{field}'"
            )
        type_ = (
            port.type
            if ready_valid_signal is ir_interfaces.ReadyValidSignal.PAYLOAD
            else ir_types.BitType()
        )
        return port, ready_valid_signal, type_

    if port.protocol is ir_interfaces.InterfaceProtocol.PACKET:
        try:
            packet_signal = ir_interfaces.PacketSignal(field)
        except ValueError as error:
            raise SemanticError(
                f"packet interface '{port.name}' has no field '{field}'"
            ) from error
        if packet_signal is ir_interfaces.PacketSignal.TRANSFER:
            raise SemanticError(
                f"packet transfer '{port.name}.transfer' is read-only"
            )
        packet_writable: set[ir_interfaces.InterfaceSignal] = (
            {ir_interfaces.PacketSignal.READY}
            if port.direction is ir_module.PortDirection.INPUT
            else {
                ir_interfaces.PacketSignal.PAYLOAD,
                ir_interfaces.PacketSignal.VALID,
                ir_interfaces.PacketSignal.LAST,
            }
        )
        if packet_signal not in packet_writable:
            raise SemanticError(
                f"cannot drive incoming packet field '{port.name}.{field}'"
            )
        type_ = (
            port.type if packet_signal is ir_interfaces.PacketSignal.PAYLOAD else ir_types.BitType()
        )
        return port, packet_signal, type_

    if port.protocol is ir_interfaces.InterfaceProtocol.VC_CREDIT:
        try:
            vc_signal = ir_interfaces.VirtualChannelCreditSignal(field)
        except ValueError as error:
            raise SemanticError(
                f"virtual-channel credit interface '{port.name}' has no field "
                f"'{field}'"
            ) from error
        if vc_signal in {
            ir_interfaces.VirtualChannelCreditSignal.TRANSFER,
            ir_interfaces.VirtualChannelCreditSignal.CREDITS,
        }:
            raise SemanticError(
                f"virtual-channel credit field '{port.name}.{field}' is read-only"
            )
        writable: set[ir_interfaces.InterfaceSignal] = (
            {
                ir_interfaces.VirtualChannelCreditSignal.RETURN,
                ir_interfaces.VirtualChannelCreditSignal.RETURN_VC,
            }
            if port.direction is ir_module.PortDirection.INPUT
            else {
                ir_interfaces.VirtualChannelCreditSignal.PAYLOAD,
                ir_interfaces.VirtualChannelCreditSignal.VC,
                ir_interfaces.VirtualChannelCreditSignal.SEND,
            }
        )
        if vc_signal not in writable:
            raise SemanticError(
                f"cannot drive incoming virtual-channel credit field "
                f"'{port.name}.{field}'"
            )
        if port.virtual_channels is None:
            raise SemanticError(
                f"virtual-channel credit interface '{port.name}' has no channel count"
            )
        if vc_signal is ir_interfaces.VirtualChannelCreditSignal.PAYLOAD:
            type_ = port.type
        elif vc_signal in {
            ir_interfaces.VirtualChannelCreditSignal.VC,
            ir_interfaces.VirtualChannelCreditSignal.RETURN_VC,
        }:
            type_ = ir_types.UIntType(max(1, (port.virtual_channels - 1).bit_length()))
        else:
            type_ = ir_types.BitType()
        return port, vc_signal, type_

    try:
        credit_signal = ir_interfaces.CreditSignal(field)
    except ValueError as error:
        raise SemanticError(
            f"credit interface '{port.name}' has no field '{field}'"
        ) from error
    if credit_signal in {ir_interfaces.CreditSignal.TRANSFER, ir_interfaces.CreditSignal.CREDITS}:
        raise SemanticError(
            f"credit field '{port.name}.{field}' is read-only"
        )
    credit_writable: set[ir_interfaces.InterfaceSignal] = (
        {ir_interfaces.CreditSignal.RETURN}
        if port.direction is ir_module.PortDirection.INPUT
        else {ir_interfaces.CreditSignal.PAYLOAD, ir_interfaces.CreditSignal.SEND}
    )
    if credit_signal not in credit_writable:
        raise SemanticError(
            f"cannot drive incoming credit field '{port.name}.{field}'"
        )
    credit_type = (
        port.type if credit_signal is ir_interfaces.CreditSignal.PAYLOAD else ir_types.BitType()
    )
    return port, credit_signal, credit_type

def resolve_request_response_target(
    target_text: str,
    interfaces: dict[str, ir_module.RequestResponseInterface],
) -> tuple[
    ir_module.RequestResponseInterface,
    ir_interfaces.RequestResponseChannel,
    ir_interfaces.ReadyValidSignal,
    ir_types.HardwareType,
]:
    parts = target_text.split(".")
    interface = interfaces.get(parts[0])
    if interface is None:
        raise SemanticError(
            f"assignment target '{target_text}' is not a request/response interface"
        )
    if len(parts) != 3:
        raise SemanticError(
            f"request/response interface '{interface.name}' assignments require "
            "a channel and field"
        )
    try:
        channel = ir_interfaces.RequestResponseChannel(parts[1])
    except ValueError as error:
        raise SemanticError(
            f"request/response interface '{interface.name}' has no channel "
            f"'{parts[1]}'"
        ) from error
    try:
        signal = ir_interfaces.ReadyValidSignal(parts[2])
    except ValueError as error:
        raise SemanticError(
            f"request/response channel '{interface.name}.{channel.value}' has "
            f"no field '{parts[2]}'"
        ) from error
    if signal is ir_interfaces.ReadyValidSignal.TRANSFER:
        raise SemanticError(
            f"request/response transfer '{target_text}' is read-only"
        )
    # Role is inferred after all fields are seen.  Accept both channel halves
    # here so a child responder can use the same declaration syntax; the
    # complete, non-overlapping ownership contract is checked below.
    writable = {
        (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.PAYLOAD),
        (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.VALID),
        (ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.READY),
        (ir_interfaces.RequestResponseChannel.REQUEST, ir_interfaces.ReadyValidSignal.READY),
        (ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.PAYLOAD),
        (ir_interfaces.RequestResponseChannel.RESPONSE, ir_interfaces.ReadyValidSignal.VALID),
    }
    if (channel, signal) not in writable:
        raise SemanticError(
            f"cannot drive incoming request/response field '{target_text}'"
        )
    if signal is ir_interfaces.ReadyValidSignal.PAYLOAD:
        type_ = (
            interface.request_type
            if channel is ir_interfaces.RequestResponseChannel.REQUEST
            else interface.response_type
        )
    else:
        type_ = ir_types.BitType()
    return interface, channel, signal, type_
