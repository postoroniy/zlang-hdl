# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Resolution and validation of CSR hardware bindings."""

from __future__ import annotations

from zlang.ast import nodes as ast
from zlang.ir import csr as ir_csr
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.types import HardwareType, StructType
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
                f"signal '{signal_name}'"
            )
        if port.protocol is not InterfaceProtocol.WIRE:
            raise SemanticError(
                f"CSR hardware signal '{port.name}' must be a wire port"
            )
        source: ir_expr.Expression = ir_expr.InputRef(
            port.name, port.type, origin=field_origin
        )
        source_type = port.type
        for member_name in member_path:
            if not isinstance(source_type, StructType):
                raise SemanticError(
                    f"CSR hardware signal '{signal_name}' selects member "
                    f"'{member_name}' from non-struct type {source_type}"
                )
            member = source_type.field(member_name)
            if member is None:
                raise SemanticError(
                    f"CSR hardware signal '{signal_name}' references unknown member "
                    f"'{member_name}' of struct '{source_type.name}'"
                )
            source = ir_expr.FieldAccess(
                source, member_name, member.type, origin=field_origin
            )
            source_type = member.type
        if source_type != field_type:
            raise SemanticError(
                f"CSR field '{field_decl.name}' has type {field_type}, but hardware "
                f"signal '{signal_name}' has type {source_type}"
            )
        kind = ir_csr.CsrBindingKind(field_decl.binding.kind.value)
        if kind is ir_csr.CsrBindingKind.STATUS:
            if access is not ir_csr.CsrAccess.READ_ONLY:
                raise SemanticError("direct CSR status bindings require ro access")
            if port.direction is not ir_module.PortDirection.INPUT:
                raise SemanticError(
                    f"CSR status signal '{port.name}' must be an input"
                )
            if field_decl.reset is not None:
                raise SemanticError(
                    f"hardware-driven status field '{field_decl.name}' must not "
                    "declare a reset"
                )
        elif kind is ir_csr.CsrBindingKind.STICKY:
            if access is not ir_csr.CsrAccess.WRITE_ONE_TO_CLEAR:
                raise SemanticError("sticky CSR bindings require w1c access")
            if port.direction is not ir_module.PortDirection.INPUT:
                raise SemanticError(
                    f"CSR sticky event '{port.name}' must be an input"
                )
        else:
            if member_path:
                raise SemanticError(
                    "CSR command bindings currently require a scalar output port"
                )
            if access not in {
                ir_csr.CsrAccess.PULSE,
                ir_csr.CsrAccess.WRITE_ONLY,
            }:
                raise SemanticError(
                    "CSR command bindings require pulse or wo access"
                )
            if port.direction is not ir_module.PortDirection.OUTPUT:
                raise SemanticError(
                    f"CSR command signal '{port.name}' must be an output"
                )
            if port.name in bound_command_outputs:
                raise SemanticError(
                    f"CSR command output '{port.name}' is bound more than once"
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
            )
        return ir_csr.CsrHardwareBinding(
            kind,
            port.name if not member_path else signal_name,
            priority,
            None if kind is ir_csr.CsrBindingKind.COMMAND else source,
        )
