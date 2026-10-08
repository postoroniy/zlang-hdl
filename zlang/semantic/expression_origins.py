# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Exact compiler-owned source origins for expressions and callable names."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.source import SourceOrigin

if TYPE_CHECKING:
    from .context import ExpressionContext


def _render_type_syntax(syntax: ast.TypeSyntax) -> str:
    """Render recursive source type syntax for one expression origin."""

    if isinstance(syntax, ast.TypeName):
        return syntax.text
    if isinstance(syntax, ast.VectorTypeName):
        return f"vec<{syntax.length},{_render_type_syntax(syntax.element_type)}>"
    if isinstance(syntax, ast.TupleTypeName):
        return f"({','.join(_render_type_syntax(item) for item in syntax.elements)})"
    raise TypeError(f"unsupported source type syntax {type(syntax).__name__}")


def semantic_origin(
    expression: ast.Expression,
    context: ExpressionContext | None = None,
) -> SourceOrigin | None:
    if expression.origin is None:
        return None
    if isinstance(expression, ast.NameExpr):
        construct = f"name {expression.name}"
    elif isinstance(expression, ast.NumberExpr):
        construct = f"literal {expression.value}"
    elif isinstance(expression, ast.CharLiteralExpr):
        construct = f"character literal 0x{expression.value:02x}"
    elif isinstance(expression, ast.StringLiteralExpr):
        construct = f"string literal {len(expression.values)} bytes"
    elif isinstance(expression, ast.TupleLiteralExpr):
        construct = f"tuple literal arity {len(expression.elements)}"
    elif (
        isinstance(expression, ast.UnaryExpr)
        and expression.operator is ast.BinaryOperator.SUBTRACT
        and isinstance(expression.expression, ast.NumberExpr)
    ):
        construct = f"literal {-expression.expression.value}"
    elif isinstance(expression, ast.PatternConstantExpr):
        construct = f"{expression.kind.value}<{expression.witness}>"
    elif isinstance(expression, ast.AddExpr):
        construct = "operator +"
    elif isinstance(expression, ast.BinaryExpr):
        construct = f"operator {expression.operator.value}"
    elif isinstance(expression, ast.ResizeExpr):
        construct = (
            expression.kind.value
            if expression.width is None
            else f"{expression.kind.value}<{expression.width}>"
        )
    elif isinstance(expression, ast.MuxExpr):
        construct = "mux"
    elif isinstance(expression, ast.SwitchExpr):
        construct = "switch"
    elif isinstance(expression, ast.CallExpr):
        construct = f"call {expression.function}"
    elif isinstance(expression, ast.VectorLiteralExpr):
        construct = "vector literal"
    elif isinstance(expression, ast.StructUpdateExpr):
        construct = "struct update"
    elif isinstance(expression, ast.FieldExpr):
        enum_type = (
            context.environment.type_resolver.enum_type(expression.expression.name)
            if context is not None
            and context.environment.type_resolver is not None
            and isinstance(expression.expression, ast.NameExpr)
            else None
        )
        construct = (
            f"enum member {enum_type.name}.{expression.field}"
            if enum_type is not None else f"field .{expression.field}"
        )
    elif isinstance(expression, ast.IndexExpr):
        if isinstance(expression.index, int):
            index_text = str(expression.index)
        elif isinstance(expression.index, ast.NameExpr):
            index_text = expression.index.name
        elif isinstance(expression.index, ast.NumberExpr):
            index_text = str(expression.index.value)
        else:
            index_text = type(expression.index).__name__
        construct = f"index [{index_text}]"
    elif isinstance(expression, ast.SliceExpr):
        construct = f"slice [{expression.msb}:{expression.lsb}]"
    elif isinstance(expression, ast.DynamicSliceExpr):
        construct = f"dynamic slice [offset +: {expression.width}]"
    elif isinstance(expression, ast.VectorRangeExpr):
        construct = f"vector range [{expression.start}..{expression.stop}]"
    elif isinstance(expression, ast.ConcatExpr):
        construct = "concat"
    elif isinstance(expression, ast.BitcastExpr):
        construct = "bitcast"
    elif isinstance(expression, ast.ReshapeExpr):
        construct = "reshape"
    elif isinstance(expression, ast.PackExpr):
        construct = "pack"
    elif isinstance(expression, ast.UnpackExpr):
        construct = f"unpack<{_render_type_syntax(expression.target_type)}>"
    elif isinstance(expression, ast.GenerateExpr):
        construct = (
            f"generate({expression.index} in "
            f"{expression.start}..{expression.stop})"
        )
    elif isinstance(expression, ast.MapExpr):
        construct = (
            f"map({expression.index} in {expression.start}..{expression.stop})"
        )
    elif isinstance(expression, ast.ReduceExpr):
        construct = f"reduce({expression.operator.value}, ...)"
    elif isinstance(expression, ast.IndexedSumExpr):
        construct = (
            f"sum({expression.index} in {expression.start}..{expression.stop})"
        )
    elif isinstance(expression, ast.CollectionSumExpr):
        construct = "sum(collection)"
    elif isinstance(expression, ast.DotExpr):
        construct = "dot"
    elif isinstance(expression, ast.DelayExpr):
        construct = f"delay<{expression.cycles}>"
    elif isinstance(expression, ast.PipelineExpr):
        construct = f"pipeline({expression.stages})"
    elif isinstance(expression, ast.ProtocolTransformExpr):
        construct = "transform pipeline(auto)"
    elif isinstance(expression, ast.ImplementationChoiceExpr):
        construct = "choice"
    elif isinstance(expression, ast.ImplementExpr):
        construct = "implement"
    else:  # pragma: no cover - the expression union is exhaustively handled.
        construct = type(expression).__name__
    return SourceOrigin(
        expression.origin,
        construct,
        context.scope.source_unit if context is not None else None,
        context.scope.source_digest if context is not None else None,
    )


def callable_reference_origin(
    expression: ast.CallExpr,
    context: ExpressionContext,
) -> SourceOrigin | None:
    """Return the exact compiler-owned callee occurrence when available."""

    if expression.callee_origin is None:
        return semantic_origin(expression, context)
    return SourceOrigin(
        expression.callee_origin,
        f"call {expression.function}",
        context.scope.source_unit,
        context.scope.source_digest,
    )
