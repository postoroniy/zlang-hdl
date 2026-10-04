# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded expansion of source CSR groups and split values."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir.types import BitsType, UIntType

from ..errors import SemanticError

if TYPE_CHECKING:
    from ..type_resolution import TypeResolver


class CsrLayoutBuilder:
    """Own the single syntax-to-register expansion used by CSR consumers."""

    def expand_block(
        self,
        declaration: ast.CsrBlockDecl,
        group_catalog: dict[str, ast.CsrGroupDecl],
        type_resolver: TypeResolver,
    ) -> ast.CsrBlockDecl:
        """Expand bounded CSR groups and split values through one path."""

        def compile_integer(value: int | str, description: str) -> int:
            if isinstance(value, int):
                return value
            return type_resolver.evaluate_constant_integer(
                value, description=description, allow_zero=True
            )

        expanded = list(declaration.registers)
        expanded_splits = list(declaration.split_registers)
        for use in declaration.group_uses:
            group = group_catalog.get(use.group_name)
            if group is None:
                raise SemanticError(
                    f"CSR group use '{use.name}' references unknown group "
                    f"'{use.group_name}'"
                )
            if not group.registers and not group.split_registers:
                raise SemanticError(f"CSR group '{group.name}' has no registers")
            count = compile_integer(use.count, f"CSR group '{use.name}' count")
            base = compile_integer(use.base_offset, f"CSR group '{use.name}' base")
            stride = compile_integer(use.stride, f"CSR group '{use.name}' stride")
            if not 1 <= count <= 64:
                raise SemanticError(f"CSR group '{use.name}' count must be in 1..64")
            if stride <= 0 or stride % 4:
                raise SemanticError(
                    f"CSR group '{use.name}' stride must be a positive multiple of 4"
                )
            extent = max(
                (
                    *(register.offset + 4 for register in group.registers),
                    *(split.offset + 8 for split in group.split_registers),
                )
            )
            if stride < extent:
                raise SemanticError(
                    f"CSR group '{use.name}' stride 0x{stride:x} is smaller than "
                    f"group extent 0x{extent:x}"
                )
            for index in range(count):
                prefix = (f"{use.name}[{index}]",)
                for register in group.registers:
                    expanded.append(
                        replace(
                            register,
                            name=f"{use.name}_{index}_{register.name}",
                            offset=base + index * stride + register.offset,
                            projection_path=(*prefix, register.name),
                        )
                    )
                for split in group.split_registers:
                    expanded_splits.append(
                        replace(
                            split,
                            name=f"{use.name}_{index}_{split.name}",
                            offset=base + index * stride + split.offset,
                            projection_path=(*prefix, split.name),
                        )
                    )

        for split in expanded_splits:
            if split.chunk_width != 32:
                raise SemanticError(
                    f"CSR split register '{split.name}' requires split<32> for "
                    "the canonical 32-bit CSR access interface"
                )
            if split.access not in {
                ast.CsrAccess.READ_WRITE,
                ast.CsrAccess.WRITE_ONLY,
            }:
                raise SemanticError(
                    f"CSR split register '{split.name}' currently requires rw or wo access"
                )
            resolved = type_resolver.resolve(split.type_name)
            if not isinstance(resolved, (UIntType, BitsType)) or resolved.width != 64:
                raise SemanticError(
                    f"CSR split register '{split.name}' requires an exact 64-bit "
                    "unsigned or bits value"
                )
            if not 0 <= split.reset < (1 << 64):
                raise SemanticError(
                    f"reset value for CSR split register '{split.name}' "
                    f"does not fit {resolved}"
                )
            low_offset = split.offset + (
                0 if split.order is ast.CsrSplitOrder.LOW_FIRST else 4
            )
            high_offset = split.offset + (
                4 if split.order is ast.CsrSplitOrder.LOW_FIRST else 0
            )
            halves = (
                ("LOW", low_offset, split.reset & 0xFFFF_FFFF),
                ("HIGH", high_offset, (split.reset >> 32) & 0xFFFF_FFFF),
            )
            for suffix, offset, reset in halves:
                expanded.append(
                    ast.CsrRegisterDecl(
                        f"{split.name}_{suffix}",
                        offset,
                        (
                            ast.CsrFieldDecl(
                                split.field_name,
                                ast.TypeName("u32"),
                                split.access,
                                31,
                                0,
                                reset,
                                None,
                                split.origin,
                            ),
                        ),
                        (),
                        split.origin,
                        (
                            (
                                *split.projection_path[:-1],
                                f"{split.projection_path[-1]}_{suffix}",
                            )
                            if split.projection_path
                            else ()
                        ),
                    )
                )
        if len(expanded) > 256:
            raise SemanticError(
                f"CSR block '{declaration.name}' expands to {len(expanded)} "
                "registers; the bounded maximum is 256"
            )
        return replace(
            declaration,
            registers=tuple(expanded),
            group_uses=(),
            split_registers=tuple(expanded_splits),
        )
