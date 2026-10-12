"""Local exact-typed simplification shared by semantic and selected IR.

The rules in this module need no equality saturation: each replacement is
decided from the already canonical operand/result types and one local node.
No reassociation, distributivity, or width-changing integer assumption is
permitted here.
"""

from __future__ import annotations

from collections.abc import Callable

from zlang.ir import expressions as expr
from zlang.ir.types import (
    BitsType,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
)


RangeProvider = Callable[[expr.Expression], tuple[int, int] | None]


def static_unsigned_range(value: expr.Expression) -> tuple[int, int] | None:
    """Return one conservative unsigned scalar interval, if locally provable."""

    if isinstance(value, expr.Constant) and isinstance(value.type, (UIntType, BitsType)):
        return value.value, value.value
    if isinstance(
        value,
        (expr.InputRef, expr.ParameterRef, expr.RegisterRef),
    ) and isinstance(value.type, (UIntType, BitsType)):
        return 0, (1 << value.type.width) - 1
    if isinstance(value, expr.Extend):
        return static_unsigned_range(value.expression)
    if isinstance(value, expr.Truncate) and isinstance(value.type, (UIntType, BitsType)):
        return 0, (1 << value.type.width) - 1
    if isinstance(value, expr.Add) and isinstance(value.type, UIntType):
        left = static_unsigned_range(value.left)
        right = static_unsigned_range(value.right)
        if left is not None and right is not None:
            return left[0] + right[0], left[1] + right[1]
    if isinstance(value, expr.Binary) and isinstance(value.type, (UIntType, BitsType)):
        left = static_unsigned_range(value.left)
        right = static_unsigned_range(value.right)
        if left is None or right is None:
            return None
        if value.operator is expr.BinaryOperator.MULTIPLY:
            return left[0] * right[0], left[1] * right[1]
        if value.operator is expr.BinaryOperator.SUBTRACT:
            if left[0] >= right[1]:
                return left[0] - right[1], left[1] - right[0]
            return 0, (1 << value.type.width) - 1
        if right[0] == right[1]:
            if value.operator is expr.BinaryOperator.SHIFT_RIGHT:
                return left[0] >> right[0], left[1] >> right[0]
            if value.operator is expr.BinaryOperator.SHIFT_LEFT:
                maximum = left[1] << right[0]
                if maximum < (1 << value.type.width):
                    return left[0] << right[0], maximum
    return None


def exact_numeric_widen(
    value: expr.Expression,
    result_type: HardwareType,
) -> expr.Expression | None:
    """Preserve one value at a provably compatible wider numeric boundary."""

    if value.type == result_type:
        return value
    if type(value.type) is not type(result_type):
        return None
    if not isinstance(
        value.type,
        (UIntType, SIntType, FixedType, UFixedType),
    ) or not isinstance(
        result_type,
        (UIntType, SIntType, FixedType, UFixedType),
    ):
        return None
    if value.type.width > result_type.width:
        return None
    if isinstance(value.type, (FixedType, UFixedType)) and (
        value.type.fraction != result_type.fraction
    ):
        return None
    return expr.Extend(value, result_type)


def simplify_add(
    left: expr.Expression,
    right: expr.Expression,
    result_type: HardwareType,
) -> expr.Expression | None:
    """Simplify a typed add without changing its carry-width contract."""

    if isinstance(left, expr.Constant) and left.value == 0:
        return exact_numeric_widen(right, result_type)
    if isinstance(right, expr.Constant) and right.value == 0:
        return exact_numeric_widen(left, result_type)
    return None


def _all_ones(value: expr.Constant) -> bool:
    expected = -1 if isinstance(value.type, SIntType) else (1 << value.type.width) - 1
    return value.value == expected


def simplify_binary(
    operator: expr.BinaryOperator,
    left: expr.Expression,
    right: expr.Expression,
    result_type: HardwareType,
    *,
    range_of: RangeProvider | None = None,
) -> expr.Expression | None:
    """Apply one local identity proven exact by operand and result types."""

    left_constant = left.value if isinstance(left, expr.Constant) else None
    right_constant = right.value if isinstance(right, expr.Constant) else None
    if operator is expr.BinaryOperator.MULTIPLY:
        if left_constant == 0 or right_constant == 0:
            return expr.Constant(0, result_type)
        if isinstance(result_type, (UIntType, SIntType)):
            if left_constant == 1:
                return exact_numeric_widen(right, result_type)
            if right_constant == 1:
                return exact_numeric_widen(left, result_type)
    if operator is expr.BinaryOperator.SUBTRACT and right_constant == 0:
        return exact_numeric_widen(left, result_type)
    if operator in {
        expr.BinaryOperator.BIT_AND,
        expr.BinaryOperator.BIT_OR,
        expr.BinaryOperator.BIT_XOR,
    }:
        if operator is expr.BinaryOperator.BIT_AND and (
            left_constant == 0 or right_constant == 0
        ):
            return expr.Constant(0, result_type)
        if operator in {
            expr.BinaryOperator.BIT_OR,
            expr.BinaryOperator.BIT_XOR,
        }:
            if left_constant == 0 and right.type == result_type:
                return right
            if right_constant == 0 and left.type == result_type:
                return left
        if operator is expr.BinaryOperator.BIT_AND:
            if isinstance(left, expr.Constant) and _all_ones(left) and right.type == result_type:
                return right
            if isinstance(right, expr.Constant) and _all_ones(right) and left.type == result_type:
                return left
    if operator in {
        expr.BinaryOperator.SHIFT_LEFT,
        expr.BinaryOperator.SHIFT_RIGHT,
    } and right_constant == 0 and left.type == result_type:
        return left
    if (
        range_of is not None
        and operator
        in {expr.BinaryOperator.EQUAL, expr.BinaryOperator.NOT_EQUAL}
    ):
        left_range = range_of(left)
        right_range = range_of(right)
        if (
            left_range is not None
            and right_range is not None
            and (
                left_range[1] < right_range[0]
                or right_range[1] < left_range[0]
            )
        ):
            return expr.Constant(
                int(operator is expr.BinaryOperator.NOT_EQUAL),
                result_type,
            )
    return None


def simplify_mux(
    condition: expr.Expression,
    when_true: expr.Expression,
    when_false: expr.Expression,
) -> expr.Expression | None:
    if isinstance(condition, expr.Constant):
        return when_true if condition.value else when_false
    if when_true == when_false:
        return when_true
    return None


__all__ = [
    "exact_numeric_widen",
    "simplify_add",
    "simplify_binary",
    "simplify_mux",
    "static_unsigned_range",
]
