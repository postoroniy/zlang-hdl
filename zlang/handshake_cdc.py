# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Old-state transitions and observable signals of a one-slot CDC handshake."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir import expressions as expr
from zlang.ir.cdc import ClockDomain, CrossingKind
from zlang.ir.interfaces import InterfaceProtocol, ReadyValidSignal
from zlang.ir.module import Assignment, Connection, NextAssignment, Register
from zlang.ir.types import BitType, HardwareType


@dataclass(frozen=True)
class HandshakeCdcPlan:
    registers: tuple[Register, ...]
    synchronizers: tuple[tuple[Register, Register], tuple[Register, Register]]
    updates: tuple[NextAssignment, ...]
    outputs: tuple[Assignment, ...]


def build_handshake_cdc_plan(
    connection: Connection, source_domain: ClockDomain, destination_domain: ClockDomain,
    *, state_prefix: str = "",
) -> HandshakeCdcPlan:
    source, destination = connection.source, connection.destination
    if (
        connection.crossing is None or connection.crossing.kind is not CrossingKind.HANDSHAKE
        or source.protocol is not InterfaceProtocol.READY_VALID
        or destination.protocol is not InterfaceProtocol.READY_VALID
        or source.type != destination.type
        or source.domain != source_domain.clock or destination.domain != destination_domain.clock
        or source.domain == destination.domain
    ):
        raise ValueError("handshake requires matching ready/valid endpoints in distinct resolved domains")

    bit = BitType()

    def state(role: str, domain: ClockDomain, type_: HardwareType = bit) -> Register:
        return Register(state_prefix + role, type_, expr.Constant(0, type_), domain.clock)

    def read(register: Register) -> expr.RegisterRef:
        return expr.RegisterRef(register.name, register.type)

    def invert(value: expr.Expression) -> expr.Binary:
        return expr.Binary(expr.BinaryOperator.BIT_XOR, value, expr.Constant(1, bit), bit, bit)

    def equal(left: Register, right: Register) -> expr.Binary:
        return expr.Binary(expr.BinaryOperator.EQUAL, read(left), read(right), bit, bit)

    def enabled(domain: ClockDomain, condition: expr.Expression) -> expr.Binary:
        reset = expr.InputRef(f"$reset:{domain.reset}", bit)
        return expr.Binary(expr.BinaryOperator.BIT_AND, invert(reset), condition, bit, bit)

    held = state("held", source_domain, source.type)
    request = state("request", source_domain)
    ack1, ack2 = state("ack_sync1", source_domain), state("ack_sync2", source_domain)
    req1, req2 = state("request_sync1", destination_domain), state("request_sync2", destination_domain)
    ack = state("acknowledge", destination_domain)
    send = expr.ReadyValidRef(source.name, ReadyValidSignal.TRANSFER, bit)
    take = expr.ReadyValidRef(destination.name, ReadyValidSignal.TRANSFER, bit)
    payload = expr.ReadyValidRef(source.name, ReadyValidSignal.PAYLOAD, source.type)
    return HandshakeCdcPlan(
        (held, request, ack1, ack2, req1, req2, ack),
        ((ack1, ack2), (req1, req2)),
        (
            NextAssignment(ack1, read(ack)), NextAssignment(ack2, read(ack1)),
            NextAssignment(held, payload, send), NextAssignment(request, invert(read(request)), send),
            NextAssignment(req1, read(request)), NextAssignment(req2, read(req1)),
            NextAssignment(ack, read(req2), take),
        ),
        (
            Assignment(source, enabled(source_domain, equal(request, ack2)), ReadyValidSignal.READY),
            Assignment(destination, read(held), ReadyValidSignal.PAYLOAD),
            Assignment(destination, enabled(destination_domain, invert(equal(req2, ack))), ReadyValidSignal.VALID),
        ),
    )
