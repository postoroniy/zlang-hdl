"""Fail-closed traversal for backend-independent typed expressions.

Expression IR is a closed union.  Traversals must therefore name every member
explicitly instead of using dataclass reflection: adding a new expression kind
without deciding its child semantics is a correctness error, not an implicit
leaf.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterator, get_args

from zlang.ir import expressions as expr


class ExpressionTraversalError(TypeError):
    """An object has no declared typed-expression traversal policy."""


class ExpressionTraversalPolicy(str, Enum):
    """Select which implementation alternatives belong to a traversal."""

    STRUCTURAL = "structural"
    SELECTED_IMPLEMENTATION = "selected_implementation"
    EXECUTABLE = "executable"


_LEAF_TYPES = (
    expr.InputRef,
    expr.ParameterRef,
    expr.RegisterRef,
    expr.ReadyValidRef,
    expr.CreditRef,
    expr.PacketRef,
    expr.VirtualChannelCreditRef,
    expr.RequestResponseRef,
    expr.FifoRef,
    expr.MemoryRef,
    expr.RomRef,
    expr.Constant,
    expr.FunctionalCaptureRef,
    expr.FunctionalTableLookup,
    expr.InstanceOutputRef,
)

_UNARY_TYPES = (
    expr.EnumEncode,
    expr.EnumValid,
    expr.UnionTag,
    expr.UnionField,
    expr.Extend,
    expr.Truncate,
    expr.FixedConvert,
    expr.FieldAccess,
    expr.VectorIndex,
    expr.Slice,
    expr.Bitcast,
    expr.Reshape,
    expr.Pack,
    expr.Unpack,
    expr.Delay,
    expr.Pipeline,
)

SUPPORTED_EXPRESSION_TYPES = (
    *_LEAF_TYPES,
    expr.EnumDecode,
    expr.Add,
    expr.Binary,
    *_UNARY_TYPES,
    expr.Mux,
    expr.Switch,
    expr.Call,
    expr.StructConstruct,
    expr.TupleConstruct,
    expr.TupleProject,
    expr.UnionConstruct,
    expr.RuntimeIndex,
    expr.VectorUpdate,
    expr.Concat,
    expr.VectorConcat,
    expr.Generate,
    expr.FunctionalRegion,
    expr.Map,
    expr.Dot,
    expr.Reduce,
    expr.ImplementationChoice,
)


def _verify_expression_union_coverage() -> None:
    declared = tuple(get_args(expr.Expression))
    if len(SUPPORTED_EXPRESSION_TYPES) != len(set(SUPPORTED_EXPRESSION_TYPES)):
        raise RuntimeError("expression traversal declares a duplicate expression type")
    missing = set(declared) - set(SUPPORTED_EXPRESSION_TYPES)
    extra = set(SUPPORTED_EXPRESSION_TYPES) - set(declared)
    if missing or extra:
        raise RuntimeError(
            "expression traversal coverage does not match Expression union: "
            f"missing={sorted(item.__name__ for item in missing)}, "
            f"extra={sorted(item.__name__ for item in extra)}"
        )


_verify_expression_union_coverage()


def expression_children(
    expression: expr.Expression,
    *,
    policy: ExpressionTraversalPolicy = ExpressionTraversalPolicy.STRUCTURAL,
) -> tuple[expr.Expression, ...]:
    """Return deterministic direct children under an explicit traversal policy."""

    try:
        policy = ExpressionTraversalPolicy(policy)
    except (TypeError, ValueError) as error:
        raise ExpressionTraversalError(
            f"unknown expression traversal policy {policy!r}"
        ) from error

    if isinstance(expression, _LEAF_TYPES):
        return ()
    if isinstance(expression, _UNARY_TYPES):
        return (expression.expression,)
    if isinstance(expression, expr.EnumDecode):
        return (expression.expression, expression.fallback)
    if isinstance(expression, (expr.Add, expr.Binary)):
        return (expression.left, expression.right)
    if isinstance(expression, expr.Mux):
        return (
            expression.condition,
            expression.when_true,
            expression.when_false,
        )
    if isinstance(expression, expr.Switch):
        return (
            expression.selector,
            *(case.expression for case in expression.cases),
            expression.default,
        )
    if isinstance(expression, expr.Call):
        return expression.arguments
    if isinstance(expression, (expr.StructConstruct, expr.UnionConstruct)):
        return tuple(value for _, value in expression.fields)
    if isinstance(expression, expr.TupleConstruct):
        return expression.elements
    if isinstance(expression, expr.TupleProject):
        return (expression.expression,)
    if isinstance(expression, expr.RuntimeIndex):
        return (expression.expression, expression.index)
    if isinstance(expression, expr.VectorUpdate):
        return (expression.expression, expression.index, expression.value)
    if isinstance(expression, (expr.Concat, expr.VectorConcat)):
        return expression.operands
    if isinstance(expression, (expr.Generate, expr.Map)):
        return expression.elements
    if isinstance(expression, expr.FunctionalRegion):
        if policy is ExpressionTraversalPolicy.EXECUTABLE:
            # Import lazily: functional lowering itself depends on the
            # expression definitions and must not participate in traversal
            # module initialization.
            from zlang.ir.functional import materialize_functional_region

            return materialize_functional_region(expression)
        return (
            expression.template,
            *(value for table in expression.tables for value in table.values),
            *(value for _, value in expression.captures),
        )
    if isinstance(expression, expr.Dot):
        if policy in {
            ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
            ExpressionTraversalPolicy.EXECUTABLE,
        }:
            return expression.products
        return (expression.left, expression.right, *expression.products)
    if isinstance(expression, expr.Reduce):
        if policy is ExpressionTraversalPolicy.EXECUTABLE:
            if expression.expanded is not None:
                return (expression.expanded,)
            if expression.plan is not None:
                from zlang.ir.functional import materialize_exact_reduction

                return (materialize_exact_reduction(expression),)
        return (
            (expression.collection, expression.expanded)
            if expression.expanded is not None
            else (expression.collection,)
        )
    if isinstance(expression, expr.ImplementationChoice):
        if policy in {
            ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
            ExpressionTraversalPolicy.EXECUTABLE,
        }:
            return (expression.selected_alternative.expression,)
        return tuple(
            alternative.expression for alternative in expression.alternatives
        )
    raise ExpressionTraversalError(
        "no child policy for typed expression "
        f"{type(expression).__module__}.{type(expression).__qualname__}"
    )


def walk_expression(
    root: expr.Expression,
    *,
    policy: ExpressionTraversalPolicy = ExpressionTraversalPolicy.STRUCTURAL,
    deduplicate: bool = True,
) -> Iterator[expr.Expression]:
    """Yield a deterministic pre-order walk of one typed expression graph."""

    stack: list[expr.Expression] = [root]
    visited: set[int] = set()
    while stack:
        expression = stack.pop()
        if deduplicate:
            identity = id(expression)
            if identity in visited:
                continue
            visited.add(identity)
        children = expression_children(expression, policy=policy)
        yield expression
        stack.extend(reversed(children))


__all__ = [
    "ExpressionTraversalError",
    "ExpressionTraversalPolicy",
    "SUPPORTED_EXPRESSION_TYPES",
    "expression_children",
    "walk_expression",
]
