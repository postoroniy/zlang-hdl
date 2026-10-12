# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative selection of one primitive protocol-lowering pass."""

from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module
from zlang.simulation_credit import lower_credit_module
from zlang.simulation_packet import lower_packet_arbiter_module
from zlang.simulation_protocol_adapters import lower_adapter_module
from zlang.simulation_protocol_shared import ProtocolSimulationLoweringError
from zlang.simulation_ready_valid import lower_ready_valid_module
from zlang.simulation_vc_credit import lower_vc_credit_module


def lower_protocol_module(module: Module) -> Module:
    """Erase exactly one supported leaf protocol family."""

    protocols = {
        port.protocol
        for port in module.ports
        if port.protocol is not InterfaceProtocol.WIRE
    }
    if not protocols:
        return module
    if protocols == {InterfaceProtocol.READY_VALID}:
        return lower_ready_valid_module(module)
    if protocols == {InterfaceProtocol.CREDIT}:
        return lower_credit_module(module)
    if protocols == {InterfaceProtocol.VC_CREDIT}:
        return lower_vc_credit_module(module)
    if protocols == {InterfaceProtocol.PACKET}:
        return lower_packet_arbiter_module(module)
    if module.connections and any(
        connection.adapter is not None for connection in module.connections
    ):
        return lower_adapter_module(module)
    rendered = ", ".join(sorted(item.value for item in protocols))
    raise ProtocolSimulationLoweringError(
        f"primitive simulation protocol lowering does not support: {rendered}"
    )


__all__ = ["lower_protocol_module"]
