# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative unified-state and ported-memory SystemVerilog renderer."""

from __future__ import annotations


from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import storage as ir_storage
from zlang.ir import state as ir_state
from zlang.backend import identifiers as identifiers
from zlang.backend import naming as naming
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang import memory_planning as memory_planning


from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import rendering as sv_rendering


def uses_dedicated_legacy_storage(module: ir_module.Module) -> bool:
    """Return whether the frozen global-control storage emitter owns state."""

    transition = module.resolved_transition
    if (
        transition is None
        or transition.action_groups
        or module.registers
        or module.rules
        or module.next_assignments
        or module.elaborated_instances
        or module.children
        or module.connections
        or module.hierarchical_connections
        or module.request_response_connections
        or module.aggregate_protocol_connections
    ):
        return False
    resource_kinds = {resource.kind for resource in transition.resources}
    if (
        module.fifos
        and all(not fifo.scheduled for fifo in module.fifos)
        and not module.memories
        and not module.roms
    ):
        return resource_kinds <= {ir_state.StateResourceKind.FIFO}
    if (
        module.memories
        and all(not memory.scheduled for memory in module.memories)
        and not module.fifos
        and not module.roms
    ):
        return resource_kinds <= {ir_state.StateResourceKind.MEMORY}
    return False


def requires_unified_state(module: ir_module.Module) -> bool:
    """Return whether typed state needs the exact unified scheduler.

    The compact rule emitter is deliberately limited to scalar register writes
    whose accepted effects are exactly representable by its per-register
    conditional chains.  A conflict between two one-effect groups is safe on
    that path; a conflict involving any multi-effect group is not, because it
    could commit part of a lower-priority rule after that rule lost elsewhere.
    Protocol state, storage legality, conditional action activation, output
    effects, and those atomic multi-effect conflicts therefore stay on the
    unified path.
    """

    transition = module.resolved_transition
    if transition is None:
        return False
    if uses_dedicated_legacy_storage(module):
        return False
    if not (
        transition.resources
        or transition.action_groups
        or module.registers
        or module.rules
        or module.next_assignments
        or any(fifo.scheduled for fifo in module.fifos)
        or any(memory.scheduled for memory in module.memories)
        or module.roms
    ):
        return False
    # Closed zero-rule sequential state historically uses the unified body,
    # including its begin/end reset spelling.  It has no rule dimensions to
    # enumerate, so retaining that route is both byte-compatible and bounded.
    if (
        not module.rules
        and not transition.action_groups
        and (module.registers or module.next_assignments)
    ):
        return True
    if (
        any(fifo.scheduled for fifo in module.fifos)
        or any(memory.scheduled for memory in module.memories)
        or bool(module.roms)
        or bool(module.request_responses)
        or bool(module.aggregate_protocol_endpoints)
        or any(
            port.protocol is not ir_interfaces.InterfaceProtocol.WIRE
            for port in module.ports
        )
        or any(
            endpoint.protocol is not ir_interfaces.InterfaceProtocol.WIRE
            for endpoint in module.protocol_endpoints
        )
        or any(
            resource.kind is not ir_state.StateResourceKind.REGISTER
            for resource in transition.resources
        )
        or any(
            action.kind is not ir_state.StateActionKind.REGISTER_WRITE
            or action.activation is not None
            for group in transition.action_groups
            for action in group.actions
        )
    ):
        return True
    groups = transition.action_groups
    return any(
        ir_state.groups_may_conflict(left, right)
        and (len(left.actions) != 1 or len(right.actions) != 1)
        for index, left in enumerate(groups)
        for right in groups[index + 1 :]
    )


def _memory_byte_mask_concatenation(
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


def _ported_memory_fragments(
    module: ir_module.Module,
    memory,
    render,
    *,
    ram_style: str | None = None,
) -> tuple[list[str], list[str]]:
    """Emit one normalized named-port memory without backend re-discovery."""

    if not memory.ports:
        raise SystemVerilogEmissionError("ported-memory emission requires named ports")
    if memory.async_memory and memory.collision is not ir_storage.MemoryCollision.READ_FIRST:
        raise SystemVerilogEmissionError(
            f"independent-clock memory collision mode '{memory.collision.value}' "
            "requires an exact target physical binding; generic synthesizable "
            "SystemVerilog publishes only the structural old-data model"
        )
    width = sv_rendering._width(memory.element_type)
    name = sv_rendering._identifier(memory.name)
    base_cells = identifiers.rtl_memory_cells_identifier(memory.name)
    initial_word = (
        render(memory.initial_value) if memory.initial_value is not None else "'0"
    )
    style = f'(* ram_style = "{ram_style}" *) ' if ram_style else ""
    readable = tuple(
        port for port in memory.ports
        if port.kind in {ir_storage.MemoryPortKind.READ, ir_storage.MemoryPortKind.READ_WRITE}
    )
    writable = tuple(
        port for port in memory.ports
        if port.kind in {ir_storage.MemoryPortKind.WRITE, ir_storage.MemoryPortKind.READ_WRITE}
    )
    implementation = memory_planning.plan_memory_implementation(memory)
    replicated = (
        implementation.implementation
        is memory_planning.MemoryImplementationKind.REPLICATED_1R1W
    )
    cells_by_read_port = {
        port.name: (
            f"{base_cells}_{sv_rendering._identifier(port.name)}"
            if replicated else base_cells
        )
        for port in readable
    }
    cell_arrays = tuple(dict.fromkeys(cells_by_read_port.values())) or (base_cells,)
    declarations = [
        *(
            f"  {style}logic [{width - 1}:0] {cells} [0:{memory.depth - 1}];"
            for cells in cell_arrays
        ),
    ]
    logic: list[str] = [
        f"  // memory_plan={implementation.implementation.value} "
        f"identity={implementation.identity}"
    ]
    if (
        memory.initial_value is not None
        or memory.contents_reset is ir_storage.MemoryResetPolicy.PRESERVE
    ):
        logic.append("  initial begin")
        for cells in cell_arrays:
            logic.extend((
                f"    for (integer {name}_reset_index = 0; "
                f"{name}_reset_index < {memory.depth}; "
                f"{name}_reset_index = {name}_reset_index + 1)",
                f"      {cells}[{name}_reset_index] = {initial_word};",
            ))
        logic.append("  end")
    by_name = {port.name: port for port in writable}
    priority = tuple(
        by_name[item] for item in memory.write_priority
    ) if memory.write_priority else writable
    effective_enable: dict[str, str] = {}
    higher: list[object] = []
    for port in priority:
        assert port.write_enable is not None
        enable = render(port.write_enable)
        blockers = [
            f"({effective_enable[item.name]} && "
            f"({render(item.address)} == {render(port.address)}))"
            for item in higher
        ]
        effective_enable[port.name] = (
            enable if not blockers
            else f"({enable} && !({' || '.join(blockers)}))"
        )
        higher.append(port)
    for port in readable:
        if memory.read_latency > 1:
            declarations.extend(
                f"  logic [{width - 1}:0] {name}_{sv_rendering._identifier(port.name)}_read_stage_{index};"
                for index in range(memory.read_latency - 1)
            )
        declarations.append(
            f"  logic [{width - 1}:0] {name}_{sv_rendering._identifier(port.name)}_read_data;"
        )
    for port in writable:
        if port.write_mask is None:
            continue
        lanes = memory.write_mask_width
        assert lanes is not None
        prefix = f"{name}_{sv_rendering._identifier(port.name)}"
        declarations.extend((
            f"  logic [{lanes - 1}:0] {prefix}_write_mask;",
            f"  logic [{width - 1}:0] {prefix}_write_mask_expanded;",
            f"  logic [{width - 1}:0] {prefix}_write_merged;",
        ))
        expanded = _memory_byte_mask_concatenation(
            f"{prefix}_write_mask", element_width=width, lane_count=lanes
        )
        assert port.write_data is not None
        logic.extend((
            f"  assign {prefix}_write_mask = {render(port.write_mask)};",
            f"  assign {prefix}_write_mask_expanded = {{{expanded}}};",
            f"  assign {prefix}_write_merged = "
            f"({cell_arrays[0]}[{render(port.address)}] & ~{prefix}_write_mask_expanded) | "
            f"({render(port.write_data)} & {prefix}_write_mask_expanded);",
        ))

    def write_value(port) -> str:
        assert port.write_data is not None
        return (
            f"{name}_{sv_rendering._identifier(port.name)}_write_merged"
            if port.write_mask is not None else render(port.write_data)
        )
    writer_domains = {port.domain for port in writable}
    if len(writer_domains) > 1:
        raise SystemVerilogEmissionError(
            "ported-memory RTL supports writers in one domain"
        )
    writer_domain = next(iter(writer_domains), memory.domain)

    def append_cell_reset(indent: str) -> None:
        if memory.contents_reset is ir_storage.MemoryResetPolicy.CLEAR:
            for cells in cell_arrays:
                logic.extend((
                    f"{indent}for (integer {name}_reset_index = 0; "
                    f"{name}_reset_index < {memory.depth}; "
                    f"{name}_reset_index = {name}_reset_index + 1)",
                    f"{indent}  {cells}[{name}_reset_index] <= {initial_word};",
                ))
        else:
            logic.append(f"{indent}// Memory contents hold across writer reset.")

    def append_writes(indent: str) -> None:
        for port in priority:
            for cells in cell_arrays:
                logic.append(
                    f"{indent}if ({effective_enable[port.name]}) "
                    f"{cells}[{render(port.address)}] <= {write_value(port)};"
                )

    def append_read_reset(domain_ports, indent: str) -> None:
        for port in domain_ports:
            target = f"{name}_{sv_rendering._identifier(port.name)}_read_data"
            if memory.read_data_reset is ir_storage.MemoryResetPolicy.CLEAR:
                for index in range(memory.read_latency - 1):
                    logic.append(
                        f"{indent}{name}_{sv_rendering._identifier(port.name)}_read_stage_{index} <= '0;"
                    )
                logic.append(f"{indent}{target} <= '0;")

    def append_reads(domain_ports, indent: str) -> None:
        for port in domain_ports:
            assert port.read_enable is not None
            prefix = f"{name}_{sv_rendering._identifier(port.name)}"
            target = (
                f"{prefix}_read_stage_0"
                if memory.read_latency > 1 else f"{prefix}_read_data"
            )
            read_cells = cells_by_read_port[port.name]
            collisions = [
                (writer, f"({effective_enable[writer.name]} && "
                 f"({render(writer.address)} == {render(port.address)}))")
                for writer in priority
            ]
            any_collision = " || ".join(term for _, term in collisions) or "1'b0"
            if memory.collision is ir_storage.MemoryCollision.NO_CHANGE:
                logic.append(
                    f"{indent}if ({render(port.read_enable)} && !({any_collision})) "
                    f"{target} <= {read_cells}[{render(port.address)}];"
                )
            elif memory.collision is ir_storage.MemoryCollision.WRITE_FIRST and collisions:
                selected = f"{read_cells}[{render(port.address)}]"
                for writer, term in reversed(collisions):
                    selected = f"{term} ? {write_value(writer)} : ({selected})"
                logic.append(
                    f"{indent}if ({render(port.read_enable)}) {target} <= {selected};"
                )
            else:
                logic.append(
                    f"{indent}if ({render(port.read_enable)}) "
                    f"{target} <= {read_cells}[{render(port.address)}];"
                )

    def append_read_shifts(domain_ports, indent: str) -> None:
        if memory.read_latency <= 1:
            return
        for port in domain_ports:
            prefix = f"{name}_{sv_rendering._identifier(port.name)}"
            for index in range(1, memory.read_latency - 1):
                logic.append(
                    f"{indent}{prefix}_read_stage_{index} <= "
                    f"{prefix}_read_stage_{index - 1};"
                )
            logic.append(
                f"{indent}{prefix}_read_data <= "
                f"{prefix}_read_stage_{memory.read_latency - 2};"
            )

    read_domains = tuple(dict.fromkeys(port.domain for port in readable))
    common_registered = (
        not memory.async_memory
        and memory.read_latency >= 1
        and writer_domain is not None
        and read_domains == (writer_domain,)
    )
    native_true_dual = (
        ram_style == "block"
        and common_registered
        and memory.read_latency == 1
        and len(memory.ports) == 2
        and all(port.kind is ir_storage.MemoryPortKind.READ_WRITE for port in memory.ports)
    )
    if native_true_dual:
        if memory.contents_reset is not ir_storage.MemoryResetPolicy.PRESERVE:
            raise SystemVerilogEmissionError(
                "selected true-dual block-memory emission requires preserved "
                "contents; clearing every cell prevents exact native inference"
            )
        # A true-dual RAM has one physical clocked process per port.  Generic
        # ported memories deliberately retain their single deterministic
        # process; this shape is emitted only after exact target selection.
        for port in memory.ports:
            target = f"{name}_{sv_rendering._identifier(port.name)}_read_data"
            logic.extend((
                f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, port.domain)}) begin",
                f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, port.domain)}) begin",
            ))
            if memory.read_data_reset is ir_storage.MemoryResetPolicy.CLEAR:
                logic.append(f"      {target} <= '0;")
            logic.append("    end else begin")
            logic.append(
                f"      if ({effective_enable[port.name]}) "
                f"{base_cells}[{render(port.address)}] <= {write_value(port)};"
            )
            append_reads((port,), "      ")
            logic.extend(("    end", "  end"))
        return declarations, logic
    if common_registered:
        logic.extend((
            f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, writer_domain)}) begin",
            f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, writer_domain)}) begin",
        ))
        append_cell_reset("      ")
        append_read_reset(readable, "      ")
        logic.append("    end else begin")
        append_writes("      ")
        append_reads(readable, "      ")
        append_read_shifts(readable, "      ")
        logic.extend(("    end", "  end"))
        return declarations, logic

    if writer_domain is not None:
        logic.extend((
            f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, writer_domain)}) begin",
            f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, writer_domain)}) begin",
        ))
        append_cell_reset("      ")
        logic.append("    end else begin")
        append_writes("      ")
        logic.extend(("    end", "  end"))

    for domain in read_domains:
        domain_ports = tuple(port for port in readable if port.domain == domain)
        if memory.read_latency == 0:
            for port in domain_ports:
                target = f"{name}_{sv_rendering._identifier(port.name)}_read_data"
                read_value = (
                    f"{cells_by_read_port[port.name]}[{render(port.address)}]"
                )
                if memory.collision is ir_storage.MemoryCollision.WRITE_FIRST:
                    for writer in reversed(priority):
                        collision = (
                            f"({effective_enable[writer.name]} && "
                            f"({render(writer.address)} == {render(port.address)}))"
                        )
                        read_value = (
                            f"{collision} ? {write_value(writer)} : ({read_value})"
                        )
                if memory.read_data_reset is ir_storage.MemoryResetPolicy.CLEAR:
                    read_value = (
                        f"{sv_sequential.reset_asserted(module, sv_rendering._identifier, domain)} "
                        f"? '0 : ({read_value})"
                    )
                logic.append(f"  assign {target} = {read_value};")
            continue
        logic.extend((
            f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, domain)}) begin",
            f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, domain)}) begin",
        ))
        append_read_reset(domain_ports, "      ")
        logic.append("    end else begin")
        append_reads(domain_ports, "      ")
        append_read_shifts(domain_ports, "      ")
        logic.extend(("    end", "  end"))
    return declarations, logic


def _append_unified_schedule_wiring(
    module, logic, render, transition, groups, activation_predicates,
    activation_names, local_names, action_enable,
) -> None:
    """Render rule selection and the FIFO/memory controls it authorizes."""

    fifo_by_name = {fifo.name: fifo for fifo in module.fifos}
    for physical_domain in module.clock_domains:
        local_transition = ir_state.transition_for_domain(
            transition, physical_domain.clock
        )
        local_groups = ir_state.ordered_groups(local_transition)
        local_fifos = tuple(
            resource
            for resource in local_transition.resources
            if resource.kind is ir_state.StateResourceKind.FIFO
        )
        local_activations = ir_state.conditional_activation_predicates(local_transition)
        guard_names = [group.rule_name for group in local_groups]
        local_regions = ir_state.selection_regions_for_transition(local_transition)
        for group in local_groups:
            clauses: list[str] = []
            for region in local_regions[group.rule_name]:
                count_values = region[:len(local_fifos)]
                guard_values = region[
                    len(local_fifos):len(local_fifos) + len(guard_names)
                ]
                activation_values = region[
                    len(local_fifos) + len(guard_names):
                ]
                terms: list[str] = []
                for resource, value in zip(
                    local_fifos, count_values, strict=True
                ):
                    fifo = fifo_by_name[resource.name]
                    name = fifo.name
                    if value is ir_state.FifoOccupancy.EMPTY:
                        terms.append(f"{sv_rendering._identifier(name)}_count == '0")
                    elif value is ir_state.FifoOccupancy.FULL:
                        terms.append(
                            f"{sv_rendering._identifier(name)}_count == "
                            f"{fifo.count_width}'d{fifo.depth}"
                        )
                    elif value is ir_state.FifoOccupancy.MIDDLE:
                        terms.append(
                            f"({sv_rendering._identifier(name)}_count > '0 && "
                            f"{sv_rendering._identifier(name)}_count < "
                            f"{fifo.count_width}'d{fifo.depth})"
                        )
                terms.extend(
                    f"{local_names.rule(name, 'guard')} == 1'b{1 if value else 0}"
                    for name, value in zip(
                        guard_names, guard_values, strict=True
                    )
                    if value is not None
                )
                terms.extend(
                    f"{activation_names[activation_predicates.index(activation)]} "
                    f"== 1'b{1 if value else 0}"
                    for activation, value in zip(
                        local_activations, activation_values, strict=True
                    )
                    if value is not None
                )
                clauses.append("(" + " && ".join(terms) + ")")
            condition = " || ".join(clauses) if clauses else "1'b0"
            logic.append(
                f"  assign {local_names.rule(group.rule_name, 'fire')} = "
                f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier, physical_domain.clock)} "
                f"&& ({condition});"
            )

    for fifo in module.fifos:
        resource_id = next(
            item.semantic_id for item in transition.resources
            if item.kind.value == "fifo" and item.name == fifo.name
        )
        push_actions = [
            (group, action)
            for group in groups
            for action in group.actions
            if action.resource_id == resource_id
            and action.kind is ir_state.StateActionKind.FIFO_PUSH
        ]
        pop_actions = [
            (group, action)
            for group in groups
            for action in group.actions
            if action.resource_id == resource_id
            and action.kind is ir_state.StateActionKind.FIFO_POP
        ]
        name = sv_rendering._identifier(fifo.name)
        push_terms = [action_enable(group, action) for group, action in push_actions]
        pop_terms = [action_enable(group, action) for group, action in pop_actions]
        logic.append(f"  assign {name}_push = " + (" || ".join(push_terms) if push_terms else "1'b0") + ";")
        logic.append(f"  assign {name}_pop = " + (" || ".join(pop_terms) if pop_terms else "1'b0") + ";")
        data = f"{sv_rendering._width(fifo.element_type)}'d0"
        for group, action in reversed(push_actions):
            data = (
                f"{action_enable(group, action)} ? "
                f"{render(action.operands[0])} : ({data})"
            )
        logic.append(f"  assign {name}_push_data = {data};")

    for memory in module.memories:
        name = sv_rendering._identifier(memory.name)
        resource_id = memory.semantic_id
        read_actions = [
            (group, action)
            for group in groups
            for action in group.actions
            if action.resource_id == resource_id
            and action.kind is ir_state.StateActionKind.MEMORY_READ_REQUEST
        ]
        write_actions = [
            (group, action)
            for group in groups
            for action in group.actions
            if action.resource_id == resource_id
            and action.kind is ir_state.StateActionKind.MEMORY_WRITE
        ]
        read_terms = [action_enable(group, action) for group, action in read_actions]
        write_terms = [action_enable(group, action) for group, action in write_actions]
        logic.append(
            f"  assign {name}_read_fire = "
            + (" || ".join(read_terms) if read_terms else "1'b0") + ";"
        )
        logic.append(
            f"  assign {name}_write_fire = "
            + (" || ".join(write_terms) if write_terms else "1'b0") + ";"
        )
        read_address = f"{memory.address_width}'d0"
        for group, action in reversed(read_actions):
            read_address = (
                f"{action_enable(group, action)} ? "
                f"{render(action.operands[0])} : ({read_address})"
            )
        write_address = f"{memory.address_width}'d0"
        write_data = f"{sv_rendering._width(memory.element_type)}'d0"
        write_mask = (
            f"{memory.write_mask_width}'d0"
            if memory.write_mask_width is not None else None
        )
        for group, action in reversed(write_actions):
            fire = action_enable(group, action)
            write_address = f"{fire} ? {render(action.operands[0])} : ({write_address})"
            write_data = f"{fire} ? {render(action.operands[1])} : ({write_data})"
            if write_mask is not None:
                write_mask = f"{fire} ? {render(action.operands[2])} : ({write_mask})"
        logic.append(f"  assign {name}_read_address = {read_address};")
        logic.append(f"  assign {name}_write_address = {write_address};")
        logic.append(f"  assign {name}_write_data = {write_data};")
        if write_mask is not None:
            logic.append(f"  assign {name}_write_mask = {write_mask};")
            expanded = _memory_byte_mask_concatenation(
                f"{name}_write_mask",
                element_width=sv_rendering._width(memory.element_type),
                lane_count=memory.write_mask_width,
            )
            logic.append(
                f"  assign {name}_write_mask_expanded = {{{expanded}}};"
            )
            logic.append(
                f"  assign {name}_write_merged = "
                f"({name}_cells[{name}_write_address] & ~{name}_write_mask_expanded) | "
                f"({name}_write_data & {name}_write_mask_expanded);"
            )


def _append_unified_state(
    module: ir_module.Module,
    declarations: list[str],
    logic: list[str],
    render,
    *,
    excluded_assignment_names: frozenset[str] = frozenset(),
) -> None:
    """Append one frozen register/rule/storage transition to a component.

    This is deliberately shared by standalone and hierarchical modules.  A
    parent that owns scheduled state must not lose that state merely because it
    also instantiates a child component.
    """
    if not module.clock_domains or module.resolved_transition is None:
        raise SystemVerilogEmissionError("unified state emission requires clock/reset transition IR")
    if any(not fifo.scheduled for fifo in module.fifos):
        raise SystemVerilogEmissionError("mixed legacy and scheduled FIFO resources are not implemented")
    transition = module.resolved_transition
    local_names = naming.module_rtl_names(module)
    groups = ir_state.ordered_groups(transition)
    activation_predicates = ir_state.conditional_activation_predicates(transition)
    activation_names = tuple(
        f"zlang_condition_{index}_active"
        for index in range(len(activation_predicates))
    )

    for memory in module.memories:
        if not memory.ported:
            continue
        memory_declarations, memory_logic = _ported_memory_fragments(
            module, memory, render
        )
        declarations.extend(memory_declarations)
        logic.extend(memory_logic)

    def action_enable(group, action) -> str:
        fire = local_names.rule(group.rule_name, "fire")
        if action.activation is None:
            return fire
        return (
            f"({fire} && "
            f"{activation_names[ir_state.action_activation_predicate_index(transition, action)]})"
        )

    def register_update_lines(register, indent: str) -> list[str]:
        resource_id = next(
            item.semantic_id for item in transition.resources
            if item.kind.value == "register" and item.name == register.name
        )
        writers = [
            (group, action)
            for group in groups
            for action in group.actions
            if (
                action.resource_id == resource_id
                and action.kind is ir_state.StateActionKind.REGISTER_WRITE
            )
        ]
        lines: list[str] = []
        for index, (group, action) in enumerate(writers):
            keyword = "if" if index == 0 else "else if"
            update = sv_rendering._register_update_statement(
                register.name, action.operands[0], render
            )
            lines.append(
                f"{indent}{keyword} ({action_enable(group, action)}) "
                f"{update}"
            )
        default = next(
            (
                item for item in module.next_assignments
                if item.target.name == register.name
            ),
            None,
        )
        if default is not None:
            lines.append(
                f"{indent}{'else ' if writers else ''}"
                f"{sv_rendering._identifier(register.name)} <= {render(default.expression)};"
            )
        return lines

    for register in module.registers:
        declarations.append(
            sv_rendering._logic_declaration(
                identifiers.rtl_register_state_identifier(register.name), register.type
            )
        )
    for fifo in module.fifos:
        width = sv_rendering._width(fifo.element_type)
        ptr_width = max(1, (fifo.depth - 1).bit_length())
        name = sv_rendering._identifier(fifo.name)
        declarations.extend((
            f"  logic {sv_rendering._range(width)}{name}_storage [0:{fifo.depth - 1}];",
            f"  logic [{fifo.count_width - 1}:0] {name}_count;",
            f"  logic [{ptr_width - 1}:0] {name}_rd, {name}_wr;",
            f"  logic {name}_push, {name}_pop;",
            f"  logic {sv_rendering._range(width)}{name}_push_data;",
            f"  logic {sv_rendering._range(width)}{name}_front;",
            f"  logic {name}_empty, {name}_full;",
            f"  logic {name}_valid, {name}_ready;",
            f"  logic {name}_overflow, {name}_underflow;",
        ))
        logic.extend((
            f"  assign {name}_front = ({name}_count == '0) "
            f"? '0 : {name}_storage[{name}_rd];",
            f"  assign {name}_empty = ({name}_count == '0);",
            f"  assign {name}_full = ({name}_count == {fifo.count_width}'d{fifo.depth});",
            f"  assign {name}_valid = "
            f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier, fifo.domain)} && !{name}_empty;",
            f"  assign {name}_ready = "
            f"{sv_sequential.reset_deasserted(module, sv_rendering._identifier, fifo.domain)} && "
            f"({name}_count < {fifo.count_width}'d{fifo.depth});",
            f"  assign {name}_overflow = 1'b0;",
            f"  assign {name}_underflow = 1'b0;",
        ))
    for memory in module.memories:
        if memory.ported:
            continue
        if not memory.scheduled:
            raise SystemVerilogEmissionError(
                "mixed legacy and scheduled memory resources are not implemented"
            )
        if memory.read_latency != 1:
            raise SystemVerilogEmissionError(
                "scheduled memory emission requires read_latency 1"
            )
        width = sv_rendering._width(memory.element_type)
        address_width = memory.address_width
        name = sv_rendering._identifier(memory.name)
        cells_name = identifiers.rtl_memory_cells_identifier(memory.name)
        read_data_name = identifiers.rtl_memory_read_data_identifier(memory.name)
        declarations.extend((
            f"  logic {sv_rendering._range(width)}{cells_name} [0:{memory.depth - 1}];",
            f"  logic {sv_rendering._range(width)}{read_data_name};",
            f"  logic {name}_read_fire, {name}_write_fire;",
            f"  logic [{address_width - 1}:0] {name}_read_address, {name}_write_address;",
            f"  logic {sv_rendering._range(width)}{name}_write_data;",
        ))
        if memory.write_mask_width is not None:
            declarations.extend((
                f"  logic [{memory.write_mask_width - 1}:0] {name}_write_mask;",
                f"  logic {sv_rendering._range(width)}{name}_write_mask_expanded;",
                f"  logic {sv_rendering._range(width)}{name}_write_merged;",
            ))

    for group in groups:
        declarations.extend((
            f"  logic {local_names.rule(group.rule_name, 'guard')};",
            f"  logic {local_names.rule(group.rule_name, 'fire')};",
        ))
        logic.append(
            f"  assign {local_names.rule(group.rule_name, 'guard')} = {render(group.guard)};"
        )
    for index, activation in enumerate(activation_predicates):
        name = activation_names[index]
        declarations.append(f"  logic {name};")
        logic.append(f"  assign {name} = {render(activation)};")

    _append_unified_schedule_wiring(
        module, logic, render, transition, groups, activation_predicates,
        activation_names, local_names, action_enable,
    )

    for memory in module.memories:
        initialize_contents = (
            memory.contents_reset is ir_storage.MemoryResetPolicy.PRESERVE
            or memory.initial_value is not None
        )
        initialize_read_data = (
            memory.read_data_reset is ir_storage.MemoryResetPolicy.PRESERVE
        )
        if not (initialize_contents or initialize_read_data):
            continue
        name = sv_rendering._identifier(memory.name)
        logic.append("  initial begin")
        if initialize_read_data:
            logic.append(f"    {name}_read_data = '0;")
        if initialize_contents:
            initial_word = (
                render(memory.initial_value)
                if memory.initial_value is not None else "'0"
            )
            logic.extend((
                f"    for (integer {name}_reset_index = 0; "
                f"{name}_reset_index < "
                f"{memory.depth}; {name}_reset_index = {name}_reset_index + 1)",
                f"      {name}_cells[{name}_reset_index] = {initial_word};",
            ))
        logic.append("  end")

    for physical_domain in module.clock_domains:
        domain_registers = tuple(
            item for item in module.registers
            if item.domain == physical_domain.clock
        )
        resettable_registers = tuple(
            item for item in domain_registers if item.initial is not None
        )
        unreset_registers = tuple(
            item for item in domain_registers if item.initial is None
        )
        domain_fifos = tuple(
            item for item in module.fifos
            if item.domain == physical_domain.clock
        )
        domain_memories = tuple(
            item for item in module.memories
            if item.domain == physical_domain.clock
        )
        for register in unreset_registers:
            updates = register_update_lines(register, "    ")
            if not updates:
                continue
            logic.append(
                f"  always_ff @({sv_sequential.active_clock_event(module, sv_rendering._identifier, physical_domain.clock)}) begin"
            )
            logic.extend(updates)
            logic.append("  end")
        if not (resettable_registers or domain_fifos or domain_memories):
            continue
        domain_has_initialization = any(
            memory.contents_reset is ir_storage.MemoryResetPolicy.PRESERVE
            or memory.read_data_reset is ir_storage.MemoryResetPolicy.PRESERVE
            or memory.initial_value is not None
            for memory in domain_memories
        )
        logic.append(
            f"  {'always' if domain_has_initialization else 'always_ff'} "
            f"@({sv_sequential.clock_event(module, sv_rendering._identifier, physical_domain.clock)}) begin"
        )
        logic.append(
            f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, physical_domain.clock)}) begin"
        )
        for register in resettable_registers:
            logic.append(
                f"      {sv_rendering._identifier(register.name)} <= {render(register.initial)};"
            )
        for fifo in domain_fifos:
            name = sv_rendering._identifier(fifo.name)
            logic.append(
                f"      {name}_count <= '0; {name}_rd <= '0; {name}_wr <= '0;"
            )
        for memory in domain_memories:
            name = sv_rendering._identifier(memory.name)
            if memory.read_data_reset is ir_storage.MemoryResetPolicy.CLEAR:
                logic.append(f"      {name}_read_data <= '0;")
            if memory.contents_reset is ir_storage.MemoryResetPolicy.CLEAR:
                initial_word = (
                    render(memory.initial_value)
                    if memory.initial_value is not None else "'0"
                )
                logic.append(
                    f"      for (integer {name}_reset_index = 0; "
                    f"{name}_reset_index < "
                    f"{memory.depth}; {name}_reset_index = {name}_reset_index + 1) "
                    f"{name}_cells[{name}_reset_index] <= {initial_word};"
                )
            if (
                memory.read_data_reset is ir_storage.MemoryResetPolicy.PRESERVE
                and memory.contents_reset is ir_storage.MemoryResetPolicy.PRESERVE
            ):
                logic.append(
                    f"      // {name} contents and read result hold across reset."
                )
        logic.append("    end else begin")
        for register in resettable_registers:
            logic.extend(register_update_lines(register, "      "))
        for fifo in domain_fifos:
            name = sv_rendering._identifier(fifo.name)
            ptr_width = max(1, (fifo.depth - 1).bit_length())
            logic.extend((
                f"      if ({name}_push) begin {name}_storage[{name}_wr] <= {name}_push_data; "
                f"{name}_wr <= ({name}_wr == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_wr + 1'b1; end",
                f"      if ({name}_pop) {name}_rd <= ({name}_rd == {ptr_width}'d{fifo.depth - 1}) ? '0 : {name}_rd + 1'b1;",
                f"      case ({{{name}_push, {name}_pop}})",
                f"        2'b10: {name}_count <= {name}_count + 1'b1;",
                f"        2'b01: {name}_count <= {name}_count - 1'b1;",
                f"        default: {name}_count <= {name}_count;",
                "      endcase",
            ))
        for memory in domain_memories:
            name = sv_rendering._identifier(memory.name)
            logic.append(
                f"      if ({name}_write_fire) "
                f"{name}_cells[{name}_write_address] <= "
                f"{name + '_write_merged' if memory.write_mask_width is not None else name + '_write_data'};"
            )
            if memory.collision is ir_storage.MemoryCollision.WRITE_FIRST:
                logic.extend((
                    f"      if ({name}_read_fire) begin",
                    f"        if ({name}_write_fire && ({name}_read_address == {name}_write_address))",
                    f"          {name}_read_data <= "
                    f"{name + '_write_merged' if memory.write_mask_width is not None else name + '_write_data'};",
                    f"        else {name}_read_data <= {name}_cells[{name}_read_address];",
                    "      end",
                ))
            else:
                logic.append(
                    f"      if ({name}_read_fire) "
                    f"{name}_read_data <= {name}_cells[{name}_read_address];"
                )
        logic.extend(("    end", "  end"))
    # Scalar rule outputs share the already-resolved rule-fire schedule with
    # register writes.  A direct assignment, when present, is the ordinary
    # combinational fallback; otherwise the reset/idle value is the exact
    # zero of the declared output type.  Protocol-member assignments retain
    # their existing independent lowering below.
    scalar_assignments = {
        assignment.target.name: assignment
        for assignment in module.assignments
        if (
            isinstance(assignment.target, ir_module.Port)
            and assignment.target.protocol is ir_interfaces.InterfaceProtocol.WIRE
        )
    }
    emitted_scalar_outputs: set[str] = set()
    for output in module.outputs:
        if output.protocol is not ir_interfaces.InterfaceProtocol.WIRE:
            continue
        resource = next(
            (
                item for item in transition.resources
                if item.kind.value == "output" and item.name == output.name
            ),
            None,
        )
        writers = (
            [
                (group, action)
                for group in groups
                for action in group.actions
                if action.resource_id == resource.semantic_id
                and action.kind is ir_state.StateActionKind.OUTPUT_WRITE
            ]
            if resource is not None else []
        )
        assignment = scalar_assignments.get(output.name)
        if assignment is None and not writers:
            continue
        value = (
            render(assignment.expression)
            if assignment is not None
            else f"{sv_rendering._width(output.type)}'d0"
        )
        for group, action in reversed(writers):
            value = (
                f"{action_enable(group, action)} ? "
                f"{render(action.operands[0])} : ({value})"
            )
        logic.append(f"  assign {sv_rendering._identifier(output.name)} = {value};")
        # ``_assignment_name`` is boundary-aware and therefore returns the
        # physical packed-root alias for aggregate top outputs.  Keep this set
        # in that same namespace; mixing the semantic source name here emitted
        # the direct output assignment a second time after boundary inlining.
        emitted_scalar_outputs.add(sv_rendering._identifier(output.name))
    for assignment in module.assignments:
        name = sv_rendering._assignment_name(assignment)
        if name in emitted_scalar_outputs or name in excluded_assignment_names:
            continue
        logic.append(
            f"  assign {sv_rendering._identifier(name)} = {render(assignment.expression)};"
        )
