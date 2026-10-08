# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Packet protocol simulation lowering."""

from __future__ import annotations

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.ir.arbitration import ArbitrationPolicy, GrantScope
from zlang.simulation_primitives import bit_binary as _bit_binary
from zlang.simulation_primitives import bit_not as _bit_not


import zlang.simulation_protocol_shared as protocol_shared


class _PacketExpressionLowerer(
    protocol_shared._MappedProtocolExpressionLowerer[tuple[str, ir_interfaces.PacketSignal]],
):

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        if not isinstance(value, expr.PacketRef):
            return None
        if value.signal is ir_interfaces.PacketSignal.TRANSFER:
            try:
                valid = self._values[(value.interface, ir_interfaces.PacketSignal.VALID)]
                ready = self._values[(value.interface, ir_interfaces.PacketSignal.READY)]
            except KeyError as error:
                raise protocol_shared.ProtocolSimulationLoweringError(
                    f"packet transfer names incomplete endpoint '{value.interface}'"
                ) from error
            return _bit_binary(expr.BinaryOperator.BIT_AND, valid, ready)
        try:
            return self._values[(value.interface, value.signal)]
        except KeyError as error:
            raise protocol_shared.ProtocolSimulationLoweringError(
                f"packet reference names unknown field "
                f"'{value.interface}.{value.signal.value}'"
            ) from error


def _packet_select(
    selector: expr.Expression,
    choices: tuple[expr.Expression, ...],
) -> expr.Expression:
    selected = choices[0]
    for index, choice in enumerate(choices[1:], 1):
        selected = expr.Mux(
            expr.Binary(
                expr.BinaryOperator.EQUAL,
                selector,
                expr.Constant(index, selector.type),
                selector.type,
                ir_types.BitType(),
            ),
            choice,
            selected,
            choice.type,
        )
    return selected


def _packet_priority_candidate(
    valid: tuple[expr.Expression, ...],
    order: tuple[int, ...],
    owner_type: ir_types.UIntType,
) -> tuple[expr.Expression, expr.Expression]:
    candidate: expr.Expression = expr.Constant(0, owner_type)
    candidate_valid: expr.Expression = expr.Constant(0, ir_types.BitType())
    for index in reversed(order):
        candidate = expr.Mux(
            valid[index],
            expr.Constant(index, owner_type),
            candidate,
            owner_type,
        )
        candidate_valid = _bit_binary(
            expr.BinaryOperator.BIT_OR,
            valid[index],
            candidate_valid,
        )
    return candidate, candidate_valid


class _PacketStallTracker:
    """Own per-source stability state and its runtime checks."""

    def __init__(
        self,
        sources: tuple[ir_module.Port, ...],
        domain: str,
    ) -> None:
        self._sources = sources
        self._stalled = tuple(
            ir_module.Register(
                protocol_shared.packet_state_name("stalled", source.name),
                ir_types.BitType(),
                expr.Constant(0, ir_types.BitType()),
                domain,
            )
            for source in sources
        )
        self._payload = tuple(
            ir_module.Register(
                protocol_shared.packet_state_name("payload", source.name),
                source.type,
                expr.Constant(0, source.type),
                domain,
            )
            for source in sources
        )
        self._last = tuple(
            ir_module.Register(
                protocol_shared.packet_state_name("last", source.name),
                ir_types.BitType(),
                expr.Constant(0, ir_types.BitType()),
                domain,
            )
            for source in sources
        )

    @property
    def registers(self) -> tuple[ir_module.Register, ...]:
        return (*self._stalled, *self._payload, *self._last)

    def lower(
        self,
        payloads: tuple[expr.Expression, ...],
        valid: tuple[expr.Expression, ...],
        last: tuple[expr.Expression, ...],
        ready: tuple[expr.Expression, ...],
    ) -> tuple[
        tuple[ir_module.NextAssignment, ...],
        tuple[tuple[str, expr.Expression], ...],
    ]:
        next_assignments: list[ir_module.NextAssignment] = []
        conditions: list[tuple[str, expr.Expression]] = []
        for index, source in enumerate(self._sources):
            stalled = expr.RegisterRef(
                self._stalled[index].name,
                self._stalled[index].type,
            )
            previous_payload = expr.RegisterRef(
                self._payload[index].name,
                self._payload[index].type,
            )
            previous_last = expr.RegisterRef(
                self._last[index].name,
                self._last[index].type,
            )
            payload_stable = expr.Binary(
                expr.BinaryOperator.EQUAL,
                payloads[index],
                previous_payload,
                source.type,
                ir_types.BitType(),
            )
            last_stable = expr.Binary(
                expr.BinaryOperator.EQUAL,
                last[index],
                previous_last,
                ir_types.BitType(),
                ir_types.BitType(),
            )
            stable = _bit_binary(
                expr.BinaryOperator.BIT_AND,
                valid[index],
                _bit_binary(
                    expr.BinaryOperator.BIT_AND,
                    payload_stable,
                    last_stable,
                ),
            )
            conditions.append(
                (
                    f"packet source '{source.name}' changed while stalled",
                    _bit_binary(
                        expr.BinaryOperator.BIT_OR,
                        _bit_not(stalled),
                        stable,
                    ),
                )
            )
            next_assignments.extend(
                (
                    ir_module.NextAssignment(
                        self._stalled[index],
                        _bit_binary(
                            expr.BinaryOperator.BIT_AND,
                            valid[index],
                            _bit_not(ready[index]),
                        ),
                    ),
                    ir_module.NextAssignment(self._payload[index], payloads[index]),
                    ir_module.NextAssignment(self._last[index], last[index]),
                )
            )
        return tuple(next_assignments), tuple(conditions)


def lower_packet_arbiter_module(module: ir_module.Module) -> ir_module.Module:
    """Erase one typed packet arbiter into scalar fields and state."""

    packet_ports = tuple(
        port for port in module.ports if port.protocol is ir_interfaces.InterfaceProtocol.PACKET
    )
    if not packet_ports:
        return module
    if not module.is_sequential or len(module.arbiters) != 1:
        raise protocol_shared.ProtocolSimulationLoweringError(
            "packet simulation requires one clocked typed arbiter"
        )
    arbiter = module.arbiters[0]
    endpoint_names = {
        *(source.name for source in arbiter.sources),
        arbiter.destination.name,
    }
    if (
        {port.name for port in module.ports} != endpoint_names
        or any(port.protocol is not ir_interfaces.InterfaceProtocol.PACKET for port in module.ports)
        or module.assignments
        or module.connections
        or module.registers
        or module.rules
        or module.fifos
        or module.memories
        or module.roms
        or module.csr_blocks
        or module.request_responses
        or module.hierarchical_connections
        or module.aggregate_protocol_connections
        or module.request_response_connections
    ):
        raise protocol_shared.ProtocolSimulationLoweringError(
            "packet arbiter lowering requires only its typed packet endpoints"
        )
    sources = arbiter.sources
    if not sources:
        raise protocol_shared.ProtocolSimulationLoweringError("packet arbiter has no sources")
    destination = arbiter.destination
    domain = destination.domain or module.clock
    if domain is None:
        raise protocol_shared.ProtocolSimulationLoweringError(
            "packet arbiter destination has no owning clock domain"
        )
    owner_type = ir_types.UIntType(max(1, (len(sources) - 1).bit_length()))
    reset = next(
        (
            candidate.reset
            for candidate in module.clock_domains
            if candidate.clock == domain
        ),
        module.reset if module.clock == domain else None,
    )
    if reset is None:
        raise protocol_shared.ProtocolSimulationLoweringError("packet arbiter has no owning reset")
    reset_deasserted = _bit_not(expr.InputRef(f"$reset:{reset}", ir_types.BitType()))

    active_register = ir_module.Register(
        protocol_shared.packet_state_name("grant_active"),
        ir_types.BitType(),
        expr.Constant(0, ir_types.BitType()),
        domain,
    )
    owner_register = ir_module.Register(
        protocol_shared.packet_state_name("grant_owner"),
        owner_type,
        expr.Constant(0, owner_type),
        domain,
    )
    priority_register = (
        ir_module.Register(
            protocol_shared.packet_state_name("next_priority"),
            owner_type,
            expr.Constant(0, owner_type),
            domain,
        )
        if arbiter.policy is ArbitrationPolicy.ROUND_ROBIN
        else None
    )
    stall_tracker = _PacketStallTracker(sources, domain)

    values: dict[tuple[str, ir_interfaces.PacketSignal], expr.Expression] = {}
    source_payloads: list[expr.Expression] = []
    source_valid: list[expr.Expression] = []
    source_last: list[expr.Expression] = []
    for source in sources:
        payload = expr.InputRef(
            protocol_shared.packet_field_name(source.name, ir_interfaces.PacketSignal.PAYLOAD),
            source.type,
        )
        valid = expr.InputRef(
            protocol_shared.packet_field_name(source.name, ir_interfaces.PacketSignal.VALID),
            ir_types.BitType(),
        )
        last = expr.InputRef(
            protocol_shared.packet_field_name(source.name, ir_interfaces.PacketSignal.LAST),
            ir_types.BitType(),
        )
        source_payloads.append(payload)
        source_valid.append(valid)
        source_last.append(last)
        values[(source.name, ir_interfaces.PacketSignal.PAYLOAD)] = payload
        values[(source.name, ir_interfaces.PacketSignal.VALID)] = valid
        values[(source.name, ir_interfaces.PacketSignal.LAST)] = last
    destination_ready = expr.InputRef(
        protocol_shared.packet_field_name(destination.name, ir_interfaces.PacketSignal.READY),
        ir_types.BitType(),
    )
    values[(destination.name, ir_interfaces.PacketSignal.READY)] = destination_ready

    valid_tuple = tuple(source_valid)
    if arbiter.policy is ArbitrationPolicy.FIXED_PRIORITY:
        candidate, candidate_valid = _packet_priority_candidate(
            valid_tuple,
            tuple(range(len(sources))),
            owner_type,
        )
    else:
        assert priority_register is not None
        priority = expr.RegisterRef(priority_register.name, priority_register.type)
        candidates = tuple(
            _packet_priority_candidate(
                valid_tuple,
                tuple((start + offset) % len(sources) for offset in range(len(sources))),
                owner_type,
            )
            for start in range(len(sources))
        )
        candidate = _packet_select(
            priority,
            tuple(item[0] for item in candidates),
        )
        candidate_valid = _packet_select(
            priority,
            tuple(item[1] for item in candidates),
        )

    active = expr.RegisterRef(active_register.name, active_register.type)
    owner = expr.RegisterRef(owner_register.name, owner_register.type)
    selected = expr.Mux(active, owner, candidate, owner_type)
    grant_available = _bit_binary(
        expr.BinaryOperator.BIT_OR,
        active,
        candidate_valid,
    )
    grant_valid = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        reset_deasserted,
        grant_available,
    )
    selected_payload = _packet_select(selected, tuple(source_payloads))
    selected_valid = _packet_select(selected, valid_tuple)
    selected_last = _packet_select(selected, tuple(source_last))
    destination_valid = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        reset_deasserted,
        _bit_binary(
            expr.BinaryOperator.BIT_AND,
            grant_valid,
            selected_valid,
        ),
    )
    destination_payload = expr.Mux(
        grant_valid,
        selected_payload,
        expr.Constant(0, destination.type),
        destination.type,
    )
    destination_last = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        grant_valid,
        selected_last,
    )
    transfer = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        destination_valid,
        destination_ready,
    )
    grant_complete = (
        transfer
        if arbiter.grant_scope is GrantScope.BEAT
        else _bit_binary(
            expr.BinaryOperator.BIT_AND,
            transfer,
            selected_last,
        )
    )
    values[(destination.name, ir_interfaces.PacketSignal.PAYLOAD)] = destination_payload
    values[(destination.name, ir_interfaces.PacketSignal.VALID)] = destination_valid
    values[(destination.name, ir_interfaces.PacketSignal.LAST)] = destination_last
    source_ready: list[expr.Expression] = []
    for index, source in enumerate(sources):
        selected_here = expr.Binary(
            expr.BinaryOperator.EQUAL,
            selected,
            expr.Constant(index, owner_type),
            owner_type,
            ir_types.BitType(),
        )
        ready = _bit_binary(
            expr.BinaryOperator.BIT_AND,
            transfer,
            selected_here,
        )
        source_ready.append(ready)
        values[(source.name, ir_interfaces.PacketSignal.READY)] = ready

    scalar = protocol_shared.ScalarPortInventory(())
    for port in module.ports:
        scalar.add_protocol_endpoint(port, protocol_shared.packet_field_name)
    for field, type_ in (("grant", owner_type), ("grant_valid", ir_types.BitType())):
        name = protocol_shared.packet_field_name(destination.name, field)
        scalar.add(
            ir_module.PortDirection.OUTPUT,
            name, type_, destination.domain,
        )

    assignments = [
        ir_module.Assignment(
            scalar.by_name[protocol_shared.packet_field_name(endpoint, signal)],
            value,
        )
        for (endpoint, signal), value in values.items()
        if scalar.by_name[protocol_shared.packet_field_name(endpoint, signal)].direction
        is ir_module.PortDirection.OUTPUT
    ]
    assignments.extend((
        ir_module.Assignment(
            scalar.by_name[protocol_shared.packet_field_name(destination.name, "grant")],
            selected,
        ),
        ir_module.Assignment(
            scalar.by_name[protocol_shared.packet_field_name(destination.name, "grant_valid")],
            grant_valid,
        ),
    ))

    next_assignments: list[ir_module.NextAssignment] = []
    acquire = _bit_binary(
        expr.BinaryOperator.BIT_AND,
        _bit_not(active),
        candidate_valid,
    )
    next_active = expr.Mux(
        grant_complete,
        expr.Constant(0, ir_types.BitType()),
        expr.Mux(
            acquire,
            expr.Constant(1, ir_types.BitType()),
            active,
            ir_types.BitType(),
        ),
        ir_types.BitType(),
    )
    next_assignments.extend((
        ir_module.NextAssignment(active_register, next_active),
        ir_module.NextAssignment(
            owner_register,
            expr.Mux(acquire, selected, owner, owner_type),
        ),
    ))
    if priority_register is not None:
        priority = expr.RegisterRef(priority_register.name, priority_register.type)
        incremented = expr.Add(
            selected,
            expr.Constant(1, owner_type),
            owner_type,
        )
        wrapped = expr.Mux(
            expr.Binary(
                expr.BinaryOperator.EQUAL,
                selected,
                expr.Constant(len(sources) - 1, owner_type),
                owner_type,
                ir_types.BitType(),
            ),
            expr.Constant(0, owner_type),
            incremented,
            owner_type,
        )
        next_assignments.append(
            ir_module.NextAssignment(
                priority_register,
                expr.Mux(grant_complete, wrapped, priority, owner_type),
            )
        )

    stall_next, conditions = stall_tracker.lower(
        tuple(source_payloads),
        tuple(source_valid),
        tuple(source_last),
        tuple(source_ready),
    )
    next_assignments.extend(stall_next)

    lowerer = _PacketExpressionLowerer(values)
    runtime_scope = protocol_shared._runtime_protocol_scope(
        module=module,
        port=destination,
        conditions=tuple(conditions),
    )
    registers = (
        active_register,
        owner_register,
        *((priority_register,) if priority_register is not None else ()),
        *stall_tracker.registers,
    )
    return protocol_shared._finalize_protocol_module(
        module,
        lowerer,
        ports=tuple(scalar.ports),
        assignments=tuple(assignments),
        rewritten={
            "registers": registers,
            "next_assignments": tuple(next_assignments),
            "rules": (),
            "fifos": (),
            "memories": (),
            "roms": (),
            "instance_bindings": (),
            "resolved_transition": None,
            "verification_scopes": (
                *lowerer.value(module.verification_scopes),
                runtime_scope,
            ),
        },
        removed={"arbiters": (), "connections": ()},
    )
