# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Immutable semantic symbols for compiler-owned storage declarations.

The records in this module are shared by declaration analysis, expression
typing, and observation projection.  They deliberately contain no validation
or orchestration behavior, so those consumers do not need to import the
storage-analysis pass merely to name an already-resolved symbol.
"""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ast import nodes as ast
from zlang.ir import types as ir_types


@dataclass(frozen=True)
class FifoSymbol:
    name: str
    element_type: ir_types.HardwareType
    depth: int
    domain: str

    @property
    def count_width(self) -> int:
        return max(1, self.depth.bit_length())


@dataclass(frozen=True)
class MemoryPortSymbol:
    name: str
    kind: ast.MemoryPortKind
    domain: str


@dataclass(frozen=True)
class MemorySymbol:
    name: str
    element_type: ir_types.HardwareType
    depth: int
    domain: str
    ports: tuple[MemoryPortSymbol, ...] = ()
    async_memory: bool = False

    @property
    def address_width(self) -> int:
        return max(1, (self.depth - 1).bit_length())


@dataclass(frozen=True)
class RomSymbol:
    name: str
    element_type: ir_types.HardwareType
    depth: int
    domain: str

    @property
    def address_width(self) -> int:
        return max(1, (self.depth - 1).bit_length())


__all__ = ["FifoSymbol", "MemoryPortSymbol", "MemorySymbol", "RomSymbol"]
