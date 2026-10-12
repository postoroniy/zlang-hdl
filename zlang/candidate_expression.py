# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Shared typed-expression traversal for implementation candidates."""

from __future__ import annotations

from zlang.ir import expressions as ir_expr
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.traversal import ExpressionTraversalPolicy, expression_children


def candidate_expression_children(
    value: ir_expr.Expression,
) -> tuple[ir_expr.Expression, ...]:
    """Traverse the exact implementation selected by compiler candidate policy."""

    if isinstance(value, ir_expr.Reduce):
        return (value.collection,)
    return expression_children(
        value,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    )


def candidate_input_refs(value: ir_expr.Expression) -> dict[str, object]:
    """Collect consistently typed inputs from one selected candidate graph."""

    result: dict[str, object] = {}
    graph = ExpressionDagIndex((value,), children=candidate_expression_children)
    for item in graph.preorder():
        if isinstance(item, ir_expr.InputRef):
            previous = result.get(item.name)
            if previous is not None and previous != item.type:
                raise ValueError(f"input '{item.name}' has inconsistent types")
            result[item.name] = item.type
    return result


__all__ = ["candidate_expression_children", "candidate_input_refs"]
