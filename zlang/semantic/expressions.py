# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Expression-analysis facade and explicit domain-handler orchestration."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from zlang.analysis_needs import AnalysisNeeds
from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from . import expression_origins, observations
from .errors import SemanticError
from .expression_aggregates import (
    _check_literal_and_aggregate_expression,
    _check_representation_expression,
    _check_struct_expression,
)
from .expression_calls import _check_call_intrinsic, _check_resolved_call
from .expression_coercion import (
    coerce_fixed_target,
    coerce_raw_target,
    is_raw_representation_type,
)
from .expression_collections import (
    _check_index_and_slice_expression,
)
from .expression_control import (
    _check_control_expression,
    _check_functional_and_operator_expression,
)
from .expression_names_members import (
    _check_instance_field,
    _check_name_expression,
    _check_protocol_field,
    _check_struct_field,
)
from .expression_support import (
    _fold_compile_time_parameter_expression,
)
from .symbols import ValueSymbol

if TYPE_CHECKING:
    from .context import ExpressionContext


_EARLY_EXPRESSION_HANDLERS = (
    _check_name_expression,
    _check_literal_and_aggregate_expression,
    _check_functional_and_operator_expression,
)
_LATE_EXPRESSION_HANDLERS = (
    _check_struct_expression,
    _check_index_and_slice_expression,
    _check_representation_expression,
    _check_control_expression,
)


class ExpressionAnalyzer:
    """Authoritative expression orchestrator composed from bounded handlers."""

    def check(
        self,
        expression: ast.Expression,
        inputs: dict[str, ValueSymbol],
        expected: ir_types.HardwareType | None,
        context: ExpressionContext,
    ) -> ir_expr.Expression:
        source_expression = expression
        try:
            from . import compile_time_evaluation

            compile_time_evaluation.budget_step(context)
            if context.services.tooling.analysis_needs.wants(AnalysisNeeds.COMPLETION):
                observations.record_completion_scope(
                    context.services.tooling.completion_scopes,
                    source_expression,
                    inputs,
                    context,
                )
            if isinstance(expression, ast.CompileTimeIfExpr):
                selected = (
                    expression.when_true
                    if compile_time_evaluation.compile_time_condition(
                        expression.condition, inputs, context
                    )
                    else expression.when_false
                )
                if selected is None:
                    raise SemanticError(
                        "compile-time if requires an else branch in an expression"
                    )
                return self.check(selected, inputs, expected, context)
            expression = _fold_compile_time_parameter_expression(expression, context)
            result = self.check_untraced(expression, inputs, expected, context)
            if context.scope.allow_fixed_target_coercion:
                result = coerce_fixed_target(result, expected)
            if (
                isinstance(source_expression, ast.NameExpr)
                and source_expression.name in context.environment.parameters
                and source_expression.origin is not None
            ):
                origin = SourceOrigin(
                    source_expression.origin,
                    f"module parameter {source_expression.name}="
                    f"{context.environment.parameters[source_expression.name]}",
                    context.scope.source_unit,
                    context.scope.source_digest,
                )
            else:
                origin = expression_origins.semantic_origin(source_expression, context)
            traced = replace(result, origin=origin) if origin is not None else result
            return context.services.expression_arena.intern(traced)
        except SemanticError as error:
            if error.primary is not None:
                raise
            primary = expression_origins.semantic_origin(source_expression, context)
            if primary is None:
                raise
            raise SemanticError(
                str(error),
                code=error.code,
                primary=primary,
                notes=error.notes,
                fixes=error.fixes,
                machine_fixes=error.machine_fixes,
            ) from error

    def check_untraced(
        self,
        expression: ast.Expression,
        inputs: dict[str, ValueSymbol],
        expected: ir_types.HardwareType | None,
        context: ExpressionContext,
    ) -> ir_expr.Expression:
        for handler in _EARLY_EXPRESSION_HANDLERS:
            result = handler(expression, inputs, expected, context)
            if result is not None:
                return result
        if isinstance(expression, ast.CallExpr):
            intrinsic = _check_call_intrinsic(expression, inputs, expected, context)
            if intrinsic is not None:
                return intrinsic
            return _check_resolved_call(expression, inputs, context)
        if isinstance(expression, ast.FieldExpr):
            protocol = _check_protocol_field(expression, inputs, context)
            if protocol is not None:
                return protocol
            instance = _check_instance_field(expression, inputs, context)
            if instance is not None:
                return instance
            return _check_struct_field(expression, inputs, context)
        for handler in _LATE_EXPRESSION_HANDLERS:
            result = handler(expression, inputs, expected, context)
            if result is not None:
                return result
        raise SemanticError(f"unsupported expression {expression!r}")

    def check_typed_boundary(
        self,
        expression: ast.Expression,
        inputs: dict[str, ValueSymbol],
        expected: ir_types.HardwareType,
        context: ExpressionContext,
    ) -> ir_expr.Expression:
        try:
            typed = self.check(expression, inputs, expected, context)
        except SemanticError as original:
            if (
                isinstance(
                    expression,
                    (ast.GenerateExpr, ast.MapExpr, ast.VectorLiteralExpr),
                )
                and is_raw_representation_type(expected)
            ):
                try:
                    generated = self.check(expression, inputs, None, context)
                    typed = coerce_raw_target(generated, expected)
                except SemanticError:
                    raise original
                if typed.type != expected:
                    raise original
                return typed
            if not (
                isinstance(expected, ir_types.VecType)
                and isinstance(expected.element_type, ir_types.BitType)
                and isinstance(expression, ast.NumberExpr)
            ):
                raise
            try:
                typed = self.check(
                    expression, inputs, ir_types.BitsType(expected.width), context
                )
            except SemanticError:
                raise original
        return coerce_raw_target(typed, expected)
