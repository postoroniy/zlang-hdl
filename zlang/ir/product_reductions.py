"""Shared exact-integer product-reduction construction and validation.

This module owns finite-width facts shared by architecture and pipeline
exploration.  It deliberately does not own candidate policy, cost, scheduling,
or source diagnostics: callers provide their domain-specific error type and
context label.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from zlang.ir import expressions as expr
from zlang.ir.numeric import NumericTypeError, addition_rule, multiplication_rule
from zlang.ir.interfaces import ReadyValidSignal
from zlang.ir.types import SIntType, UIntType


ReductionError = TypeVar("ReductionError", bound=Exception)
IntegerType = UIntType | SIntType


def validate_full_precision_products(
    products: Sequence[expr.Binary],
    result_type: IntegerType,
    *,
    error_type: type[ReductionError],
    context: str,
) -> None:
    """Validate one-family, full-width, pure combinational products."""

    family = type(result_type)
    for product in products:
        require_pure_operand(product.left, error_type=error_type, context=context)
        require_pure_operand(product.right, error_type=error_type, context=context)
        if not isinstance(product.left.type, family) or not isinstance(
            product.right.type, family
        ):
            raise error_type(
                f"{context} products must use one integer signedness family"
            )
        try:
            rule = multiplication_rule(product.left.type, product.right.type)
        except NumericTypeError as error:
            raise error_type(
                f"{context} products must use one integer signedness family"
            ) from error
        if (
            product.operand_type != rule.operand_type
            or product.type != rule.result_type
        ):
            raise error_type(f"{context} requires full-precision product terms")


def require_pure_operand(
    expression: expr.Expression,
    *,
    error_type: type[ReductionError],
    context: str,
) -> None:
    """Reject stateful/effectful product operands using the frozen allow-list."""

    if isinstance(expression, (expr.InputRef, expr.ParameterRef, expr.Constant)):
        return
    if (
        isinstance(expression, expr.ReadyValidRef)
        and expression.signal is ReadyValidSignal.PAYLOAD
    ):
        # Payload is an immutable value observation.  Handshake control is
        # deliberately not accepted here; the elastic region owns valid/ready.
        return
    if isinstance(
        expression,
        (
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
        ),
    ):
        require_pure_operand(
            expression.expression, error_type=error_type, context=context
        )
        return
    if isinstance(expression, (expr.Concat, expr.VectorConcat)):
        for operand in expression.operands:
            require_pure_operand(operand, error_type=error_type, context=context)
        return
    if isinstance(expression, expr.RuntimeIndex):
        require_pure_operand(
            expression.expression, error_type=error_type, context=context
        )
        require_pure_operand(expression.index, error_type=error_type, context=context)
        return
    if isinstance(expression, expr.VectorUpdate):
        require_pure_operand(
            expression.expression, error_type=error_type, context=context
        )
        require_pure_operand(expression.index, error_type=error_type, context=context)
        require_pure_operand(expression.value, error_type=error_type, context=context)
        return
    raise error_type(
        f"{context} products must be pure combinational values; found "
        f"{type(expression).__name__}"
    )


def sum_fits(
    products: Sequence[expr.Binary],
    result_type: IntegerType,
) -> bool:
    """Return whether every mathematical product sum fits ``result_type``."""

    if isinstance(result_type, UIntType):
        maximum = sum((1 << product.type.width) - 1 for product in products)
        return maximum < (1 << result_type.width)
    minimum = sum(-(1 << (product.type.width - 1)) for product in products)
    maximum = sum((1 << (product.type.width - 1)) - 1 for product in products)
    return (
        minimum >= -(1 << (result_type.width - 1))
        and maximum < (1 << (result_type.width - 1))
    )


def add_integer_terms(
    left: expr.Expression,
    right: expr.Expression,
    *,
    error_type: type[ReductionError],
) -> expr.Add:
    """Construct the exact widened integer addition used by reduction trees."""

    try:
        rule = addition_rule(left.type, right.type)
    except NumericTypeError as error:
        raise error_type(
            f"cannot reassociate product types {left.type} and {right.type}"
        ) from error
    if not isinstance(rule.result_type, (UIntType, SIntType)):
        raise error_type(
            f"cannot reassociate product types {left.type} and {right.type}"
        )
    return expr.Add(left, right, rule.result_type)


def linear_tree(
    expressions: Sequence[expr.Expression],
    *,
    error_type: type[ReductionError],
) -> expr.Expression:
    """Build the frozen left-to-right exact reduction tree."""

    if not expressions:
        raise error_type("cannot build an empty product reduction")
    result = expressions[0]
    for expression in expressions[1:]:
        result = add_integer_terms(result, expression, error_type=error_type)
    return result


def balanced_tree(
    expressions: Sequence[expr.Expression],
    *,
    error_type: type[ReductionError],
) -> expr.Expression:
    """Build the frozen source-order balanced tree, carrying an odd term."""

    if not expressions:
        raise error_type("cannot build an empty product reduction")
    level = tuple(expressions)
    while len(level) > 1:
        next_level: list[expr.Expression] = []
        for index in range(0, len(level), 2):
            if index + 1 == len(level):
                next_level.append(level[index])
            else:
                next_level.append(
                    add_integer_terms(
                        level[index], level[index + 1], error_type=error_type
                    )
                )
        level = tuple(next_level)
    return level[0]


def coerce_integer_result(
    expression: expr.Expression,
    result_type: IntegerType,
    *,
    error_type: type[ReductionError],
) -> expr.Expression:
    """Apply the existing explicit integer resize at a candidate boundary."""

    if expression.type == result_type:
        return expression
    if type(expression.type) is not type(result_type):
        raise error_type(
            f"cannot preserve {expression.type} expression as {result_type}"
        )
    if expression.type.width < result_type.width:
        return expr.Extend(expression, result_type)
    return expr.Truncate(expression, result_type)


__all__ = [
    "add_integer_terms",
    "balanced_tree",
    "coerce_integer_result",
    "linear_tree",
    "require_pure_operand",
    "sum_fits",
    "validate_full_precision_products",
]
