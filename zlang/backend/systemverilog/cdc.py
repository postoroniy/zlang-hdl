"""Backend-local direct-SystemVerilog lowering for explicit CDC crossings."""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import groupby
from typing import Callable, Mapping

from zlang.async_fifo import build_async_fifo_physical_plan
from zlang.handshake_cdc import build_handshake_cdc_plan
from zlang.ir import expressions as expr
from zlang.ir.cdc import CrossingKind
from zlang.scalar_cdc import ScalarCdcPlan, build_scalar_cdc_plan
from zlang.ir.interfaces import InterfaceProtocol, ReadyValidSignal
from zlang.ir import module as ir_module
from zlang.ir import storage as ir_storage
from zlang.backend import identifiers
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError


@dataclass(frozen=True)
class CDCRendering:
    """Per-emission CDC capabilities that genuinely depend on caller state."""

    module: Callable[[ir_module.Module, list[str]], str]
    ported_memory: Callable[
        [ir_module.Module, ir_storage.Memory, Mapping[str, str]],
        tuple[list[str], list[str]],
    ]


def _clock_event(module: ir_module.Module, clock: str) -> str:
    return sv_sequential.clock_event(module, sv_rendering._identifier, clock)


def _reset_asserted(module: ir_module.Module, clock: str) -> str:
    return sv_sequential.reset_asserted(module, sv_rendering._identifier, clock)


def _cdc_used_names(module: ir_module.Module) -> set[str]:
    owner = sv_materialized.module_materialization_owner(module)
    used = set(owner.names.allocated_names)
    used.update(item.name for item in owner.materialization_plan)
    return used


def _allocate_cdc_names(
    preferred: tuple[str, ...], used: set[str],
) -> tuple[str, ...]:
    return tuple(
        identifiers.allocate_private_rtl_identifier(
            name, semantic_identity=f"cdc:{name}", used=used,
        )
        for name in preferred
    )


def _connection(module: ir_module.Module, kind: CrossingKind, *, composed: bool = False):
    crossings = tuple(
        connection
        for connection in module.connections
        if connection.crossing is not None
    )
    if len(crossings) != 1 or len(module.connections) != 1:
        raise SystemVerilogEmissionError(
            f"direct {kind.value} CDC requires exactly one explicit crossing"
        )
    if (
        module.fifos
        or module.memories
        or module.roms
        or module.csr_blocks
        or (module.elaborated_instances and not composed)
        or module.hierarchical_connections
    ):
        raise SystemVerilogEmissionError(
            f"direct {kind.value} CDC cannot yet be mixed with storage, "
            "hierarchy, or CSR"
        )
    connection = crossings[0]
    crossing = connection.crossing
    assert crossing is not None
    if crossing.kind is not kind:
        raise SystemVerilogEmissionError(
            f"direct CDC dispatcher expected {kind.value}, got {crossing.kind.value}"
        )
    source, destination = connection.source, connection.destination
    if source.domain is None or destination.domain is None:
        raise SystemVerilogEmissionError(
            f"direct {kind.value} CDC requires explicit endpoint domains"
        )
    domains = {domain.clock: domain for domain in module.clock_domains}
    try:
        source_domain = domains[source.domain]
        destination_domain = domains[destination.domain]
    except KeyError as error:
        raise SystemVerilogEmissionError(
            f"direct {kind.value} CDC references an unknown clock domain"
        ) from error
    if source.domain == destination.domain:
        raise SystemVerilogEmissionError(
            f"direct {kind.value} CDC requires distinct clock domains"
        )
    return connection, source_domain, destination_domain


# Preserve the established physical spellings when there is no collision.
_SCALAR_RTL_NAMES = {
    "stage1": "zlang_sync_stage1", "stage2": "zlang_sync_stage2",
    "source_toggle": "zlang_source_toggle", "sync1": "zlang_toggle_sync1",
    "sync2": "zlang_toggle_sync2", "previous": "zlang_previous_toggle",
}


def _scalar_binding(
    module: ir_module.Module, kind: CrossingKind, *, composed: bool = False,
) -> tuple[ScalarCdcPlan, dict[str, str]]:
    connection, src, dst = _connection(module, kind, composed=composed)
    try:
        plan = build_scalar_cdc_plan(connection, src, dst)
    except ValueError as error:
        raise SystemVerilogEmissionError(str(error)) from error
    roles = tuple(update.target.name for update in plan.updates)
    preferred = tuple(_SCALAR_RTL_NAMES[role] for role in roles)
    names = dict(zip(roles, _allocate_cdc_names(preferred, _cdc_used_names(module)), strict=True))
    return plan, names


def composed_crossing_lines(module: ir_module.Module) -> list[str]:
    if not any(connection.crossing is not None for connection in module.connections):
        return []
    return _scalar_lines(module, *_scalar_binding(module, CrossingKind.SYNC_LEVEL, composed=True))


def composed_crossing_names(module: ir_module.Module) -> tuple[str, ...]:
    if module.elaborated_instances and any(
        connection.crossing is not None and connection.crossing.kind is CrossingKind.SYNC_LEVEL
        for connection in module.connections
    ):
        _, names = _scalar_binding(module, CrossingKind.SYNC_LEVEL, composed=True)
        return tuple(names.values())
    return ()


def _scalar_lines(module: ir_module.Module, plan: ScalarCdcPlan, names: dict[str, str]) -> list[str]:
    resets = {
        f"$reset:{domain.reset}": _reset_asserted(module, domain.clock)
        for domain in module.clock_domains
    }

    def render(value: expr.Expression) -> str:
        if isinstance(value, expr.RegisterRef):
            return names[value.name]
        if isinstance(value, expr.InputRef):
            return resets[value.name] if value.name in resets else sv_rendering._identifier(value.name)
        if isinstance(value, expr.Constant):
            return f"1'b{value.value}"
        if isinstance(value, expr.Binary) and value.operator is expr.BinaryOperator.BIT_XOR:
            if isinstance(value.right, expr.Constant) and value.right.value == 1:
                return f"~{render(value.left)}"
            return f"({render(value.left)} ^ {render(value.right)})"
        if isinstance(value, expr.Mux):
            return f"{render(value.condition)} ? {render(value.when_true)} : {render(value.when_false)}"
        raise SystemVerilogEmissionError(f"unsupported scalar CDC expression {type(value).__name__}")

    lines = []
    for synchronized, group in groupby(plan.updates, key=lambda u: u.target in plan.synchronizers):
        attribute = '(* ASYNC_REG = "TRUE" *) ' if synchronized else ""
        signals = ", ".join(names[u.target.name] for u in group)
        lines.append(f"  {attribute}logic {signals};")
    destination = sv_rendering._identifier(plan.output.target.name)
    lines.append(f"  assign {destination} = {render(plan.output.expression)};")
    for clock, group in groupby(plan.updates, key=lambda u: u.target.domain):
        updates = tuple(group)
        reset = _reset_asserted(module, clock)
        lines.append(f"  always_ff @({_clock_event(module, clock)}) begin")
        update = updates[0]
        value = update.expression
        if (
            len(updates) == 1
            and isinstance(value, expr.Mux)
            and isinstance(value.when_false, expr.RegisterRef)
            and value.when_false.name == update.target.name
        ):
            target = names[update.target.name]
            lines.append(f"    if ({reset}) {target} <= {render(update.target.initial)};")
            lines.append(f"    else if ({render(value.condition)}) {target} <= {render(value.when_true)};")
        else:
            lines.append(f"    if ({reset}) begin")
            for update in updates:
                lines.append(f"      {names[update.target.name]} <= {render(update.target.initial)};")
            lines.append("    end else begin")
            for update in updates:
                lines.append(f"      {names[update.target.name]} <= {render(update.expression)};")
            lines.append("    end")
        lines.append("  end")
    return lines


_HANDSHAKE_RTL_NAMES = {
    "held": "zlang_held_payload", "request": "zlang_source_request",
    "acknowledge": "zlang_destination_acknowledge",
    "ack_sync1": "zlang_ack_sync1", "ack_sync2": "zlang_ack_sync2",
    "request_sync1": "zlang_request_sync1", "request_sync2": "zlang_request_sync2",
}


def _emit_handshake(module: ir_module.Module, rendering: CDCRendering) -> str:
    connection, source, destination = _connection(module, CrossingKind.HANDSHAKE)
    try:
        plan = build_handshake_cdc_plan(connection, source, destination)
    except ValueError as error:
        raise SystemVerilogEmissionError(str(error)) from error

    used = _cdc_used_names(module)
    names = dict(zip(
        _HANDSHAKE_RTL_NAMES, _allocate_cdc_names(tuple(_HANDSHAKE_RTL_NAMES.values()), used),
        strict=True,
    ))
    send, take = _allocate_cdc_names(("zlang_source_transfer", "zlang_destination_transfer"), used)
    transfer = {connection.source.name: send, connection.destination.name: take}
    resets = {f"$reset:{d.reset}": _reset_asserted(module, d.clock) for d in (source, destination)}

    def comparison(value: expr.Binary, operator: str) -> str:
        return f"({render(value.left)} {operator} {render(value.right)})"

    def render(value: expr.Expression) -> str:
        if isinstance(value, expr.RegisterRef):
            return names[value.name]
        if isinstance(value, expr.InputRef):
            return resets[value.name]
        if isinstance(value, expr.ReadyValidRef):
            if value.signal is ReadyValidSignal.TRANSFER:
                return transfer[value.interface]
            return f"{sv_rendering._identifier(value.interface)}_{value.signal.value}"
        if isinstance(value, expr.Binary):
            if value.operator is expr.BinaryOperator.BIT_AND:
                return f"{render(value.left)} && {render(value.right)}"
            if value.operator is expr.BinaryOperator.EQUAL:
                return comparison(value, "==")
            if (value.operator is expr.BinaryOperator.BIT_XOR
                    and isinstance(value.right, expr.Constant) and value.right.value == 1):
                if isinstance(value.left, expr.Binary) and value.left.operator is expr.BinaryOperator.EQUAL:
                    return comparison(value.left, "!=")
                prefix = "!" if isinstance(value.left, expr.InputRef) else "~"
                return prefix + render(value.left)
        raise SystemVerilogEmissionError(f"unsupported handshake expression: {value!r}")

    held = names["held"]
    lines = [
        f"  logic {sv_rendering._range(connection.source.type.width)}{held};",
        f"  logic {names['request']}, {names['acknowledge']};",
    ]
    for first, second in plan.synchronizers:
        lines.append(f'  (* ASYNC_REG = "TRUE" *) logic {names[first.name]}, {names[second.name]};')
    lines.append(f"  logic {send}, {take};")
    for port, wire in transfer.items():
        endpoint = sv_rendering._identifier(port)
        lines.append(f"  assign {wire} = {endpoint}_valid && {endpoint}_ready;")
    for assignment in sorted(plan.outputs, key=lambda a: a.signal is ReadyValidSignal.PAYLOAD):
        port = sv_rendering._identifier(assignment.target.name)
        lines.append(f"  assign {port}_{assignment.signal.value} = {render(assignment.expression)};")

    for domain in (source, destination):
        lines.append(f"  always_ff @({_clock_event(module, domain.clock)}) begin")
        lines.append(f"    if ({_reset_asserted(module, domain.clock)}) begin")
        for reg in plan.registers:
            if reg.domain == domain.clock:
                initial = reg.initial
                if not isinstance(initial, expr.Constant):
                    raise SystemVerilogEmissionError("handshake register requires a constant reset")
                literal = "'0" if reg.name == "held" and initial.value == 0 else f"{initial.type.width}'d{initial.value}"
                if initial.type.width == 1 and reg.name != "held":
                    literal = f"1'b{initial.value}"
                lines.append(f"      {names[reg.name]} <= {literal};")
        lines.append("    end else begin")
        updates = (update for update in plan.updates if update.target.domain == domain.clock)
        for guard, group in groupby(updates, key=lambda u: u.activation):
            statements = [f"{names[u.target.name]} <= {render(u.expression)};" for u in group]
            if guard is None:
                lines.extend("      " + line for line in statements)
            else:
                block = len(statements) > 1
                lines.append(f"      if ({render(guard)})" + (" begin" if block else ""))
                lines.extend("        " + line for line in statements)
                if block:
                    lines.append("      end")
        lines.extend(("    end", "  end"))
    return rendering.module(module, lines)


def _emit_async_fifo(module: ir_module.Module, rendering: CDCRendering) -> str:
    connection, source_domain, destination_domain = _connection(
        module, CrossingKind.ASYNC_FIFO
    )
    crossing = connection.crossing
    assert crossing is not None
    if crossing.kind is not CrossingKind.ASYNC_FIFO or crossing.depth is None:
        raise SystemVerilogEmissionError(
            "direct CDC lowering requires an async_fifo depth"
        )
    source, destination = connection.source, connection.destination
    if (
        source.protocol is not InterfaceProtocol.READY_VALID
        or destination.protocol is not InterfaceProtocol.READY_VALID
        or source.domain is None
        or destination.domain is None
    ):
        raise SystemVerilogEmissionError(
            "direct async_fifo CDC requires typed ready/valid endpoints and domains"
        )
    try:
        physical = build_async_fifo_physical_plan(module, connection)
    except ValueError as error:
        raise SystemVerilogEmissionError(str(error)) from error
    address_width = physical.address_width
    pointer_width = physical.pointer_width
    used = _cdc_used_names(module)
    state_names = tuple(item.name for item in physical.registers)
    value_names = (
        "zlang_write_binary_next", "zlang_write_gray_next",
        "zlang_read_binary_next", "zlang_read_gray_next",
        "zlang_fifo_write_address", "zlang_fifo_fetch_address",
        "zlang_fifo_input_payload", "zlang_push", "zlang_pop",
        "zlang_fifo_prefetch", "zlang_full_next",
        "zlang_unread_current", "zlang_unread_next",
    )
    names = state_names + value_names
    wires = dict(zip(names, _allocate_cdc_names(names, used), strict=True))
    storage = physical.memory.name
    # Both storage nets are emitted by the one-cycle typed memory renderer.
    while (
        identifiers.rtl_memory_cells_identifier(storage) in used
        or f"{storage}_rd_read_data" in used
    ):
        used.add(storage)
        storage = identifiers.allocate_private_rtl_identifier(
            physical.memory.name, semantic_identity="cdc:async-fifo-storage", used=used
        )
    memory_declarations, memory_logic = rendering.ported_memory(
        module, replace(physical.memory, name=storage), wires
    )
    state_declarations = [
        (
            '  (* ASYNC_REG = "TRUE" *) '
            if "sync" in item.name else "  "
        )
        + (
            f"logic [{item.width - 1}:0] {wires[item.name]};"
            if item.width > 1 else f"logic {wires[item.name]};"
        )
        for item in physical.registers
    ]
    payload_width = sv_rendering._width(source.type)
    identifier = sv_rendering._identifier
    src = identifier(source.name)
    dst = identifier(destination.name)
    src_clock_event = _clock_event(module, source_domain.clock)
    src_reset = _reset_asserted(module, source_domain.clock)
    dst_clock_event = _clock_event(module, destination_domain.clock)
    dst_reset = _reset_asserted(module, destination_domain.clock)
    controller_symbols = {
        **wires,
        "zlang_source_valid": f"{src}_valid",
        "zlang_destination_ready": f"{dst}_ready",
        "zlang_source_reset": src_reset,
        "zlang_destination_reset": dst_reset,
        "zlang_source_ready": f"{src}_ready",
        "zlang_destination_valid": f"{dst}_valid",
    }
    w = wires.__getitem__
    lines = [
        f"  // async_fifo_physical={physical.identity} storage=typed_async_mem",
        *memory_declarations,
        *state_declarations,
        f"  logic [{pointer_width - 1}:0] {w('zlang_write_binary_next')}, {w('zlang_write_gray_next')};",
        f"  logic [{pointer_width - 1}:0] {w('zlang_read_binary_next')}, {w('zlang_read_gray_next')};",
        f"  logic [{address_width - 1}:0] {w('zlang_fifo_write_address')};",
        f"  logic [{address_width - 1}:0] {w('zlang_fifo_fetch_address')};",
        f"  logic [{payload_width - 1}:0] {w('zlang_fifo_input_payload')};",
        f"  logic {w('zlang_push')}, {w('zlang_pop')};",
        f"  logic {w('zlang_fifo_prefetch')}, {w('zlang_full_next')};",
        f"  logic {w('zlang_unread_current')}, {w('zlang_unread_next')};",
        f"  assign {w('zlang_fifo_input_payload')} = {src}_payload;",
        f"  assign {w('zlang_fifo_write_address')} = "
        f"{w('zlang_write_binary')}[{address_width - 1}:0];",
        f"  assign {w('zlang_fifo_fetch_address')} = "
        f"{w('zlang_read_binary_next')}[{address_width - 1}:0];",
        *physical.controller.render_sv(controller_symbols),
        f"  assign {dst}_payload = {storage}_rd_read_data;",
        f"  always_ff @({src_clock_event}) begin",
        f"    if ({src_reset}) begin",
        f"      {w('zlang_write_binary')} <= '0;",
        f"      {w('zlang_write_gray')} <= '0;",
        f"      {w('zlang_read_gray_sync1')} <= '0;",
        f"      {w('zlang_read_gray_sync2')} <= '0;",
        f"      {w('zlang_full')} <= 1'b0;",
        "    end else begin",
        f"      {w('zlang_read_gray_sync1')} <= {w('zlang_read_gray')};",
        f"      {w('zlang_read_gray_sync2')} <= {w('zlang_read_gray_sync1')};",
        f"      {w('zlang_write_binary')} <= {w('zlang_write_binary_next')};",
        f"      {w('zlang_write_gray')} <= {w('zlang_write_gray_next')};",
        f"      {w('zlang_full')} <= {w('zlang_full_next')};",
        "    end",
        "  end",
        f"  always_ff @({dst_clock_event}) begin",
        f"    if ({dst_reset}) begin",
        f"      {w('zlang_read_binary')} <= '0;",
        f"      {w('zlang_read_gray')} <= '0;",
        f"      {w('zlang_write_gray_sync1')} <= '0;",
        f"      {w('zlang_write_gray_sync2')} <= '0;",
        f"      {w('zlang_output_valid')} <= 1'b0;",
        "    end else begin",
        f"      {w('zlang_write_gray_sync1')} <= {w('zlang_write_gray')};",
        f"      {w('zlang_write_gray_sync2')} <= {w('zlang_write_gray_sync1')};",
        f"      {w('zlang_read_binary')} <= {w('zlang_read_binary_next')};",
        f"      {w('zlang_read_gray')} <= {w('zlang_read_gray_next')};",
        f"      if ({w('zlang_fifo_prefetch')}) {w('zlang_output_valid')} <= 1'b1;",
        f"      else if ({w('zlang_pop')}) {w('zlang_output_valid')} <= 1'b0;",
        "    end",
        "  end",
        *memory_logic,
    ]
    return rendering.module(module, lines)


def emit_cdc_module(module: ir_module.Module, rendering: CDCRendering) -> str:
    crossing = next(
        connection.crossing
        for connection in module.connections
        if connection.crossing is not None
    )
    if crossing.kind in (CrossingKind.SYNC_LEVEL, CrossingKind.PULSE_TOGGLE):
        return rendering.module(module, _scalar_lines(module, *_scalar_binding(module, crossing.kind)))
    if crossing.kind is CrossingKind.HANDSHAKE:
        return _emit_handshake(module, rendering)
    return _emit_async_fifo(module, rendering)


__all__ = ["CDCRendering", "emit_cdc_module"]
