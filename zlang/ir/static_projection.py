# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Exact bit ranges through typed representation operations."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import NamedTuple

from zlang.ir import expressions as expr
from zlang.ir.packing import struct_field_lsb, tuple_element_lsb, vector_element_lsb
from zlang.ir.types import BitsType, FixedType, HardwareType, SIntType, StructType, UIntType, VecType


class StaticView(NamedTuple):
    expression: expr.Expression
    msb: int
    lsb: int


def static_view(value: object) -> StaticView | None:
    """Return a statically selected range in the immediate operand."""
    if isinstance(value, expr.Slice):
        return StaticView(value.expression, value.msb, value.lsb)
    if isinstance(value, expr.Truncate):
        return StaticView(value.expression, value.type.width - 1, 0)
    if isinstance(value, expr.FieldAccess) and isinstance(value.expression.type, StructType):
        lsb = struct_field_lsb(value.expression.type, value.field)
        return StaticView(value.expression, lsb + value.type.width - 1, lsb)
    if isinstance(value, expr.TupleProject):
        lsb = tuple_element_lsb(value.expression.type, value.index)
        return StaticView(value.expression, lsb + value.type.width - 1, lsb)
    if isinstance(value, expr.VectorIndex) and isinstance(value.index, int):
        lsb = vector_element_lsb(value.expression.type, value.index)
        return StaticView(value.expression, lsb + value.type.width - 1, lsb)
    return None


def static_parts(
    view: StaticView,
    locals_: Mapping[str, expr.Expression],
    active_locals: frozenset[str] = frozenset(),
) -> Iterator[StaticView]:
    """Resolve MSB-first parts and stop at operators that can mix bits."""
    pending = [(view, active_locals)]
    while pending:
        part, active = pending.pop()
        value, msb, lsb = part
        selected = static_view(value)
        if selected is not None:
            pending.append((
                StaticView(selected.expression, msb + selected.lsb, lsb + selected.lsb), active,
            ))
            continue
        if isinstance(value, expr.Extend):
            source = value.expression
            top = source.type.width - 1
            if lsb <= top:
                pending.append((StaticView(source, min(msb, top), lsb), active))
            if msb > top:
                # Emit the high part first. Unsigned padding is constant; all
                # signed padding depends on the single original sign bit.
                width = msb - max(top, lsb - 1)
                if isinstance(source.type, (SIntType, FixedType)):
                    if width == 1:
                        pending.append((StaticView(source, top, top), active))
                        continue
                    sign = expr.Slice(source, top, top, UIntType(1), origin=value.origin)
                    padding = expr.Extend(
                        expr.Bitcast(sign, SIntType(1), origin=value.origin),
                        SIntType(width), origin=value.origin,
                    )
                else:
                    padding = expr.Constant(0, BitsType(width), origin=value.origin)
                yield StaticView(padding, width - 1, 0)
            continue
        if isinstance(value, (expr.Bitcast, expr.Reshape)) and msb < value.expression.type.width:
            pending.append((StaticView(value.expression, msb, lsb), active))
            continue
        if isinstance(value, expr.InputRef) and value.name in locals_ and value.name not in active:
            pending.append((StaticView(locals_[value.name], msb, lsb), active | {value.name}))
            continue
        if isinstance(value, expr.StructConstruct):
            fields = dict(value.fields)
            operands = tuple(fields[field.name] for field in value.type.fields)
        elif isinstance(value, expr.TupleConstruct):
            operands = tuple(reversed(value.elements))
        elif (
            isinstance(value, (expr.Generate, expr.Map))
            and isinstance(value.type, VecType)
            and len(value.elements) == value.type.length
        ):
            # Materialized vector elements start at the least significant lane.
            operands = tuple(reversed(value.elements))
        elif isinstance(value, expr.VectorConcat):
            operands = tuple(reversed(value.operands))
        elif isinstance(value, expr.Concat):
            operands = value.operands
        else:
            yield part
            continue
        remaining = value.type.width
        selected_operands = []
        for operand in operands:
            low = remaining - operand.type.width
            high, first = min(msb, remaining - 1), max(lsb, low)
            if high >= first:
                selected_operands.append((StaticView(operand, high - low, first - low), active))
            remaining = low
        pending.extend(reversed(selected_operands))


def project_static_bits(
    view: StaticView, locals_: Mapping[str, expr.Expression], result_type: HardwareType,
) -> expr.Expression:
    """Materialize the same range that dependency analysis follows."""
    return _materialize_parts(static_parts(view, locals_), result_type)


def narrow_static_view(
    value: expr.Expression, locals_: Mapping[str, expr.Expression],
) -> expr.Expression | None:
    """Return a rewritten view only when the requested range can move inward."""
    view = static_view(value)
    if view is None:
        return None
    parts = tuple(static_parts(view, locals_))
    if not parts or (len(parts) == 1 and parts[0].expression is view.expression):
        return None
    return _materialize_parts(iter(parts), value.type)


def _materialize_parts(parts: Iterator[StaticView], result_type: HardwareType) -> expr.Expression:
    parts = tuple(
        value if lsb == 0 and msb == value.type.width - 1 else
        expr.Slice(value, msb, lsb, UIntType(msb - lsb + 1), origin=value.origin)
        for value, msb, lsb in parts
    )
    value = parts[0] if len(parts) == 1 else expr.Concat(parts, BitsType(result_type.width))
    return value if value.type == result_type else expr.Bitcast(value, result_type, origin=value.origin)
