"""Erase explicit CDC families into ordinary compiler-owned simulation state.

The native simulation boundary deliberately has no CDC operation.  These
recipes are the same typed state machines used by the RTL implementation, but
are expressed as registers, next-state equations and (for ``async_fifo``) one
typed ``async_mem`` before construction of the primitive SimulationPlan.
"""

from __future__ import annotations

from dataclasses import replace

from zlang.async_fifo import FifoLogic, build_async_fifo_physical_plan
from zlang.common import stable_digest
from zlang.ir import expressions as expr
from zlang.ir.cdc import CrossingKind
from zlang.ir.interfaces import InterfaceProtocol, ReadyValidSignal
from zlang.ir.module import Assignment, Connection, Module, NextAssignment, Register
from zlang.ir.storage import MemoryPort, MemorySignal
from zlang.ir.types import BitType, BitsType, UIntType


class CdcSimulationLoweringError(ValueError):
    """An explicit crossing cannot be represented by the primitive simulator."""


def _xor(left: expr.Expression, right: expr.Expression) -> expr.Expression:
    return expr.Binary(
        expr.BinaryOperator.BIT_XOR,
        left,
        right,
        left.type,
        left.type,
    )


def _and(left: expr.Expression, right: expr.Expression) -> expr.Expression:
    return expr.Binary(
        expr.BinaryOperator.BIT_AND,
        left,
        right,
        left.type,
        left.type,
    )


def _equal(left: expr.Expression, right: expr.Expression) -> expr.Expression:
    return expr.Binary(
        expr.BinaryOperator.EQUAL,
        left,
        right,
        left.type,
        BitType(),
    )


def _not(value: expr.Expression) -> expr.Expression:
    return _xor(value, expr.Constant(1, BitType()))


def _register(name: str, type_, domain: str) -> Register:
    return Register(name, type_, expr.Constant(0, type_), domain)


def _prefix(module: Module, connection: Connection) -> str:
    crossing = connection.crossing
    assert crossing is not None
    identity = stable_digest(
        {
            "schema": "zlang-simulation-cdc-state-v1",
            "module": module.name,
            "source": connection.source.name,
            "destination": connection.destination.name,
            "kind": crossing.kind.value,
            "depth": crossing.depth,
        }
    )
    return f"$zlang_cdc_{identity[:16]}"


def _reset(module: Module, clock: str) -> expr.InputRef:
    domain = next((item for item in module.clock_domains if item.clock == clock), None)
    if domain is None:
        raise CdcSimulationLoweringError(
            f"CDC crossing references unavailable clock domain '{clock}'"
        )
    return expr.InputRef(f"$reset:{domain.reset}", BitType())


def _validate_connection(connection: Connection) -> None:
    if connection.crossing is None:
        raise CdcSimulationLoweringError("CDC lowering requires an explicit crossing")
    if connection.buffer_depth or connection.adapter is not None:
        raise CdcSimulationLoweringError(
            "CDC crossing cannot also carry buffering or a protocol adapter"
        )
    source = connection.source
    destination = connection.destination
    if (
        source.domain is None
        or destination.domain is None
        or source.domain == destination.domain
        or source.type != destination.type
    ):
        raise CdcSimulationLoweringError(
            "CDC crossing requires matching types in two distinct clock domains"
        )


def _lower_sync_level(
    _module: Module, connection: Connection, prefix: str
) -> tuple[list[Register], list[NextAssignment], list[Assignment], list[object]]:
    source, destination = connection.source, connection.destination
    if (
        source.protocol is not InterfaceProtocol.WIRE
        or destination.protocol is not InterfaceProtocol.WIRE
        or source.type != BitType()
    ):
        raise CdcSimulationLoweringError("sync_level requires matching bit wires")
    stage1 = _register(f"{prefix}:stage1", BitType(), destination.domain or "")
    stage2 = _register(f"{prefix}:stage2", BitType(), destination.domain or "")
    return (
        [stage1, stage2],
        [
            NextAssignment(stage1, expr.InputRef(source.name, source.type)),
            NextAssignment(stage2, expr.RegisterRef(stage1.name, stage1.type)),
        ],
        [Assignment(destination, expr.RegisterRef(stage2.name, stage2.type))],
        [],
    )


def _lower_pulse_toggle(
    _module: Module, connection: Connection, prefix: str
) -> tuple[list[Register], list[NextAssignment], list[Assignment], list[object]]:
    source, destination = connection.source, connection.destination
    if (
        source.protocol is not InterfaceProtocol.WIRE
        or destination.protocol is not InterfaceProtocol.WIRE
        or source.type != BitType()
    ):
        raise CdcSimulationLoweringError("pulse_toggle requires matching bit wires")
    source_toggle = _register(
        f"{prefix}:source_toggle", BitType(), source.domain or ""
    )
    sync1 = _register(f"{prefix}:sync1", BitType(), destination.domain or "")
    sync2 = _register(f"{prefix}:sync2", BitType(), destination.domain or "")
    previous = _register(f"{prefix}:previous", BitType(), destination.domain or "")
    source_ref = expr.InputRef(source.name, source.type)
    toggle_ref = expr.RegisterRef(source_toggle.name, source_toggle.type)
    sync1_ref = expr.RegisterRef(sync1.name, sync1.type)
    sync2_ref = expr.RegisterRef(sync2.name, sync2.type)
    previous_ref = expr.RegisterRef(previous.name, previous.type)
    return (
        [source_toggle, sync1, sync2, previous],
        [
            NextAssignment(
                source_toggle,
                expr.Mux(source_ref, _not(toggle_ref), toggle_ref, BitType()),
            ),
            NextAssignment(sync1, toggle_ref),
            NextAssignment(sync2, sync1_ref),
            NextAssignment(previous, sync2_ref),
        ],
        [Assignment(destination, _xor(sync2_ref, previous_ref))],
        [],
    )


def _lower_handshake(
    module: Module, connection: Connection, prefix: str
) -> tuple[list[Register], list[NextAssignment], list[Assignment], list[object]]:
    source, destination = connection.source, connection.destination
    if (
        source.protocol is not InterfaceProtocol.READY_VALID
        or destination.protocol is not InterfaceProtocol.READY_VALID
    ):
        raise CdcSimulationLoweringError(
            "handshake requires matching ready/valid endpoints"
        )
    source_domain = source.domain or ""
    destination_domain = destination.domain or ""
    held = _register(f"{prefix}:held", source.type, source_domain)
    request = _register(f"{prefix}:request", BitType(), source_domain)
    ack_sync1 = _register(f"{prefix}:ack_sync1", BitType(), source_domain)
    ack_sync2 = _register(f"{prefix}:ack_sync2", BitType(), source_domain)
    request_sync1 = _register(
        f"{prefix}:request_sync1", BitType(), destination_domain
    )
    request_sync2 = _register(
        f"{prefix}:request_sync2", BitType(), destination_domain
    )
    acknowledge = _register(f"{prefix}:acknowledge", BitType(), destination_domain)
    held_ref = expr.RegisterRef(held.name, held.type)
    request_ref = expr.RegisterRef(request.name, request.type)
    ack1_ref = expr.RegisterRef(ack_sync1.name, ack_sync1.type)
    ack2_ref = expr.RegisterRef(ack_sync2.name, ack_sync2.type)
    request1_ref = expr.RegisterRef(request_sync1.name, request_sync1.type)
    request2_ref = expr.RegisterRef(request_sync2.name, request_sync2.type)
    acknowledge_ref = expr.RegisterRef(acknowledge.name, acknowledge.type)
    source_transfer = expr.ReadyValidRef(
        source.name, ReadyValidSignal.TRANSFER, BitType()
    )
    destination_transfer = expr.ReadyValidRef(
        destination.name, ReadyValidSignal.TRANSFER, BitType()
    )
    source_ready = _and(
        _not(_reset(module, source_domain)), _equal(request_ref, ack2_ref)
    )
    destination_valid = _and(
        _not(_reset(module, destination_domain)), _not(_equal(request2_ref, acknowledge_ref))
    )
    return (
        [
            held,
            request,
            ack_sync1,
            ack_sync2,
            request_sync1,
            request_sync2,
            acknowledge,
        ],
        [
            NextAssignment(ack_sync1, acknowledge_ref),
            NextAssignment(ack_sync2, ack1_ref),
            NextAssignment(
                held,
                expr.ReadyValidRef(source.name, ReadyValidSignal.PAYLOAD, source.type),
                source_transfer,
            ),
            NextAssignment(request, _not(request_ref), source_transfer),
            NextAssignment(request_sync1, request_ref),
            NextAssignment(request_sync2, request1_ref),
            NextAssignment(acknowledge, request2_ref, destination_transfer),
        ],
        [
            Assignment(source, source_ready, ReadyValidSignal.READY),
            Assignment(destination, held_ref, ReadyValidSignal.PAYLOAD),
            Assignment(destination, destination_valid, ReadyValidSignal.VALID),
        ],
        [],
    )


def _fifo_logic_expression(
    value: FifoLogic,
    values: dict[str, expr.Expression],
) -> expr.Expression:
    if value.op == "input":
        try:
            return values[value.name]
        except KeyError as error:
            raise CdcSimulationLoweringError(
                f"async FIFO equation references unavailable value '{value.name}'"
            ) from error
    type_ = BitType() if value.width == 1 else BitsType(value.width)
    if value.op == "constant":
        return expr.Constant(value.value, type_)
    operands = tuple(_fifo_logic_expression(item, values) for item in value.args)
    if value.op == "not":
        return _not(operands[0])
    if value.op in {"and", "or", "xor", "eq", "ne"}:
        operator = {
            "and": expr.BinaryOperator.BIT_AND,
            "or": expr.BinaryOperator.BIT_OR,
            "xor": expr.BinaryOperator.BIT_XOR,
            "eq": expr.BinaryOperator.EQUAL,
            "ne": expr.BinaryOperator.NOT_EQUAL,
        }[value.op]
        result_type = BitType() if value.op in {"eq", "ne"} else type_
        return expr.Binary(operator, operands[0], operands[1], operands[0].type, result_type)
    if value.op == "add":
        return expr.Add(operands[0], operands[1], type_)
    if value.op == "gray":
        shifted = expr.Binary(
            expr.BinaryOperator.SHIFT_RIGHT,
            operands[0],
            expr.Constant(1, UIntType(max(1, value.width.bit_length()))),
            operands[0].type,
            operands[0].type,
        )
        return _xor(shifted, operands[0])
    raise CdcSimulationLoweringError(
        f"unsupported async FIFO controller operation '{value.op}'"
    )


def _lower_async_fifo(
    module: Module, connection: Connection, prefix: str
) -> tuple[list[Register], list[NextAssignment], list[Assignment], list[object]]:
    source, destination = connection.source, connection.destination
    if (
        source.protocol is not InterfaceProtocol.READY_VALID
        or destination.protocol is not InterfaceProtocol.READY_VALID
    ):
        raise CdcSimulationLoweringError(
            "async_fifo requires matching ready/valid endpoints"
        )
    try:
        physical = build_async_fifo_physical_plan(module, connection)
    except ValueError as error:
        raise CdcSimulationLoweringError(str(error)) from error
    registers = [
        _register(f"{prefix}:{item.name}", BitsType(item.width), item.domain)
        for item in physical.registers
    ]
    state = {
        item.role: expr.RegisterRef(register.name, register.type)
        for item, register in zip(physical.registers, registers, strict=True)
    }
    source_reset = _reset(module, physical.source_domain)
    destination_reset = _reset(module, physical.destination_domain)
    values: dict[str, expr.Expression] = {
        **state,
        "zlang_source_valid": expr.ReadyValidRef(
            source.name, ReadyValidSignal.VALID, BitType()
        ),
        "zlang_destination_ready": expr.ReadyValidRef(
            destination.name, ReadyValidSignal.READY, BitType()
        ),
        "zlang_source_reset": source_reset,
        "zlang_destination_reset": destination_reset,
    }
    for name, equation in physical.controller.equations:
        values[name] = _fifo_logic_expression(equation, values)

    write_address = expr.Slice(
        state["zlang_write_binary"],
        physical.address_width - 1,
        0,
        UIntType(physical.address_width),
    )
    read_address = expr.Slice(
        values["zlang_read_binary_next"],
        physical.address_width - 1,
        0,
        UIntType(physical.address_width),
    )
    memory_name = f"{prefix}:storage"
    writer, reader = physical.memory.ports
    memory = replace(
        physical.memory,
        name=memory_name,
        # ``Memory.domain`` remains the cell-write owner even though the
        # independently clocked read port owns its registered output state.
        domain=physical.source_domain,
        ports=(
            MemoryPort(
                "wr",
                writer.semantic_id,
                writer.kind,
                writer.domain,
                write_address,
                write_enable=values["zlang_push"],
                write_data=expr.ReadyValidRef(
                    source.name, ReadyValidSignal.PAYLOAD, source.type
                ),
            ),
            MemoryPort(
                "rd",
                reader.semantic_id,
                reader.kind,
                reader.domain,
                read_address,
                read_enable=values["zlang_fifo_prefetch"],
            ),
        ),
    )
    next_by_role = {
        "zlang_write_binary": values["zlang_write_binary_next"],
        "zlang_write_gray": values["zlang_write_gray_next"],
        "zlang_read_gray_sync1": state["zlang_read_gray"],
        "zlang_read_gray_sync2": state["zlang_read_gray_sync1"],
        "zlang_full": values["zlang_full_next"],
        "zlang_read_binary": values["zlang_read_binary_next"],
        "zlang_read_gray": values["zlang_read_gray_next"],
        "zlang_write_gray_sync1": state["zlang_write_gray"],
        "zlang_write_gray_sync2": state["zlang_write_gray_sync1"],
        "zlang_output_valid": expr.Mux(
            values["zlang_fifo_prefetch"],
            expr.Constant(1, BitType()),
            expr.Mux(
                values["zlang_pop"],
                expr.Constant(0, BitType()),
                state["zlang_output_valid"],
                BitType(),
            ),
            BitType(),
        ),
    }
    next_assignments = [
        NextAssignment(register, next_by_role[item.role])
        for item, register in zip(physical.registers, registers, strict=True)
    ]
    assignments = [
        Assignment(source, values["zlang_source_ready"], ReadyValidSignal.READY),
        Assignment(
            destination,
            expr.MemoryRef(
                memory_name, MemorySignal.READ_DATA, source.type, port="rd"
            ),
            ReadyValidSignal.PAYLOAD,
        ),
        Assignment(
            destination,
            values["zlang_destination_valid"],
            ReadyValidSignal.VALID,
        ),
    ]
    return registers, next_assignments, assignments, [memory]


def lower_cdc_module(module: Module) -> Module:
    """Lower all explicit leaf crossings without exposing CDC to the runtime."""

    lowered_children = tuple(lower_cdc_module(child) for child in module.children)
    if lowered_children != module.children:
        module = replace(module, children=lowered_children)
    crossings = tuple(item for item in module.connections if item.crossing is not None)
    if not crossings:
        return module
    ordinary = tuple(item for item in module.connections if item.crossing is None)
    registers = list(module.registers)
    next_assignments = list(module.next_assignments)
    assignments = list(module.assignments)
    memories = list(module.memories)
    for connection in crossings:
        _validate_connection(connection)
        prefix = _prefix(module, connection)
        crossing = connection.crossing
        assert crossing is not None
        lowerer = {
            CrossingKind.SYNC_LEVEL: _lower_sync_level,
            CrossingKind.PULSE_TOGGLE: _lower_pulse_toggle,
            CrossingKind.HANDSHAKE: _lower_handshake,
            CrossingKind.ASYNC_FIFO: _lower_async_fifo,
        }[crossing.kind]
        new_registers, new_next, new_assignments, new_memories = lowerer(
            module, connection, prefix
        )
        registers.extend(new_registers)
        next_assignments.extend(new_next)
        assignments.extend(new_assignments)
        memories.extend(new_memories)
    return replace(
        module,
        registers=tuple(registers),
        next_assignments=tuple(next_assignments),
        assignments=tuple(assignments),
        memories=tuple(memories),
        connections=ordinary,
    )


__all__ = ["CdcSimulationLoweringError", "lower_cdc_module"]
