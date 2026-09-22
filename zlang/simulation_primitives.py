"""Small representation-neutral primitives shared by simulation layers."""

from __future__ import annotations

from collections.abc import Iterable

from zlang.ir import expressions as expr
from zlang.ir.types import BitType


def int_from_limbs(limbs: Iterable[int]) -> int:
    """Reconstruct an unsigned integer from little-endian 64-bit limbs."""

    return sum(int(limb) << (64 * index) for index, limb in enumerate(limbs))


def int_to_limbs(value: int, width: int) -> list[int]:
    """Split an unsigned integer into the exact-width limb representation."""

    return [
        (value >> offset) & ((1 << 64) - 1)
        for offset in range(0, width, 64)
    ]


def bit_binary(
    operator: expr.BinaryOperator,
    left: expr.Expression,
    right: expr.Expression,
) -> expr.Binary:
    """Build one exact one-bit binary expression."""

    bit = BitType()
    return expr.Binary(operator, left, right, bit, bit)


def bit_not(value: expr.Expression) -> expr.Binary:
    """Build logical inversion in the primitive one-bit value algebra."""

    return bit_binary(
        expr.BinaryOperator.BIT_XOR,
        value,
        expr.Constant(1, BitType()),
    )


__all__ = ["bit_binary", "bit_not", "int_from_limbs", "int_to_limbs"]
