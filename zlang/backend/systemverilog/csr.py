"""CSR SystemVerilog emission from compiler-owned CSR IR."""

from __future__ import annotations

from zlang.ir import csr as ir_csr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.backend.systemverilog import boundary as sv_boundary
from zlang.backend.systemverilog import materialized as sv_materialized
from zlang.backend.systemverilog import module_rendering as sv_module_rendering
from zlang.backend.systemverilog import sequential as sv_sequential
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import expression as sv_expression
from zlang.backend.systemverilog import rendering as sv_rendering
from zlang.backend.systemverilog import state as sv_state


def _csr_event_state_name(
    event: ir_csr.CsrEventBinding,
) -> str:
    identity = event.identity
    return (
        "zlang_csr_event_"
        f"{identity.register.block.declaration_ordinal}_"
        f"{identity.register.declaration_ordinal}_"
        f"{identity.declaration_ordinal}_state"
    )


def _csr_event_active_value(
    access: ir_csr.CsrAccessInterface,
    block: ir_csr.CsrBlock,
    register: ir_csr.CsrRegister,
    event: ir_csr.CsrEventBinding,
) -> str:
    hit_port = (
        access.write_port
        if event.kind is ir_csr.CsrEventKind.WRITE
        else access.read_port
    )
    hit = (
        f"({hit_port} && {access.address_port} == "
        f"32'h{block.base_address + register.offset:08x})"
    )
    if event.kind is ir_csr.CsrEventKind.WRITE:
        selected = sv_rendering._slice(
            access.write_data_port, event.msb, event.lsb
        )
        return f"({hit} ? {selected} : '0)"
    return hit


def _emit_csr(module: ir_module.Module, *, expose_internal_abi: bool = False) -> str:
    blocks = module.csr_blocks
    if not blocks:
        raise SystemVerilogEmissionError("direct CSR emission requires a block")
    if any(block.domain is None or block.reset is None for block in blocks):
        raise SystemVerilogEmissionError(
            "direct CSR emission requires an explicitly resolved physical domain"
        )
    physical_domains = {(block.domain, block.reset) for block in blocks}
    if len(physical_domains) != 1:
        raise SystemVerilogEmissionError(
            "direct CSR emission requires all blocks to share one physical domain"
        )
    csr_clock = blocks[0].domain
    assert csr_clock is not None
    access_observations = {
        block.name: ir_csr.derived_access_observations(
            block,
            module_identity=module.name,
            block_ordinal=index,
            clock_domain=block.domain,
        )
        for index, block in enumerate(blocks)
    }
    access = module.csr_access
    if access is None:
        raise SystemVerilogEmissionError("typed CSR access interface is missing")
    if any(port.protocol is not ir_interfaces.InterfaceProtocol.WIRE for port in module.ports):
        raise SystemVerilogEmissionError(
            "CSR composition currently supports ordinary scalar wire ports only"
        )
    internal_names = ir_csr.csr_internal_port_names(access, module.csr_blocks)
    user_ports = tuple(
        port for port in module.ports if port.name not in internal_names
    )
    internal_ports = []
    if expose_internal_abi:
        internal_ports = [
            *(
                sv_boundary._logic_port("output", ir_csr.csr_state_port_name(binding),
                            binding.canonical_type)
                for block in blocks for binding in block.state_bindings
            ),
            *(
                sv_boundary._logic_port("output", ir_csr.csr_read_hit_port_name(binding),
                            ir_types.BitType())
                for block in blocks
                for binding in access_observations[block.name]
            ),
            *(
                sv_boundary._logic_port(
                    "output",
                    ir_csr.csr_observation_write_hit_port_name(binding),
                    ir_types.BitType(),
                )
                for block in blocks
                for binding in access_observations[block.name]
            ),
            *(
                sv_boundary._logic_port(
                    "output",
                    ir_csr.csr_observation_write_value_port_name(binding),
                    binding.canonical_type,
                )
                for block in blocks
                for binding in access_observations[block.name]
            ),
            *(
                sv_boundary._logic_port(
                    "output",
                    ir_csr.csr_observation_value_port_name(binding),
                    binding.canonical_type,
                )
                for block in blocks
                for binding in access_observations[block.name]
            ),
            *(
                sv_boundary._logic_port("output", ir_csr.csr_event_port_name(binding),
                            binding.canonical_type)
                for block in blocks for register in block.registers
                for binding in register.events
            ),
            *(
                sv_boundary._logic_port("output", ir_csr.csr_split_port_name(block, view),
                            view.canonical_type)
                for block in blocks for view in block.split_views
            ),
        ]
    ports = [
        *(
            item
            for domain in module.clock_domains
            for item in (
                f"input logic {sv_rendering._identifier(domain.clock)}",
                f"input logic {sv_rendering._identifier(domain.reset)}",
            )
        ),
        *(sv_boundary._port_declaration(port) for port in user_ports),
        *(sv_boundary._logic_port("input", name, type_)
          for name, type_ in access.input_types),
        *(sv_boundary._logic_port("output", name, type_)
          for name, type_ in access.output_types),
        *internal_ports,
    ]
    stored = tuple(
        (block, register, field)
        for block in blocks
        for register in block.registers
        for field in register.fields
        if ir_csr.access_owns_state(field.access)
    )
    registered_events = tuple(
        (block, register, event)
        for block in blocks
        for register in block.registers
        for event in register.events
        if event.phase is ir_csr.CsrEventPhase.POST_ACCEPT
    )
    lines = [
        *(
            f"  logic {sv_rendering._range(field.width)}{_csr_field_name(block, register, field)};"
            for block, register, field in stored
        ),
        *(
            f"  logic {sv_rendering._range(event.canonical_type.width)}"
            f"{_csr_event_state_name(event)};"
            for block, register, event in registered_events
        ),
        f"  always_ff @({sv_sequential.clock_event(module, sv_rendering._identifier, csr_clock)}) begin",
        f"    if ({sv_sequential.reset_asserted(module, sv_rendering._identifier, csr_clock)}) begin",
        *(
            f"      {_csr_field_name(block, register, field)} <= "
            f"{field.width}'d{field.reset};"
            for block, register, field in stored
        ),
        *(
            f"      {_csr_event_state_name(event)} <= '0;"
            for block, register, event in registered_events
        ),
        "    end else begin",
    ]
    for block, register, field in stored:
        address = block.base_address + register.offset
        name = _csr_field_name(block, register, field)
        hit = (
            f"({access.write_port} && {access.address_port} == "
            f"32'h{address:08x})"
        )
        incoming = sv_rendering._slice(access.write_data_port, field.msb, field.lsb)
        binding = field.binding
        if (
            field.access is ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR
            and binding is not None
            and binding.kind is ir_csr.CsrBindingKind.STICKY
        ):
            hardware = sv_expression._expression(ir_csr.csr_hardware_source(
                binding, field.type, origin=field.source_origin,
            ))
            if binding.priority is ir_csr.CsrPriority.SOFTWARE:
                update = (
                    f"(({name} | {hardware}) & "
                    f"~({hit} ? {incoming} : '0))"
                )
            else:
                update = (
                    f"(({name} & ~({hit} ? {incoming} : '0)) | {hardware})"
                )
            lines.append(f"      {name} <= {update};")
        elif field.access is ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR:
            lines.append(
                f"      if ({hit}) {name} <= {name} & ~{incoming};"
            )
        elif field.access is ir_csr.CsrAccess.PULSE:
            lines.append(f"      {name} <= {hit} ? {incoming} : '0;")
        else:
            lines.append(f"      if ({hit}) {name} <= {incoming};")
    for block, register, event in registered_events:
        lines.append(
            f"      {_csr_event_state_name(event)} <= "
            f"{_csr_event_active_value(access, block, register, event)};"
        )
    lines.extend((
        "    end", "  end", "  always_comb begin",
        f"    {access.read_data_port} = 32'b0;",
    ))
    lines.append(f"    if ({access.read_port}) begin")
    lines.append(f"      case ({access.address_port})")
    for block in blocks:
        for register in block.registers:
            address = block.base_address + register.offset
            lines.append(f"        32'h{address:08x}: begin")
            for field in register.fields:
                if field.access not in {
                    ir_csr.CsrAccess.READ_WRITE,
                    ir_csr.CsrAccess.READ_ONLY,
                    ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR,
                }:
                    continue
                if (
                    field.access is ir_csr.CsrAccess.READ_ONLY
                    and field.binding is not None
                    and field.binding.kind is ir_csr.CsrBindingKind.STATUS
                ):
                    value = sv_expression._expression(ir_csr.csr_hardware_source(
                        field.binding, field.type, origin=field.source_origin,
                    ))
                elif field.access is ir_csr.CsrAccess.READ_ONLY:
                    value = f"{field.width}'d{field.reset}"
                else:
                    value = _csr_field_name(block, register, field)
                lines.append(
                    f"          {access.read_data_port}[{field.msb}:{field.lsb}] = {value};"
                )
            lines.append("        end")
    lines.extend((
        f"        default: {access.read_data_port} = 32'b0;",
        "      endcase", "    end",
    ))
    addresses = " || ".join(
        f"addr == 32'h{block.base_address + register.offset:08x}"
        for block in blocks for register in block.registers
    )
    lines.extend(
        (
            f"    {access.ready_port} = ({access.read_port} || {access.write_port}) && ({addresses});",
            "  end",
        )
    )
    for block, register, field in stored:
        if (
            field.binding is not None
            and field.binding.kind is ir_csr.CsrBindingKind.COMMAND
        ):
            lines.append(
                f"  assign {sv_rendering._identifier(field.binding.signal)} = "
                f"{_csr_field_name(block, register, field)};"
            )
    fields_by_identity = {
        field.identity: (block, register, field)
        for block in blocks
        for register in block.registers
        for field in register.fields
    }
    if expose_internal_abi:
        for block in blocks:
            for binding in block.state_bindings:
                _, register, field = fields_by_identity[binding.csr_field_id]
                lines.append(
                    f"  assign {ir_csr.csr_state_port_name(binding)} = "
                    f"{_csr_field_name(block, register, field)};"
                )
            for observation in access_observations[block.name]:
                _, register, field = fields_by_identity[observation.csr_field_id]
                address = block.base_address + register.offset
                if (
                    field.access is ir_csr.CsrAccess.READ_ONLY
                    and field.binding is not None
                    and field.binding.kind is ir_csr.CsrBindingKind.STATUS
                ):
                    observed_value = sv_expression._expression(ir_csr.csr_hardware_source(
                        field.binding, field.type, origin=field.source_origin,
                    ))
                elif field.access is ir_csr.CsrAccess.READ_ONLY:
                    observed_value = f"{field.width}'d{field.reset}"
                else:
                    observed_value = _csr_field_name(block, register, field)
                lines.extend((
                    f"  assign {ir_csr.csr_read_hit_port_name(observation)} = "
                    f"{access.read_port} && {access.address_port} == 32'h{address:08x};",
                    f"  assign {ir_csr.csr_observation_write_hit_port_name(observation)} = "
                    f"{access.write_port} && {access.address_port} == 32'h{address:08x};",
                    f"  assign {ir_csr.csr_observation_write_value_port_name(observation)} = "
                    f"{sv_rendering._slice(access.write_data_port, field.msb, field.lsb)};",
                    f"  assign {ir_csr.csr_observation_value_port_name(observation)} = "
                    f"{observed_value};",
                ))
            for view in block.split_views:
                _, low_register, low_field = fields_by_identity[view.low_field_id]
                _, high_register, high_field = fields_by_identity[view.high_field_id]
                lines.append(
                    f"  assign {ir_csr.csr_split_port_name(block, view)} = "
                    f"{{{_csr_field_name(block, high_register, high_field)}, "
                    f"{_csr_field_name(block, low_register, low_field)}}};"
                )
    for block in blocks:
        for register in block.registers:
            for event in register.events:
                if event.phase is ir_csr.CsrEventPhase.POST_ACCEPT:
                    value = _csr_event_state_name(event)
                else:
                    value = _csr_event_active_value(
                        access, block, register, event
                    )
                lines.append(f"  assign {sv_rendering._identifier(event.signal)} = {value};")
                if expose_internal_abi:
                    lines.append(
                        f"  assign {ir_csr.csr_event_port_name(event)} = {value};"
                    )
    has_user_transition = bool(
        module.registers or module.next_assignments or module.rules
    )
    has_user_assignments = bool(module.assignments)
    if has_user_transition or has_user_assignments:
        materialized_declarations, materialized_assignments, render = (
            sv_materialized._materialized_emission(module)
        )
        composed_logic = list(materialized_assignments)
        if has_user_transition:
            sv_state._append_unified_state(
                module, materialized_declarations, composed_logic, render
            )
        else:
            composed_logic.extend(
                f"  assign {sv_rendering._identifier(sv_rendering._assignment_name(assignment))} = "
                f"{render(assignment.expression)};"
                for assignment in module.assignments
            )
        # Declarations must precede both the CSR and resolved-transition logic;
        # the two state contributors own disjoint typed identities and share
        # only the module clock/reset boundary.
        lines = [*materialized_declarations, *lines, *composed_logic]
    return sv_module_rendering._module(module, ports, lines)
def _csr_field_name(
    block: ir_csr.CsrBlock,
    register: ir_csr.CsrRegister,
    field: ir_csr.CsrField,
) -> str:
    return f"csr_{block.name}_{register.name.lower()}_{field.name}"
