# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative typing of callable-local immutable bindings and results."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types

from .errors import SemanticError
from . import observations
from . import symbols as semantic_symbols

if TYPE_CHECKING:
    from .context import ExpressionContext


class CallableBodyAnalyzer:
    """Type one callable body without owning specialization/cache policy."""

    def check(
        self,
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        parameters: dict[str, semantic_symbols.ValueSymbol],
        expected: ir_types.HardwareType | None,
        context: ExpressionContext,
        *,
        typed_boundary: bool,
    ) -> ir_expr.Expression:
        """Type concise immutable aliases followed by one result expression.

        Bindings are aliases for already typed pure expressions. No local state
        or backend node is created, so the retained call graph is identical to
        the equivalent explicitly nested expression.
        """

        symbols = dict(parameters)
        expression_analysis = context.expressions
        self._record_parameter_definitions(declaration, parameters, context)
        for binding in declaration.bindings:
            if isinstance(binding, ast.TupleDestructureDecl):
                self._bind_tuple(binding, symbols, context)
                continue
            if binding.target in symbols:
                raise SemanticError(
                    f"duplicate callable binding '{binding.target}'; callable "
                    "parameters and inferred bindings are immutable"
                )
            binding_value = expression_analysis.check(
                binding.expression,
                symbols,
                None,
                context,
            )
            symbols[binding.target] = binding_value
            observations.remember_definition_target(
                context,
                binding_value,
                observations.declaration_origin(
                    getattr(binding, "name_origin", None) or binding.origin,
                    f"value {binding.target}",
                    context,
                ),
                name=binding.target,
                kind="value",
            )
        if typed_boundary and expected is not None:
            return expression_analysis.check_typed_boundary(
                declaration.body,
                symbols,
                expected,
                context,
            )
        return expression_analysis.check(
            declaration.body,
            symbols,
            expected,
            context,
        )

    @staticmethod
    def _record_parameter_definitions(
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        parameters: dict[str, semantic_symbols.ValueSymbol],
        context: ExpressionContext,
    ) -> None:
        # Parameter declarations retain their compiler-owned name/declaration
        # origins; no source-text reconstruction belongs in callable typing.
        for parameter in parameters.values():
            if not isinstance(parameter, ir_module.FunctionParameter):
                continue
            parameter_declaration = next(
                (
                    item
                    for item in declaration.parameters
                    if item.name == parameter.name
                ),
                None,
            )
            parameter_origin = observations.declaration_origin(
                (
                    getattr(parameter_declaration, "name_origin", None)
                    if parameter_declaration is not None
                    else None
                )
                or getattr(parameter, "name_origin", None)
                or declaration.origin,
                f"parameter {parameter.name}",
                context,
                source_unit=declaration.source_identity,
            )
            observations.remember_definition_target(
                context,
                parameter,
                parameter_origin,
                name=parameter.name,
                kind="parameter",
            )

    @staticmethod
    def _bind_tuple(
        binding: ast.TupleDestructureDecl,
        symbols: dict[str, semantic_symbols.ValueSymbol],
        context: ExpressionContext,
    ) -> None:
        if len(binding.names) != len(set(binding.names)):
            duplicate = next(
                name for name in binding.names if binding.names.count(name) > 1
            )
            raise SemanticError(
                f"tuple destructuring repeats binding '{duplicate}'"
            )
        collision = next((name for name in binding.names if name in symbols), None)
        if collision is not None:
            raise SemanticError(
                f"tuple binding '{collision}' shadows an existing immutable symbol"
            )
        value = context.expressions.check(
            binding.expression,
            symbols,
            None,
            context,
        )
        if not isinstance(value.type, ir_types.TupleType):
            raise SemanticError(
                f"tuple destructuring requires a tuple value, got {value.type}"
            )
        if len(binding.names) != len(value.type.elements):
            raise SemanticError(
                f"tuple destructuring has {len(binding.names)} bindings, "
                f"but {value.type} has {len(value.type.elements)} components"
            )
        for index, (name, type_) in enumerate(
            zip(binding.names, value.type.elements, strict=True)
        ):
            projection = ir_expr.TupleProject(value, index, type_, origin=value.origin)
            symbols[name] = projection
            observations.remember_definition_target(
                context,
                projection,
                observations.declaration_origin(
                    getattr(binding, "name_origin", None) or binding.origin,
                    f"value {name}",
                    context,
                ),
                name=name,
                kind="value",
            )


__all__ = ["CallableBodyAnalyzer"]
