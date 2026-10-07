"""Bounded, target-neutral structural multiplier candidates.

The semantic operation remains the ordinary typed ``Binary(MULTIPLY)``.  This
module only builds one exact, II=1 CSA implementation alternative for an
``implement`` search.  Keeping it as ordinary typed IR means the established
simulator, formal lowering and Direct-SV renderer remain the single semantic
authority; there is no backend-only arithmetic interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir import expressions as expr
from zlang.ir.types import BitsType, SIntType, UIntType


# The first structural family is deliberately bounded.  Wider products retain
# the native generic implementation and can be covered by a source-defined
# physical resource through the target planner.  This is not an iterative
# multiplier: every generated candidate is a purely combinational CSA tree.
MAX_CSA_MULTIPLIER_OPERAND_WIDTH = 16


@dataclass(frozen=True)
class StructuralMultiplierCandidate:
    """One exact generic-logic multiplier alternative and its cost facts."""

    expression: expr.Expression
    strategy: str
    partial_products: int
    compressor_count: int


def csa_multiplier_candidate(
    value: expr.Expression,
) -> StructuralMultiplierCandidate | None:
    """Build an exact CSA-tree alternative for one uniform integer product.

    The generated product uses a Wallace-style 3:2 carry-save reduction and a
    single final carry-propagate adder.  Signed products account for the
    multiplier sign bit explicitly, preserving the existing full-width two's
    complement multiply semantics.  Returning ``None`` is intentional and
    fail-closed for mixed, fixed-point, nested, or unbounded-width shapes.
    """

    if not (
        isinstance(value, expr.Binary)
        and value.operator is expr.BinaryOperator.MULTIPLY
        and type(value.left.type) is type(value.right.type) is type(value.type)
        and isinstance(value.type, (UIntType, SIntType))
        and value.type.width == value.left.type.width + value.right.type.width
        and max(value.left.type.width, value.right.type.width)
        <= MAX_CSA_MULTIPLIER_OPERAND_WIDTH
    ):
        return None

    result_type = value.type
    left = expr.Extend(value.left, result_type)
    zero = expr.Constant(0, result_type)
    rows: list[expr.Expression] = []
    right_width = value.right.type.width
    shift_type = UIntType(max(1, (right_width - 1).bit_length()))
    for bit in range(right_width):
        select = expr.Slice(value.right, bit, bit, BitsType(1))
        shifted = _shift(left, bit, shift_type, result_type)
        if isinstance(result_type, SIntType) and bit == right_width - 1:
            shifted = expr.Binary(
                expr.BinaryOperator.SUBTRACT,
                zero,
                shifted,
                result_type,
                result_type,
            )
        rows.append(expr.Mux(select, shifted, zero, result_type))

    compressors = 0
    while len(rows) > 2:
        reduced: list[expr.Expression] = []
        offset = 0
        while offset + 2 < len(rows):
            first, second, third = rows[offset : offset + 3]
            reduced.extend(_compress(first, second, third, result_type, shift_type))
            compressors += 1
            offset += 3
        reduced.extend(rows[offset:])
        rows = reduced
    assert rows
    product = rows[0] if len(rows) == 1 else expr.Add(rows[0], rows[1], result_type)
    return StructuralMultiplierCandidate(
        product,
        "multiply:csa_tree",
        right_width,
        compressors,
    )


def _shift(
    value: expr.Expression,
    amount: int,
    shift_type: UIntType,
    result_type: UIntType | SIntType,
) -> expr.Expression:
    if amount == 0:
        return value
    return expr.Binary(
        expr.BinaryOperator.SHIFT_LEFT,
        value,
        expr.Constant(amount, shift_type),
        result_type,
        result_type,
    )


def _compress(
    first: expr.Expression,
    second: expr.Expression,
    third: expr.Expression,
    result_type: UIntType | SIntType,
    shift_type: UIntType,
) -> tuple[expr.Expression, expr.Expression]:
    """Return the sum and shifted carry of one exact 3:2 compressor."""

    def xor(left: expr.Expression, right: expr.Expression) -> expr.Expression:
        return expr.Binary(
            expr.BinaryOperator.BIT_XOR, left, right, result_type, result_type
        )

    def and_(left: expr.Expression, right: expr.Expression) -> expr.Expression:
        return expr.Binary(
            expr.BinaryOperator.BIT_AND, left, right, result_type, result_type
        )

    def or_(left: expr.Expression, right: expr.Expression) -> expr.Expression:
        return expr.Binary(
            expr.BinaryOperator.BIT_OR, left, right, result_type, result_type
        )
    summed = xor(xor(first, second), third)
    carry_bits = or_(or_(and_(first, second), and_(first, third)), and_(second, third))
    return summed, _shift(carry_bits, 1, shift_type, result_type)


__all__ = [
    "MAX_CSA_MULTIPLIER_OPERAND_WIDTH",
    "StructuralMultiplierCandidate",
    "csa_multiplier_candidate",
]
