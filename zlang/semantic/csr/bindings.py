# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Resolution and validation of CSR hardware bindings."""

from __future__ import annotations

from zlang.ast import nodes as ast
from zlang.ir import csr as ir_csr
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.static_projection import StaticView, project_static_bits
from zlang.ir.types import HardwareType, StructType, UIntType
from zlang.source import SourceOrigin

from ..errors import SemanticError


class CsrBindingResolver:
    """Resolve one source binding against exact typed module ports."""

    def resolve(
        self,
        field_decl: ast.CsrFieldDecl,
        field_type: HardwareType,
        access: ir_csr.CsrAccess,
        symbols: dict[str, ir_module.Port],
        selected_clock: str,
        field_origin: SourceOrigin | None,
        bound_command_outputs: set[str],
    ) -> ir_csr.CsrHardwareBinding | None:
        if field_decl.binding is None:
            return None

        signal_name = field_decl.binding.signal
        signal_parts = signal_name.split(".")
        port = symbols.get(signal_name)
        member_path: tuple[str, ...] = ()
        if port is None and len(signal_parts) > 1:
            port = symbols.get(signal_parts[0])
            if port is not None:
                member_path = tuple(signal_parts[1:])
        if port is None and "." in signal_name:
            # Retain the historical generated-scalar spelling for old source
            # while preferring a typed aggregate member whenever the root exists.
            port = symbols.get(signal_name.replace(".", "_"))
        if port is None:
            raise SemanticError(
                f"CSR field '{field_decl.name}' references unknown hardware "
                f"signal '{signal_name}'",
                primary=field_origin,
            )
        if port.protocol is not InterfaceProtocol.WIRE:
            raise SemanticError(
                f"CSR hardware signal '{port.name}' must be a wire port",
                primary=field_origin,
            )
        source: ir_expr.Expression = ir_expr.InputRef(
            port.name, port.type, origin=field_origin
        )
        source_type = port.type
        for member_name in member_path:
            if not isinstance(source_type, StructType):
                raise SemanticError(
                    f"CSR hardware signal '{signal_name}' selects member "
                    f"'{member_name}' from non-struct type {source_type}",
                    primary=field_origin,
                )
            member = source_type.field(member_name)
            if member is None:
                raise SemanticError(
                    f"CSR hardware signal '{signal_name}' references unknown member "
                    f"'{member_name}' of struct '{source_type.name}'",
                    primary=field_origin,
                )
            source = ir_expr.FieldAccess(
                source, member_name, member.type, origin=field_origin
            )
            source_type = member.type
        if source_type != field_type:
            raise SemanticError(
                f"CSR field '{field_decl.name}' has type {field_type}, but hardware "
                f"signal '{signal_name}' has type {source_type}",
                primary=field_origin,
            )
        kind = ir_csr.CsrBindingKind(field_decl.binding.kind.value)
        if kind is ir_csr.CsrBindingKind.STATUS:
            if access is not ir_csr.CsrAccess.READ_ONLY:
                raise SemanticError("direct CSR status bindings require ro access", primary=field_origin)
            if port.direction is not ir_module.PortDirection.INPUT:
                raise SemanticError(
                    f"CSR status signal '{port.name}' must be an input",
                    primary=field_origin,
                )
            if field_decl.reset is not None:
                raise SemanticError(
                    f"hardware-driven status field '{field_decl.name}' must not "
                    "declare a reset",
                    primary=field_origin,
                )
        elif kind is ir_csr.CsrBindingKind.STICKY:
            if access is not ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR:
                raise SemanticError("sticky CSR bindings require w1c access", primary=field_origin)
            if port.direction is not ir_module.PortDirection.INPUT:
                raise SemanticError(
                    f"CSR sticky event '{port.name}' must be an input",
                    primary=field_origin,
                )
        else:
            if member_path:
                raise SemanticError(
                    "CSR command bindings currently require a scalar output port",
                    primary=field_origin,
                )
            if access not in {
                ir_csr.CsrAccess.PULSE,
                ir_csr.CsrAccess.WRITE_ONLY,
            }:
                raise SemanticError(
                    "CSR command bindings require pulse or wo access",
                    primary=field_origin,
                )
            if port.direction is not ir_module.PortDirection.OUTPUT:
                raise SemanticError(
                    f"CSR command signal '{port.name}' must be an output",
                    primary=field_origin,
                )
            if port.name in bound_command_outputs:
                raise SemanticError(
                    f"CSR command output '{port.name}' is bound more than once",
                    primary=field_origin,
                )
            bound_command_outputs.add(port.name)
        priority = (
            ir_csr.CsrPriority(field_decl.binding.priority.value)
            if field_decl.binding.priority is not None
            else None
        )
        if port.domain != selected_clock:
            raise SemanticError(
                f"CSR hardware signal '{signal_name}' belongs to clock domain "
                f"'{port.domain}', expected '{selected_clock}'",
                code="ZL-DOMAIN-CROSSING",
                primary=field_origin,
            )
        return ir_csr.CsrHardwareBinding(
            kind,
            port.name if not member_path else signal_name,
            priority,
            None if kind is ir_csr.CsrBindingKind.COMMAND else source,
        )


def csr_output_controls(
    child: ir_module.Module,
    access_constants: dict[str, int],
    read_bits: tuple[int, ...] = (),
) -> dict[str, tuple[ir_expr.Expression, ...]]:
    """Project the current-cycle controls of synthesized CSR output leaves.

    Stored fields and post-accept events expose only prior state. An exact
    address or read qualifier at the child binding can restrict bus readback
    to the selected register. A static slice of rdata further restricts the
    fields whose values can contribute.
    """
    access = child.csr_access
    if access is None:
        return {}
    inputs = {
        name: ir_expr.InputRef(name, type_)
        for name, type_ in access.input_types
    }
    address = inputs[access.address_port]
    read = inputs[access.read_port]
    write = inputs[access.write_port]
    write_data = inputs[access.write_data_port]
    selected_address = access_constants.get(access.address_port)
    read_enabled = access_constants.get(access.read_port) != 0
    write_enabled = access_constants.get(access.write_port) != 0
    msb, lsb = read_bits or (access.data_width - 1, 0)
    controls: dict[str, tuple[ir_expr.Expression, ...]] = {}
    read_sources: list[ir_expr.Expression] = []
    can_read_value = False
    can_select_register = False

    for block in child.csr_blocks:
        fields = {
            field.identity: (register, field)
            for register in block.registers
            for field in register.fields
        }

        def selected(register: ir_csr.CsrRegister) -> bool:
            return selected_address in (None, block.base_address + register.offset)

        for observation in block.access_observations:
            register, field = fields[observation.csr_field_id]
            source = (
                ir_csr.csr_hardware_source(
                    field.binding, field.type, origin=field.source_origin,
                )
                if field.access is ir_csr.CsrAccess.READ_ONLY
                and field.binding is not None
                and field.binding.kind is ir_csr.CsrBindingKind.STATUS
                else None
            )
            if source is not None:
                controls[ir_csr.csr_observation_value_port_name(observation)] = (source,)
            if selected(register):
                if read_enabled:
                    controls[ir_csr.csr_read_hit_port_name(observation)] = (read, address)
                    if field.lsb <= msb and lsb <= field.msb and (
                        field.access in (ir_csr.CsrAccess.READ_WRITE, ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR)
                        or (field.access is ir_csr.CsrAccess.READ_ONLY and (source is not None or field.reset))
                    ):
                        can_read_value = True
                        if source is not None:
                            low = max(lsb, field.lsb) - field.lsb
                            high = min(msb, field.msb) - field.lsb
                            read_sources.append(
                                source if (low, high) == (0, source.type.width - 1) else
                                project_static_bits(
                                    StaticView(source, high, low), {}, UIntType(high - low + 1),
                                )
                            )
                if write_enabled:
                    controls[ir_csr.csr_observation_write_hit_port_name(observation)] = (write, address)
            controls[ir_csr.csr_observation_write_value_port_name(observation)] = (write_data,)

        for register in block.registers:
            if not selected(register):
                continue
            can_select_register = True
            for event in register.events:
                if event.phase is not ir_csr.CsrEventPhase.ACTIVE_TRANSFER:
                    continue
                if event.kind is ir_csr.CsrEventKind.READ:
                    if not read_enabled:
                        continue
                    event_controls = (read, address)
                else:
                    if not write_enabled:
                        continue
                    event_controls = (write, address, write_data)
                controls[event.signal] = event_controls
                controls[ir_csr.csr_event_port_name(event)] = event_controls

    if can_read_value:
        controls[access.read_data_port] = (read, address, *read_sources)
    if can_select_register and (read_enabled or write_enabled):
        qualifiers = (
            (read, write)
            if 1 not in (access_constants.get(access.read_port), access_constants.get(access.write_port))
            else ()
        )
        controls[access.ready_port] = (address, *qualifiers)
    return controls
