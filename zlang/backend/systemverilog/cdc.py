"""Backend-local direct-SystemVerilog lowering for explicit CDC crossings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from zlang.ir.cdc import CrossingKind
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module
from zlang.ir.types import BitType, HardwareType


@dataclass(frozen=True)
class CDCRendering:
    """The small renderer surface needed by the isolated CDC subsystem."""

    error: Callable[[str], Exception]
    identifier: Callable[[str], str]
    logic_port: Callable[[str, str, HardwareType], str]
    packed_width: Callable[[HardwareType], int]
    packed_range: Callable[[int], str]
    module: Callable[[Module, list[str], list[str]], str]


def _connection(module: Module, kind: CrossingKind, rendering: CDCRendering):
    crossings = tuple(
        connection
        for connection in module.connections
        if connection.crossing is not None
    )
    if len(crossings) != 1 or len(module.connections) != 1:
        raise rendering.error(
            f"direct {kind.value} CDC requires exactly one explicit crossing"
        )
    if (
        module.assignments
        or module.registers
        or module.next_assignments
        or module.rules
        or module.fifos
        or module.memories
        or module.csr_blocks
        or module.elaborated_instances
        or module.hierarchical_connections
    ):
        raise rendering.error(
            f"direct {kind.value} CDC cannot be mixed with state, hierarchy, or CSR"
        )
    connection = crossings[0]
    crossing = connection.crossing
    assert crossing is not None
    if crossing.kind is not kind:
        raise rendering.error(
            f"direct CDC dispatcher expected {kind.value}, got {crossing.kind.value}"
        )
    source, destination = connection.source, connection.destination
    if source.domain is None or destination.domain is None:
        raise rendering.error(
            f"direct {kind.value} CDC requires explicit endpoint domains"
        )
    domains = {domain.clock: domain for domain in module.clock_domains}
    try:
        source_domain = domains[source.domain]
        destination_domain = domains[destination.domain]
    except KeyError as error:
        raise rendering.error(
            f"direct {kind.value} CDC references an unknown clock domain"
        ) from error
    if source.domain == destination.domain:
        raise rendering.error(
            f"direct {kind.value} CDC requires distinct clock domains"
        )
    return connection, source_domain, destination_domain


def _ports(module: Module, rendering: CDCRendering) -> list[str]:
    identifier = rendering.identifier
    return [
        *(f"input wire logic {identifier(domain.clock)}" for domain in module.clock_domains),
        *(f"input wire logic {identifier(domain.reset)}" for domain in module.clock_domains),
    ]


def _emit_sync_level(module: Module, rendering: CDCRendering) -> str:
    connection, _, destination_domain = _connection(
        module, CrossingKind.SYNC_LEVEL, rendering
    )
    source, destination = connection.source, connection.destination
    if (
        source.protocol is not InterfaceProtocol.WIRE
        or destination.protocol is not InterfaceProtocol.WIRE
        or not isinstance(source.type, BitType)
        or not isinstance(destination.type, BitType)
    ):
        raise rendering.error("direct sync_level CDC requires bit wire endpoints")
    identifier = rendering.identifier
    src = identifier(source.name)
    dst = identifier(destination.name)
    clock = identifier(destination_domain.clock)
    reset = identifier(destination_domain.reset)
    ports = [
        *_ports(module, rendering),
        rendering.logic_port("input", src, source.type),
        rendering.logic_port("output", dst, destination.type),
    ]
    lines = [
        '  (* ASYNC_REG = "TRUE" *) logic zlang_sync_stage1, zlang_sync_stage2;',
        f"  assign {dst} = {reset} ? 1'b0 : zlang_sync_stage2;",
        f"  always_ff @(posedge {clock}) begin",
        f"    if ({reset}) begin",
        "      zlang_sync_stage1 <= 1'b0;",
        "      zlang_sync_stage2 <= 1'b0;",
        "    end else begin",
        f"      zlang_sync_stage1 <= {src};",
        "      zlang_sync_stage2 <= zlang_sync_stage1;",
        "    end",
        "  end",
    ]
    return rendering.module(module, ports, lines)


def _emit_pulse_toggle(module: Module, rendering: CDCRendering) -> str:
    connection, source_domain, destination_domain = _connection(
        module, CrossingKind.PULSE_TOGGLE, rendering
    )
    source, destination = connection.source, connection.destination
    if (
        source.protocol is not InterfaceProtocol.WIRE
        or destination.protocol is not InterfaceProtocol.WIRE
        or not isinstance(source.type, BitType)
        or not isinstance(destination.type, BitType)
    ):
        raise rendering.error("direct pulse_toggle CDC requires bit wire endpoints")
    identifier = rendering.identifier
    src = identifier(source.name)
    dst = identifier(destination.name)
    src_clock = identifier(source_domain.clock)
    src_reset = identifier(source_domain.reset)
    dst_clock = identifier(destination_domain.clock)
    dst_reset = identifier(destination_domain.reset)
    ports = [
        *_ports(module, rendering),
        rendering.logic_port("input", src, source.type),
        rendering.logic_port("output", dst, destination.type),
    ]
    lines = [
        "  logic zlang_source_toggle;",
        '  (* ASYNC_REG = "TRUE" *) logic zlang_toggle_sync1, zlang_toggle_sync2;',
        "  logic zlang_previous_toggle;",
        f"  assign {dst} = {dst_reset} ? 1'b0 : "
        "(zlang_toggle_sync2 ^ zlang_previous_toggle);",
        f"  always_ff @(posedge {src_clock}) begin",
        f"    if ({src_reset}) zlang_source_toggle <= 1'b0;",
        f"    else if ({src}) zlang_source_toggle <= ~zlang_source_toggle;",
        "  end",
        f"  always_ff @(posedge {dst_clock}) begin",
        f"    if ({dst_reset}) begin",
        "      zlang_toggle_sync1 <= 1'b0;",
        "      zlang_toggle_sync2 <= 1'b0;",
        "      zlang_previous_toggle <= 1'b0;",
        "    end else begin",
        "      zlang_toggle_sync1 <= zlang_source_toggle;",
        "      zlang_toggle_sync2 <= zlang_toggle_sync1;",
        "      zlang_previous_toggle <= zlang_toggle_sync2;",
        "    end",
        "  end",
    ]
    return rendering.module(module, ports, lines)


def _emit_handshake(module: Module, rendering: CDCRendering) -> str:
    connection, source_domain, destination_domain = _connection(
        module, CrossingKind.HANDSHAKE, rendering
    )
    source, destination = connection.source, connection.destination
    if (
        source.protocol is not InterfaceProtocol.READY_VALID
        or destination.protocol is not InterfaceProtocol.READY_VALID
        or source.type != destination.type
    ):
        raise rendering.error(
            "direct handshake CDC requires matching typed ready/valid endpoints"
        )
    identifier = rendering.identifier
    src = identifier(source.name)
    dst = identifier(destination.name)
    src_clock = identifier(source_domain.clock)
    src_reset = identifier(source_domain.reset)
    dst_clock = identifier(destination_domain.clock)
    dst_reset = identifier(destination_domain.reset)
    width = rendering.packed_width(source.type)
    ports = [
        *_ports(module, rendering),
        rendering.logic_port("input", f"{src}_payload", source.type),
        f"input wire logic {src}_valid",
        f"output logic {src}_ready",
        rendering.logic_port("output", f"{dst}_payload", destination.type),
        f"output logic {dst}_valid",
        f"input wire logic {dst}_ready",
    ]
    lines = [
        f"  logic {rendering.packed_range(width)}zlang_held_payload;",
        "  logic zlang_source_request, zlang_destination_acknowledge;",
        '  (* ASYNC_REG = "TRUE" *) logic zlang_ack_sync1, zlang_ack_sync2;',
        '  (* ASYNC_REG = "TRUE" *) logic zlang_request_sync1, zlang_request_sync2;',
        "  logic zlang_source_transfer, zlang_destination_transfer;",
        f"  assign zlang_source_transfer = {src}_valid && {src}_ready;",
        f"  assign zlang_destination_transfer = {dst}_valid && {dst}_ready;",
        f"  assign {src}_ready = !{src_reset} && "
        "(zlang_source_request == zlang_ack_sync2);",
        f"  assign {dst}_valid = !{dst_reset} && "
        "(zlang_request_sync2 != zlang_destination_acknowledge);",
        f"  assign {dst}_payload = zlang_held_payload;",
        f"  always_ff @(posedge {src_clock}) begin",
        f"    if ({src_reset}) begin",
        "      zlang_held_payload <= '0;",
        "      zlang_source_request <= 1'b0;",
        "      zlang_ack_sync1 <= 1'b0;",
        "      zlang_ack_sync2 <= 1'b0;",
        "    end else begin",
        "      zlang_ack_sync1 <= zlang_destination_acknowledge;",
        "      zlang_ack_sync2 <= zlang_ack_sync1;",
        "      if (zlang_source_transfer) begin",
        f"        zlang_held_payload <= {src}_payload;",
        "        zlang_source_request <= ~zlang_source_request;",
        "      end",
        "    end",
        "  end",
        f"  always_ff @(posedge {dst_clock}) begin",
        f"    if ({dst_reset}) begin",
        "      zlang_request_sync1 <= 1'b0;",
        "      zlang_request_sync2 <= 1'b0;",
        "      zlang_destination_acknowledge <= 1'b0;",
        "    end else begin",
        "      zlang_request_sync1 <= zlang_source_request;",
        "      zlang_request_sync2 <= zlang_request_sync1;",
        "      if (zlang_destination_transfer)",
        "        zlang_destination_acknowledge <= zlang_request_sync2;",
        "    end",
        "  end",
    ]
    return rendering.module(module, ports, lines)


def _emit_async_fifo(module: Module, rendering: CDCRendering) -> str:
    connection, source_domain, destination_domain = _connection(
        module, CrossingKind.ASYNC_FIFO, rendering
    )
    crossing = connection.crossing
    assert crossing is not None
    if crossing.kind is not CrossingKind.ASYNC_FIFO or crossing.depth is None:
        raise rendering.error("direct CDC lowering requires an async_fifo depth")
    source, destination = connection.source, connection.destination
    if (
        source.protocol is not InterfaceProtocol.READY_VALID
        or destination.protocol is not InterfaceProtocol.READY_VALID
        or source.domain is None
        or destination.domain is None
    ):
        raise rendering.error(
            "direct async_fifo CDC requires typed ready/valid endpoints and domains"
        )
    depth = crossing.depth
    address_width = (depth - 1).bit_length()
    pointer_width = address_width + 1
    payload_width = rendering.packed_width(source.type)
    identifier = rendering.identifier
    src = identifier(source.name)
    dst = identifier(destination.name)
    src_clock = identifier(source_domain.clock)
    src_reset = identifier(source_domain.reset)
    dst_clock = identifier(destination_domain.clock)
    dst_reset = identifier(destination_domain.reset)
    inverted_read_pointer = (
        f"{{~zlang_read_gray_sync2[{pointer_width - 1}:"
        f"{pointer_width - 2}], "
        f"zlang_read_gray_sync2[{pointer_width - 3}:0]}}"
    )
    ports = [
        *_ports(module, rendering),
        rendering.logic_port("input", f"{src}_payload", source.type),
        f"input wire logic {src}_valid",
        f"output logic {src}_ready",
        rendering.logic_port("output", f"{dst}_payload", destination.type),
        f"output logic {dst}_valid",
        f"input wire logic {dst}_ready",
    ]
    lines = [
        f"  logic [{payload_width - 1}:0] zlang_storage [0:{depth - 1}];",
        f"  logic [{pointer_width - 1}:0] zlang_write_binary, zlang_write_gray;",
        f"  logic [{pointer_width - 1}:0] zlang_read_binary, zlang_read_gray;",
        f"  logic [{pointer_width - 1}:0] zlang_write_binary_next, zlang_write_gray_next;",
        f"  logic [{pointer_width - 1}:0] zlang_read_binary_next, zlang_read_gray_next;",
        f"  (* ASYNC_REG = \"TRUE\" *) logic [{pointer_width - 1}:0] "
        "zlang_read_gray_sync1, zlang_read_gray_sync2;",
        f"  (* ASYNC_REG = \"TRUE\" *) logic [{pointer_width - 1}:0] "
        "zlang_write_gray_sync1, zlang_write_gray_sync2;",
        "  logic zlang_full, zlang_empty;",
        "  logic zlang_push, zlang_pop;",
        f"  assign zlang_push = {src}_valid && {src}_ready;",
        f"  assign zlang_pop = {dst}_valid && {dst}_ready;",
        f"  assign zlang_write_binary_next = zlang_write_binary + "
        f"{pointer_width}'(zlang_push);",
        "  assign zlang_write_gray_next = "
        "(zlang_write_binary_next >> 1) ^ zlang_write_binary_next;",
        f"  assign zlang_read_binary_next = zlang_read_binary + "
        f"{pointer_width}'(zlang_pop);",
        "  assign zlang_read_gray_next = "
        "(zlang_read_binary_next >> 1) ^ zlang_read_binary_next;",
        f"  assign {src}_ready = !{src_reset} && !zlang_full;",
        f"  assign {dst}_valid = !{dst_reset} && !zlang_empty;",
        f"  assign {dst}_payload = "
        f"zlang_storage[zlang_read_binary[{address_width - 1}:0]];",
        f"  always_ff @(posedge {src_clock}) begin",
        f"    if ({src_reset}) begin",
        "      zlang_write_binary <= '0;",
        "      zlang_write_gray <= '0;",
        "      zlang_read_gray_sync1 <= '0;",
        "      zlang_read_gray_sync2 <= '0;",
        "      zlang_full <= 1'b0;",
        "    end else begin",
        "      zlang_read_gray_sync1 <= zlang_read_gray;",
        "      zlang_read_gray_sync2 <= zlang_read_gray_sync1;",
        "      zlang_write_binary <= zlang_write_binary_next;",
        "      zlang_write_gray <= zlang_write_gray_next;",
        f"      zlang_full <= (zlang_write_gray_next == {inverted_read_pointer});",
        f"      if (zlang_push) zlang_storage[zlang_write_binary[{address_width - 1}:0]] "
        f"<= {src}_payload;",
        "    end",
        "  end",
        f"  always_ff @(posedge {dst_clock}) begin",
        f"    if ({dst_reset}) begin",
        "      zlang_read_binary <= '0;",
        "      zlang_read_gray <= '0;",
        "      zlang_write_gray_sync1 <= '0;",
        "      zlang_write_gray_sync2 <= '0;",
        "      zlang_empty <= 1'b1;",
        "    end else begin",
        "      zlang_write_gray_sync1 <= zlang_write_gray;",
        "      zlang_write_gray_sync2 <= zlang_write_gray_sync1;",
        "      zlang_read_binary <= zlang_read_binary_next;",
        "      zlang_read_gray <= zlang_read_gray_next;",
        "      zlang_empty <= (zlang_read_gray_next == zlang_write_gray_sync2);",
        "    end",
        "  end",
    ]
    return rendering.module(module, ports, lines)


def emit_cdc_module(module: Module, rendering: CDCRendering) -> str:
    crossing = next(
        connection.crossing
        for connection in module.connections
        if connection.crossing is not None
    )
    if crossing.kind is CrossingKind.SYNC_LEVEL:
        return _emit_sync_level(module, rendering)
    if crossing.kind is CrossingKind.PULSE_TOGGLE:
        return _emit_pulse_toggle(module, rendering)
    if crossing.kind is CrossingKind.HANDSHAKE:
        return _emit_handshake(module, rendering)
    return _emit_async_fifo(module, rendering)


__all__ = ["CDCRendering", "emit_cdc_module"]
