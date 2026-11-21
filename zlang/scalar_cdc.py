# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Clocked state and observable outputs of the two scalar CDC primitives."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir import expressions as expr
from zlang.ir.cdc import ClockDomain, CrossingKind
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Assignment, Connection, NextAssignment, Register
from zlang.ir.types import BitType


@dataclass(frozen=True)
class ScalarCdcPlan:
    updates: tuple[NextAssignment, ...]
    synchronizers: tuple[Register, Register]
    output: Assignment


def build_scalar_cdc_plan(
    connection: Connection,
    source_domain: ClockDomain,
    destination_domain: ClockDomain,
    *,
    state_prefix: str = "",
) -> ScalarCdcPlan:
    """Build simultaneous old-state updates. Only the output mask is combinational."""
    source, destination = connection.source, connection.destination
    crossing = connection.crossing
    if crossing is None or crossing.kind not in (CrossingKind.SYNC_LEVEL, CrossingKind.PULSE_TOGGLE):
        raise ValueError("expected an explicit scalar CDC")
    bit = BitType()
    if (
        source.protocol is not InterfaceProtocol.WIRE
        or destination.protocol is not InterfaceProtocol.WIRE
        or source.type != bit
        or destination.type != bit
    ):
        raise ValueError(f"{crossing.kind.value} requires matching bit wire endpoints")
    if (
        source.domain != source_domain.clock
        or destination.domain != destination_domain.clock
        or source.domain == destination.domain
    ):
        raise ValueError("scalar CDC requires two distinct, resolved endpoint domains")

    zero = expr.Constant(0, bit)
    sample = expr.InputRef(source.name, bit)

    def state(role: str, domain: ClockDomain) -> Register:
        return Register(state_prefix + role, bit, zero, domain.clock)

    def read(register: Register) -> expr.RegisterRef:
        return expr.RegisterRef(register.name, bit)

    def xor(left: expr.Expression, right: expr.Expression) -> expr.Binary:
        return expr.Binary(expr.BinaryOperator.BIT_XOR, left, right, bit, bit)

    if crossing.kind is CrossingKind.SYNC_LEVEL:
        first = state("stage1", destination_domain)
        second = state("stage2", destination_domain)
        updates = (NextAssignment(first, sample), NextAssignment(second, read(first)))
        output = read(second)
    else:
        toggle = state("source_toggle", source_domain)
        first = state("sync1", destination_domain)
        second = state("sync2", destination_domain)
        previous = state("previous", destination_domain)
        toggled = xor(read(toggle), expr.Constant(1, bit))
        updates = (
            NextAssignment(toggle, expr.Mux(sample, toggled, read(toggle), bit)),
            NextAssignment(first, read(toggle)),
            NextAssignment(second, read(first)),
            NextAssignment(previous, read(second)),
        )
        output = xor(read(second), read(previous))

    # This is the logical, effective domain reset, including conditioned release.
    reset = expr.InputRef(f"$reset:{destination_domain.reset}", bit)
    visible = expr.Mux(reset, zero, output, bit)
    return ScalarCdcPlan(updates, (first, second), Assignment(destination, visible))
