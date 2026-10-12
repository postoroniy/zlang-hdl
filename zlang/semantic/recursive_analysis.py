"""Narrow recursive module-analysis contract used by hierarchy elaboration."""

from __future__ import annotations

from typing import Protocol

from zlang.ast import nodes as ast
from zlang.ir import module as ir_module


class RecursiveModuleAnalyzer(Protocol):
    """Analyze one already-resolved child through the same semantic owner."""

    def analyze_module(
        self,
        module: ast.Module,
        **options: object,
    ) -> ir_module.Module: ...


__all__ = ["RecursiveModuleAnalyzer"]
