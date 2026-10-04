# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative CSR semantic analysis."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import cdc as ir_cdc
from zlang.ir import csr as ir_csr
from zlang.ir import module as ir_module
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.types import BitType, BitsType, UIntType
from zlang.source import SourceOrigin

from ..errors import SemanticError
from .bindings import CsrBindingResolver
from .layout import CsrLayoutBuilder

if TYPE_CHECKING:
    from ..type_resolution import TypeResolver


class CsrAnalyzer:
    """Build compiler-owned CSR IR from expanded source declarations."""

    def __init__(
        self,
        layout: CsrLayoutBuilder,
        bindings: CsrBindingResolver,
    ) -> None:
        self._layout = layout
        self._bindings = bindings

    def analyze(
        self,
        declarations: tuple[ast.CsrBlockDecl, ...],
        groups: tuple[ast.CsrGroupDecl, ...],
        type_resolver: TypeResolver,
        symbols: dict[str, ir_module.Port],
        *,
        module_identity: str,
        clock_domain: str | None,
        clock_domains: tuple[ir_cdc.ClockDomain, ...],
        source_unit: str | None,
        source_digest: str | None,
    ) -> tuple[ir_csr.CsrBlock, ...]:
        group_catalog: dict[str, ast.CsrGroupDecl] = {}
        for group in groups:
            if group.name in group_catalog:
                raise SemanticError(f"duplicate CSR group '{group.name}'")
            if not group.registers and not group.split_registers:
                raise SemanticError(f"CSR group '{group.name}' has no registers")
            group_catalog[group.name] = group
        expanded_declarations = tuple(
            self._layout.expand_block(declaration, group_catalog, type_resolver)
            for declaration in declarations
        )

        blocks: list[ir_csr.CsrBlock] = []
        block_names: set[str] = set()
        absolute_addresses: set[int] = set()
        bound_command_outputs: set[str] = set()
        for block_ordinal, declaration in enumerate(expanded_declarations):
            if not clock_domains:
                raise SemanticError(
                    f"CSR blocks require a module clock and reset; found "
                    f"'{declaration.name}'"
                )
            declared_domains = {item.clock: item for item in clock_domains}
            selected_clock = declaration.domain or clock_domain
            if selected_clock is None:
                available = ", ".join(item.clock for item in clock_domains)
                raise SemanticError(
                    f"ambiguous clock domain for CSR block '{declaration.name}'; "
                    f"available domains: {available}; annotate it with @clock",
                    code="ZL-DOMAIN-AMBIGUOUS",
                )
            selected_domain = declared_domains.get(selected_clock)
            if selected_domain is None:
                raise SemanticError(
                    f"CSR block '{declaration.name}' references unknown clock domain "
                    f"'{selected_clock}'",
                    code="ZL-DOMAIN-UNKNOWN",
                )
            selected_reset = selected_domain.reset
            block_identity = ir_csr.CsrBlockIdentity(module_identity, block_ordinal)
            block_origin = (
                SourceOrigin(
                    declaration.origin,
                    f"csr block {declaration.name}",
                    source_unit,
                    source_digest,
                )
                if declaration.origin is not None
                else None
            )
            if declaration.name in block_names:
                raise SemanticError(f"duplicate CSR block '{declaration.name}'")
            block_names.add(declaration.name)
            base_address = (
                declaration.base_address
                if isinstance(declaration.base_address, int)
                else type_resolver.evaluate_constant_integer(
                    declaration.base_address,
                    description=f"CSR block '{declaration.name}' base address",
                    allow_zero=True,
                )
            )
            if base_address % 4:
                raise SemanticError(
                    f"CSR block '{declaration.name}' base address must be 4-byte aligned"
                )
            if not 0 <= base_address <= 0xFFFF_FFFF:
                raise SemanticError(
                    f"CSR block '{declaration.name}' base address does not fit 32 bits"
                )
            register_names: set[str] = set()
            offsets: set[int] = set()
            registers: list[ir_csr.CsrRegister] = []
            state_bindings: list[ir_csr.CsrFieldStateBinding] = []
            access_observations: list[ir_csr.CsrAccessObservation] = []
            for register_ordinal, register in enumerate(declaration.registers):
                register_identity = ir_csr.CsrRegisterIdentity(
                    block_identity, register_ordinal
                )
                register_origin = (
                    SourceOrigin(
                        register.origin,
                        f"CSR register {declaration.name}.{register.name}",
                        source_unit,
                        source_digest,
                    )
                    if register.origin is not None
                    else block_origin
                )
                if register.name in register_names:
                    raise SemanticError(
                        f"duplicate register '{register.name}' in CSR block "
                        f"'{declaration.name}'"
                    )
                register_names.add(register.name)
                if register.offset % 4:
                    raise SemanticError(
                        f"CSR register '{register.name}' offset must be 4-byte aligned"
                    )
                if register.offset in offsets:
                    raise SemanticError(
                        f"duplicate CSR register offset 0x{register.offset:x}"
                    )
                offsets.add(register.offset)
                address = base_address + register.offset
                if address > 0xFFFF_FFFF:
                    raise SemanticError(
                        f"CSR register '{register.name}' address does not fit 32 bits"
                    )
                if address in absolute_addresses:
                    raise SemanticError(f"overlapping CSR address 0x{address:08x}")
                absolute_addresses.add(address)
                if not register.fields:
                    raise SemanticError(
                        f"CSR register '{register.name}' has no fields"
                    )
                field_names: set[str] = set()
                occupied_bits: set[int] = set()
                next_lsb = 0
                fields: list[ir_csr.CsrField] = []
                events: list[ir_csr.CsrEventBinding] = []
                for field_ordinal, field_decl in enumerate(register.fields):
                    field_identity = ir_csr.CsrFieldIdentity(
                        register_identity, field_ordinal
                    )
                    field_origin = (
                        SourceOrigin(
                            field_decl.origin,
                            f"CSR field {declaration.name}.{register.name}."
                            f"{field_decl.name}",
                            source_unit,
                            source_digest,
                        )
                        if field_decl.origin is not None
                        else register_origin
                    )
                    if field_decl.name in field_names:
                        raise SemanticError(
                            f"duplicate field '{field_decl.name}' in CSR register "
                            f"'{register.name}'"
                        )
                    field_names.add(field_decl.name)
                    type_ = type_resolver.resolve(field_decl.type_name)
                    if not isinstance(type_, (BitType, UIntType, BitsType)):
                        raise SemanticError(
                            f"CSR field '{field_decl.name}' requires bit, unsigned, "
                            "or bits type"
                        )
                    if field_decl.msb is None:
                        lsb = next_lsb
                        msb = lsb + type_.width - 1
                    else:
                        if field_decl.lsb is None:
                            raise SemanticError(
                                f"CSR field '{field_decl.name}' has an incomplete "
                                "bit position"
                            )
                        msb = field_decl.msb
                        lsb = field_decl.lsb
                        if msb < lsb:
                            raise SemanticError(
                                f"CSR field '{field_decl.name}' bit range must be msb:lsb"
                            )
                        if msb - lsb + 1 != type_.width:
                            raise SemanticError(
                                f"CSR field '{field_decl.name}' range width does not "
                                f"match {type_}"
                            )
                    if msb >= 32:
                        raise SemanticError(
                            f"CSR field '{field_decl.name}' exceeds 32-bit register width"
                        )
                    bits = set(range(lsb, msb + 1))
                    if bits & occupied_bits:
                        raise SemanticError(
                            f"CSR field '{field_decl.name}' overlaps another field in "
                            f"'{register.name}'"
                        )
                    occupied_bits.update(bits)
                    next_lsb = max(next_lsb, msb + 1)
                    reset = field_decl.reset if field_decl.reset is not None else 0
                    if reset >= (1 << type_.width):
                        raise SemanticError(
                            f"reset value for CSR field '{field_decl.name}' does not "
                            f"fit {type_}"
                        )
                    access = ir_csr.CsrAccess(field_decl.access.value)
                    if (
                        access is ir_csr.CsrAccess.RESERVED
                        and field_decl.reset is not None
                    ):
                        raise SemanticError(
                            f"reserved CSR field '{field_decl.name}' must not have a "
                            "reset value"
                        )
                    if access is ir_csr.CsrAccess.PULSE and reset != 0:
                        raise SemanticError(
                            f"pulse CSR field '{field_decl.name}' must reset to zero"
                        )
                    binding = self._bindings.resolve(
                        field_decl,
                        type_,
                        access,
                        symbols,
                        selected_clock,
                        field_origin,
                        bound_command_outputs,
                    )
                    typed_field = ir_csr.CsrField(
                        field_decl.name,
                        type_,
                        access,
                        msb,
                        lsb,
                        reset,
                        binding,
                        field_identity,
                        field_origin,
                    )
                    fields.append(typed_field)
                    if access is not ir_csr.CsrAccess.RESERVED:
                        access_observations.append(
                            ir_csr.CsrAccessObservation(
                                field_identity,
                                access,
                                type_,
                                typed_field.width,
                                field_origin,
                                selected_clock,
                            )
                        )
                    if ir_csr.access_owns_state(access):
                        state_bindings.append(
                            ir_csr.CsrFieldStateBinding(
                                field_identity,
                                access,
                                type_,
                                typed_field.width,
                                32,
                                reset,
                                field_origin,
                                f"csr-state:{field_identity.render()}",
                                selected_clock,
                                selected_reset,
                            )
                        )
                event_names: set[str] = set()
                for event_ordinal, event_decl in enumerate(register.events):
                    if event_decl.name in event_names or event_decl.name in field_names:
                        raise SemanticError(
                            f"duplicate field/event '{event_decl.name}' in CSR register "
                            f"'{register.name}'"
                        )
                    event_names.add(event_decl.name)
                    event_type = type_resolver.resolve(event_decl.type_name)
                    if not isinstance(event_type, (BitType, UIntType, BitsType)):
                        raise SemanticError(
                            f"CSR event '{event_decl.name}' requires bit, unsigned, "
                            "or bits type"
                        )
                    if event_decl.msb is None:
                        event_lsb = 0
                        event_msb = event_type.width - 1
                    else:
                        event_msb = event_decl.msb
                        event_lsb = event_decl.lsb
                        if event_lsb is None or event_msb < event_lsb:
                            raise SemanticError(
                                f"CSR event '{event_decl.name}' bit range must be msb:lsb"
                            )
                    if (
                        event_msb >= 32
                        or event_msb - event_lsb + 1 != event_type.width
                    ):
                        raise SemanticError(
                            f"CSR event '{event_decl.name}' range must fit its exact "
                            "type in 32 bits"
                        )
                    if (
                        event_decl.kind is ast.CsrEventKind.READ
                        and not isinstance(event_type, BitType)
                    ):
                        raise SemanticError(
                            f"on_read CSR event '{event_decl.name}' must have type bit"
                        )
                    output = symbols.get(event_decl.signal)
                    if (
                        output is None
                        or output.direction is not ir_module.PortDirection.OUTPUT
                    ):
                        raise SemanticError(
                            f"CSR event '{event_decl.name}' target "
                            f"'{event_decl.signal}' must be an output"
                        )
                    if (
                        output.protocol is not InterfaceProtocol.WIRE
                        or output.type != event_type
                    ):
                        raise SemanticError(
                            f"CSR event '{event_decl.name}' target "
                            f"'{event_decl.signal}' must be a wire output of exact "
                            f"type {event_type}"
                        )
                    if output.name in bound_command_outputs:
                        raise SemanticError(
                            f"CSR command/event output '{output.name}' is bound more "
                            "than once"
                        )
                    bound_command_outputs.add(output.name)
                    events.append(
                        ir_csr.CsrEventBinding(
                            ir_csr.CsrEventIdentity(
                                register_identity, event_ordinal
                            ),
                            event_decl.name,
                            ir_csr.CsrEventKind(event_decl.kind.value),
                            event_type,
                            event_msb,
                            event_lsb,
                            output.name,
                            (
                                SourceOrigin(
                                    event_decl.origin,
                                    f"CSR event {declaration.name}."
                                    f"{register.name}.{event_decl.name}",
                                    source_unit,
                                    source_digest,
                                )
                                if event_decl.origin is not None
                                else register_origin
                            ),
                            selected_clock,
                        )
                    )
                registers.append(
                    ir_csr.CsrRegister(
                        register.name,
                        register.offset,
                        tuple(fields),
                        tuple(events),
                        register_identity,
                        register_origin,
                        register.projection_path,
                    )
                )
            if not registers:
                raise SemanticError(f"CSR block '{declaration.name}' has no registers")
            split_views: list[ir_csr.CsrSplitView] = []
            register_by_name = {item.name: item for item in registers}
            for split in declaration.split_registers:
                low = register_by_name[f"{split.name}_LOW"].fields[0]
                high = register_by_name[f"{split.name}_HIGH"].fields[0]
                split_views.append(
                    ir_csr.CsrSplitView(
                        split.name,
                        split.field_name,
                        type_resolver.resolve(split.type_name),
                        low.identity,
                        high.identity,
                        split.order is ast.CsrSplitOrder.LOW_FIRST,
                        (
                            SourceOrigin(
                                split.origin,
                                f"CSR split value {declaration.name}.{split.name}."
                                f"{split.field_name}",
                                source_unit,
                                source_digest,
                            )
                            if split.origin is not None
                            else block_origin
                        ),
                        split.projection_path,
                    )
                )
            typed_block = ir_csr.CsrBlock(
                declaration.name,
                base_address,
                tuple(registers),
                block_identity,
                block_origin,
                tuple(state_bindings),
                tuple(access_observations),
                tuple(split_views),
                selected_clock,
                selected_reset,
            )
            ir_csr.validate_state_bindings(typed_block)
            blocks.append(typed_block)
        return tuple(blocks)
