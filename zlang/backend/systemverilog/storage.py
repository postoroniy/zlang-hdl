"""Typed memory, ROM, and FIFO SystemVerilog emission."""

from __future__ import annotations

from zlang.ir import expressions as expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import storage as ir_storage
from zlang.backend.companions import companion_for_rom
from zlang.backend import expression_materialization as materialization
from zlang.backend import identifiers as identifiers
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog import state as sv_state


def _emit_memory(module: ir_module.Module, *, ram_style: str | None = None) -> str:
    if len(module.memories) != 1 or not module.clock_domains:
        raise SystemVerilogEmissionError(
            "direct SystemVerilog memory emission requires one memory and clock/reset"
        )
    memory = module.memories[0]
    if memory.ports:
        declarations, lines = sv_state._ported_memory_fragments(
            module, memory, sv_expression._expression, ram_style=ram_style
        )
        for assignment in module.assignments:
            if assignment.target.name == memory.name:
                continue
            lines.append(
                f"  assign {sv_rendering._identifier(sv_rendering._assignment_name(assignment))} = "
                f"{sv_expression._expression(assignment.expression)};"
            )
        return sv_module_rendering._named_module(
            module.name,
            sv_boundary._physical_port_declarations(module),
            declarations + lines,
            typed_module=module,
        )
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
        sv_expression._expression(memory.initial_value)
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
        expanded = sv_state._memory_byte_mask_concatenation(
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
            f"  assign {name}_write_mask = {sv_expression._expression(memory.write_mask)};",
            f"  assign {name}_write_mask_expanded = {{{expanded}}};",
            f"  assign {name}_write_merged = "
            f"({name}_cells[{sv_expression._expression(memory.write_address)}] & ~{name}_write_mask_expanded) | "
            f"({sv_expression._expression(memory.write_data)} & {name}_write_mask_expanded);",
        ))
    if memory.read_latency == 0:
        read_value = f"{name}_cells[{sv_expression._expression(memory.read_address)}]"
        if memory.collision is ir_storage.MemoryCollision.WRITE_FIRST:
            write_value = (
                name + "_write_merged"
                if memory.write_mask is not None
                else sv_expression._expression(memory.write_data)
            )
            read_value = (
                f"({sv_sequential.reset_deasserted(module, sv_rendering._identifier, memory_clock)} && "
                f"{sv_expression._expression(memory.write_enable)} && "
                f"({sv_expression._expression(memory.read_address)} == "
                f"{sv_expression._expression(memory.write_address)})) ? "
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
        f"      if ({sv_expression._expression(memory.write_enable)}) {name}_cells[{sv_expression._expression(memory.write_address)}] <= "
        f"{name + '_write_merged' if memory.write_mask is not None else sv_expression._expression(memory.write_data)};",
    ))
    if memory.read_latency >= 1 and memory.collision is ir_storage.MemoryCollision.WRITE_FIRST:
        lines.append(
            f"      if ({sv_expression._expression(memory.write_enable)} && "
            f"({sv_expression._expression(memory.read_address)} == {sv_expression._expression(memory.write_address)})) "
            f"{read_capture} <= "
            f"{name + '_write_merged' if memory.write_mask is not None else sv_expression._expression(memory.write_data)};"
        )
        lines.append(
            f"      else {read_capture} <= {name}_cells[{sv_expression._expression(memory.read_address)}];"
        )
    elif memory.read_latency >= 1:
        lines.append(
            f"      {read_capture} <= {name}_cells[{sv_expression._expression(memory.read_address)}];"
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
    for assignment in module.assignments:
        lines.append(
            f"  assign {sv_rendering._identifier(sv_rendering._assignment_name(assignment))} = {sv_expression._expression(assignment.expression)};"
        )
    return sv_module_rendering._named_module(
        module.name,
        sv_boundary._physical_port_declarations(module),
        lines,
        typed_module=module,
    )


def _emit_rom_logic(module: ir_module.Module, render) -> tuple[list[str], list[str]]:
    """Emit immutable ROM arrays and their single registered read boundary."""

    if module.roms and not module.clock_domains:
        raise SystemVerilogEmissionError(
            "initialized ROM emission requires one module clock and reset"
        )
    declarations: list[str] = []
    logic: list[str] = []
    for rom in module.roms:
        if rom.domain is None:
            raise SystemVerilogEmissionError(
                f"ROM '{rom.name}' has no resolved clock domain"
            )
        companion = companion_for_rom(rom)
        name = sv_rendering._identifier(rom.name)
        width = sv_rendering._width(rom.element_type)
        declarations.extend((
            f"  logic [{width - 1}:0] {name}_cells [0:{rom.depth - 1}];",
            f"  logic [{width - 1}:0] {name}_read_data;",
        ))
        logic.extend((
            "  initial begin",
            f'    $readmemb("{companion.logical_path}", {name}_cells);',
            "  end",
            f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, rom.domain)}) begin",
            f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, rom.domain)}) {name}_read_data <= '0;",
            f"    else {name}_read_data <= {name}_cells[{render(rom.read_address)}];",
            "  end",
        ))
    return declarations, logic


def _emit_rom_module(module: ir_module.Module) -> str:
    if module.memories or module.fifos:
        raise SystemVerilogEmissionError(
            "initialized ROM cannot share the legacy memory/FIFO emitter"
        )
    declarations, logic = _emit_rom_logic(module, sv_expression._expression)
    for assignment in module.assignments:
        logic.append(
            f"  assign {sv_rendering._identifier(sv_rendering._assignment_name(assignment))} = "
            f"{sv_expression._expression(assignment.expression)};"
        )
    return sv_module_rendering._named_module(
        module.name,
        sv_boundary._physical_port_declarations(module),
        declarations + logic,
        typed_module=module,
    )


def _referenced_fifo_signals(module: ir_module.Module, fifo: object) -> set[ir_storage.FifoSignal]:
    """Return the exact optional observations emitted by the legacy FIFO path."""

    referenced = {
        value.signal
        for root in (
            fifo.data,
            fifo.push,
            fifo.pop,
            *(assignment.expression for assignment in module.assignments),
        )
        for value in materialization.walk_expression(root)
        if isinstance(value, expr.FifoRef) and value.fifo == fifo.name
    }
    if ir_storage.FifoSignal.OVERFLOW in referenced:
        referenced.add(ir_storage.FifoSignal.FULL)
    if ir_storage.FifoSignal.UNDERFLOW in referenced:
        referenced.add(ir_storage.FifoSignal.EMPTY)
    return referenced


def _emit_fifo(module: ir_module.Module) -> str:
    if len(module.fifos) != 1 or not module.clock_domains:
        raise SystemVerilogEmissionError("direct FIFO emission requires one clock/reset FIFO")
    fifo = module.fifos[0]
    if fifo.domain is None:
        raise SystemVerilogEmissionError(
            f"FIFO '{fifo.name}' has no resolved clock domain"
        )
    fifo_clock = fifo.domain
    sources = tuple(p for p in module.inputs if p.protocol is ir_interfaces.InterfaceProtocol.READY_VALID)
    sinks = tuple(p for p in module.outputs if p.protocol is ir_interfaces.InterfaceProtocol.READY_VALID)
    scalar_wire = all(
        port.protocol is ir_interfaces.InterfaceProtocol.WIRE for port in module.ports
    )
    if not scalar_wire and (len(sources) != 1 or len(sinks) != 1):
        raise SystemVerilogEmissionError(
            "direct FIFO emission requires scalar wire ports or one "
            "ready/valid input and output"
        )
    width, count_width = sv_rendering._width(fifo.element_type), fifo.count_width
    ptr_width = max(1, (fifo.depth - 1).bit_length())
    if scalar_wire:
        ports = sv_boundary._physical_port_declarations(module)
    else:
        ports = sv_boundary._physical_port_declarations(module)
    name = fifo.name
    if fifo.data is None or fifo.push is None or fifo.pop is None:
        raise SystemVerilogEmissionError(
            "legacy direct FIFO emission requires explicit data/push/pop controls"
        )
    referenced_fifo_signals = _referenced_fifo_signals(module, fifo)
    # The FIFO state is a typed storage resource, not an implicit ready/valid
    # pass-through.  Publish its semantic read-side signals once, then render
    # every source assignment from typed IR.  This matters when the output is
    # a projection, permutation, or other width-changing expression over
    # ``fifo.front`` rather than the stored element itself.
    observation_declarations: list[str] = []
    observation_assignments: list[str] = []
    if ir_storage.FifoSignal.FRONT in referenced_fifo_signals:
        observation_declarations.append(
            f"  logic {sv_rendering._range(width)}{name}_front;"
        )
        observation_assignments.append(
            f"  assign {name}_front = ({name}_count == '0) "
            f"? '0 : {name}_storage[{name}_rd];"
        )
    if ir_storage.FifoSignal.VALID in referenced_fifo_signals:
        observation_declarations.append(f"  logic {name}_valid;")
        observation_assignments.append(
            f"  assign {name}_valid = !({sv_sequential.reset_asserted(module, sv_rendering._identifier, fifo_clock)}) "
            f"&& ({name}_count != '0);"
        )
    if ir_storage.FifoSignal.READY in referenced_fifo_signals:
        observation_declarations.append(f"  logic {name}_ready;")
        observation_assignments.append(
            f"  assign {name}_ready = !({sv_sequential.reset_asserted(module, sv_rendering._identifier, fifo_clock)}) && "
            f"(({name}_count < {count_width}'d{fifo.depth}) || {name}_pop);"
        )
    if ir_storage.FifoSignal.FULL in referenced_fifo_signals:
        observation_declarations.append(f"  logic {name}_full;")
        observation_assignments.append(
            f"  assign {name}_full = "
            f"({name}_count == {count_width}'d{fifo.depth});"
        )
    if ir_storage.FifoSignal.EMPTY in referenced_fifo_signals:
        observation_declarations.append(f"  logic {name}_empty;")
        observation_assignments.append(
            f"  assign {name}_empty = ({name}_count == '0);"
        )
    request_declarations = [
        f"  logic {name}_push_request, {name}_pop_request;",
        f"  logic {name}_push, {name}_pop;",
    ]
    request_assignments = [
        f"  assign {name}_push_request = {sv_expression._expression(fifo.push)};",
        f"  assign {name}_pop_request = {sv_expression._expression(fifo.pop)};",
        f"  assign {name}_pop = !({sv_sequential.reset_asserted(module, sv_rendering._identifier, fifo_clock)}) && "
        f"{name}_pop_request && ({name}_count != '0);",
        f"  assign {name}_push = !({sv_sequential.reset_asserted(module, sv_rendering._identifier, fifo_clock)}) && "
        f"{name}_push_request && "
        f"(({name}_count < {count_width}'d{fifo.depth}) || {name}_pop);",
    ]
    if ir_storage.FifoSignal.OVERFLOW in referenced_fifo_signals:
        request_declarations.append(f"  logic {name}_overflow;")
        request_assignments.append(
            f"  assign {name}_overflow = !({sv_sequential.reset_asserted(module, sv_rendering._identifier, fifo_clock)}) && "
            f"{name}_push_request && {name}_full && !{name}_pop;"
        )
    if ir_storage.FifoSignal.UNDERFLOW in referenced_fifo_signals:
        request_declarations.append(f"  logic {name}_underflow;")
        request_assignments.append(
            f"  assign {name}_underflow = !({sv_sequential.reset_asserted(module, sv_rendering._identifier, fifo_clock)}) && "
            f"{name}_pop_request && {name}_empty;"
        )
    lines = [f"  logic {sv_rendering._range(width)}{name}_storage [0:{fifo.depth - 1}];",
             f"  logic [{count_width - 1}:0] {name}_count;",
             f"  logic [{ptr_width - 1}:0] {name}_rd, {name}_wr;",
             *observation_declarations,
             *request_declarations,
             *observation_assignments,
             *request_assignments,
             *(
                 f"  assign {sv_rendering._identifier(sv_rendering._assignment_name(assignment))} = "
                 f"{sv_expression._expression(assignment.expression)};"
                 for assignment in module.assignments
             ),
             f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, fifo_clock)}) begin",
             f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, fifo_clock)}) begin {name}_count <= '0; {name}_rd <= '0; {name}_wr <= '0; end",
             "    else begin",
             f"      if ({name}_push) begin {name}_storage[{name}_wr] <= {sv_expression._expression(fifo.data)}; {name}_wr <= ({name}_wr == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_wr + 1'b1; end",
             f"      if ({name}_pop) {name}_rd <= ({name}_rd == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_rd + 1'b1;",
             f"      case ({{{name}_push, {name}_pop}})", f"        2'b10: {name}_count <= {name}_count + 1'b1;",
             f"        2'b01: {name}_count <= {name}_count - 1'b1;", f"        default: {name}_count <= {name}_count;",
             "      endcase", "    end", "  end"]
    return sv_module_rendering._module(module, ports, lines)


def _emit_unified_state_module(module: ir_module.Module) -> str:
    """Emit the frozen register/rule/FIFO transition as one clocked component."""
    ports = sv_boundary._physical_port_declarations(module)
    declarations: list[str] = []
    logic: list[str] = []
    # Unified state used to bypass the backend-wide materialization policy and
    # render transition expressions directly.  Keep one typed renderer for
    # guards, state actions, defaults, and outputs so aggregate runtime reads
    # and expensive fixed-point conversions are named once and then reused.
    stage_declarations, stage_logic, stage_render = (
        sv_materialized._embedded_staging_emission(module)
    )
    if stage_declarations:
        # Delay/Pipeline roots may live in an action activation rather than a
        # conventional assignment.  The shared root traversal includes those
        # predicates; retain their physical stage before scheduling effects.
        declarations.extend(stage_declarations)
        logic.extend(stage_logic)
        render = stage_render
    else:
        materialized_declarations, materialized_assignments, render = (
            sv_materialized._materialized_emission(module)
        )
        declarations.extend(materialized_declarations)
        logic.extend(materialized_assignments)
    rom_declarations, rom_logic = _emit_rom_logic(module, render)
    declarations.extend(rom_declarations)
    logic.extend(rom_logic)
    sv_state._append_unified_state(module, declarations, logic, render)
    return sv_module_rendering._module(module, ports, declarations + logic)
