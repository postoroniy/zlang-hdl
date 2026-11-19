"""Typed memory, ROM, and FIFO SystemVerilog emission."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir import module as ir_module
from zlang.ir import storage as ir_storage
from zlang.backend.companions import companion_for_rom
from zlang.backend import expression_materialization as materialization
from zlang.backend import module_features
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import feature_accounting as sv_accounting
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog import state as sv_state
from zlang.backend.systemverilog import state_planning as sv_state_planning
from zlang.backend.systemverilog import memory as sv_memory


def _emit_memory(module: ir_module.Module, *, ram_style: str | None = None) -> str:
    if not module.memories or not module.clock_domains:
        raise SystemVerilogEmissionError(
            "direct SystemVerilog memory emission requires memory and clock/reset"
        )
    if len(module.memories) > 1 and (
        ram_style is not None or not sv_state_planning.uses_dedicated_legacy_storage(module)
    ):
        raise SystemVerilogEmissionError(
            "direct SystemVerilog multi-memory emission requires only globally controlled memories"
        )
    memories, ported_names = _memory_fragments(module, ram_style=ram_style)
    lines = [line for group in memories for section in group for line in section]
    _append_storage_assignments(module, lines, ported_names)
    return sv_module_rendering._named_module(
        module.name,
        sv_boundary._physical_port_declarations(module),
        lines,
        typed_module=module,
    )


def _memory_fragments(
    module: ir_module.Module, *, ram_style: str | None = None,
) -> tuple[list[tuple[list[str], list[str]]], set[str]]:
    fragments: list[tuple[list[str], list[str]]] = []
    ported_names: set[str] = set()
    for memory in module.memories:
        if memory.ports:
            fragments.append(sv_state._ported_memory_fragments(
                module, memory, sv_expression._expression, ram_style=ram_style
            ))
            ported_names.add(memory.name)
        else:
            fragments.append(sv_memory.global_memory_fragments(
                module, memory, sv_expression._expression, ram_style=ram_style,
            ))
    return fragments, ported_names


def _append_storage_assignments(
    module: ir_module.Module, lines: list[str], ported_names: set[str],
) -> None:
    for assignment in module.assignments:
        if assignment.target.name in ported_names:
            continue
        lines.append(
            f"  assign {sv_rendering._identifier(sv_rendering._assignment_name(assignment))} = "
            f"{sv_expression._expression(assignment.expression)};"
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


def _fifo_signal_references(
    module: ir_module.Module,
) -> dict[str, set[ir_storage.FifoSignal]]:
    """Return the exact optional observations emitted by the legacy FIFO path."""

    references: dict[str, set[ir_storage.FifoSignal]] = {
        fifo.name: set() for fifo in module.fifos if not fifo.scheduled
    }
    if not references:
        return references
    # Storage operands can read a FIFO observation without exposing it at
    # an output. Include every expression rendered by the global fragments.
    roots = (
        *(value for fifo in module.fifos for value in (fifo.data, fifo.push, fifo.pop)),
        *(assignment.expression for assignment in module.assignments),
        *(rom.read_address for rom in module.roms),
        *(value for memory in module.memories for value in (
            memory.read_address, memory.write_enable, memory.write_address,
            memory.write_data, memory.write_mask, memory.initial_value,
        )),
        *(value for memory in module.memories for port in memory.ports for value in (
            port.address, port.read_enable, port.write_enable, port.write_data,
            port.write_mask,
        )),
    )
    for root in roots:
        if root is None:
            continue
        for value in materialization.walk_expression(root):
            if isinstance(value, expr.FifoRef) and value.fifo in references:
                references[value.fifo].add(value.signal)
    for referenced in references.values():
        if ir_storage.FifoSignal.OVERFLOW in referenced:
            referenced.add(ir_storage.FifoSignal.FULL)
        if ir_storage.FifoSignal.UNDERFLOW in referenced:
            referenced.add(ir_storage.FifoSignal.EMPTY)
    return references


def _emit_fifo(module: ir_module.Module, *, name: str | None = None) -> str:
    """Emit global FIFO controls and other closed, independently driven storage."""
    if not module.fifos or not module.clock_domains:
        raise SystemVerilogEmissionError("direct FIFO emission requires FIFO and clock/reset")
    if (
        (len(module.fifos) > 1 or module.memories or module.roms)
        and not sv_state_planning.uses_dedicated_legacy_storage(module)
    ):
        raise SystemVerilogEmissionError(
            "direct SystemVerilog mixed storage requires globally controlled FIFOs and RAMs"
        )
    sv_accounting.account_emission_plan(
        module,
        "fifo",
        *sv_accounting.STORAGE_GROUPS,
        module_features.ModuleFeatureGroup.PROTOCOL_PORTS,
        *sv_accounting.physical_boundary_groups(module),
    )
    if name is not None:
        module = replace(module, name=name)
    ports = sv_boundary._physical_port_declarations(module)
    references = _fifo_signal_references(module)
    lines: list[str] = []
    declarations: list[str] = []
    mixed = bool(module.memories or module.roms)
    updates: list[str] = []
    for fifo in module.fifos:
        fifo_declarations, controls, sequential = _fifo_fragments(
            module, fifo, references.get(fifo.name, set())
        )
        # Mixed storage operands may read another resource in either
        # direction. Otherwise keep the established per-FIFO section order.
        (declarations if mixed else lines).extend(fifo_declarations)
        lines.extend(controls)
        updates.extend(sequential)
    memories, ported_names = _memory_fragments(module)
    for memory_declarations, _ in memories:
        declarations.extend(memory_declarations)
    rom_declarations, rom_logic = _emit_rom_logic(module, sv_expression._expression)
    declarations.extend(rom_declarations)
    for _, memory_logic in memories:
        lines.extend(memory_logic)
    lines.extend(rom_logic)
    _append_storage_assignments(module, lines, ported_names)
    lines.extend(updates)
    return sv_module_rendering._module(module, ports, declarations + lines)


def _fifo_fragments(
    module: ir_module.Module,
    fifo: ir_storage.Fifo,
    referenced_fifo_signals: set[ir_storage.FifoSignal],
) -> tuple[list[str], list[str], list[str]]:
    if fifo.domain is None:
        raise SystemVerilogEmissionError(
            f"FIFO '{fifo.name}' has no resolved clock domain"
        )
    fifo_clock = fifo.domain
    width, count_width = sv_rendering._width(fifo.element_type), fifo.count_width
    ptr_width = max(1, (fifo.depth - 1).bit_length())
    name = sv_rendering._identifier(fifo.name)
    if fifo.data is None or fifo.push is None or fifo.pop is None:
        raise SystemVerilogEmissionError(
            "legacy direct FIFO emission requires explicit data/push/pop controls"
        )
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
    declarations = [
        f"  logic {sv_rendering._range(width)}{name}_storage [0:{fifo.depth - 1}];",
        f"  logic [{count_width - 1}:0] {name}_count;",
        f"  logic [{ptr_width - 1}:0] {name}_rd, {name}_wr;",
        *observation_declarations,
        *request_declarations,
    ]
    updates = [
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, fifo_clock)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, fifo_clock)}) begin "
        f"{name}_count <= '0; {name}_rd <= '0; {name}_wr <= '0; end",
        "    else begin",
        f"      if ({name}_push) begin {name}_storage[{name}_wr] <= {sv_expression._expression(fifo.data)}; "
        f"{name}_wr <= ({name}_wr == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_wr + 1'b1; end",
        f"      if ({name}_pop) {name}_rd <= ({name}_rd == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_rd + 1'b1;",
        f"      case ({{{name}_push, {name}_pop}})",
        f"        2'b10: {name}_count <= {name}_count + 1'b1;",
        f"        2'b01: {name}_count <= {name}_count - 1'b1;",
        f"        default: {name}_count <= {name}_count;",
        "      endcase",
        "    end",
        "  end",
    ]
    return declarations, observation_assignments + request_assignments, updates


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
