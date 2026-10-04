# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative coercion expression semantics."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from collections.abc import Iterator
from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import packing as ir_packing
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir import runtime_values as runtime_values
from zlang.ir.traversal import walk_expression
from zlang.ir import types as ir_types
from .errors import SemanticError

def walk_syntax_expressions(expression: ast.Expression) -> Iterator[ast.Expression]:
    """Yield parser-owned expression nodes without duplicating AST shape tables."""

    pending: list[object] = [expression]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Expression):
            yield current
        if isinstance(current, tuple):
            pending.extend(reversed(current))
        elif is_dataclass(current) and not isinstance(current, type):
            pending.extend(
                getattr(current, descriptor.name)
                for descriptor in reversed(fields(current))
                if descriptor.name != "origin"
            )

def is_raw_representation_type(type_: ir_types.HardwareType) -> bool:
    """Return whether a type is one of the two explicit raw-bit boundaries."""

    return isinstance(type_, ir_types.BitsType) or (
        isinstance(type_, ir_types.VecType) and isinstance(type_.element_type, ir_types.BitType)
    )

def make_bitcast(
    expression: ir_expr.Expression,
    target: ir_types.HardwareType,
    *,
    description: str = "bitcast",
) -> ir_expr.Expression:
    """Create one exact-width non-enum representation reinterpretation."""

    try:
        source_width = ir_packing.packed_width(expression.type)
    except ir_packing.PackingError as error:
        raise SemanticError(
            f"{description} source must be recursively bit-packable and non-enum, "
            f"got {expression.type}: {error}"
        ) from error
    try:
        target_width = ir_packing.packed_width(target)
    except ir_packing.PackingError as error:
        raise SemanticError(
            f"{description} target must be recursively bit-packable and non-enum, "
            f"got {target}: {error}"
        ) from error
    if source_width != target_width:
        raise SemanticError(
            f"{description} requires equal packed widths, got "
            f"{expression.type} ({source_width}) and {target} ({target_width})"
        )
    if expression.type == target:
        return expression
    if isinstance(expression, ir_expr.Bitcast):
        original = expression.expression
        if original.type == target:
            return original
        return ir_expr.Bitcast(original, target, origin=expression.origin)
    return ir_expr.Bitcast(expression, target, origin=expression.origin)

def coerce_raw_target(
    expression: ir_expr.Expression,
    expected: ir_types.HardwareType | None,
) -> ir_expr.Expression:
    """Apply the deliberately narrow implicit raw-boundary conversion."""

    if expected is None or expression.type == expected:
        return expression
    if not (
        is_raw_representation_type(expression.type)
        or is_raw_representation_type(expected)
    ):
        return expression
    try:
        return make_bitcast(expression, expected, description="implicit raw bitcast")
    except SemanticError:
        return expression

def can_implicitly_bitcast_types(
    source: ir_types.HardwareType,
    target: ir_types.HardwareType,
) -> bool:
    if not (
        is_raw_representation_type(source)
        or is_raw_representation_type(target)
    ):
        return False
    try:
        return ir_packing.packed_width(source) == ir_packing.packed_width(target)
    except ir_packing.PackingError:
        return False

def check_integer_literal(
    value: int,
    expected: ir_types.HardwareType | None,
    *,
    signed_syntax: bool,
) -> ir_expr.Constant:
    """Type one exact integral literal without host-width or wrap semantics."""

    if isinstance(expected, ir_types.EnumType):
        raise SemanticError(
            f"numeric literal {value} cannot initialize enum '{expected.name}'; "
            "use a qualified member"
        )
    if signed_syntax and expected is not None and not isinstance(
        expected, (ir_types.SIntType, ir_types.FixedType)
    ):
        raise SemanticError(
            f"negative integer literal {value} cannot initialize unsigned/raw "
            f"type {expected}"
        )
    if expected is None:
        type_: ir_types.HardwareType = (
            ir_types.SIntType(runtime_values.minimum_signed_width(value))
            if signed_syntax
            else ir_types.UIntType(runtime_values.minimum_unsigned_width(value))
        )
    else:
        type_ = expected
    raw_value = (
        value << type_.fraction
        if isinstance(type_, (ir_types.FixedType, ir_types.UFixedType))
        else value
    )
    if isinstance(type_, ir_types.EnumType) or not runtime_values.scalar_fits(raw_value, type_):
        suggestion = (
            "; use quantize(...) for explicit overflow handling"
            if isinstance(type_, (ir_types.FixedType, ir_types.UFixedType))
            else ""
        )
        raise SemanticError(f"constant {value} does not fit {type_}{suggestion}")
    return ir_expr.Constant(raw_value, type_)

def is_constant_expression(expression: ir_expr.Expression) -> bool:
    """Recognize reset constants through the shared exact constant evaluator."""

    try:
        constant_runtime_value(expression)
    except ConstantExpressionError:
        return False
    return True

def has_runtime_value_dependency(expression: ir_expr.Expression) -> bool:
    """Conservatively detect runtime data without expanding callable bodies."""

    runtime_leaves = (
        ir_expr.InputRef,
        ir_expr.RegisterRef,
        ir_expr.ReadyValidRef,
        ir_expr.CreditRef,
        ir_expr.PacketRef,
        ir_expr.VirtualChannelCreditRef,
        ir_expr.RequestResponseRef,
        ir_expr.FifoRef,
        ir_expr.MemoryRef,
        ir_expr.RomRef,
        ir_expr.InstanceOutputRef,
    )
    return any(
        isinstance(value, runtime_leaves)
        for value in walk_expression(expression)
    )

def fixed_rounding(mode: ast.FixedRoundingMode) -> ir_expr.FixedRounding:
    return {
        ast.FixedRoundingMode.TOWARD_ZERO: ir_expr.FixedRounding.TOWARD_ZERO,
        ast.FixedRoundingMode.FLOOR: ir_expr.FixedRounding.FLOOR,
        ast.FixedRoundingMode.AWAY_ZERO: ir_expr.FixedRounding.AWAY_ZERO,
        ast.FixedRoundingMode.NEAREST_EVEN: ir_expr.FixedRounding.NEAREST_EVEN,
    }[mode]

def fixed_overflow_for_type(
    type_: ir_types.FixedType | ir_types.UFixedType,
) -> ir_expr.FixedOverflow:
    return (
        ir_expr.FixedOverflow.SATURATE
        if type_.overflow is ir_types.FixedOverflowPolicy.SATURATE
        else ir_expr.FixedOverflow.WRAP
    )

def coerce_fixed_target(
    expression: ir_expr.Expression,
    expected: ir_types.HardwareType | None,
) -> ir_expr.Expression:
    """Apply a fixed destination's storage policy without changing its scale."""

    if expected is None or expression.type == expected:
        return expression
    if not isinstance(expected, (ir_types.FixedType, ir_types.UFixedType)):
        return expression
    if type(expression.type) is not type(expected):
        return expression
    if expression.type.fraction > expected.fraction:
        return expression
    if (
        expression.type.fraction < expected.fraction
        and expression.type.width - expression.type.fraction
        > expected.width - expected.fraction
    ):
        return expression
    return ir_expr.FixedConvert(
        expression,
        ir_expr.FixedRounding.TOWARD_ZERO,
        fixed_overflow_for_type(expected),
        ir_expr.FixedConversionKind.RESCALE,
        expected,
    )
