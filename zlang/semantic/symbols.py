# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Shared semantic value-symbol contract for expression-facing services."""

from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module

from .storage_symbols import FifoSymbol, MemorySymbol, RomSymbol

ValueSymbol = (
    ir_module.Port
    | ir_module.RequestResponseInterface
    | ir_module.FunctionParameter
    | ir_module.Register
    | FifoSymbol
    | MemorySymbol
    | RomSymbol
    | ir_module.LocalValue
    | ir_expr.Expression
)
