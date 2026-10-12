# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Language-neutral primitive simulation vocabulary."""

from __future__ import annotations

from typing import Any, Protocol, Self


class PrimitiveLoweringError(ValueError):
    """The compiler produced a semantic plan that cannot be made primitive."""


class PrimitiveBuilder(Protocol):
    """Common bit-vector construction surface used by primitive lowerers."""

    semantic_nodes: list[dict[str, Any]]
    semantic_memories: dict[str, dict[str, Any]]
    semantic_fifos: dict[str, dict[str, Any]]
    nodes: list[dict[str, Any]]
    lowered: dict[int, int]
    regions: list[dict[str, Any]]
    capture_slots: dict[str, int] | None
    binder_scope: dict[str, int]
    table_scope: dict[str, tuple[int, tuple[int, ...]]]
    reset_release_clocks: dict[str, str]

    def spawn_region_builder(self, capture_slots: dict[str, int]) -> Self: ...

    def emit(
        self,
        op: str,
        width: int,
        operands: tuple[int, ...] = (),
        attributes: dict[str, Any] | None = None,
        origins: list[dict[str, Any]] | None = None,
    ) -> int: ...

    def lower(self, identifier: int) -> int: ...

    def width(self, identifier: int) -> int: ...

    def constant(self, value: int, width: int) -> int: ...

    def event(self, clock: str) -> int: ...

    def truthy(self, identifier: int) -> int: ...

    def resize(self, identifier: int, width: int, *, signed: bool = False) -> int: ...

    def unary(self, op: str, operand: int, width: int | None = None) -> int: ...

    def binary(self, op: str, left: int, right: int, width: int) -> int: ...

    def select(
        self,
        condition: int,
        when_true: int,
        when_false: int,
        width: int,
    ) -> int: ...

    def merge_masked_value(
        self,
        old: int,
        new: int,
        mask: int | None,
        *,
        width: int,
        mask_width: int | None,
    ) -> int: ...

    def _signed(self, type_: dict[str, Any]) -> bool: ...


class PrimitiveLowerer:
    """Base owner for lowering services sharing one primitive builder."""

    def __init__(self, builder: PrimitiveBuilder) -> None:
        self._builder = builder


PRIMITIVE_OPS = frozenset(
    {
        "constant",
        "load_input",
        "load_state",
        "load_event",
        "load_memory",
        "add",
        "sub",
        "mul",
        "and",
        "or",
        "xor",
        "not",
        "shl",
        "lshr",
        "ashr",
        "eq",
        "ult",
        "ule",
        "slt",
        "sle",
        "select",
        "extract_bits",
        "insert_bits",
        "concat_bits",
        "load_capture",
        "load_index",
        "loop_region",
    }
)


__all__ = ["PRIMITIVE_OPS", "PrimitiveBuilder", "PrimitiveLowerer", "PrimitiveLoweringError"]
