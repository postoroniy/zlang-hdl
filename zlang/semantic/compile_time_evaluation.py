# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded compile-time evaluation owned independently of callable specialization."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import re
from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types

from . import compile_time_real as ct_real
from . import limits as semantic_limits
from . import symbols as semantic_symbols
from . import type_resolution
from .errors import SemanticError
from .integer_intrinsics import INTEGER_INTRINSICS, evaluate_integer_intrinsic

if TYPE_CHECKING:
    from .context import ExpressionContext


@dataclass
class CompileTimeBudget:
    """Shared bounded budget for one selected top elaboration."""

    generated_elements: int = 0
    operations: int = 0
    call_depth: int = 0


def budget_step(context: ExpressionContext, operations: int = 1) -> None:
    budget = context.services.compile_time_budget
    if budget is None:
        return
    budget.operations += operations
    if budget.operations > semantic_limits.COMPILE_TIME_OPERATIONS:
        raise SemanticError(
            f"compile-time evaluator exceeded {semantic_limits.COMPILE_TIME_OPERATIONS} operations"
        )


def replay_specialization_budget(
    context: ExpressionContext,
    identity: str,
) -> None:
    """Charge the same logical cost for a cached specialization invocation."""

    budget = context.services.compile_time_budget
    if budget is None:
        return
    generated, operations = context.services.callables.specialization_budget_costs.get(
        identity, (0, 0)
    )
    budget.generated_elements += generated
    if budget.generated_elements > semantic_limits.TOTAL_GENERATED:
        raise SemanticError(
            f"compile-time generation exceeds {semantic_limits.TOTAL_GENERATED} elements"
        )
    budget.operations += operations
    if budget.operations > semantic_limits.COMPILE_TIME_OPERATIONS:
        raise SemanticError(
            f"compile-time evaluator exceeded {semantic_limits.COMPILE_TIME_OPERATIONS} operations"
        )


def compile_time_type_value(
    expression: ast.Expression,
    context: ExpressionContext,
) -> ir_types.HardwareType | None:
    if context.environment.type_resolver is None:
        return None
    resolver = context.environment.type_resolver
    if isinstance(expression, ast.TypeValueExpr):
        try:
            return resolver.resolve(expression.type_name)
        except SemanticError:
            return None
    if not isinstance(expression, ast.NameExpr):
        return None
    if expression.name in resolver._type_bindings:
        return resolver._type_bindings[expression.name]
    try:
        return resolver.resolve(ast.TypeName(expression.name))
    except SemanticError:
        return None


def constant_ir_to_compile_time_real(
    value: ir_expr.Expression,
) -> ct_real.CompileTimeReal | None:
    if not isinstance(value, ir_expr.Constant):
        return None
    if isinstance(value.type, (ir_types.BitType, ir_types.UIntType, ir_types.SIntType, ir_types.BitsType)):
        return ct_real.CompileTimeReal.rational_value(Fraction(value.value))
    if isinstance(value.type, (ir_types.FixedType, ir_types.UFixedType)):
        return ct_real.CompileTimeReal.rational_value(
            Fraction(value.value, 1 << value.type.fraction)
        )
    return None


def compile_time_real_value(
    expression: ast.Expression,
    inputs: dict[str, "semantic_symbols.ValueSymbol"],
    context: ExpressionContext,
) -> ct_real.CompileTimeReal:
    """Evaluate the frozen compiler-only real subset.

    The result is not hardware IR. Callers must either require an exact integer
    or quantize it immediately into an ordinary fixed-point constant.
    """

    try:
        budget_step(context)
        if isinstance(expression, ast.CompileTimeIfExpr):
            selected = (
                expression.when_true
                if compile_time_condition(expression.condition, inputs, context)
                else expression.when_false
            )
            if selected is None:
                raise SemanticError("compile-time if requires an else branch in an expression")
            return compile_time_real_value(selected, inputs, context)
        if isinstance(expression, ast.NumberExpr):
            return ct_real.CompileTimeReal.rational_value(Fraction(expression.value))
        if isinstance(expression, ast.RationalExpr):
            return ct_real.CompileTimeReal.rational_value(
                Fraction(expression.numerator, expression.denominator)
            )
        if isinstance(expression, ast.NameExpr):
            if expression.name in context.scope.index_bindings:
                return ct_real.CompileTimeReal.rational_value(
                    Fraction(context.scope.index_bindings[expression.name])
                )
            if expression.name in context.environment.parameters:
                return ct_real.CompileTimeReal.rational_value(
                    Fraction(context.environment.parameters[expression.name])
                )
            symbol = inputs.get(expression.name)
            if isinstance(symbol, ir_module.LocalValue) and symbol.compile_time:
                value = constant_ir_to_compile_time_real(symbol.expression)
                if value is not None:
                    return value
            raise SemanticError(
                f"compile-time real intrinsic references runtime value '{expression.name}'"
            )
        if isinstance(expression, ast.UnaryExpr):
            if expression.operator is ast.BinaryOperator.SUBTRACT:
                return ct_real.negate(
                    compile_time_real_value(expression.expression, inputs, context)
                )
            raise SemanticError("unsupported compile-time real unary operator")
        if isinstance(expression, ast.AddExpr):
            return ct_real.add(
                compile_time_real_value(expression.left, inputs, context),
                compile_time_real_value(expression.right, inputs, context),
            )
        if isinstance(expression, ast.BinaryExpr):
            left = compile_time_real_value(expression.left, inputs, context)
            right = compile_time_real_value(expression.right, inputs, context)
            if expression.operator is ast.BinaryOperator.SUBTRACT:
                return ct_real.subtract(left, right)
            if expression.operator is ast.BinaryOperator.MULTIPLY:
                return ct_real.multiply(left, right)
            if expression.operator is ast.BinaryOperator.DIVIDE:
                return ct_real.divide(left, right)
            raise SemanticError(
                f"operator '{expression.operator.value}' is not allowed in a compile-time real expression"
            )
        if isinstance(expression, ast.CallExpr):
            if expression.function in semantic_limits.REAL_INTRINSICS:
                type_resolution.validate_compile_time_real_intrinsic_arity(
                    expression.function, len(expression.arguments)
                )
                return type_resolution.apply_compile_time_real_intrinsic(
                    expression.function,
                    tuple(
                        compile_time_real_value(argument, inputs, context)
                        for argument in expression.arguments
                    ),
                )
            if expression.function in {
                "length", "floor_log2", "ceil_log2", "index_width",
                "is_power_of_two",
            }:
                return ct_real.CompileTimeReal.rational_value(
                    Fraction(compile_time_integer_value(expression, inputs, context))
                )
            raise SemanticError(
                f"function '{expression.function}' is not available to compile-time real evaluation"
            )
    except ct_real.CompileTimeRealError as error:
        raise SemanticError(str(error)) from error
    raise SemanticError(
        "compile-time real expression requires constants and compiler intrinsics"
    )


def compile_time_integer_value(
    expression: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    context: ExpressionContext,
) -> int:
    """Evaluate the frozen exact integer/Boolean condition subset."""

    budget_step(context)
    if isinstance(expression, ast.NumberExpr):
        return expression.value
    if isinstance(expression, ast.RationalExpr):
        value = Fraction(expression.numerator, expression.denominator)
        if value.denominator == 1:
            return value.numerator
        raise SemanticError("non-integral compile-time real value cannot be used as an integer")
    if isinstance(expression, ast.NameExpr):
        if expression.name in context.scope.index_bindings:
            return context.scope.index_bindings[expression.name]
        if expression.name in context.environment.parameters:
            return context.environment.parameters[expression.name]
        raise SemanticError(
            f"compile-time condition references runtime value '{expression.name}'"
        )
    if isinstance(expression, ast.UnaryExpr):
        if expression.operator is ast.BinaryOperator.LOGIC_NOT:
            return int(not compile_time_integer_value(expression.expression, inputs, context))
        if expression.operator is ast.BinaryOperator.SUBTRACT:
            return -compile_time_integer_value(expression.expression, inputs, context)
        raise SemanticError("unsupported compile-time unary operator")
    if isinstance(expression, ast.AddExpr):
        return (
            compile_time_integer_value(expression.left, inputs, context)
            + compile_time_integer_value(expression.right, inputs, context)
        )
    if isinstance(expression, ast.BinaryExpr):
        operator = expression.operator
        if operator in {ast.BinaryOperator.EQUAL, ast.BinaryOperator.NOT_EQUAL}:
            left_type = compile_time_type_value(expression.left, context)
            right_type = compile_time_type_value(expression.right, context)
            if left_type is not None or right_type is not None:
                equal = left_type is not None and right_type is not None and left_type == right_type
                return int(equal if operator is ast.BinaryOperator.EQUAL else not equal)
        left = compile_time_integer_value(expression.left, inputs, context)
        right = compile_time_integer_value(expression.right, inputs, context)
        if operator is ast.BinaryOperator.LOGIC_AND:
            return int(bool(left) and bool(right))
        if operator is ast.BinaryOperator.LOGIC_OR:
            return int(bool(left) or bool(right))
        if operator is ast.BinaryOperator.EQUAL:
            return int(left == right)
        if operator is ast.BinaryOperator.NOT_EQUAL:
            return int(left != right)
        if operator is ast.BinaryOperator.LESS:
            return int(left < right)
        if operator is ast.BinaryOperator.LESS_EQUAL:
            return int(left <= right)
        if operator is ast.BinaryOperator.GREATER:
            return int(left > right)
        if operator is ast.BinaryOperator.GREATER_EQUAL:
            return int(left >= right)
        if operator is ast.BinaryOperator.SUBTRACT:
            return left - right
        if operator is ast.BinaryOperator.MULTIPLY:
            return left * right
        if operator is ast.BinaryOperator.DIVIDE:
            if right == 0 or left % right:
                raise SemanticError("compile-time division must be exact and non-zero")
            return left // right
        if operator is ast.BinaryOperator.SHIFT_LEFT:
            if right < 0:
                raise SemanticError("compile-time shift must be non-negative")
            return left << right
        if operator is ast.BinaryOperator.SHIFT_RIGHT:
            if right < 0:
                raise SemanticError("compile-time shift must be non-negative")
            return left >> right
        raise SemanticError(
            f"operator '{operator.value}' is not allowed in a compile-time condition"
        )
    if isinstance(expression, ast.CallExpr):
        if expression.function in semantic_limits.REAL_INTRINSICS:
            value = compile_time_real_value(expression, inputs, context)
            exact = value.exact_integer()
            if exact is None:
                raise SemanticError(
                    f"intrinsic '{expression.function}' produced a non-integral compile-time real value; "
                    "use explicit quantize(...) for fixed-point hardware"
                )
            return exact
        if len(expression.arguments) != 1:
            raise SemanticError(f"compile-time intrinsic '{expression.function}' expects one argument")
        if expression.function == "length":
            value = context.expressions.check(
                expression.arguments[0], inputs, None, context
            )
            if not isinstance(value.type, ir_types.VecType):
                raise SemanticError("length(...) requires a concrete vec<N,T>")
            return value.type.length
        value = compile_time_integer_value(expression.arguments[0], inputs, context)
        if expression.function in INTEGER_INTRINSICS:
            return evaluate_integer_intrinsic(
                expression.function, value
            )
        raise SemanticError(
            f"compile-time condition function '{expression.function}' is not supported"
        )
    type_value = compile_time_type_value(expression, context)
    if type_value is not None:
        raise SemanticError("type values may only be used with == or !=")
    raise SemanticError(
        "compile-time if condition must use compile-time values; use ?:, mux, "
        "switch, when, or priority { ... } for runtime actions"
    )


def compile_time_condition(
    expression: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    context: ExpressionContext,
) -> bool:
    # Type equality is handled before integer evaluation so an unresolved type
    # name cannot be mistaken for a runtime signal.
    if isinstance(expression, ast.BinaryExpr) and expression.operator in {
        ast.BinaryOperator.EQUAL,
        ast.BinaryOperator.NOT_EQUAL,
    }:
        left_type = compile_time_type_value(expression.left, context)
        right_type = compile_time_type_value(expression.right, context)
        if left_type is not None or right_type is not None:
            if left_type is None or right_type is None:
                result = expression.operator is ast.BinaryOperator.NOT_EQUAL
            else:
                result = left_type == right_type
            return result if expression.operator is ast.BinaryOperator.EQUAL else not result
    return bool(compile_time_integer_value(expression, inputs, context))


def resolve_range_bound(
    value: int | str,
    context: ExpressionContext,
    label: str,
    inputs: dict[str, semantic_symbols.ValueSymbol] | None = None,
) -> int:
    if isinstance(value, int):
        result = value
    elif (
        inputs is not None
        and context.environment.type_resolver is not None
        and (match := re.fullmatch(r"length\(([A-Za-z_][A-Za-z0-9_]*)\)", value))
    ):
        symbol = inputs.get(match.group(1))
        if symbol is None:
            raise SemanticError(
                f"unresolved vector '{match.group(1)}' in {label} range bound"
            )
        type_ = getattr(symbol, "type", None)
        if not isinstance(type_, ir_types.VecType):
            raise SemanticError(f"length(...) requires a concrete vec<N,T> in {label} range bound")
        result = type_.length
    elif context.environment.type_resolver is not None:
        result = context.environment.type_resolver._eval_constant_integer(
            value,
            description=f"{label} range bound",
            allow_zero=True,
            allow_negative=True,
        )
    else:
        raise SemanticError(f"{label} range bound requires compile-time evaluation")
    if result < 0:
        raise SemanticError(f"{label} range bound must be non-negative")
    return result
