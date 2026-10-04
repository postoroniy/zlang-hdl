# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Shared low-level typed SystemVerilog rendering primitives."""

from __future__ import annotations

import re
from typing import Callable

from zlang.ir import expressions as expr
from zlang.ir import types as ir_types
from zlang.backend import identifiers as identifiers
from zlang.backend.systemverilog import context as emission_context


from zlang.backend.systemverilog.errors import SystemVerilogEmissionError

def _scaled_packed_index(rendered_index: str, element_width: int) -> str:
    """Render exact bit scaling without a multiplier for power-of-two widths."""

    if element_width == 1:
        return f"32'({rendered_index})"
    if element_width & (element_width - 1) == 0:
        shift = element_width.bit_length() - 1
        return f"(32'({rendered_index}) << {shift})"
    return f"(32'({rendered_index}) * 32'd{element_width})"


def _comma_join(items: tuple[str, ...]) -> str:
    """Keep ordinary RTL compact while bounding lexer work for giant DAGs."""

    separator = ",\n" if sum(len(item) for item in items) > 16_384 else ", "
    return separator.join(items)


def _identifier(name: str) -> str:
    boundary = emission_context.current_top_boundary()
    if boundary is not None:
        alias = boundary.alias(name)
        if alias is not None:
            return alias
    return identifiers.rtl_identifier(name)


def _logic_declaration(name: str, type_: ir_types.HardwareType) -> str:
    """Declare one internal signal with its exact typed RTL signedness."""

    signed = " signed" if isinstance(type_, (ir_types.SIntType, ir_types.FixedType)) else ""
    return f"  logic{signed} {_range(_width(type_))}{name};"


def _uses_native_vector_indexing(value: expr.Expression) -> bool:
    """Return whether ``value[index]`` matches its emitted declaration."""

    if not isinstance(value, expr.InputRef) or not isinstance(value.type, ir_types.VecType):
        return False
    boundary = emission_context.current_top_boundary()
    return boundary is not None and boundary.is_direct_vector(value.name)


def _register_update_statement(
    register_name: str,
    value: expr.Expression,
    render: Callable[[expr.Expression], str],
) -> str:
    """Render one exact register update, preserving element-write intent."""

    target = _identifier(register_name)
    if (
        isinstance(value, expr.VectorUpdate)
        and isinstance(value.expression, expr.RegisterRef)
        and value.expression.name == register_name
    ):
        vector_type = value.expression.type
        if not isinstance(vector_type, ir_types.VecType):
            raise SystemVerilogEmissionError(
                "vector register update requires a vector register"
            )
        element_width = _width(vector_type.element_type)
        base = _scaled_packed_index(render(value.index), element_width)
        return (
            f"{target}[{base} +: {element_width}] <= "
            f"{render(value.value)};"
        )
    return f"{target} <= {render(value)};"


def _range(width: int) -> str:
    return "" if width == 1 else f"[{width - 1}:0] "


def _width(type_: ir_types.HardwareType) -> int:
    if isinstance(type_, (ir_types.BitType, ir_types.BitsType, ir_types.UIntType, ir_types.SIntType, ir_types.FixedType, ir_types.UFixedType, ir_types.EnumType, ir_types.TaggedUnionType)):
        return type_.width
    if isinstance(type_, ir_types.VecType):
        return type_.length * _width(type_.element_type)
    if isinstance(type_, ir_types.StructType):
        return sum(_width(field.type) for field in type_.fields)
    if isinstance(type_, ir_types.TupleType):
        return sum(_width(element) for element in type_.elements)
    raise SystemVerilogEmissionError(f"no packed width for {type_}")


def _slice(value: str, msb: int, lsb: int) -> str:
    # A select may be applied directly to a named packed value, but constructs
    # such as a sized cast are not legal select bases in all supported
    # SystemVerilog front ends (for example ``7'($unsigned(x))[3]``).  Preserve
    # the same raw-bit semantics for a compound expression with an explicitly
    # sized logical shift instead of relying on parser-specific postfix-select
    # acceptance.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        return f"{value}[{msb}]" if msb == lsb else f"{value}[{msb}:{lsb}]"
    width = msb - lsb + 1
    return f"{width}'(($unsigned({value})) >> {lsb})"


def _struct_field_expression(value: str, type_: ir_types.HardwareType, field: str) -> str:
    if not isinstance(type_, ir_types.StructType):
        raise SystemVerilogEmissionError("ID matching requires a struct payload")
    offset = _width(type_)
    for item in type_.fields:
        width = _width(item.type)
        msb = offset - 1
        lsb = offset - width
        if item.name == field:
            return _slice(value, msb, lsb)
        offset -= width
    raise SystemVerilogEmissionError(
        f"struct '{type_.name}' has no field '{field}'"
    )


def _assignment_name(assignment: object) -> str:
    target = assignment.target
    signal = assignment.signal
    base = _identifier(target.name)
    if assignment.channel is not None:
        return f"{base}_{assignment.channel.value}_{signal.value}"
    return base if signal is None else f"{base}_{signal.value}"
