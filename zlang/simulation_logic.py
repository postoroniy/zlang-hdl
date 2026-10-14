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


__all__ = ["LogicBit", "LogicVector"]
