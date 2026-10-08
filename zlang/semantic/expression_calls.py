# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative calls expression semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import callables as ir_callables
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir import runtime_values as runtime_values
from zlang.ir import types as ir_types
from . import callables as semantic_callables
from . import compile_time_evaluation
from . import expression_origins
from . import expression_operators
from . import limits as semantic_limits
from . import observations
from . import symbols as semantic_symbols
from .errors import SemanticError
from .expression_coercion import can_implicitly_bitcast_types, make_bitcast
from .expression_collections import _check_reduction

if TYPE_CHECKING:
    from .context import ExpressionContext

def _check_call_intrinsic(
    expression: ast.CallExpr,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    if expression.function == "repeat":
        if len(expression.arguments) != 1:
            raise SemanticError("repeat expects one value argument")
        if len(expression.specializations) > 1:
            raise SemanticError("repeat accepts at most one positional length")
        explicit_length: int | None = None
        if expression.specializations:
            argument = expression.specializations[0]
            if argument.name is not None or not isinstance(argument.value, int):
                raise SemanticError(
                    "repeat length must be one positional compile-time integer"
                )
            explicit_length = argument.value
            if explicit_length <= 0:
                raise SemanticError("repeat length must be positive")
        if expected is not None and not isinstance(expected, ir_types.VecType):
            raise SemanticError(f"repeat cannot initialize non-vector {expected}")
        contextual_length = expected.length if isinstance(expected, ir_types.VecType) else None
        if explicit_length is None and contextual_length is None:
            raise SemanticError(
                "repeat requires either repeat<N>(value) or a declared vec<N,T> target"
            )
        if (
            explicit_length is not None
            and contextual_length is not None
            and explicit_length != contextual_length
        ):
            raise SemanticError(
                f"repeat length {explicit_length} does not match target length "
                f"{contextual_length}"
            )
        length = explicit_length if explicit_length is not None else contextual_length
        assert length is not None
        element_expected = expected.element_type if isinstance(expected, ir_types.VecType) else None
        element = (
            context.expressions.check_typed_boundary(
                expression.arguments[0], inputs, element_expected, context
            )
            if element_expected is not None
            else context.expressions.check(expression.arguments[0], inputs, None, context)
        )
        type_ = ir_types.VecType(length, element.type)
        return ir_expr.Generate(
            "repeat", 0, length, tuple(element for _ in range(length)), type_
        )
    if expression.function == "enum_encode":
        if expression.specializations:
            raise SemanticError(
                "enum_encode does not accept specialization arguments"
            )
        if len(expression.arguments) != 1:
            raise SemanticError("enum_encode expects one argument")
        operand = context.expressions.check(
            expression.arguments[0], inputs, None, context
        )
        if not isinstance(operand.type, ir_types.EnumType):
            raise SemanticError(
                f"enum_encode requires a nominal enum input, got {operand.type}"
            )
        result_type = ir_types.BitsType(operand.type.width)
        if expected is not None and expected != result_type:
            raise SemanticError(
                f"enum_encode produces {result_type}, expected exact {expected}"
            )
        return ir_expr.EnumEncode(operand, result_type)
    if expression.function in {"enum_valid", "enum_decode"}:
        if len(expression.specializations) != 1:
            raise SemanticError(
                f"{expression.function} requires one explicit enum type"
            )
        specialization = expression.specializations[0]
        if specialization.name is not None or not isinstance(
            specialization.value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)
        ):
            raise SemanticError(
                f"{expression.function} requires one positional enum type"
            )
        if context.environment.type_resolver is None:
            raise SemanticError(
                f"{expression.function} requires a type resolver"
            )
        enum_type = context.environment.type_resolver.resolve(specialization.value)
        if not isinstance(enum_type, ir_types.EnumType):
            raise SemanticError(
                f"{expression.function} target must be a nominal enum, got {enum_type}"
            )
        expected_arity = 1 if expression.function == "enum_valid" else 2
        if len(expression.arguments) != expected_arity:
            raise SemanticError(
                f"{expression.function} expects {expected_arity} argument"
                f"{'s' if expected_arity != 1 else ''}"
            )
        raw_type = ir_types.BitsType(enum_type.width)
        raw = context.expressions.check(
            expression.arguments[0], inputs, raw_type, context
        )
        if raw.type != raw_type:
            raise SemanticError(
                f"{expression.function}<{enum_type.name}> requires exact "
                f"{raw_type} input, got {raw.type}"
            )
        if expression.function == "enum_valid":
            if expected is not None and expected != ir_types.BitType():
                raise SemanticError(
                    f"enum_valid produces bit, expected exact {expected}"
                )
            return ir_expr.EnumValid(raw, enum_type, ir_types.BitType())
        fallback = context.expressions.check(
            expression.arguments[1], inputs, enum_type, context
        )
        if fallback.type != enum_type:
            raise SemanticError(
                f"enum_decode fallback has type {fallback.type}, expected exact {enum_type}"
            )
        if expected is not None and expected != enum_type:
            raise SemanticError(
                f"enum_decode produces {enum_type}, expected exact {expected}"
            )
        return ir_expr.EnumDecode(raw, fallback, enum_type)
    if expression.function == "parity":
        if expression.specializations:
            raise SemanticError("parity does not accept specialization arguments")
        if len(expression.arguments) != 1:
            raise SemanticError("parity expects one argument")
        collection = context.expressions.check(
            expression.arguments[0], inputs, None, context
        )
        if isinstance(collection.type, ir_types.VecType):
            if not isinstance(collection.type.element_type, ir_types.BitType):
                raise SemanticError(
                    f"parity vector input must be vec<N,bit>, got {collection.type}"
                )
            return _check_reduction(
                ir_expr.ReductionOperator.BIT_XOR, collection, context
            )
        if not isinstance(
            collection.type, (ir_types.BitType, ir_types.BitsType, ir_types.UIntType, ir_types.SIntType)
        ):
            raise SemanticError(
                f"parity requires bit, bits, unsigned, signed, or vec<N,bit> "
                f"input, got {collection.type}"
            )
        raw_vector = make_bitcast(
            collection,
            ir_types.VecType(collection.type.width, ir_types.BitType()),
            description="parity",
        )
        return _check_reduction(
            ir_expr.ReductionOperator.BIT_XOR, raw_vector, context
        )
    if expression.function in semantic_limits.REAL_INTRINSICS:
        if isinstance(expected, (ir_types.FixedType, ir_types.UFixedType)):
            raise SemanticError(
                f"intrinsic '{expression.function}' returns a compile-time real value; "
                "use explicit quantize(...) for fixed-point hardware"
            )
        value = compile_time_evaluation.compile_time_real_value(expression, inputs, context)
        exact = value.exact_integer()
        if exact is None:
            raise SemanticError(
                f"intrinsic '{expression.function}' produced a non-integral compile-time real value; "
                "use explicit quantize(...) for fixed-point hardware"
            )
        result_type: ir_types.HardwareType = expected or (
            ir_types.SIntType(runtime_values.minimum_signed_width(exact))
            if exact < 0
            else ir_types.UIntType(runtime_values.minimum_unsigned_width(exact))
        )
        if not isinstance(result_type, (ir_types.BitType, ir_types.UIntType, ir_types.SIntType, ir_types.BitsType)):
            raise SemanticError(
                f"intrinsic '{expression.function}' exact integer result cannot produce {result_type}"
            )
        if not expression_operators.constant_fits(exact, result_type):
            raise SemanticError(
                f"intrinsic '{expression.function}' result {exact} does not fit {result_type}"
            )
        return ir_expr.Constant(exact, result_type)
    if expression.function in {
        "length", "floor_log2", "ceil_log2", "index_width",
        "is_power_of_two",
    }:
        if len(expression.arguments) != 1:
            raise SemanticError(
                f"intrinsic '{expression.function}' expects one argument"
            )
        value = compile_time_evaluation.compile_time_integer_value(
            expression, inputs, context
        )
        result_type: ir_types.HardwareType = (
            ir_types.BitType()
            if expression.function == "is_power_of_two"
            else expected or ir_types.UIntType(max(1, value.bit_length()))
        )
        if not isinstance(result_type, (ir_types.BitType, ir_types.UIntType, ir_types.BitsType)):
            raise SemanticError(
                f"intrinsic '{expression.function}' result must be integral"
            )
        if isinstance(result_type, (ir_types.UIntType, ir_types.BitsType)) and not expression_operators.constant_fits(value, result_type):
            raise SemanticError(
                f"intrinsic '{expression.function}' result {value} does not fit {result_type}"
            )
        return ir_expr.Constant(value, result_type)
    fixed_intrinsics = {
        "fixed_truncate_wrap": (ir_expr.FixedRounding.TOWARD_ZERO, ir_expr.FixedOverflow.WRAP),
        "fixed_truncate_saturate": (ir_expr.FixedRounding.TOWARD_ZERO, ir_expr.FixedOverflow.SATURATE),
        "fixed_round_even_wrap": (ir_expr.FixedRounding.NEAREST_EVEN, ir_expr.FixedOverflow.WRAP),
        "fixed_round_even_saturate": (ir_expr.FixedRounding.NEAREST_EVEN, ir_expr.FixedOverflow.SATURATE),
    }
    if expression.function == "fixed_to_raw":
        if len(expression.arguments) != 1:
            raise SemanticError("fixed_to_raw expects one argument")
        operand = context.expressions.check(expression.arguments[0], inputs, None, context)
        if not isinstance(operand.type, (ir_types.FixedType, ir_types.UFixedType)):
            raise SemanticError(f"fixed_to_raw requires fixed-point input, got {operand.type}")
        result_type = (ir_types.SIntType if isinstance(operand.type, ir_types.FixedType) else ir_types.UIntType)(operand.type.width)
        if (
            expected is not None
            and expected != result_type
            and not can_implicitly_bitcast_types(result_type, expected)
        ):
            raise SemanticError(f"fixed_to_raw produces {result_type}, expected {expected}")
        return ir_expr.FixedConvert(
            operand, ir_expr.FixedRounding.TOWARD_ZERO, ir_expr.FixedOverflow.WRAP,
            ir_expr.FixedConversionKind.TO_RAW, result_type,
        )
    if expression.function == "fixed_raw":
        if len(expression.arguments) != 1 or not isinstance(expected, (ir_types.FixedType, ir_types.UFixedType)):
            raise SemanticError("fixed_raw requires one argument and a fixed-point assignment context")
        raw_type = ir_types.SIntType(expected.width) if isinstance(expected, ir_types.FixedType) else ir_types.UIntType(expected.width)
        argument = expression.arguments[0]
        if (
            isinstance(argument, ast.UnaryExpr)
            and argument.operator is ast.BinaryOperator.SUBTRACT
            and isinstance(argument.expression, ast.NumberExpr)
        ):
            raise SemanticError(
                "fixed_raw requires a non-negative integral raw bit pattern"
            )
        if isinstance(argument, ast.NumberExpr):
            if not 0 <= argument.value < (1 << expected.width):
                raise SemanticError(
                    f"fixed_raw pattern {argument.value} does not fit {expected.width} bits"
                )
            operand = ir_expr.Constant(runtime_values.normalize_scalar(argument.value, raw_type), raw_type)
        elif isinstance(argument, ast.RationalExpr):
            raise SemanticError("fixed_raw requires an integral raw bit pattern")
        else:
            operand = context.expressions.check(argument, inputs, None, context)
        if operand.type != raw_type:
            raise SemanticError(f"fixed_raw for {expected} requires {raw_type}, got {operand.type}")
        return ir_expr.FixedConvert(
            operand, ir_expr.FixedRounding.TOWARD_ZERO, ir_expr.FixedOverflow.WRAP,
            ir_expr.FixedConversionKind.FROM_RAW, expected,
        )
    if expression.function in fixed_intrinsics:
        if len(expression.arguments) != 1 or not isinstance(expected, (ir_types.FixedType, ir_types.UFixedType)):
            raise SemanticError(
                f"{expression.function} requires one argument and a fixed-point assignment context"
            )
        operand = context.expressions.check(expression.arguments[0], inputs, None, context)
        compatible = (
            isinstance(expected, ir_types.FixedType) and isinstance(operand.type, (ir_types.FixedType, ir_types.SIntType))
        ) or (
            isinstance(expected, ir_types.UFixedType) and isinstance(operand.type, (ir_types.UFixedType, ir_types.UIntType))
        )
        if not compatible:
            raise SemanticError(
                f"{expression.function} cannot convert {operand.type} to {expected}"
            )
        rounding, overflow = fixed_intrinsics[expression.function]
        return ir_expr.FixedConvert(
            operand, rounding, overflow, ir_expr.FixedConversionKind.RESCALE, expected,
        )
    return None

def _check_resolved_call(
    expression: ast.CallExpr,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    call_origin = expression_origins.semantic_origin(expression, context)
    reference_origin = expression_origins.callable_reference_origin(expression, context)
    signature = semantic_callables._lookup_function_signature(
        context,
        expression.function,
        call_origin=call_origin,
    )
    generic = context.environment.generic_functions.get(expression.function)
    if generic is not None:
        observations.record_definition(
            context,
            reference_origin,
            observations.declaration_origin(
                generic.name_origin or generic.origin,
                f"function {generic.name}",
                context,
                source_unit=generic.source_identity,
            ),
            name=generic.name,
            kind="function",
        )
        arguments = tuple(
            context.expressions.check(argument, inputs, None, context)
            for argument in expression.arguments
        )
        arguments = context.services.callable_specializer.contextualize_integer_arguments(
            generic,
            expression.arguments,
            arguments,
            expression.specializations,
            context,
        )
        symbolic_result = context.services.callable_specializer.try_functional(
            generic,
            arguments,
            expression.specializations,
            context,
            specialization_symbols=inputs,
            call_origin=expression_origins.semantic_origin(expression, context),
        )
        if symbolic_result is not None:
            return symbolic_result
        result = context.services.callable_specializer.specialize(
            generic,
            arguments,
            expression.specializations,
            context,
            call_origin=expression_origins.semantic_origin(expression, context),
            specialization_symbols=inputs,
        )
        if isinstance(result, ir_expr.Call):
            definition = (
                context.services.callables.callable_definitions.get(
                    result.callee_identity
                )
                if result.callee_identity is not None
                else None
            )
            if definition is not None:
                observations.record_signature_help_call(
                    context.services.tooling.signature_help_calls,
                    expression,
                    context,
                    definition.parameters,
                    definition.return_type,
                )
                # Publish scalar constants immediately after generic
                # specialization.  The cached definition and provenance
                # record remain available for later identical calls, while
                # surrounding typed arithmetic can simplify before a
                # functional domain is materialized.
                try:
                    constant_value = constant_runtime_value(definition.body)
                except ConstantExpressionError:
                    constant_value = None
                if (
                    context.scope.functional_symbolic_values
                    and isinstance(constant_value, int)
                    and isinstance(
                        definition.return_type,
                        (
                            ir_types.BitType,
                            ir_types.BitsType,
                            ir_types.EnumType,
                            ir_types.FixedType,
                            ir_types.SIntType,
                            ir_types.UFixedType,
                            ir_types.UIntType,
                        ),
                    )
                ):
                    return ir_expr.Constant(
                        runtime_values.normalize_scalar(constant_value, definition.return_type),
                        definition.return_type,
                        origin=call_origin,
                    )
        return result
    static_callable = context.scope.static_callables.get(expression.function)
    if static_callable is not None:
        if expression.specializations:
            raise SemanticError(
                f"callable parameter '{expression.function}' is already "
                "statically specialized"
            )
        if len(expression.arguments) != len(static_callable.parameter_types):
            raise SemanticError(
                f"callable parameter '{expression.function}' expects "
                f"{len(static_callable.parameter_types)} arguments, got "
                f"{len(expression.arguments)}"
            )
        arguments = tuple(
            context.expressions.check(argument, inputs, expected_type, context)
            for argument, expected_type in zip(
                expression.arguments,
                static_callable.parameter_types,
                strict=True,
            )
        )
        for index, (argument, expected_type) in enumerate(
            zip(arguments, static_callable.parameter_types, strict=True)
        ):
            if argument.type != expected_type:
                raise SemanticError(
                    f"argument {index} to callable parameter "
                    f"'{expression.function}' has type {argument.type}, "
                    f"expected exact {expected_type}"
                )
        reference = static_callable.reference
        concrete_definition = context.services.callables.callable_definitions.get(
            static_callable.callee_identity
        )
        if concrete_definition is not None:
            actual_parameters = tuple(
                parameter.type for parameter in concrete_definition.parameters
            )
            if (
                actual_parameters != static_callable.parameter_types
                or concrete_definition.return_type
                != static_callable.return_type
            ):
                raise SemanticError(
                    f"callable parameter '{expression.function}' concrete "
                    "definition does not match its exact signature"
                )
            context.services.callables.record_use(concrete_definition.callee_identity)
            result = ir_expr.Call(
                concrete_definition.name,
                arguments,
                concrete_definition.return_type,
                concrete_definition.callee_identity,
                origin=expression_origins.semantic_origin(expression, context),
            )
        elif context.environment.generic_functions.get(reference.name) is not None:
            # A generic callable actual is fully specialized and published
            # when the binding is accepted.  Re-specializing here could
            # change identity under a child module's dependency context.
            raise SemanticError(
                f"callable parameter '{expression.function}' has no "
                "published concrete definition"
            )
        else:
            target_signature = semantic_callables._lookup_function_signature(
                context,
                reference.name,
                call_origin=call_origin,
            )
            if target_signature is None:
                raise SemanticError(
                    f"statically selected function '{reference.name}' is unavailable"
                )
            result = ir_expr.Call(
                reference.name,
                arguments,
                target_signature.return_type,
                static_callable.callee_identity,
            )
        if result.type != static_callable.return_type:
            raise SemanticError(
                f"callable parameter '{expression.function}' returns "
                f"{result.type}, expected exact {static_callable.return_type}"
            )
        if result.callee_identity != static_callable.callee_identity:
            raise SemanticError(
                f"callable parameter '{expression.function}' resolved to "
                "a different concrete callee identity"
            )
        if concrete_definition is not None:
            observations.record_signature_help_call(
                context.services.tooling.signature_help_calls,
                expression,
                context,
                concrete_definition.parameters,
                concrete_definition.return_type,
            )
        else:
            observations.record_signature_help_call(
                context.services.tooling.signature_help_calls,
                expression,
                context,
                target_signature.parameters,
                target_signature.return_type,
            )
        return result
    if signature is None:
        raise SemanticError(f"unknown function '{expression.function}'")
    observations.record_definition(
        context,
        reference_origin,
        observations.declaration_origin(
            signature.declaration.name_origin or signature.declaration.origin,
            f"function {signature.declaration.name}",
            context,
            source_unit=signature.declaration.source_identity,
        ),
        name=signature.declaration.name,
        kind="function",
    )
    if len(expression.arguments) != len(signature.parameters):
        raise SemanticError(
            f"function '{expression.function}' expects "
            f"{len(signature.parameters)} arguments, got "
            f"{len(expression.arguments)}"
        )
    arguments: list[ir_expr.Expression] = []
    for argument_syntax, parameter in zip(
        expression.arguments, signature.parameters, strict=True
    ):
        argument = context.expressions.check(
            argument_syntax, inputs, parameter.type, context
        )
        if argument.type != parameter.type:
            raise SemanticError(
                f"argument '{parameter.name}' to function "
                f"'{expression.function}' has type {argument.type}, "
                f"expected {parameter.type}"
            )
        arguments.append(argument)
    observations.record_signature_help_call(
        context.services.tooling.signature_help_calls,
        expression,
        context,
        signature.parameters,
        signature.return_type,
    )
    metadata = ir_callables.source_function_metadata(
        signature.declaration.name,
        signature.declaration.source_identity
        or context.scope.source_unit
        or "<source>",
    )
    return ir_expr.Call(
        expression.function,
        tuple(arguments),
        signature.return_type,
        ir_callables.stable_callee_identity(
            signature.parameters,
            signature.return_type,
            metadata,
        ),
    )
