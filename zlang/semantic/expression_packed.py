# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Packed scalar indexing and slicing semantics.

This owner is deliberately narrow: vector and tuple collection semantics remain
in :mod:`expression_collections` while this module owns only bit-packable scalar
selection and its range proof.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import packing as ir_packing
from zlang.ir import types as ir_types

from . import callables as semantic_callables
from . import compile_time_evaluation
from . import expression_ranges
from .errors import SemanticError
from .expression_coercion import can_implicitly_bitcast_types, make_bitcast
from .expression_support import _expand_immutable_locals
from .symbols import ValueSymbol

if TYPE_CHECKING:
    from .context import ExpressionContext


_AGGREGATE_TYPES = (
    ir_types.EnumType,
    ir_types.StructType,
    ir_types.TupleType,
    ir_types.VecType,
)


def _runtime_packed_slice(
    operand: ir_expr.Expression,
    offset: ir_expr.Expression,
    width: int,
    *,
    description: str,
) -> ir_expr.Expression:
    """Lower a proven runtime packed selection to shift and truncation IR."""

    if width < 1 or width > operand.type.width:
        raise ValueError("packed runtime slice width is outside its operand")
    if not isinstance(offset.type, (ir_types.UIntType, ir_types.BitsType)):
        raise ValueError("packed runtime slice offset must be unsigned integral")
    raw = make_bitcast(
        operand,
        ir_types.BitsType(operand.type.width),
        description=description,
    )
    unsigned_offset = make_bitcast(
        offset,
        ir_types.UIntType(offset.type.width),
        description=f"{description} offset",
    )
    shifted = ir_expr.Binary(
        ir_expr.BinaryOperator.SHIFT_RIGHT,
        raw,
        unsigned_offset,
        raw.type,
        raw.type,
    )
    return ir_expr.Truncate(shifted, ir_types.BitsType(width))


def check_packed_index(
    expression: ast.IndexExpr,
    collection: ir_expr.Expression,
    inputs: dict[str, ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    """Return a packed-scalar index, or ``None`` for aggregate collections."""

    if isinstance(collection.type, _AGGREGATE_TYPES) or not ir_packing.is_bit_packable(
        collection.type
    ):
        return None
    if isinstance(expression.index, int):
        index = expression.index
    elif (
        isinstance(expression.index, ast.NameExpr)
        and expression.index.name in context.scope.index_bindings
    ):
        index = context.scope.index_bindings[expression.index.name]
    else:
        typed_index = context.expressions.check(expression.index, inputs, None, context)
        typed_index = _expand_immutable_locals(
            typed_index, inputs, work_budget=context.services
        )
        typed_index = semantic_callables._expand_analysis_calls(
            typed_index, context, purpose="packed bit index"
        )
        if isinstance(typed_index, ir_expr.Constant):
            index = typed_index.value
        else:
            if not isinstance(typed_index.type, (ir_types.UIntType, ir_types.BitsType)):
                raise SemanticError(
                    "runtime packed-bit index must be an unsigned integral "
                    f"expression; got {typed_index.type}"
                )
            value_range = expression_ranges.static_value_range(
                typed_index, context.scope.range_refinements
            )
            if value_range is None:
                raise SemanticError(
                    "runtime packed-bit index has no statically provable unsigned "
                    f"range; required 0..{collection.type.width - 1}"
                )
            if value_range.minimum < 0 or value_range.maximum >= collection.type.width:
                raise SemanticError(
                    "runtime packed-bit index range "
                    f"{value_range.minimum}..{value_range.maximum} is not provably "
                    f"within packed width {collection.type.width} "
                    f"(required 0..{collection.type.width - 1}, "
                    f"type {typed_index.type})"
                )
            selected = _runtime_packed_slice(
                collection,
                typed_index,
                1,
                description="runtime packed-bit index",
            )
            result_type = ir_types.BitType()
            if (
                expected is not None
                and expected != result_type
                and not can_implicitly_bitcast_types(result_type, expected)
            ):
                raise SemanticError(
                    "packed bit index produces bit, "
                    f"expected exact {expected}"
                )
            return make_bitcast(
                selected,
                result_type,
                description="runtime packed-bit index",
            )
    if index < 0 or index >= collection.type.width:
        raise SemanticError(
            f"packed bit index {index} is out of range for "
            f"{collection.type} (required 0..{collection.type.width - 1})"
        )
    result_type = ir_types.BitType()
    if (
        expected is not None
        and expected != result_type
        and not can_implicitly_bitcast_types(result_type, expected)
    ):
        raise SemanticError(f"packed bit index produces bit, expected exact {expected}")
    selected = ir_expr.Slice(collection, index, index, ir_types.BitsType(1))
    return make_bitcast(selected, result_type, description="packed bit index")


def check_static_slice(
    expression: ast.SliceExpr,
    inputs: dict[str, ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression:
    operand = context.expressions.check(expression.expression, inputs, None, context)
    if isinstance(operand.type, _AGGREGATE_TYPES) or not ir_packing.is_bit_packable(
        operand.type
    ):
        raise SemanticError(f"bit slicing requires a packed scalar value, got {operand.type}")
    if context.environment.type_resolver is None:
        raise SemanticError("bit-slice bounds require a type resolver")

    def bound(value: int | str, description: str) -> int:
        try:
            return context.environment.type_resolver._eval_constant_integer(
                str(value),
                description=description,
                allow_zero=True,
                allow_negative=True,
            )
        except SemanticError as error:
            if "unresolved compile-time parameter" not in str(error):
                raise
            raise SemanticError(
                f"unresolved constant '{value}' in {description}: {error}"
            ) from error

    msb = bound(expression.msb, "bit-slice MSB")
    lsb = bound(expression.lsb, "bit-slice LSB")
    if lsb < 0:
        raise SemanticError("bit-slice LSB must not be negative")
    if msb < lsb:
        raise SemanticError(
            f"bit slice [{msb}:{lsb}] is reversed; MSB must be at least LSB"
        )
    if msb >= operand.type.width:
        raise SemanticError(
            f"bit slice [{msb}:{lsb}] is out of range for {operand.type} "
            f"(width {operand.type.width})"
        )
    result_type = ir_types.BitsType(ir_packing.slice_width(msb, lsb))
    if (
        expected is not None
        and expected != result_type
        and not can_implicitly_bitcast_types(result_type, expected)
    ):
        raise SemanticError(
            f"bit slice [{msb}:{lsb}] produces {result_type}, expected exact {expected}"
        )
    return ir_expr.Slice(operand, msb, lsb, result_type)


def check_dynamic_slice(
    expression: ast.DynamicSliceExpr,
    inputs: dict[str, ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression:
    operand = context.expressions.check(expression.expression, inputs, None, context)
    if isinstance(operand.type, _AGGREGATE_TYPES) or not ir_packing.is_bit_packable(
        operand.type
    ):
        raise SemanticError(
            f"dynamic bit slice requires a packed scalar value, got {operand.type}"
        )
    width = compile_time_evaluation.resolve_range_bound(
        expression.width, context, "dynamic bit-slice width", inputs
    )
    if width < 1:
        raise SemanticError("dynamic bit-slice width must be positive")
    if width > operand.type.width:
        raise SemanticError(
            f"dynamic bit-slice width {width} exceeds packed width {operand.type.width}"
        )
    if (
        isinstance(expression.offset, ast.NameExpr)
        and expression.offset.name in context.scope.index_bindings
    ):
        offset = context.scope.index_bindings[expression.offset.name]
        return _constant_dynamic_slice(operand, offset, width)
    typed_offset = context.expressions.check(expression.offset, inputs, None, context)
    typed_offset = _expand_immutable_locals(
        typed_offset, inputs, work_budget=context.services
    )
    typed_offset = semantic_callables._expand_analysis_calls(
        typed_offset, context, purpose="dynamic packed slice offset"
    )
    if isinstance(typed_offset, ir_expr.Constant):
        return _constant_dynamic_slice(operand, typed_offset.value, width)
    if not isinstance(typed_offset.type, (ir_types.UIntType, ir_types.BitsType)):
        raise SemanticError(
            "dynamic bit-slice offset must be an unsigned integral "
            f"expression; got {typed_offset.type}"
        )
    value_range = expression_ranges.static_value_range(
        typed_offset, context.scope.range_refinements
    )
    if value_range is None:
        raise SemanticError(
            "dynamic bit-slice offset has no statically provable unsigned "
            f"range; required 0..{operand.type.width - width}"
        )
    if value_range.minimum < 0 or value_range.maximum + width > operand.type.width:
        raise SemanticError(
            "dynamic bit-slice offset range "
            f"{value_range.minimum}..{value_range.maximum} with width {width} "
            f"is not provably within packed width {operand.type.width} "
            f"(required 0..{operand.type.width - width}, type {typed_offset.type})"
        )
    result_type = ir_types.BitsType(width)
    if (
        expected is not None
        and expected != result_type
        and not can_implicitly_bitcast_types(result_type, expected)
    ):
        raise SemanticError(
            f"dynamic bit slice produces {result_type}, expected exact {expected}"
        )
    return _runtime_packed_slice(
        operand,
        typed_offset,
        width,
        description="dynamic bit slice",
    )


def _constant_dynamic_slice(
    operand: ir_expr.Expression,
    offset: int,
    width: int,
) -> ir_expr.Expression:
    if offset < 0 or offset + width > operand.type.width:
        raise SemanticError(
            f"dynamic bit slice [{offset} +: {width}] is out of range "
            f"for {operand.type} (width {operand.type.width})"
        )
    return ir_expr.Slice(
        operand,
        offset + width - 1,
        offset,
        ir_types.BitsType(width),
    )


__all__ = ["check_dynamic_slice", "check_packed_index", "check_static_slice"]
