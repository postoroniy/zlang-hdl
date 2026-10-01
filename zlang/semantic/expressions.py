# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Stable expression-checking boundary shared by semantic orchestration."""

from __future__ import annotations

from collections.abc import Callable

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir.types import HardwareType
from zlang.source import SourceOrigin

from .errors import SemanticError


def check_expression(
    checker: Callable[
        [ast.Expression, dict[str, object], HardwareType | None, object],
        ir_expr.Expression,
    ],
    expression: ast.Expression,
    inputs: dict[str, object],
    expected: HardwareType | None,
    context: object,
    origin_provider: Callable[[ast.Expression, object], SourceOrigin | None],
) -> ir_expr.Expression:
    """Type one expression and attach its exact compiler-owned origin."""

    try:
        return checker(expression, inputs, expected, context)
    except SemanticError as error:
        if error.primary is not None:
            raise
        primary = origin_provider(expression, context)
        if primary is None:
            raise
        raise SemanticError(
            str(error),
            code=error.code,
            primary=primary,
            notes=error.notes,
            fixes=error.fixes,
            machine_fixes=error.machine_fixes,
        ) from error


__all__ = ["check_expression"]
