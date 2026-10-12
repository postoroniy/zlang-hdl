# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable per-bit 0/1/U/X values for opt-in native simulation.

The representation uses two bit planes.  ``unknown`` marks non-binary bits;
within that plane ``value`` distinguishes U (0) from X (1).  This keeps the
ordinary two-state simulator independent while making controlling-value
resolution explicit and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LogicBit(str, Enum):
    ZERO = "0"
    ONE = "1"
    UNINITIALIZED = "u"
    UNKNOWN = "x"


@dataclass(frozen=True)
class LogicVector:
    """One width-exact packed four-state value, indexed least-significant bit."""

    width: int
    value: int
    unknown: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.width, bool) or not isinstance(self.width, int) or self.width < 1:
            raise ValueError("logic-vector width must be a positive integer")
        mask = self.mask
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0 or item & ~mask
            for item in (self.value, self.unknown)
        ):
            raise ValueError(f"logic-vector planes must fit {self.width} bits")

    @property
    def mask(self) -> int:
        return (1 << self.width) - 1

    @property
    def is_binary(self) -> bool:
        return self.unknown == 0

    @property
    def has_uninitialized(self) -> bool:
        return bool(self.unknown & ~self.value)

    @property
    def has_unknown(self) -> bool:
        return bool(self.unknown & self.value)

    @classmethod
    def known(cls, value: int, width: int) -> "LogicVector":
        return cls(width, value & ((1 << width) - 1))

    @classmethod
    def filled(cls, bit: LogicBit, width: int) -> "LogicVector":
        mask = (1 << width) - 1
        if bit is LogicBit.ZERO:
            return cls(width, 0, 0)
        if bit is LogicBit.ONE:
            return cls(width, mask, 0)
        if bit is LogicBit.UNINITIALIZED:
            return cls(width, 0, mask)
        return cls(width, mask, mask)

    @classmethod
    def parse(cls, spelling: str, width: int) -> "LogicVector":
        if not isinstance(spelling, str) or not spelling:
            raise ValueError("logic value must contain 0, 1, u, or x")
        normalized = spelling.lower().replace("_", "")
        if len(normalized) == 1:
            normalized *= width
        if len(normalized) != width:
            raise ValueError(
                f"logic value requires one broadcast character or exactly {width} bits"
            )
        invalid = next((item for item in normalized if item not in "01ux"), None)
        if invalid is not None:
            raise ValueError(f"invalid logic-state character '{invalid}'")
        value = 0
        unknown = 0
        for index, item in enumerate(reversed(normalized)):
            bit = 1 << index
            if item in "1x":
                value |= bit
            if item in "ux":
                unknown |= bit
        return cls(width, value, unknown)

    def to_bits(self) -> str:
        result: list[str] = []
        for index in reversed(range(self.width)):
            bit = 1 << index
            if not self.unknown & bit:
                result.append("1" if self.value & bit else "0")
            else:
                result.append("x" if self.value & bit else "u")
        return "".join(result)

    def require_binary(self, *, name: str = "value") -> int:
        if self.unknown:
            kinds = []
            if self.has_uninitialized:
                kinds.append("U (never refreshed)")
            if self.has_unknown:
                kinds.append("X (computed unknown)")
            raise ValueError(f"{name} contains {' and '.join(kinds)} bits")
        return self.value

    def accepted_write(self) -> "LogicVector":
        """Convert written U bits to X while retaining known and existing X bits."""

        return LogicVector(self.width, self.value | self.unknown, self.unknown)

    def resize(self, width: int, *, signed: bool = False) -> "LogicVector":
        if width <= self.width:
            mask = (1 << width) - 1
            return LogicVector(width, self.value & mask, self.unknown & mask)
        extension = width - self.width
        if not signed:
            return LogicVector(width, self.value, self.unknown)
        sign = self.extract(self.width - 1, 1)
        high = LogicVector.filled(_single_bit(sign), extension)
        return high.concat(self)

    def extract(self, offset: int, width: int) -> "LogicVector":
        mask = (1 << width) - 1
        return LogicVector(width, (self.value >> offset) & mask, (self.unknown >> offset) & mask)

    def concat(self, low: "LogicVector") -> "LogicVector":
        return LogicVector(
            self.width + low.width,
            (self.value << low.width) | low.value,
            (self.unknown << low.width) | low.unknown,
        )


def _single_bit(value: LogicVector) -> LogicBit:
    if value.width != 1:
        raise ValueError("expected one logic bit")
    if not value.unknown:
        return LogicBit.ONE if value.value else LogicBit.ZERO
    return LogicBit.UNKNOWN if value.value else LogicBit.UNINITIALIZED


def _unknown_kind(*values: LogicVector) -> LogicBit:
    return (
        LogicBit.UNKNOWN
        if any(value.has_unknown for value in values)
        else LogicBit.UNINITIALIZED
    )


def logic_not(value: LogicVector) -> LogicVector:
    return LogicVector(value.width, (value.value ^ ~value.unknown) & value.mask, value.unknown)


def logic_and(left: LogicVector, right: LogicVector) -> LogicVector:
    mask = left.mask
    left_known_zero = ~left.unknown & ~left.value & mask
    right_known_zero = ~right.unknown & ~right.value & mask
    left_known_one = ~left.unknown & left.value & mask
    right_known_one = ~right.unknown & right.value & mask
    known_zero = left_known_zero | right_known_zero
    known_one = left_known_one & right_known_one
    unknown = mask & ~(known_zero | known_one)
    x_sources = (left.unknown & left.value) | (right.unknown & right.value)
    return LogicVector(left.width, known_one | (unknown & x_sources), unknown)


def logic_or(left: LogicVector, right: LogicVector) -> LogicVector:
    mask = left.mask
    left_known_zero = ~left.unknown & ~left.value & mask
    right_known_zero = ~right.unknown & ~right.value & mask
    left_known_one = ~left.unknown & left.value & mask
    right_known_one = ~right.unknown & right.value & mask
    known_one = left_known_one | right_known_one
    known_zero = left_known_zero & right_known_zero
    unknown = mask & ~(known_zero | known_one)
    x_sources = (left.unknown & left.value) | (right.unknown & right.value)
    return LogicVector(left.width, known_one | (unknown & x_sources), unknown)


def logic_xor(left: LogicVector, right: LogicVector) -> LogicVector:
    unknown = left.unknown | right.unknown
    known_value = (left.value ^ right.value) & ~unknown
    x_sources = (left.unknown & left.value) | (right.unknown & right.value)
    return LogicVector(left.width, known_value | (unknown & x_sources), unknown)


def logic_select(condition: LogicVector, yes: LogicVector, no: LogicVector) -> LogicVector:
    truth = logic_truthy(condition)
    if truth.is_binary:
        return yes if truth.value else no
    same = ~((yes.unknown ^ no.unknown) | (yes.value ^ no.value)) & yes.mask
    value = yes.value & same
    unknown = yes.unknown & same
    differing = yes.mask & ~same
    unknown |= differing
    differing_x = (
        truth.has_unknown
        or bool((yes.unknown & yes.value) & differing)
        or bool((no.unknown & no.value) & differing)
    )
    if differing_x:
        value |= differing
    return LogicVector(yes.width, value, unknown)


def logic_truthy(value: LogicVector) -> LogicVector:
    known_one = value.value & ~value.unknown
    if known_one:
        return LogicVector.known(1, 1)
    if not value.unknown:
        return LogicVector.known(0, 1)
    return LogicVector.filled(_unknown_kind(value), 1)


def logic_add(left: LogicVector, right: LogicVector, width: int) -> LogicVector:
    if left.is_binary and right.is_binary:
        return LogicVector.known(left.value + right.value, width)
    carry = LogicVector.known(0, 1)
    bits: list[LogicVector] = []
    for index in range(width):
        a = left.extract(index, 1) if index < left.width else LogicVector.known(0, 1)
        b = right.extract(index, 1) if index < right.width else LogicVector.known(0, 1)
        bits.append(logic_xor(logic_xor(a, b), carry))
        carry = logic_or(logic_and(a, b), logic_and(carry, logic_xor(a, b)))
    return concat_lsb(bits)


def logic_sub(left: LogicVector, right: LogicVector, width: int) -> LogicVector:
    inverted = logic_not(right.resize(width))
    return logic_add(logic_add(left.resize(width), inverted, width), LogicVector.known(1, width), width)


def logic_mul(left: LogicVector, right: LogicVector, width: int) -> LogicVector:
    if left.is_binary and right.is_binary:
        return LogicVector.known(left.value * right.value, width)
    result = LogicVector.known(0, width)
    multiplicand = left.resize(width)
    for index in range(min(right.width, width)):
        bit = right.extract(index, 1)
        shifted = logic_shift_known(multiplicand, index, width, left=True, arithmetic=False)
        result = logic_add(result, logic_select(bit, shifted, LogicVector.known(0, width)), width)
    return result


def logic_shift_known(
    value: LogicVector, amount: int, width: int, *, left: bool, arithmetic: bool
) -> LogicVector:
    source = value.resize(width)
    if amount >= width:
        if arithmetic:
            return LogicVector.filled(_single_bit(source.extract(width - 1, 1)), width)
        return LogicVector.known(0, width)
    if left:
        return LogicVector(width, source.value << amount & source.mask, source.unknown << amount & source.mask)
    if not arithmetic:
        return LogicVector(width, source.value >> amount, source.unknown >> amount)
    low = source.extract(amount, width - amount)
    high = LogicVector.filled(_single_bit(source.extract(width - 1, 1)), amount)
    return high.concat(low)


def logic_shift(
    value: LogicVector, amount: LogicVector, width: int, *, left: bool, arithmetic: bool
) -> LogicVector:
    if amount.is_binary:
        return logic_shift_known(value, amount.value, width, left=left, arithmetic=arithmetic)
    result = value.resize(width)
    for index in range(amount.width):
        shifted = logic_shift_known(result, 1 << index, width, left=left, arithmetic=arithmetic)
        result = logic_select(amount.extract(index, 1), shifted, result)
    return result


def logic_equal(left: LogicVector, right: LogicVector) -> LogicVector:
    mismatch = (left.value ^ right.value) & ~(left.unknown | right.unknown)
    if mismatch:
        return LogicVector.known(0, 1)
    if not (left.unknown | right.unknown):
        return LogicVector.known(1, 1)
    return LogicVector.filled(_unknown_kind(left, right), 1)


def logic_compare(
    left: LogicVector, right: LogicVector, *, signed: bool, or_equal: bool
) -> LogicVector:
    a = left
    b = right
    if signed:
        sign = 1 << (left.width - 1)
        a = LogicVector(left.width, left.value ^ sign, left.unknown)
        b = LogicVector(right.width, right.value ^ sign, right.unknown)
    equal = LogicVector.known(1, 1)
    less = LogicVector.known(0, 1)
    for index in reversed(range(left.width)):
        ai = a.extract(index, 1)
        bi = b.extract(index, 1)
        less = logic_or(less, logic_and(equal, logic_and(logic_not(ai), bi)))
        equal = logic_and(equal, logic_not(logic_xor(ai, bi)))
    return logic_or(less, equal) if or_equal else less


def concat_lsb(parts: list[LogicVector]) -> LogicVector:
    result = parts[-1]
    for part in reversed(parts[:-1]):
        result = result.concat(part)
    return result


__all__ = ["LogicBit", "LogicVector"]
