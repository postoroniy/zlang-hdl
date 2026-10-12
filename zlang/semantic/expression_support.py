# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative support expression semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import fields, is_dataclass, replace
from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import traversal as ir_traversal
from zlang.ir import types as ir_types
from . import callables as semantic_callables
from . import expression_ranges
from . import limits as semantic_limits
from . import symbols as semantic_symbols
from .errors import SemanticError
from .expression_coercion import has_runtime_value_dependency, is_constant_expression, walk_syntax_expressions

if TYPE_CHECKING:
    from .context import AnalysisServices, ExpressionContext

def _validate_elastic_kernel_capture(
    expression: ir_expr.Expression,
    source_endpoint: str,
) -> None:
    """Enforce the frozen pure single-payload capture boundary.

    pipeline scheduling may insert ``Pipeline`` nodes after this check.  Those nodes are the
    only state the elastic region is allowed to own; arbitrary source state or
    ready/valid control cannot be hidden in the selected scalar graph.
    """

    forbidden = (
        ir_expr.InputRef,
        ir_expr.RegisterRef,
        ir_expr.CreditRef,
        ir_expr.PacketRef,
        ir_expr.VirtualChannelCreditRef,
        ir_expr.RequestResponseRef,
        ir_expr.FifoRef,
        ir_expr.MemoryRef,
        ir_expr.RomRef,
        ir_expr.InstanceOutputRef,
        ir_expr.Delay,
    )
    for node in ir_traversal.walk_expression(
        expression,
        policy=ir_traversal.ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    ):
        if isinstance(node, ir_expr.ReadyValidRef):
            if (
                node.interface != source_endpoint
                or node.signal is not ir_interfaces.ReadyValidSignal.PAYLOAD
            ):
                raise SemanticError(
                    "elastic pipeline kernel may capture only the designated "
                    f"'{source_endpoint}.payload' value"
                )
            continue
        if isinstance(node, forbidden):
            raise SemanticError(
                "elastic pipeline kernel contains unsupported state/control "
                f"capture {type(node).__name__}"
            )

def _constant_parameter_expression_text(
    expression: ast.Expression,
    context: ExpressionContext,
) -> str | None:
    """Render the compile-time-only arithmetic subset for the canonical evaluator."""

    if isinstance(expression, ast.NumberExpr):
        return str(expression.value)
    if isinstance(expression, ast.NameExpr):
        if expression.name in context.environment.parameters:
            return str(context.environment.parameters[expression.name])
        if expression.name in context.scope.index_bindings:
            return str(context.scope.index_bindings[expression.name])
        if expression.name in context.environment.unresolved_parameters:
            raise SemanticError(
                f"unresolved compile-time parameter '{expression.name}' in ordinary expression"
            )
        return None
    if isinstance(expression, ast.UnaryExpr) and expression.operator is ast.BinaryOperator.SUBTRACT:
        operand = _constant_parameter_expression_text(expression.expression, context)
        return None if operand is None else f"(-({operand}))"
    if isinstance(expression, ast.AddExpr):
        left = _constant_parameter_expression_text(expression.left, context)
        right = _constant_parameter_expression_text(expression.right, context)
        return None if left is None or right is None else f"(({left})+({right}))"
    if isinstance(expression, ast.BinaryExpr) and expression.operator in {
        ast.BinaryOperator.SUBTRACT,
        ast.BinaryOperator.MULTIPLY,
        ast.BinaryOperator.DIVIDE,
        ast.BinaryOperator.SHIFT_LEFT,
    }:
        left = _constant_parameter_expression_text(expression.left, context)
        right = _constant_parameter_expression_text(expression.right, context)
        return (
            None if left is None or right is None
            else f"(({left}){expression.operator.value}({right}))"
        )
    if (
        isinstance(expression, ast.CallExpr)
        and not expression.specializations
        and expression.function in {
            "floor_log2", "ceil_log2", "index_width", "is_power_of_two"
        }
        and len(expression.arguments) == 1
    ):
        argument = _constant_parameter_expression_text(
            expression.arguments[0], context
        )
        return (
            None
            if argument is None
            else f"{expression.function}({argument})"
        )
    return None

def _fold_compile_time_parameter_expression(
    expression: ast.Expression,
    context: ExpressionContext,
) -> ast.Expression:
    # A bare range binder already has the one uniform type derived from the
    # complete half-open range.  Replacing it with a minimum-width literal on
    # each iteration would make ``generate(i in 0..4) i`` spuriously change
    # element type at i=2.  Composite constant expressions are still folded
    # below; their ordinary typed operators retain the binder's range type.
    if (
        isinstance(expression, ast.NameExpr)
        and expression.name in context.scope.index_bindings
    ):
        return expression

    # This path exists to substitute elaboration parameters in ordinary
    # expressions. Folding a literal-only hardware expression here would
    # erase the exact finite-width operator tree (for example ``3 + 0`` is u3,
    # not a newly inferred u2 literal) and would incorrectly turn general
    # unary minus into signed-literal syntax.
    parameter_names = {
        *context.environment.parameters,
        *context.scope.index_bindings,
        *context.environment.unresolved_parameters,
    }
    if not any(
        isinstance(value, ast.NameExpr) and value.name in parameter_names
        for value in walk_syntax_expressions(expression)
    ):
        return expression
    text = _constant_parameter_expression_text(expression, context)
    if text is None:
        return expression
    if context.environment.type_resolver is None:
        raise SemanticError("compile-time parameter evaluation requires a type resolver")
    value = context.environment.type_resolver._eval_constant_integer(
        text,
        description="ordinary expression",
        allow_zero=True,
        allow_negative=True,
    )
    if value >= 0:
        return ast.NumberExpr(value, origin=expression.origin)
    return ast.UnaryExpr(
        ast.BinaryOperator.SUBTRACT,
        ast.NumberExpr(-value, origin=expression.origin),
        origin=expression.origin,
    )

def _try_resolve_instance_array_index(
    index: int | ast.Expression,
    context: ExpressionContext,
    *,
    array: str,
) -> int | None:
    """Resolve a compile-time instance selector, or return ``None``."""

    if isinstance(index, int):
        return index
    if isinstance(index, ast.NumberExpr):
        return index.value
    if isinstance(index, ast.NameExpr):
        if index.name in context.scope.index_bindings:
            return context.scope.index_bindings[index.name]
        if index.name in context.environment.parameters:
            return context.environment.parameters[index.name]
    text = _constant_parameter_expression_text(index, context)
    if text is not None and context.environment.type_resolver is not None:
        return context.environment.type_resolver._eval_constant_integer(
            text,
            description=f"instance array '{array}' index",
            allow_zero=True,
            allow_negative=True,
        )
    return None

def _resolve_instance_array_index(
    index: int | ast.Expression,
    context: ExpressionContext,
    *,
    array: str,
) -> int:
    """Resolve an instance selector without creating a runtime hardware mux."""

    resolved = _try_resolve_instance_array_index(index, context, array=array)
    if resolved is not None:
        return resolved
    raise SemanticError(
        f"instance array '{array}' requires a compile-time index; "
        "runtime instance selection is not hardware generation"
    )

def _expand_immutable_locals(
    value: ir_expr.Expression,
    symbols: dict[str, semantic_symbols.ValueSymbol],
    active: frozenset[str] = frozenset(),
    memo: dict[int, ir_expr.Expression] | None = None,
    *,
    work_budget: AnalysisServices | None = None,
) -> ir_expr.Expression:
    """Inline immutable locals without expanding shared typed DAG paths."""

    if memo is None:
        memo = {}
    # A local can occur thousands of times in one value.  The local cache
    # handles repeated names, while this per-query cache also preserves
    # sharing of their already-typed descendants.  The active set is part of
    # the key so cycle detection cannot be bypassed through another path.
    node_memo: dict[
        tuple[int, frozenset[str]],
        tuple[ir_expr.Expression, ir_expr.Expression],
    ] = {}
    visited_nodes = 0

    def walk(current: ir_expr.Expression, active_names: frozenset[str]) -> ir_expr.Expression:
        nonlocal visited_nodes
        key = (id(current), active_names)
        cached_node = node_memo.get(key)
        if cached_node is not None and cached_node[0] is current:
            return cached_node[1]
        visited_nodes += 1
        if visited_nodes > semantic_limits.MAX_LOCAL_EXPANSION_NODES:
            raise SemanticError(
                "immutable-local analysis exceeds the bounded expression DAG size",
                code="ZL-IR-EXPANSION-LIMIT",
                primary=current.origin,
            )
        if work_budget is not None:
            work_budget.local_expansion_nodes += 1
            if (
                work_budget.local_expansion_nodes
                > semantic_limits.MAX_ANALYSIS_LOCAL_EXPANSION_NODES
            ):
                raise SemanticError(
                    "semantic analysis exceeds the bounded immutable-local "
                    "expansion work for this source",
                    code="ZL-IR-EXPANSION-LIMIT",
                    primary=current.origin,
                )

        if isinstance(current, ir_expr.InputRef):
            symbol = symbols.get(current.name)
            if isinstance(symbol, ir_module.LocalValue):
                if symbol.name in active_names:
                    raise SemanticError(
                        f"cyclic immutable local '{symbol.name}' during semantic expansion"
                    )
                cache_key = id(symbol)
                expanded = memo.get(cache_key)
                if expanded is None:
                    expanded = walk(
                        symbol.expression, active_names | {symbol.name}
                    )
                    memo[cache_key] = expanded
                result = expanded
            else:
                result = current
        elif isinstance(current, ir_expr.Switch):
            result = replace(
                current,
                selector=walk(current.selector, active_names),
                cases=tuple(
                    replace(case, expression=walk(case.expression, active_names))
                    for case in current.cases
                ),
                default=walk(current.default, active_names),
            )
        elif isinstance(current, ir_expr.StructConstruct):
            result = replace(
                current,
                fields=tuple(
                    (name, walk(item, active_names))
                    for name, item in current.fields
                ),
            )
        elif not isinstance(current, ir_expr.TracedExpression):
            result = current
        else:
            updates: dict[str, object] = {}
            for item in fields(current):
                if item.name == "origin" or not item.init:
                    continue
                child = getattr(current, item.name)
                if isinstance(child, ir_expr.TracedExpression):
                    updates[item.name] = walk(child, active_names)
                elif isinstance(child, tuple):
                    updates[item.name] = tuple(
                        walk(element, active_names)
                        if isinstance(element, ir_expr.TracedExpression)
                        else element
                        for element in child
                    )
            result = replace(current, **updates) if updates else current
        node_memo[key] = (current, result)
        return result

    return walk(value, active)

def _local_constant_and_range(
    value: ir_expr.Expression,
    context: ExpressionContext,
    *,
    name: str,
) -> tuple[bool, ir_expr.ValueRange | None]:
    """Classify one local while keeping obviously-runtime call graphs compact."""

    if has_runtime_value_dependency(value):
        return False, expression_ranges.static_value_range(value)
    analysis = semantic_callables._expand_analysis_calls(
        value, context, purpose=f"local '{name}'"
    )
    return (
        is_constant_expression(analysis),
        expression_ranges.static_value_range(analysis),
    )

def _inline_semantic_locals(
    expression: ir_expr.Expression,
    locals_: tuple[ir_module.LocalValue, ...],
) -> ir_expr.Expression:
    """Resolve pure locals before architecture/pipeline recognition."""
    values = {item.name: item.expression for item in locals_}

    def walk(value):
        if isinstance(value, ir_expr.InputRef) and value.name in values:
            return walk(values[value.name])
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        if is_dataclass(value):
            updates = {}
            for item in fields(value):
                if item.name == "origin" or not item.init:
                    continue
                current = getattr(value, item.name)
                if isinstance(current, tuple):
                    updates[item.name] = tuple(walk(child) for child in current)
                elif is_dataclass(current):
                    updates[item.name] = walk(current)
            return replace(value, **updates) if updates else value
        return value

    return walk(expression)

def _resized_type(type_: ir_types.HardwareType, width: int) -> ir_types.HardwareType:
    if isinstance(type_, ir_types.UIntType):
        return ir_types.UIntType(width)
    if isinstance(type_, ir_types.SIntType):
        return ir_types.SIntType(width)
    if isinstance(type_, ir_types.BitsType):
        return ir_types.BitsType(width)
    if isinstance(type_, (ir_types.FixedType, ir_types.UFixedType)):
        if width <= type_.fraction:
            raise SemanticError("fixed-point resize must retain at least one integer/sign bit")
        return type(type_)(width, type_.fraction)
    raise SemanticError(f"cannot resize {type_}")
