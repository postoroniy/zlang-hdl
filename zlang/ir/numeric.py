"""Exact backend-independent scalar numeric type rules.

This module owns finite-width built-in operator typing only.  It does not
perform overload resolution, insert conversions, reshape expression trees, or
define diagnostic wording; callers translate structured failures at their
existing language boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
)


class NumericTypeErrorReason(str, Enum):
    FAMILY_MISMATCH = "family_mismatch"
    FRACTION_MISMATCH = "fraction_mismatch"
    NOMINAL_ENUM_MISMATCH = "nominal_enum_mismatch"
    ORDERED_ENUM = "ordered_enum"
    ORDERED_BIT = "ordered_bit"
    ORDERED_BITS = "ordered_bits"
    UNSUPPORTED = "unsupported"


class NumericTypeError(ValueError):
    """A pair of canonical hardware types has no requested built-in rule."""

    def __init__(
        self,
        reason: NumericTypeErrorReason,
        left: HardwareType,
        right: HardwareType,
    ) -> None:
        self.reason = reason
        self.left = left
        self.right = right
        super().__init__(f"{reason.value}: {left} and {right}")


@dataclass(frozen=True)
class BinaryTypeRule:
    """Exact common operand and result types for one built-in operation."""

    operand_type: HardwareType
    result_type: HardwareType


def _matching_family(
    left: HardwareType,
    right: HardwareType,
) -> None:
    if type(left) is not type(right):
        raise NumericTypeError(
            NumericTypeErrorReason.FAMILY_MISMATCH,
            left,
            right,
        )


def addition_rule(
    left: HardwareType,
    right: HardwareType,
) -> BinaryTypeRule:
    """Return the exact widened built-in addition rule."""

    _matching_family(left, right)
    if isinstance(left, UIntType):
        result: HardwareType = UIntType(max(left.width, right.width) + 1)
    elif isinstance(left, SIntType):
        result = SIntType(max(left.width, right.width) + 1)
    elif isinstance(left, (FixedType, UFixedType)):
        if left.fraction != right.fraction:
            raise NumericTypeError(
                NumericTypeErrorReason.FRACTION_MISMATCH,
                left,
                right,
            )
        # Arithmetic intermediates are exact/widened and therefore use the
        # canonical wrap policy; storage targets own any later saturation.
        result = type(left)(max(left.width, right.width) + 1, left.fraction)
    else:
        raise NumericTypeError(NumericTypeErrorReason.UNSUPPORTED, left, right)
    return BinaryTypeRule(result, result)


def subtraction_rule(
    left: HardwareType,
    right: HardwareType,
) -> BinaryTypeRule:
    """Return exact signed/fixed subtraction or modular unsigned subtraction."""

    _matching_family(left, right)
    if isinstance(left, UIntType):
        result: HardwareType = UIntType(max(left.width, right.width))
    elif isinstance(left, SIntType):
        result = SIntType(max(left.width, right.width) + 1)
    elif isinstance(left, (FixedType, UFixedType)):
        if left.fraction != right.fraction:
            raise NumericTypeError(
                NumericTypeErrorReason.FRACTION_MISMATCH,
                left,
                right,
            )
        carry = 1 if isinstance(left, FixedType) else 0
        result = type(left)(max(left.width, right.width) + carry, left.fraction)
    else:
        raise NumericTypeError(NumericTypeErrorReason.UNSUPPORTED, left, right)
    return BinaryTypeRule(result, result)


def multiplication_rule(
    left: HardwareType,
    right: HardwareType,
) -> BinaryTypeRule:
    """Return the full-precision built-in multiplication rule."""

    _matching_family(left, right)
    if isinstance(left, UIntType):
        result: HardwareType = UIntType(left.width + right.width)
    elif isinstance(left, SIntType):
        result = SIntType(left.width + right.width)
    elif isinstance(left, (FixedType, UFixedType)):
        result = type(left)(
            left.width + right.width,
            left.fraction + right.fraction,
        )
    else:
        raise NumericTypeError(NumericTypeErrorReason.UNSUPPORTED, left, right)
    return BinaryTypeRule(result, result)


def bitwise_rule(
    left: HardwareType,
    right: HardwareType,
) -> BinaryTypeRule:
    """Return the common representation-preserving bitwise type."""

    _matching_family(left, right)
    if isinstance(left, BitType):
        result: HardwareType = BitType()
    elif isinstance(left, UIntType):
        result = UIntType(max(left.width, right.width))
    elif isinstance(left, SIntType):
        result = SIntType(max(left.width, right.width))
    elif isinstance(left, BitsType):
        result = BitsType(max(left.width, right.width))
    else:
        raise NumericTypeError(NumericTypeErrorReason.UNSUPPORTED, left, right)
    return BinaryTypeRule(result, result)


def comparison_rule(
    left: HardwareType,
    right: HardwareType,
    *,
    equality: bool,
) -> BinaryTypeRule:
    """Return exact comparison operand type and the one-bit result type."""

    _matching_family(left, right)
    if isinstance(left, EnumType):
        if left != right:
            raise NumericTypeError(
                NumericTypeErrorReason.NOMINAL_ENUM_MISMATCH,
                left,
                right,
            )
        if not equality:
            raise NumericTypeError(
                NumericTypeErrorReason.ORDERED_ENUM,
                left,
                right,
            )
        operand: HardwareType = left
    elif isinstance(left, BitType):
        if not equality:
            raise NumericTypeError(
                NumericTypeErrorReason.ORDERED_BIT,
                left,
                right,
            )
        operand = BitType()
    elif isinstance(left, UIntType):
        operand = UIntType(max(left.width, right.width))
    elif isinstance(left, SIntType):
        operand = SIntType(max(left.width, right.width))
    elif isinstance(left, (FixedType, UFixedType)):
        if left.fraction != right.fraction:
            raise NumericTypeError(
                NumericTypeErrorReason.FRACTION_MISMATCH,
                left,
                right,
            )
        operand = type(left)(max(left.width, right.width), left.fraction)
    elif isinstance(left, BitsType):
        if not equality:
            raise NumericTypeError(
                NumericTypeErrorReason.ORDERED_BITS,
                left,
                right,
            )
        operand = BitsType(max(left.width, right.width))
    else:
        raise NumericTypeError(NumericTypeErrorReason.UNSUPPORTED, left, right)
    return BinaryTypeRule(operand, BitType())


__all__ = [
    "BinaryTypeRule",
    "NumericTypeError",
    "NumericTypeErrorReason",
    "addition_rule",
    "bitwise_rule",
    "comparison_rule",
    "multiplication_rule",
    "subtraction_rule",
]
