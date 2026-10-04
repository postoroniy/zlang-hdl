# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Canonical compile-time integer intrinsics shared by semantic services."""

from __future__ import annotations

from .errors import SemanticError


INTEGER_INTRINSICS = frozenset({
    "floor_log2", "ceil_log2", "index_width", "is_power_of_two",
})


def evaluate_integer_intrinsic(name: str, value: int) -> int:
    """Evaluate one canonical compile-time integer intrinsic."""

    if name == "is_power_of_two":
        return int(value > 0 and (value & (value - 1)) == 0)
    if value <= 0:
        raise SemanticError(f"{name} requires a positive integer")
    if name == "floor_log2":
        return value.bit_length() - 1
    if name == "ceil_log2":
        return max(0, (value - 1).bit_length())
    if name == "index_width":
        return max(1, (value - 1).bit_length())
    raise SemanticError(f"unknown integer intrinsic '{name}'")


__all__ = ["INTEGER_INTRINSICS", "evaluate_integer_intrinsic"]
