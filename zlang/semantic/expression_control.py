# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative control expression semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import replace
from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import exact_simplification
from zlang.ir import module as ir_module
from zlang.ir import numeric as ir_numeric
from zlang.ir import runtime_values as runtime_values
from zlang.ir import types as ir_types
from zlang.source import SourceSpan
from . import callables as semantic_callables
from . import compile_time_evaluation
from . import compile_time_real as ct_real
from . import expression_ranges
from . import expression_domains
from . import expression_origins
from . import expression_operators
from . import implementation as semantic_implementation
from . import limits as semantic_limits
from . import symbols as semantic_symbols
from . import type_resolution
from .storage_symbols import FifoSymbol, MemorySymbol, RomSymbol
from .errors import SemanticError
from .expression_coercion import check_integer_literal, fixed_overflow_for_type, fixed_rounding, make_bitcast, walk_syntax_expressions
from .expression_collections import _check_dot, _check_indexed_vector, _check_reduction
from .expression_support import _constant_parameter_expression_text, _expand_immutable_locals, _fold_compile_time_parameter_expression

if TYPE_CHECKING:
    from .context import ExpressionContext

IMPLEMENTATION_ANALYZER = semantic_implementation.ImplementationIntentAnalyzer()

def _check_functional_and_operator_expression(
    expression: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    if isinstance(expression, ast.GenerateExpr):
        return _check_indexed_vector(
            expression.index,
            expression.start,
            expression.stop,
            expression.expression,
            inputs,
            expected,
            context,
            ir_expr.Generate,
        )
    if isinstance(expression, ast.MapExpr):
        return _check_indexed_vector(
            expression.index,
            expression.start,
            expression.stop,
            expression.expression,
            inputs,
            expected,
            context,
            ir_expr.Map,
        )
    if isinstance(expression, ast.IndexedSumExpr):
        collection = _check_indexed_vector(
            expression.index,
            expression.start,
            expression.stop,
            expression.expression,
            inputs,
            None,
            context,
            ir_expr.Generate,
        )
        origin = expression_origins.semantic_origin(expression, context)
        if origin is not None:
            collection = replace(collection, origin=origin)
        return _check_reduction(
            ir_expr.ReductionOperator.ADD,
            collection,
            context,
        )
    if isinstance(expression, ast.CollectionSumExpr):
        collection = context.expressions.check(
            expression.collection, inputs, None, context
        )
        return _check_reduction(
            ir_expr.ReductionOperator.ADD,
            collection,
            context,
        )
    if isinstance(expression, ast.ReduceExpr):
        collection = context.expressions.check(
            expression.collection, inputs, None, context
        )
        operator = ir_expr.ReductionOperator(expression.operator.value)
        if not isinstance(collection.type, ir_types.VecType):
            if operator not in {
                ir_expr.ReductionOperator.BIT_AND,
                ir_expr.ReductionOperator.BIT_OR,
                ir_expr.ReductionOperator.BIT_XOR,
            }:
                raise SemanticError(
                    f"reduce({operator.value}, ...) requires a vector collection, "
                    f"got {collection.type}"
                )
            if not isinstance(
                collection.type, (ir_types.BitType, ir_types.BitsType, ir_types.UIntType, ir_types.SIntType)
            ):
                raise SemanticError(
                    f"scalar reduce({operator.value}, ...) requires bit, bits, "
                    f"unsigned, or signed integer input, got {collection.type}"
                )
            raw_vector = make_bitcast(
                collection,
                ir_types.VecType(collection.type.width, ir_types.BitType()),
                description=f"scalar reduce({operator.value}, ...)",
            )
            return _check_reduction(operator, raw_vector, context)
        return _check_reduction(
            operator,
            collection,
            context,
        )
    if isinstance(expression, ast.DotExpr):
        return _check_dot(expression, inputs, context, expected)
    if isinstance(expression, ast.QuantizeExpr):
        return _check_quantize(expression, inputs, expected, context)
    if isinstance(expression, ast.AddExpr):
        left, right = _check_operand_pair(
            expression.left, expression.right, inputs, context
        )
        if isinstance(left.type, ir_types.EnumType) or isinstance(right.type, ir_types.EnumType):
            raise SemanticError(
                "arithmetic is not defined for enum values"
            )
        if isinstance(left.type, ir_types.StructType) or isinstance(right.type, ir_types.StructType):
            return expression_operators.resolve_operator(
                "+",
                (left, right),
                context,
                call_origin=expression_origins.semantic_origin(expression, context),
            )
        try:
            result_type = ir_numeric.addition_rule(left.type, right.type).result_type
        except ir_numeric.NumericTypeError as error:
            if error.reason is ir_numeric.NumericTypeErrorReason.FRACTION_MISMATCH:
                raise SemanticError(
                    "fixed-point addition requires identical fractional widths; "
                    "use explicit quantize/rescale before the operator"
                ) from error
            if error.reason is ir_numeric.NumericTypeErrorReason.FAMILY_MISMATCH:
                raise SemanticError(
                    f"cannot add operands with different type families "
                    f"{left.type} and {right.type}"
                ) from error
            raise SemanticError(f"addition is not defined for {left.type}") from error
        if isinstance(left, ir_expr.Constant) and isinstance(right, ir_expr.Constant):
            return ir_expr.Constant(left.value + right.value, result_type)
        if (
            context.scope.functional_symbolic_values
            and isinstance(left, ir_expr.Constant)
            and left.value == 0
        ):
            simplified = exact_simplification.exact_numeric_widen(right, result_type)
            if simplified is not None:
                return simplified
        if (
            context.scope.functional_symbolic_values
            and isinstance(right, ir_expr.Constant)
            and right.value == 0
        ):
            simplified = exact_simplification.exact_numeric_widen(left, result_type)
            if simplified is not None:
                return simplified
        return ir_expr.Add(left, right, result_type)
    if isinstance(expression, ast.BinaryExpr):
        return _check_binary(expression, inputs, context)
    if isinstance(expression, ast.UnaryExpr):
        if (
            expression.operator is ast.BinaryOperator.SUBTRACT
            and isinstance(expression.expression, ast.NumberExpr)
        ):
            return check_integer_literal(
                -expression.expression.value,
                expected,
                signed_syntax=True,
            )
        operand = context.expressions.check(expression.expression, inputs, None, context)
        if expression.operator is ast.BinaryOperator.LOGIC_NOT:
            if not isinstance(operand.type, ir_types.BitType):
                raise SemanticError(
                    f"logical not requires bit, got {operand.type}"
                )
            return expression_operators.build_binary(
                ir_expr.BinaryOperator.EQUAL,
                operand,
                ir_expr.Constant(0, ir_types.BitType()),
            )
        if expression.operator is ast.BinaryOperator.BIT_NOT:
            if not isinstance(
                operand.type, (ir_types.BitType, ir_types.BitsType, ir_types.UIntType, ir_types.SIntType)
            ):
                raise SemanticError(
                    f"bitwise complement requires bit, bits, unsigned, or "
                    f"signed integer input, got {operand.type}"
                )
            ones = (
                -1
                if isinstance(operand.type, ir_types.SIntType)
                else (1 << operand.type.width) - 1
            )
            return expression_operators.build_binary(
                ir_expr.BinaryOperator.BIT_XOR,
                operand,
                ir_expr.Constant(ones, operand.type),
            )
        if isinstance(operand.type, ir_types.StructType):
            return expression_operators.resolve_operator(
                "-",
                (operand,),
                context,
                call_origin=expression_origins.semantic_origin(expression, context),
            )
        if not isinstance(
            operand.type, (ir_types.UIntType, ir_types.SIntType, ir_types.FixedType, ir_types.UFixedType)
        ):
            raise SemanticError(f"unary minus is not defined for {operand.type}")
        zero = ir_expr.Constant(0, operand.type)
        return expression_operators.build_binary(ir_expr.BinaryOperator.SUBTRACT, zero, operand)
    if isinstance(expression, ast.DelayExpr):
        if not context.scope.allow_delay:
            raise SemanticError("delay requires a module clock and reset")
        operand = context.expressions.check(expression.expression, inputs, expected, context)
        if not isinstance(
            operand.type,
            (
                ir_types.BitType,
                ir_types.UIntType,
                ir_types.SIntType,
                ir_types.BitsType,
                ir_types.FixedType,
                ir_types.UFixedType,
                ir_types.TupleType,
            ),
        ):
            raise SemanticError(
                f"delay reset value is not defined for {operand.type}"
            )
        return ir_expr.Delay(
            expression.cycles,
            operand,
            context.allocate_delay(),
            operand.type,
        )
    if isinstance(expression, ast.ImplementExpr):
        raise SemanticError(
            "implement is allowed only as a complete wire-output assignment"
        )
    if isinstance(expression, ast.PipelineExpr):
        if expression.constraints:
            raise SemanticError(
                "fixed pipeline stages do not accept automatic constraints"
            )
        if not context.scope.allow_delay:
            raise SemanticError("pipeline requires a module clock and reset")
        operand = context.expressions.check(expression.expression, inputs, expected, context)
        if not isinstance(
            operand.type,
            (
                ir_types.BitType,
                ir_types.UIntType,
                ir_types.SIntType,
                ir_types.BitsType,
                ir_types.FixedType,
                ir_types.UFixedType,
                ir_types.TupleType,
            ),
        ):
            raise SemanticError(
                f"pipeline reset value is not defined for {operand.type}"
            )
        # Fixed pipelines are exact semantic timing contracts.  Expand only
        # immutable aliases and pure callable bodies here; physical partition
        # and register placement belongs to implementation planning, after
        # target/profile/evidence inputs are known.
        scheduling_operand = _expand_immutable_locals(
            operand, inputs, work_budget=context.services
        )
        scheduling_operand = semantic_callables._expand_analysis_calls(
            scheduling_operand,
            context,
            purpose=f"pipeline({expression.stages}) expression",
        )
        operand_ports = {
            name: symbol
            for name, symbol in inputs.items()
            if isinstance(symbol, (ir_module.Port, FifoSymbol, MemorySymbol, RomSymbol))
        }
        operand_registers = {
            name: symbol
            for name, symbol in inputs.items()
            if isinstance(symbol, ir_module.Register)
        }
        operand_domains = {
            item
            for item in expression_domains.expression_domains(
                scheduling_operand, operand_ports, operand_registers
            )
            if item is not None
        }
        pipeline_domain = (
            expression.domain or context.environment.default_clock_domain
        )
        if (
            expression.domain is not None
            and expression.domain not in context.environment.clock_domains
        ):
            raise SemanticError(
                f"pipeline references unknown clock domain '{expression.domain}'",
                code="ZL-DOMAIN-UNKNOWN",
                primary=expression_origins.semantic_origin(expression, context),
            )
        if pipeline_domain is None and len(operand_domains) == 1:
            pipeline_domain = next(iter(operand_domains))
        if pipeline_domain is None:
            if len(context.environment.clock_domains) == 1:
                pipeline_domain = context.environment.clock_domains[0]
            else:
                raise SemanticError(
                    "ambiguous clock domain for pipeline; annotate it with @clock",
                    code="ZL-DOMAIN-AMBIGUOUS",
                    primary=expression_origins.semantic_origin(expression, context),
                )
        if operand_domains - {pipeline_domain}:
            foreign = sorted(operand_domains - {pipeline_domain})[0]
            raise SemanticError(
                f"scalar pipeline in '{pipeline_domain}' reads dynamic value "
                f"from '{foreign}'; a pipeline cut is not a CDC crossing",
                code="ZL-DOMAIN-CROSSING",
                primary=expression_origins.semantic_origin(expression, context),
                fixes=("insert an explicit supported clock-domain crossing first",),
            )
        return ir_expr.Pipeline(
            expression.stages,
            scheduling_operand,
            context.allocate_delay(),
            scheduling_operand.type,
            origin=expression_origins.semantic_origin(expression, context),
            domain=pipeline_domain,
        )
    if isinstance(expression, ast.ImplementationChoiceExpr):
        if not context.scope.allow_implementation_choice:
            raise SemanticError(
                "implementation choice is allowed only as a complete wire-output "
                "assignment"
            )
        return IMPLEMENTATION_ANALYZER.analyze_choice(
            expression,
            inputs,
            expected,
            context,
        )
    return None

def _check_control_expression(
    expression: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    if isinstance(expression, ast.SwitchExpr):
        selector = context.expressions.check(expression.selector, inputs, None, context)
        if isinstance(selector.type, ir_types.EnumType):
            if expression.default is not None:
                raise SemanticError(
                    f"exhaustive enum switch on '{selector.type.name}' must not "
                    "contain an else arm"
                )
            if context.environment.type_resolver is None:
                raise SemanticError("enum switch requires a type resolver")
            keys: set[int] = set()
            seen_members: set[str] = set()
            keyed_arms: list[tuple[int, ast.Expression]] = []
            for arm in expression.arms:
                if not isinstance(arm.key, ast.EnumMemberRef):
                    raise SemanticError(
                        f"enum switch key for '{selector.type.name}' must be "
                        "a qualified member"
                    )
                key_type = context.environment.type_resolver.enum_type(
                    arm.key.enum_name
                )
                if key_type is None:
                    raise SemanticError(
                        f"unknown enum type '{arm.key.enum_name}' in switch label"
                    )
                if key_type != selector.type:
                    raise SemanticError(
                        f"switch label {arm.key.enum_name}.{arm.key.member} has "
                        f"enum type {key_type}, expected exact {selector.type}"
                    )
                try:
                    code = key_type.member_code(arm.key.member)
                except ValueError as error:
                    raise SemanticError(
                        f"enum '{key_type.name}' has no member '{arm.key.member}'"
                    ) from error
                type_resolution.record_named_type_definition(
                    context,
                    ast.TypeName(
                        arm.key.enum_name,
                        origin=(
                            SourceSpan(
                                arm.key.origin.start_line,
                                arm.key.origin.start_column,
                                arm.key.origin.start_line,
                                arm.key.origin.start_column + len(arm.key.enum_name),
                            )
                            if arm.key.origin is not None
                            else None
                        ),
                    ),
                    context.environment.type_resolver,
                )
                type_resolution.record_enum_member_definition(
                    context, arm.key, key_type, context.environment.type_resolver
                )
                if arm.key.member in seen_members:
                    raise SemanticError(
                        f"duplicate enum switch member "
                        f"{key_type.name}.{arm.key.member}"
                    )
                seen_members.add(arm.key.member)
                keys.add(code)
                keyed_arms.append((code, arm.expression))
            missing = tuple(
                member for member in selector.type.members
                if member not in seen_members
            )
            if missing:
                raise SemanticError(
                    f"missing enum member in switch on '{selector.type.name}': "
                    + ", ".join(missing)
                )
            branch_syntax = tuple(branch for _, branch in keyed_arms)
            branches, result_type = _check_alternatives(
                branch_syntax, inputs, expected, "switch branch", context
            )
            if isinstance(selector, ir_expr.Constant):
                for (code, _), branch in zip(
                    keyed_arms, branches, strict=True
                ):
                    if code == selector.value:
                        return branch
                raise SemanticError(
                    f"enum constant {selector.value} is outside "
                    f"'{selector.type.name}'"
                )
            cases = tuple(
                ir_expr.SwitchCase(ordinal, branch)
                for (ordinal, _), branch in zip(
                    keyed_arms, branches, strict=True
                )
            )
            # Invalid spare bit patterns are outside the nominal enum domain.
            # Keep the existing total Switch IR/backend contract by selecting
            # a deterministic unreachable default after exact coverage proof.
            return ir_expr.Switch(selector, cases, branches[0], result_type)

        if not isinstance(selector.type, (ir_types.UIntType, ir_types.BitsType)):
            raise SemanticError(
                f"switch selector must be unsigned or bits, or a nominal enum; "
                f"got {selector.type}"
            )
        if expression.default is None:
            raise SemanticError("numeric switch requires an else arm")
        keys: set[int] = set()
        for arm in expression.arms:
            if not isinstance(arm.key, int):
                raise SemanticError(
                    "numeric switch requires numeric case labels"
                )
            if arm.key in keys:
                raise SemanticError(f"duplicate switch case {arm.key}")
            if arm.key >= (1 << selector.type.width):
                raise SemanticError(
                    f"switch case {arm.key} does not fit {selector.type} selector"
                )
            keys.add(arm.key)
        branch_syntax = tuple(arm.expression for arm in expression.arms) + (
            expression.default,
        )
        branches, result_type = _check_alternatives(
            branch_syntax, inputs, expected, "switch branch", context
        )
        if isinstance(selector, ir_expr.Constant):
            for arm, branch in zip(expression.arms, branches[:-1], strict=True):
                if arm.key == selector.value:
                    return branch
            return branches[-1]
        cases = tuple(
            ir_expr.SwitchCase(arm.key, branch)
            for arm, branch in zip(expression.arms, branches[:-1], strict=True)
        )
        return ir_expr.Switch(selector, cases, branches[-1], result_type)
    return None

def _check_quantize(
    expression: ast.QuantizeExpr,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression:
    if expression.target_type is None:
        target = expected
        if not isinstance(target, (ir_types.FixedType, ir_types.UFixedType)):
            raise SemanticError(
                "contextual quantize requires an unambiguous fixed-point target"
            )
    else:
        if context.environment.type_resolver is None:
            raise SemanticError("explicit quantize target cannot be resolved here")
        target = context.environment.type_resolver.resolve(expression.target_type)
        if not isinstance(target, (ir_types.FixedType, ir_types.UFixedType)):
            raise SemanticError(f"quantize target must be fixed-point, got {target}")

    rounding = fixed_rounding(expression.rounding)
    overflow = (
        ir_expr.FixedOverflow(expression.overflow.value)
        if expression.overflow is not None
        else fixed_overflow_for_type(target)
    )

    if any(
        isinstance(value, ast.CallExpr) and value.function in semantic_limits.REAL_INTRINSICS
        for value in walk_syntax_expressions(
            expression.expression
        )
    ):
        try:
            real_value = compile_time_evaluation.compile_time_real_value(
                expression.expression, inputs, context
            )
            cache_key = (
                real_value.identity,
                target,
                rounding.value,
                overflow.value,
            )
            cached = context.services.compile_time_real_quantize_cache.get(cache_key)
            if cached is not None:
                raw, logical_operations = cached
                compile_time_evaluation.budget_step(context, logical_operations)
            else:
                logical_operations = 0

                def charge_quantization(operations: int) -> None:
                    nonlocal logical_operations
                    logical_operations += operations
                    compile_time_evaluation.budget_step(context, operations)

                raw, _precision = ct_real.quantize_to_raw(
                    real_value,
                    width=target.width,
                    fraction=target.fraction,
                    signed=isinstance(target, ir_types.FixedType),
                    rounding=rounding,
                    overflow=overflow,
                    budget_step=charge_quantization,
                )
                context.services.compile_time_real_quantize_cache[cache_key] = (
                    raw,
                    logical_operations,
                )
        except ct_real.CompileTimeRealError as error:
            raise SemanticError(str(error)) from error
        return ir_expr.Constant(raw, target)

    if isinstance(expression.expression, ast.RationalExpr):
        numerator = expression.expression.numerator
        source_type: ir_types.HardwareType
        if numerator < 0:
            source_type = ir_types.SIntType(runtime_values.minimum_signed_width(numerator))
        else:
            source_type = ir_types.UIntType(runtime_values.minimum_unsigned_width(numerator))
        source = ir_expr.Constant(numerator, source_type)
        return ir_expr.FixedConvert(
            source,
            rounding,
            overflow,
            ir_expr.FixedConversionKind.RESCALE,
            target,
            expression.expression.denominator,
        )

    source = context.expressions.check(expression.expression, inputs, None, context)
    if not isinstance(
        source.type, (ir_types.FixedType, ir_types.UFixedType, ir_types.SIntType, ir_types.UIntType)
    ):
        raise SemanticError(
            f"quantize requires a fixed-point or integer source, got {source.type}"
        )
    return ir_expr.FixedConvert(
        source,
        rounding,
        overflow,
        ir_expr.FixedConversionKind.RESCALE,
        target,
    )

def _check_operand_pair(
    left_syntax: ast.Expression,
    right_syntax: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    context: ExpressionContext,
) -> tuple[ir_expr.Expression, ir_expr.Expression]:
    folded_left = _fold_compile_time_parameter_expression(left_syntax, context)
    folded_right = _fold_compile_time_parameter_expression(right_syntax, context)
    left_is_literal = context.services.callable_specializer.is_direct_integer_literal(
        folded_left
    )
    right_is_literal = context.services.callable_specializer.is_direct_integer_literal(
        folded_right
    )
    if left_is_literal and right_is_literal:
        return (
            context.expressions.check(left_syntax, inputs, None, context),
            context.expressions.check(right_syntax, inputs, None, context),
        )
    if left_is_literal and not right_is_literal:
        right = context.expressions.check(right_syntax, inputs, None, context)
        left = context.expressions.check(left_syntax, inputs, right.type, context)
        return left, right
    left = context.expressions.check(left_syntax, inputs, None, context)
    right_expected = left.type if right_is_literal else None
    right = context.expressions.check(right_syntax, inputs, right_expected, context)
    return left, right

def _check_binary(
    expression: ast.BinaryExpr,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    context: ExpressionContext,
) -> ir_expr.Expression:
    if expression.operator in {
        ast.BinaryOperator.LOGIC_AND,
        ast.BinaryOperator.LOGIC_OR,
    }:
        raise SemanticError(
            f"runtime logical operator '{expression.operator.value}' is not "
            "supported; use bitwise '&'/'|' for hardware values, or keep "
            "'&&'/'||' inside a compile-time condition or equiv guard",
            code="ZL-SEMANTIC-RUNTIME-LOGIC",
            primary=expression_origins.semantic_origin(expression, context),
            fixes=(
                "replace the operator with '&' or '|' when both operands are bits",
            ),
        )
    if expression.operator is ast.BinaryOperator.DIVIDE:
        raise SemanticError(
            "division is only supported in compile-time constant expressions"
        )
    if expression.operator in {
        ast.BinaryOperator.SHIFT_LEFT, ast.BinaryOperator.SHIFT_RIGHT,
    }:
        amount = _constant_parameter_expression_text(expression.right, context)
        if amount is not None and context.environment.type_resolver is not None:
            resolved_amount = context.environment.type_resolver._eval_constant_integer(
                amount,
                description="shift amount",
                allow_zero=True,
                allow_negative=True,
            )
            if resolved_amount < 0:
                raise SemanticError("shift amount must be non-negative")
    operator = ir_expr.BinaryOperator(expression.operator.value)
    if operator in {
        ir_expr.BinaryOperator.SHIFT_LEFT,
        ir_expr.BinaryOperator.SHIFT_RIGHT,
    }:
        left = context.expressions.check(expression.left, inputs, None, context)
        right = context.expressions.check(expression.right, inputs, None, context)
    else:
        left, right = _check_operand_pair(
            expression.left, expression.right, inputs, context
        )
    if (
        operator in {ir_expr.BinaryOperator.SUBTRACT, ir_expr.BinaryOperator.MULTIPLY}
        and (isinstance(left.type, ir_types.StructType) or isinstance(right.type, ir_types.StructType))
    ):
        return expression_operators.resolve_operator(
            operator.value,
            (left, right),
            context,
            call_origin=expression_origins.semantic_origin(expression, context),
        )
    if operator in {
        ir_expr.BinaryOperator.EQUAL,
        ir_expr.BinaryOperator.NOT_EQUAL,
    } and (
        isinstance(left.type, (ir_types.StructType, ir_types.TupleType, ir_types.VecType))
        or isinstance(right.type, (ir_types.StructType, ir_types.TupleType, ir_types.VecType))
    ):
        if left.type != right.type:
            raise SemanticError(
                f"aggregate equality requires one exact type, got "
                f"{left.type} and {right.type}"
            )
        equal = expression_operators.build_aggregate_equality(left, right)
        if operator is ir_expr.BinaryOperator.EQUAL:
            return equal
        return expression_operators.build_binary(
            ir_expr.BinaryOperator.EQUAL,
            equal,
            ir_expr.Constant(0, ir_types.BitType()),
        )
    result = expression_operators.build_binary(operator, left, right)
    if context.scope.functional_symbolic_values and isinstance(result, ir_expr.Binary):
        simplified = exact_simplification.simplify_binary(
            result.operator,
            result.left,
            result.right,
            result.type,
            range_of=lambda value: (
                (value_range.minimum, value_range.maximum)
                if (
                    value_range := expression_ranges.static_value_range(value)
                )
                is not None
                else None
            ),
        )
        if simplified is not None:
            return simplified
    return result

def _check_alternatives(
    syntax: tuple[ast.Expression, ...],
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    description: str,
    context: ExpressionContext,
) -> tuple[tuple[ir_expr.Expression, ...], ir_types.HardwareType]:
    result_type = expected
    if result_type is None:
        first_nonliteral = next(
            (
                item
                for item in syntax
                if not context.services.callable_specializer.is_direct_integer_literal(
                    item
                )
            ),
            syntax[0],
        )
        result_type = context.expressions.check(
            first_nonliteral, inputs, None, context
        ).type
    branches = tuple(
        context.expressions.check(item, inputs, result_type, context) for item in syntax
    )
    for branch in branches:
        if branch.type != result_type:
            raise SemanticError(
                f"{description} has type {branch.type}, expected {result_type}"
            )
    return branches, result_type
