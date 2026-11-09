# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Typed global RAM fragments and exact-width byte-mask rendering."""

from __future__ import annotations

from zlang.ir import module as ir_module
from zlang.ir import storage as ir_storage
from zlang.backend import identifiers
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError


def byte_mask_concatenation(
    signal: str,
    *,
    element_width: int,
    lane_count: int,
) -> str:
    """Render an exact-width byte mask, including one partial MSB lane."""

    expected_lanes = (element_width + 7) // 8
    if lane_count != expected_lanes:
        raise SystemVerilogEmissionError(
            "memory write-mask width does not match its element width"
        )
    high_lane_width = element_width - (lane_count - 1) * 8
    return ", ".join(
        f"{{{high_lane_width if lane == lane_count - 1 else 8}"
        f"{{{signal}[{lane}]}}}}"
        for lane in reversed(range(lane_count))
    )


def global_memory_fragments(
    module: ir_module.Module,
    memory: ir_storage.Memory,
    render,
    *,
    ram_style: str | None = None,
) -> list[str]:
    """Emit one globally driven RAM, including its own reset/read pipeline."""
    if memory.domain is None:
        raise SystemVerilogEmissionError(
            f"memory '{memory.name}' has no resolved clock domain"
        )
    memory_clock = memory.domain
    if not 0 <= memory.read_latency <= 16:
        raise SystemVerilogEmissionError(
            "direct SystemVerilog memory emission requires read_latency in 0..16"
        )
    width = sv_rendering._width(memory.element_type)
    name = sv_rendering._identifier(memory.name)
    cells_name = identifiers.rtl_memory_cells_identifier(memory.name)
    read_data_name = identifiers.rtl_memory_read_data_identifier(memory.name)
    initial_word = (
        render(memory.initial_value)
        if memory.initial_value is not None else "'0"
    )
    style = f'(* ram_style = "{ram_style}" *) ' if ram_style else ""
    declarations = [
        f"  {style}logic [{width - 1}:0] {cells_name} [0:{memory.depth - 1}];",
        f"  logic [{width - 1}:0] {read_data_name};",
        *(
            f"  logic [{width - 1}:0] {name}_read_stage_{index};"
            for index in range(memory.read_latency - 1)
        ),
    ]
    read_capture = (
        f"{name}_read_stage_0"
        if memory.read_latency > 1 else read_data_name
    )
    combinational: list[str] = []
    if memory.write_mask is not None:
        lanes = memory.write_mask_width
        assert lanes is not None
        expanded = byte_mask_concatenation(
            f"{name}_write_mask",
            element_width=width,
            lane_count=lanes,
        )
        declarations.extend((
            f"  logic [{lanes - 1}:0] {name}_write_mask;",
            f"  logic [{width - 1}:0] {name}_write_mask_expanded;",
            f"  logic [{width - 1}:0] {name}_write_merged;",
        ))
        combinational.extend((
            f"  assign {name}_write_mask = {render(memory.write_mask)};",
            f"  assign {name}_write_mask_expanded = {{{expanded}}};",
            f"  assign {name}_write_merged = "
            f"({name}_cells[{render(memory.write_address)}] & ~{name}_write_mask_expanded) | "
            f"({render(memory.write_data)} & {name}_write_mask_expanded);",
        ))
    if memory.read_latency == 0:
        read_value = f"{name}_cells[{render(memory.read_address)}]"
        if memory.collision is ir_storage.MemoryCollision.WRITE_FIRST:
            write_value = (
                name + "_write_merged"
                if memory.write_mask is not None
                else render(memory.write_data)
            )
            read_value = (
                f"({sv_sequential.reset_deasserted(module, sv_rendering._identifier, memory_clock)} && "
                f"{render(memory.write_enable)} && "
                f"({render(memory.read_address)} == "
                f"{render(memory.write_address)})) ? "
                f"{write_value} : ({read_value})"
            )
        if memory.read_data_reset is ir_storage.MemoryResetPolicy.CLEAR:
            read_value = (
                f"{sv_sequential.reset_asserted(module, sv_rendering._identifier, memory_clock)} ? '0 : ({read_value})"
            )
        combinational.append(f"  assign {name}_read_data = {read_value};")

    initialization: list[str] = []
    if (
        memory.initial_value is not None
        or memory.contents_reset is ir_storage.MemoryResetPolicy.PRESERVE
        or (
            memory.read_latency >= 1
            and memory.read_data_reset is ir_storage.MemoryResetPolicy.PRESERVE
        )
    ):
        initialization.append("  initial begin")
        if (
            memory.read_latency >= 1
            and memory.read_data_reset is ir_storage.MemoryResetPolicy.PRESERVE
        ):
            initialization.append(f"    {name}_read_data = '0;")
            initialization.extend(
                f"    {name}_read_stage_{index} = '0;"
                for index in range(memory.read_latency - 1)
            )
        if (
            memory.contents_reset is ir_storage.MemoryResetPolicy.PRESERVE
            or memory.initial_value is not None
        ):
            initialization.extend((
                f"    for (integer zlang_memory_reset_index = 0; "
                f"zlang_memory_reset_index < {memory.depth}; "
                "zlang_memory_reset_index = zlang_memory_reset_index + 1)",
                f"      {name}_cells[zlang_memory_reset_index] = {initial_word};",
            ))
        initialization.append("  end")

    lines = [
        *declarations,
        *combinational,
        *initialization,
        f"  {'always' if initialization else 'always_ff'} "
        f"@({sv_sequential.clock_event(module, sv_rendering._identifier, memory_clock)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, memory_clock)}) begin",
    ]
    if memory.read_latency >= 1 and memory.read_data_reset is ir_storage.MemoryResetPolicy.CLEAR:
        lines.append(f"      {name}_read_data <= '0;")
        lines.extend(
            f"      {name}_read_stage_{index} <= '0;"
            for index in range(memory.read_latency - 1)
        )
    if memory.contents_reset is ir_storage.MemoryResetPolicy.CLEAR:
        lines.append(
            f"      for (integer zlang_memory_reset_index = 0; "
            f"zlang_memory_reset_index < {memory.depth}; "
            "zlang_memory_reset_index = zlang_memory_reset_index + 1) "
        )
        lines.append(
            f"        {name}_cells[zlang_memory_reset_index] <= {initial_word};"
        )
    if (
        memory.contents_reset is ir_storage.MemoryResetPolicy.PRESERVE
        and (
            memory.read_latency == 0
            or memory.read_data_reset is ir_storage.MemoryResetPolicy.PRESERVE
        )
    ):
        lines.append("      // Memory contents and read result hold across reset.")
    lines.extend((
        "    end",
        "    else begin",
        f"      if ({render(memory.write_enable)}) {name}_cells[{render(memory.write_address)}] <= "
        f"{name + '_write_merged' if memory.write_mask is not None else render(memory.write_data)};",
    ))
    if memory.read_latency >= 1 and memory.collision is ir_storage.MemoryCollision.WRITE_FIRST:
        lines.append(
            f"      if ({render(memory.write_enable)} && "
            f"({render(memory.read_address)} == {render(memory.write_address)})) "
            f"{read_capture} <= "
            f"{name + '_write_merged' if memory.write_mask is not None else render(memory.write_data)};"
        )
        lines.append(
            f"      else {read_capture} <= {name}_cells[{render(memory.read_address)}];"
        )
    elif memory.read_latency >= 1 and memory.collision is ir_storage.MemoryCollision.NO_CHANGE:
        lines.append(
            f"      if (!({render(memory.write_enable)} && "
            f"({render(memory.read_address)} == {render(memory.write_address)}))) "
            f"{read_capture} <= {name}_cells[{render(memory.read_address)}];"
        )
    elif memory.read_latency >= 1:
        lines.append(
            f"      {read_capture} <= {name}_cells[{render(memory.read_address)}];"
        )
    if memory.read_latency > 1:
        lines.extend(
            f"      {name}_read_stage_{index} <= {name}_read_stage_{index - 1};"
            for index in range(1, memory.read_latency - 1)
        )
        lines.append(
            f"      {name}_read_data <= "
            f"{name}_read_stage_{memory.read_latency - 2};"
        )
    lines.extend(("    end", "  end"))
    return lines
