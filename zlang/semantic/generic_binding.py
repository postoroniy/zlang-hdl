# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""State-owned exact generic type binding for callables and modules."""

from __future__ import annotations

from dataclasses import dataclass
import re

from zlang.ast import nodes as ast
from zlang.ir import types as ir_types

from .errors import SemanticError
from . import type_resolution


@dataclass
class GenericBinder:
    """Own mutable exact type/value inference for one specialization."""

    parameters: dict[str, ast.ModuleParameter]
    type_bindings: dict[str, ir_types.HardwareType]
    value_bindings: dict[str, int]
    resolver: type_resolution.TypeResolver

    def bind_callable_type(
        self,
        syntax: ast.TypeSyntax,
        actual: ir_types.HardwareType,
    ) -> None:
        """Unify one declared type pattern with one exact concrete hardware type."""

        parameters = self.parameters
        type_bindings = self.type_bindings
        value_bindings = self.value_bindings
        resolver = self.resolver

        if isinstance(syntax, ast.VectorTypeName):
            if not isinstance(actual, ir_types.VecType):
                raise SemanticError(f"expected {syntax}, got {actual}")
            if isinstance(syntax.length, str) and syntax.length in parameters:
                previous = value_bindings.get(syntax.length)
                if previous is not None and previous != actual.length:
                    raise SemanticError(
                        f"conflicting inference for '{syntax.length}': {previous} and {actual.length}"
                    )
                value_bindings[syntax.length] = actual.length
            elif int(syntax.length) != actual.length:
                raise SemanticError(
                    f"vector length mismatch: expected {syntax.length}, got {actual.length}"
                )
            self.bind_callable_type(syntax.element_type, actual.element_type)
            return

        if isinstance(syntax, ast.TupleTypeName):
            if not isinstance(actual, ir_types.TupleType):
                raise SemanticError(f"expected {syntax}, got {actual}")
            if len(syntax.elements) != len(actual.elements):
                raise SemanticError(
                    f"tuple arity mismatch: expected {len(syntax.elements)}, "
                    f"got {len(actual.elements)}"
                )
            for pattern, concrete in zip(syntax.elements, actual.elements, strict=True):
                self.bind_callable_type(pattern, concrete)
            return

        assert isinstance(syntax, ast.TypeName)
        tuple_parts = type_resolution.tuple_type_parts(syntax.text)
        if tuple_parts is not None:
            if not isinstance(actual, ir_types.TupleType) or len(tuple_parts) != len(
                actual.elements
            ):
                raise SemanticError(f"expected tuple type {syntax.text}, got {actual}")
            for pattern, concrete in zip(tuple_parts, actual.elements, strict=True):
                self.bind_callable_type(ast.TypeName(pattern), concrete)
            return
        parameter = parameters.get(syntax.text)
        if parameter is not None and parameter.kind == "type":
            previous = type_bindings.get(parameter.name)
            if previous is not None and previous != actual:
                raise SemanticError(
                    f"conflicting inference for type '{parameter.name}': {previous} and {actual}"
                )
            type_bindings[parameter.name] = actual
            return

        pattern_generic = type_resolution.TypeResolver._generic_parts(syntax.text)
        actual_generic = (
            type_resolution.TypeResolver._generic_parts(actual.name)
            if isinstance(actual, ir_types.StructType)
            else None
        )
        if pattern_generic is not None and actual_generic is not None:
            pattern_base, pattern_arguments = pattern_generic
            actual_base, actual_arguments = actual_generic
            if pattern_base == actual_base and len(pattern_arguments) == len(
                actual_arguments
            ):
                for pattern_argument, actual_argument in zip(
                    pattern_arguments, actual_arguments, strict=True
                ):
                    value_parameter = parameters.get(pattern_argument)
                    if value_parameter is not None and value_parameter.kind == "value":
                        try:
                            concrete_value = int(actual_argument)
                        except ValueError as error:
                            raise SemanticError(
                                f"nominal value argument '{actual_argument}' is not concrete"
                            ) from error
                        previous = value_bindings.get(pattern_argument)
                        if previous is not None and previous != concrete_value:
                            raise SemanticError(
                                f"conflicting inference for value '{pattern_argument}': "
                                f"{previous} and {concrete_value}"
                            )
                        value_bindings[pattern_argument] = concrete_value
                        continue
                    if pattern_argument.isdecimal() and actual_argument.isdecimal():
                        if int(pattern_argument) != int(actual_argument):
                            raise SemanticError(
                                f"nominal value argument mismatch: expected "
                                f"{pattern_argument}, got {actual_argument}"
                            )
                        continue
                    self.bind_callable_type(
                        ast.TypeName(pattern_argument),
                        resolver.resolve(ast.TypeName(actual_argument)),
                    )
                return

        specialized = type_resolution.TypeResolver(
            tuple(
                ast.TypeAlias(name, target)
                for name, target in resolver._aliases.items()
            ),
            tuple(resolver._structs.values()),
            tuple(resolver._enum_declarations.values()),
            tuple(parameters.values()),
            {**resolver._parameter_values, **value_bindings},
            {**resolver._type_bindings, **type_bindings},
            resolver._identity_namespace,
        ).resolve(syntax)
        if specialized != actual:
            raise SemanticError(
                f"exact type unification requires {specialized}, got {actual}"
            )

    def bind_module_type(
        self,
        syntax: ast.TypeSyntax,
        actual: ir_types.HardwareType,
    ) -> None:
        """Infer only direct, exact module-parameter occurrences from one type.

        Unlike generic callable inference this helper intentionally does not solve
        arithmetic equations in widths.  ``vec<N,T>`` and ``uint<N>`` are direct
        structural patterns; ``vec<2*N,T>`` can only be checked after ``N`` was
        supplied explicitly or by a default/another direct occurrence.
        """

        parameters = self.parameters
        type_bindings = self.type_bindings
        value_bindings = self.value_bindings
        resolver = self.resolver

        def bind_value(name: str, value: int) -> bool:
            parameter = parameters.get(name)
            if parameter is None or parameter.kind != "value":
                return False
            previous = value_bindings.get(name)
            if previous is not None and previous != value:
                raise SemanticError(
                    f"conflicting exact inference for value parameter '{name}': "
                    f"{previous} and {value}"
                )
            value_bindings[name] = value
            return True

        def bind_type(name: str, value: ir_types.HardwareType) -> bool:
            parameter = parameters.get(name)
            if parameter is None or parameter.kind != "type":
                return False
            previous = type_bindings.get(name)
            if previous is not None and previous != value:
                raise SemanticError(
                    f"conflicting exact inference for type parameter '{name}': "
                    f"{previous} and {value}"
                )
            type_bindings[name] = value
            return True

        if isinstance(syntax, ast.VectorTypeName):
            if not isinstance(actual, ir_types.VecType):
                raise SemanticError(
                    f"exact specialization inference expected a vector, got {actual}"
                )
            length = syntax.length
            if isinstance(length, str):
                if not bind_value(length, actual.length):
                    unresolved = {
                        name
                        for name, parameter in parameters.items()
                        if parameter.kind == "value"
                        and re.search(
                            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                            length,
                        )
                        and name not in value_bindings
                    }
                    if not unresolved:
                        specialized = type_resolution.TypeResolver(
                            tuple(
                                ast.TypeAlias(name, target)
                                for name, target in resolver._aliases.items()
                            ),
                            tuple(resolver._structs.values()),
                            tuple(resolver._enum_declarations.values()),
                            tuple(parameters.values()),
                            {**resolver._parameter_values, **value_bindings},
                            {**resolver._type_bindings, **type_bindings},
                            resolver._identity_namespace,
                        )._eval_width(length)
                        if specialized != actual.length:
                            raise SemanticError(
                                "exact specialization inference requires vector "
                                f"length {specialized}, got {actual.length}"
                            )
            elif length != actual.length:
                raise SemanticError(
                    f"exact specialization inference requires vector length "
                    f"{length}, got {actual.length}"
                )
            self.bind_module_type(syntax.element_type, actual.element_type)
            return

        if isinstance(syntax, ast.TupleTypeName):
            if not isinstance(actual, ir_types.TupleType):
                raise SemanticError(
                    f"exact specialization inference expected a tuple, got {actual}"
                )
            if len(syntax.elements) != len(actual.elements):
                raise SemanticError(
                    f"exact specialization inference requires tuple arity "
                    f"{len(syntax.elements)}, got {len(actual.elements)}"
                )
            for pattern, concrete in zip(syntax.elements, actual.elements, strict=True):
                self.bind_module_type(pattern, concrete)
            return

        assert isinstance(syntax, ast.TypeName)
        tuple_parts = type_resolution.tuple_type_parts(syntax.text)
        if tuple_parts is not None:
            if not isinstance(actual, ir_types.TupleType) or len(tuple_parts) != len(
                actual.elements
            ):
                raise SemanticError(
                    f"exact specialization inference expected {syntax.text}, got {actual}"
                )
            for pattern, concrete in zip(tuple_parts, actual.elements, strict=True):
                self.bind_module_type(ast.TypeName(pattern), concrete)
            return
        if bind_type(syntax.text, actual):
            return

        scalar = re.fullmatch(
            r"(uint|sint|bits)<([A-Za-z_][A-Za-z0-9_]*)>", syntax.text
        )
        if scalar is not None:
            family, width_name = scalar.groups()
            expected_class = {
                "uint": ir_types.UIntType,
                "sint": ir_types.SIntType,
                "bits": ir_types.BitsType,
            }[family]
            if not isinstance(actual, expected_class):
                raise SemanticError(
                    f"exact specialization inference expected {family}, got {actual}"
                )
            if bind_value(width_name, actual.width):
                return

        fixed = re.fullmatch(
            r"(fixed|ufixed|fixed_sat|ufixed_sat)"
            r"<([A-Za-z_][A-Za-z0-9_]*|[0-9]+),"
            r"([A-Za-z_][A-Za-z0-9_]*|[0-9]+)>",
            syntax.text,
        )
        if fixed is not None:
            family, width_name, fraction_name = fixed.groups()
            expected_class = (
                ir_types.FixedType
                if family in {"fixed", "fixed_sat"}
                else ir_types.UFixedType
            )
            expected_overflow = (
                ir_types.FixedOverflowPolicy.SATURATE
                if family.endswith("_sat")
                else ir_types.FixedOverflowPolicy.WRAP
            )
            if (
                not isinstance(actual, expected_class)
                or actual.overflow is not expected_overflow
            ):
                raise SemanticError(
                    f"exact specialization inference expected {family}, got {actual}"
                )
            width_ok = bind_value(width_name, actual.width)
            fraction_ok = bind_value(fraction_name, actual.fraction)
            if width_ok or fraction_ok:
                if width_name.isdigit() and int(width_name) != actual.width:
                    raise SemanticError(
                        f"exact specialization inference requires width {width_name}, "
                        f"got {actual.width}"
                    )
                if fraction_name.isdigit() and int(fraction_name) != actual.fraction:
                    raise SemanticError(
                        "exact specialization inference requires fractional width "
                        f"{fraction_name}, got {actual.fraction}"
                    )
                return

        pattern_generic = type_resolution.TypeResolver._generic_parts(syntax.text)
        actual_generic = (
            type_resolution.TypeResolver._generic_parts(actual.name)
            if isinstance(actual, ir_types.StructType)
            else None
        )
        if pattern_generic is not None and actual_generic is not None:
            pattern_base, pattern_arguments = pattern_generic
            actual_base, actual_arguments = actual_generic
            declaration = resolver._structs.get(pattern_base)
            if (
                declaration is not None
                and pattern_base == actual_base
                and len(pattern_arguments) == len(actual_arguments)
                and len(declaration.parameters) == len(pattern_arguments)
            ):
                for struct_parameter, pattern_argument, actual_argument in zip(
                    declaration.parameters,
                    pattern_arguments,
                    actual_arguments,
                    strict=True,
                ):
                    if struct_parameter.kind == "type":
                        self.bind_module_type(
                            ast.TypeName(pattern_argument),
                            resolver.resolve(ast.TypeName(actual_argument)),
                        )
                        continue
                    try:
                        actual_value = int(actual_argument)
                    except ValueError as error:
                        raise SemanticError(
                            "concrete generic struct value argument is not an integer"
                        ) from error
                    if bind_value(pattern_argument, actual_value):
                        continue
                    if pattern_argument.isdigit():
                        if int(pattern_argument) != actual_value:
                            raise SemanticError(
                                "exact specialization inference requires generic "
                                f"value {pattern_argument}, got {actual_value}"
                            )
                        continue
                    # A compound value expression is intentionally not inverted.
                    # If its dependencies were supplied elsewhere, the exact
                    # comparison below validates it.
                unresolved_after_struct = {
                    name
                    for name, parameter in parameters.items()
                    if (
                        parameter.kind == "type"
                        and name not in type_bindings
                        or parameter.kind == "value"
                        and name not in value_bindings
                    )
                    and re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                        syntax.text,
                    )
                }
                if not unresolved_after_struct:
                    return

        # Resolve and compare any pattern which is already fully determined.  An
        # unresolved parameter is left for the caller's explicit ambiguity error;
        # it is never guessed by equation solving or conversion.
        unresolved_names = {
            name
            for name, parameter in parameters.items()
            if (
                parameter.kind == "type"
                and name not in type_bindings
                or parameter.kind == "value"
                and name not in value_bindings
            )
            and re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                syntax.text,
            )
        }
        if unresolved_names:
            return
        specialized_resolver = type_resolution.TypeResolver(
            tuple(
                ast.TypeAlias(name, target)
                for name, target in resolver._aliases.items()
            ),
            tuple(resolver._structs.values()),
            tuple(resolver._enum_declarations.values()),
            tuple(parameters.values()),
            {**resolver._parameter_values, **value_bindings},
            {**resolver._type_bindings, **type_bindings},
            resolver._identity_namespace,
        )
        specialized = specialized_resolver.resolve(syntax)
        if specialized != actual:
            raise SemanticError(
                f"exact specialization inference requires {specialized}, got {actual}"
            )
