# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Exact explicit and inferred arguments for callable specialization."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from zlang.ast import nodes as ast
from zlang.ir import callables as ir_callables
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import packing as ir_packing
from zlang.ir import types as ir_types
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.source import SourceOrigin

from . import type_resolution
from .callables import (
    StaticCallableBinding,
    _expand_analysis_calls,
    _lookup_function_signature,
    _static_callable_definition_closure,
)
from .errors import SemanticError
from .expression_coercion import check_integer_literal
from .expression_origins import semantic_origin
from .generic_binding import GenericBinder

if TYPE_CHECKING:
    from .context import ExpressionContext


class CallableSpecializationOwner(Protocol):
    """Narrow recursion boundary required by callable-valued arguments."""

    def specialize(
        self,
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        arguments: tuple[ir_expr.Expression, ...],
        explicit: tuple[ast.SpecializationArgument, ...],
        context: ExpressionContext,
        *,
        call_origin: SourceOrigin | None = None,
        specialization_symbols: dict[str, object] | None = None,
    ) -> ir_expr.Expression: ...


class SpecializationArgumentBinder:
    """Own exact generic argument binding and contextual literal typing."""

    def bindings(
        self,
        specializer: CallableSpecializationOwner,
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        explicit: tuple[ast.SpecializationArgument, ...],
        arguments: tuple[ir_expr.Expression, ...],
        context: ExpressionContext,
        specialization_symbols: dict[str, object] | None = None,
    ) -> tuple[
        dict[str, ir_types.HardwareType],
        dict[str, int],
        dict[str, ir_expr.Expression],
        dict[str, StaticCallableBinding],
    ]:
        if context.environment.type_resolver is None:
            raise SemanticError("generic specialization requires a type resolver")
        parameters = {item.name: item for item in declaration.generic_parameters}
        type_bindings: dict[str, ir_types.HardwareType] = {}
        value_bindings: dict[str, int] = {}
        constant_bindings: dict[str, ir_expr.Expression] = {}
        callable_bindings: dict[str, StaticCallableBinding] = {}
        positional = [item for item in explicit if item.name is None]
        named = [item for item in explicit if item.name is not None]
        if positional and named:
            raise SemanticError(
                "generic specialization cannot mix positional and named arguments"
            )
        explicit_names = tuple(item.name for item in named)
        if len(explicit_names) != len(set(explicit_names)):
            duplicate = next(
                name for name in explicit_names if explicit_names.count(name) > 1
            )
            raise SemanticError(
                f"generic specialization parameter '{duplicate}' is assigned more than once"
            )
        deferred: list[tuple[ast.ModuleParameter, ast.SpecializationArgument]] = []
        for index, item in enumerate(explicit):
            name = item.name or (
                declaration.generic_parameters[index].name
                if index < len(declaration.generic_parameters)
                else ""
            )
            parameter = parameters.get(name)
            if parameter is None:
                raise SemanticError(f"unknown or excess generic argument '{name}'")
            if parameter.kind in {"constant", "callable"}:
                if item.name is None:
                    raise SemanticError(
                        f"compile-time {parameter.kind} parameter '{name}' requires "
                        "a named specialization argument"
                    )
                deferred.append((parameter, item))
                continue
            if parameter.kind == "type":
                syntax = (
                    item.value
                    if isinstance(
                        item.value,
                        (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName),
                    )
                    else ast.TypeName(str(item.value))
                )
                type_bindings[name] = context.environment.type_resolver.resolve(syntax)
            else:
                try:
                    if isinstance(item.value, int):
                        value_bindings[name] = item.value
                    else:
                        text = (
                            item.value.text
                            if isinstance(item.value, ast.TypeName)
                            else str(item.value)
                        )
                        value_bindings[name] = (
                            context.environment.type_resolver._eval_constant_integer(
                                text,
                                description=f"value parameter '{name}'",
                                allow_zero=True,
                                allow_negative=True,
                                local_values={
                                    **context.environment.parameters,
                                    **context.scope.index_bindings,
                                },
                            )
                        )
                except (TypeError, ValueError, SemanticError) as error:
                    raise SemanticError(
                        f"value parameter '{name}' requires an integer"
                    ) from error
        if len(arguments) != len(declaration.parameters):
            callable_name = (
                declaration.name
                if isinstance(declaration, ast.FunctionDecl)
                else f"operator {declaration.operator}"
            )
            raise SemanticError(
                f"'{callable_name}' expects "
                f"{len(declaration.parameters)} arguments, got {len(arguments)}"
            )
        generic_binder = GenericBinder(
            parameters,
            type_bindings,
            value_bindings,
            context.environment.type_resolver,
        )
        for formal, actual in zip(declaration.parameters, arguments, strict=True):
            generic_binder.bind_callable_type(formal.type_name, actual.type)
        for parameter in declaration.generic_parameters:
            if parameter.kind == "type" and parameter.name not in type_bindings:
                raise SemanticError(f"cannot infer type parameter '{parameter.name}'")
            if parameter.kind == "value" and parameter.name not in value_bindings:
                if isinstance(parameter.default, int):
                    value_bindings[parameter.name] = parameter.default
                else:
                    raise SemanticError(
                        f"cannot infer value parameter '{parameter.name}'"
                    )

        assert context.environment.type_resolver is not None
        binding_resolver = type_resolution.TypeResolver(
            tuple(
                ast.TypeAlias(name, target)
                for name, target in context.environment.type_resolver._aliases.items()
            ),
            tuple(context.environment.type_resolver._structs.values()),
            tuple(context.environment.type_resolver._enum_declarations.values()),
            declaration.generic_parameters,
            {**context.environment.parameters, **value_bindings},
            {
                **type_bindings,
                **{str(type_): type_ for type_ in type_bindings.values()},
            },
            context.environment.type_resolver._identity_namespace,
            tagged_unions=tuple(
                context.environment.type_resolver._tagged_union_declarations.values()
            ),
        )

        symbols = specialization_symbols or {}
        for parameter, item in deferred:
            if parameter.kind == "constant":
                assert parameter.type_name is not None
                expected_type = binding_resolver.resolve(parameter.type_name)
                if isinstance(
                    expected_type, ir_types.EnumType
                ) or type_resolution.contains_nominal_type(
                    expected_type, ir_types.EnumType
                ):
                    raise SemanticError(
                        f"compile-time constant parameter '{parameter.name}' cannot contain an enum"
                    )
                try:
                    ir_packing.packed_width(expected_type)
                except ir_packing.PackingError as error:
                    raise SemanticError(
                        f"compile-time constant parameter '{parameter.name}' type "
                        f"{expected_type} is not recursively bit-packable: {error}"
                    ) from error
                constant_name = (
                    item.value.text
                    if isinstance(item.value, ast.TypeName)
                    else item.value
                )
                if not isinstance(constant_name, str):
                    raise SemanticError(
                        f"compile-time constant parameter '{parameter.name}' requires "
                        "an immutable named value"
                    )
                candidate = context.scope.compile_time_constants.get(constant_name)
                if candidate is None:
                    symbol = symbols.get(constant_name)
                    if isinstance(symbol, ir_module.LocalValue) and symbol.compile_time:
                        candidate = symbol.expression
                    elif isinstance(symbol, ir_expr.Expression):
                        candidate = symbol
                if candidate is None:
                    raise SemanticError(
                        f"compile-time constant argument '{constant_name}' for "
                        f"'{parameter.name}' is not an immutable compile-time value"
                    )
                candidate = _expand_analysis_calls(
                    candidate,
                    context,
                    purpose=f"compile-time constant parameter '{parameter.name}'",
                )
                if candidate.type != expected_type:
                    raise SemanticError(
                        f"compile-time constant parameter '{parameter.name}' has type "
                        f"{candidate.type}, expected exact {expected_type}"
                    )
                try:
                    constant_runtime_value(candidate)
                except ConstantExpressionError as error:
                    raise SemanticError(
                        f"compile-time constant parameter '{parameter.name}' is not "
                        f"fully constant: {error}"
                    ) from error
                # Re-materialize through the already typed expression; the digest
                # enters specialization identity while source spelling does not.
                constant_bindings[parameter.name] = candidate
                continue

            assert parameter.kind == "callable"
            if not isinstance(item.value, ast.CallableRef):
                raise SemanticError(
                    f"compile-time callable parameter '{parameter.name}' requires "
                    "an explicit 'fn name' reference"
                )
            expected_parameters = tuple(
                binding_resolver.resolve(type_name)
                for type_name in parameter.callable_parameters
            )
            assert parameter.callable_return_type is not None
            expected_return = binding_resolver.resolve(parameter.callable_return_type)
            forwarded = context.scope.static_callables.get(item.value.name)
            if forwarded is not None:
                if item.value.specializations:
                    raise SemanticError(
                        f"forwarded callable '{item.value.name}' cannot be re-specialized"
                    )
                if (
                    forwarded.parameter_types != expected_parameters
                    or forwarded.return_type != expected_return
                ):
                    raise SemanticError(
                        f"callable parameter '{parameter.name}' expects "
                        f"fn({', '.join(map(str, expected_parameters))})->{expected_return}, "
                        f"got fn({', '.join(map(str, forwarded.parameter_types))})->"
                        f"{forwarded.return_type}"
                    )
                callable_bindings[parameter.name] = forwarded
                continue
            signature = _lookup_function_signature(
                context,
                item.value.name,
            )
            generic = context.environment.generic_functions.get(item.value.name)
            if signature is None and generic is None:
                raise SemanticError(
                    f"unknown pure function '{item.value.name}' for callable "
                    f"parameter '{parameter.name}'"
                )
            if signature is not None:
                if item.value.specializations:
                    raise SemanticError(
                        f"non-generic function '{item.value.name}' does not accept "
                        "specialization arguments"
                    )
                actual_parameters = tuple(item.type for item in signature.parameters)
                actual_return = signature.return_type
                if (
                    actual_parameters != expected_parameters
                    or actual_return != expected_return
                ):
                    raise SemanticError(
                        f"callable parameter '{parameter.name}' expects "
                        f"fn({', '.join(map(str, expected_parameters))})->{expected_return}, "
                        f"got fn({', '.join(map(str, actual_parameters))})->{actual_return}"
                    )
                concrete_identity = ir_callables.stable_callee_identity(
                    signature.parameters,
                    signature.return_type,
                    ir_callables.source_function_metadata(
                        signature.declaration.name,
                        signature.declaration.source_identity
                        or context.scope.source_unit
                        or "<source>",
                    ),
                )
                concrete_definitions = _static_callable_definition_closure(
                    ir_expr.Call(
                        item.value.name,
                        tuple(
                            ir_expr.ParameterRef(
                                f"__zlang_callable_argument_{index}", type_
                            )
                            for index, type_ in enumerate(expected_parameters)
                        ),
                        expected_return,
                        concrete_identity,
                    ),
                    context,
                )
            else:
                assert generic is not None
                placeholders = tuple(
                    ir_expr.ParameterRef(
                        f"__zlang_callable_argument_{index}",
                        type_,
                    )
                    for index, type_ in enumerate(expected_parameters)
                )
                concrete_call = specializer.specialize(
                    generic,
                    placeholders,
                    item.value.specializations,
                    context,
                    specialization_symbols=symbols,
                )
                if concrete_call.type != expected_return:
                    raise SemanticError(
                        f"callable parameter '{parameter.name}' expects "
                        f"fn({', '.join(map(str, expected_parameters))})->{expected_return}, "
                        f"got fn({', '.join(map(str, expected_parameters))})->"
                        f"{concrete_call.type}"
                    )
                if concrete_call.callee_identity is None:
                    raise SemanticError(
                        f"generic callable argument '{item.value.name}' did not "
                        "resolve to a concrete callee identity"
                    )
                concrete_definition = (
                    context.services.callables.callable_definitions.get(
                        concrete_call.callee_identity
                    )
                )
                if concrete_definition is None:
                    raise SemanticError(
                        f"generic callable argument '{item.value.name}' did not "
                        "publish its concrete definition"
                    )
                actual_parameters = tuple(
                    item.type for item in concrete_definition.parameters
                )
                if (
                    actual_parameters != expected_parameters
                    or concrete_definition.return_type != expected_return
                ):
                    raise SemanticError(
                        f"callable parameter '{parameter.name}' expects "
                        f"fn({', '.join(map(str, expected_parameters))})->{expected_return}, "
                        f"got fn({', '.join(map(str, actual_parameters))})->"
                        f"{concrete_definition.return_type}"
                    )
                concrete_identity = concrete_definition.callee_identity
                concrete_definitions = _static_callable_definition_closure(
                    concrete_call, context
                )
            callable_bindings[parameter.name] = StaticCallableBinding(
                item.value,
                expected_parameters,
                expected_return,
                concrete_identity,
                concrete_definitions,
            )

        missing_constant = next(
            (
                parameter.name
                for parameter in declaration.generic_parameters
                if parameter.kind == "constant"
                and parameter.name not in constant_bindings
            ),
            None,
        )
        if missing_constant is not None:
            raise SemanticError(
                f"missing required compile-time constant argument '{missing_constant}'"
            )
        missing_callable = next(
            (
                parameter.name
                for parameter in declaration.generic_parameters
                if parameter.kind == "callable"
                and parameter.name not in callable_bindings
            ),
            None,
        )
        if missing_callable is not None:
            raise SemanticError(
                f"missing required compile-time callable argument '{missing_callable}'"
            )
        return type_bindings, value_bindings, constant_bindings, callable_bindings

    @staticmethod
    def is_direct_integer_literal(expression: ast.Expression) -> bool:
        """Return whether syntax is one direct integer literal, including ``-N``."""

        return isinstance(expression, ast.NumberExpr) or (
            isinstance(expression, ast.UnaryExpr)
            and expression.operator is ast.BinaryOperator.SUBTRACT
            and isinstance(expression.expression, ast.NumberExpr)
        )

    @staticmethod
    def contextualize_integer_arguments(
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        syntax_arguments: tuple[ast.Expression, ...],
        typed_arguments: tuple[ir_expr.Expression, ...],
        explicit: tuple[ast.SpecializationArgument, ...],
        context: ExpressionContext,
    ) -> tuple[ir_expr.Expression, ...]:
        """Retype direct integer arguments after exact generic shape inference.

        A literal must not choose a generic type before a non-literal argument (or
        an explicit specialization) has fixed that type.  Conversely, the call's
        expected result is deliberately absent here: ``id(0)`` still specializes
        to the minimum literal type unless ``T`` is fixed at the call itself.
        """

        literal_positions = tuple(
            index
            for index, value in enumerate(syntax_arguments)
            if SpecializationArgumentBinder.is_direct_integer_literal(value)
        )
        if (
            not literal_positions
            or context.environment.type_resolver is None
            or len(syntax_arguments) != len(declaration.parameters)
        ):
            return typed_arguments

        parameters = {item.name: item for item in declaration.generic_parameters}
        type_bindings: dict[str, ir_types.HardwareType] = {}
        value_bindings: dict[str, int] = {}
        positional = [item for item in explicit if item.name is None]
        named = [item for item in explicit if item.name is not None]
        if positional and named:
            return typed_arguments
        for index, item in enumerate(explicit):
            name = item.name or (
                declaration.generic_parameters[index].name
                if index < len(declaration.generic_parameters)
                else ""
            )
            parameter = parameters.get(name)
            if parameter is None:
                continue
            if parameter.kind == "type" and isinstance(
                item.value, (ast.TypeName, ast.VectorTypeName, ast.TupleTypeName)
            ):
                type_bindings[name] = context.environment.type_resolver.resolve(
                    item.value
                )
            elif parameter.kind == "value":
                try:
                    if isinstance(item.value, int):
                        value_bindings[name] = item.value
                    else:
                        text = (
                            item.value.text
                            if isinstance(item.value, ast.TypeName)
                            else str(item.value)
                        )
                        value_bindings[name] = (
                            context.environment.type_resolver._eval_constant_integer(
                                text,
                                description=f"value parameter '{name}'",
                                allow_zero=True,
                                allow_negative=True,
                                local_values={
                                    **context.environment.parameters,
                                    **context.scope.index_bindings,
                                },
                            )
                        )
                except (TypeError, ValueError, SemanticError):
                    # The authoritative specialization pass below owns the public
                    # diagnostic for an invalid explicit argument.
                    return typed_arguments

        generic_binder = GenericBinder(
            parameters,
            type_bindings,
            value_bindings,
            context.environment.type_resolver,
        )
        for index, (formal, actual) in enumerate(
            zip(declaration.parameters, typed_arguments, strict=True)
        ):
            if index in literal_positions:
                continue
            try:
                generic_binder.bind_callable_type(formal.type_name, actual.type)
            except SemanticError:
                # This is a provisional pass used only to discover a literal's
                # contextual formal type. The authoritative specialization pass
                # below owns conflict wording and call/declaration attribution.
                return typed_arguments

        binding_resolver = type_resolution.TypeResolver(
            tuple(
                ast.TypeAlias(name, target)
                for name, target in context.environment.type_resolver._aliases.items()
            ),
            tuple(context.environment.type_resolver._structs.values()),
            tuple(context.environment.type_resolver._enum_declarations.values()),
            declaration.generic_parameters,
            {**context.environment.parameters, **value_bindings},
            {
                **type_bindings,
                **{str(type_): type_ for type_ in type_bindings.values()},
            },
            context.environment.type_resolver._identity_namespace,
            tagged_unions=tuple(
                context.environment.type_resolver._tagged_union_declarations.values()
            ),
        )
        result = list(typed_arguments)
        contextual_types = (
            ir_types.BitType,
            ir_types.UIntType,
            ir_types.SIntType,
            ir_types.BitsType,
            ir_types.FixedType,
            ir_types.UFixedType,
        )
        for index in literal_positions:
            try:
                target = binding_resolver.resolve(
                    declaration.parameters[index].type_name
                )
            except SemanticError:
                continue
            if isinstance(target, contextual_types):
                syntax = syntax_arguments[index]
                assert SpecializationArgumentBinder.is_direct_integer_literal(syntax)
                if isinstance(syntax, ast.NumberExpr):
                    value = syntax.value
                    signed_syntax = False
                else:
                    assert isinstance(syntax, ast.UnaryExpr)
                    assert isinstance(syntax.expression, ast.NumberExpr)
                    value = -syntax.expression.value
                    signed_syntax = True
                literal = check_integer_literal(
                    value,
                    target,
                    signed_syntax=signed_syntax,
                )
                origin = semantic_origin(syntax, context)
                result[index] = (
                    replace(literal, origin=origin) if origin is not None else literal
                )
        return tuple(result)


__all__ = [
    "CallableSpecializationOwner",
    "SpecializationArgumentBinder",
]
