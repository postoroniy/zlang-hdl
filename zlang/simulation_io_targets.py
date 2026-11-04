# SPDX-License-Identifier: Apache-2.0
"""Compiler-owned locations of environment-driven simulation fields."""

from dataclasses import dataclass

from zlang.ir import expressions as ex
from zlang.ir.interfaces import InterfaceProtocol, ready_valid_field_name
from zlang.ir.module import PortDirection
from zlang.ir.packing import struct_field_lsb
from zlang.ir.types import HardwareType
from zlang.simulation_protocol_shared import (
    protocol_scalar_fields,
    request_response_scalar_fields,
    request_response_field_name,
)

PROTOCOL_REFS = (
    ex.ReadyValidRef,
    ex.PacketRef,
    ex.CreditRef,
    ex.VirtualChannelCreditRef,
)


def reference_name(expression):
    if isinstance(expression, ex.InputRef):
        return expression.name
    if isinstance(expression, ex.RequestResponseRef):
        return request_response_field_name(
            expression.interface, expression.channel, expression.signal
        )
    if isinstance(expression, PROTOCOL_REFS):
        return ready_valid_field_name(expression.interface, expression.signal.value)
    return None


@dataclass(frozen=True)
class InputLocation:
    name: str
    lsb: int
    width: int
    container_type: HardwareType


def input_location(expression, ports, request_responses=None) -> InputLocation:
    """Resolve a typed field using the same packing and protocol names as RTL."""
    if isinstance(expression, ex.FieldAccess):
        parent = input_location(expression.expression, ports, request_responses)
        return InputLocation(
            parent.name,
            parent.lsb + struct_field_lsb(expression.expression.type, expression.field),
            expression.type.width,
            parent.container_type,
        )
    if isinstance(expression, ex.InputRef):
        port = ports.get(expression.name)
        if (
            port is not None
            and port.direction is PortDirection.INPUT
            and port.protocol is InterfaceProtocol.WIRE
        ):
            return InputLocation(port.name, 0, port.type.width, port.type)
    if isinstance(expression, PROTOCOL_REFS):
        port = ports.get(expression.interface)
        if port is not None:
            if any(
                f.external and f.signal is expression.signal
                for f in protocol_scalar_fields(port)
            ):
                return InputLocation(
                    reference_name(expression),
                    0,
                    expression.type.width,
                    expression.type,
                )
    if isinstance(expression, ex.RequestResponseRef):
        interface = (request_responses or {}).get(expression.interface)
        if interface is not None and any(
            f.external
            and f.channel is expression.channel
            and f.signal is expression.signal
            for f in request_response_scalar_fields(interface)
        ):
            return InputLocation(
                reference_name(expression), 0, expression.type.width, expression.type
            )
    raise ValueError(
        "simulation read/drive target must be an environment-owned top input or interface field"
    )


def overlaps(left: InputLocation, right: InputLocation) -> bool:
    return (
        left.name == right.name
        and left.lsb < right.lsb + right.width
        and right.lsb < left.lsb + left.width
    )


def dependency_names(expression):
    """Transfer is a derived observation, not a separately driven input."""
    from dataclasses import replace

    if (
        isinstance(expression, (*PROTOCOL_REFS, ex.RequestResponseRef))
        and expression.signal.value == "transfer"
    ):
        signals = (
            ("send",)
            if isinstance(expression, (ex.CreditRef, ex.VirtualChannelCreditRef))
            else ("valid", "ready")
        )
        return tuple(
            reference_name(replace(expression, signal=type(expression.signal)(signal)))
            for signal in signals
        )
    name = reference_name(expression)
    return (name,) if name is not None else ()
