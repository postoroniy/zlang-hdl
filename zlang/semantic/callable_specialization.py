# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned generic and functional callable specialization."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.common import stable_digest
from zlang.generics import GenericArgument, SpecializationIdentity
from zlang.ir import callables as ir_callables
from zlang.ir.constants import constant_runtime_value
from zlang.ir import expressions as ir_expr
from zlang.ir import functional_regions
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from . import compile_time_evaluation
from . import limits as semantic_limits
from . import type_resolution
from .callables import (
    StaticCallableBinding,
    _canonical_rom_runtime_values,
    _context_for_callable_body,
)
from .errors import SemanticError
from .callable_functional_specialization import FunctionalCallableSpecializer
from .callable_specialization_bindings import SpecializationArgumentBinder
from .generic_binding import GenericBinder

if TYPE_CHECKING:
    from .context import ExpressionContext


def _callable_declaration_origin(
    declaration: ast.FunctionDecl | ast.OperatorDecl,
    context: ExpressionContext,
) -> SourceOrigin | None:
    """Return diagnostic-only provenance for one source callable declaration."""

    if declaration.origin is None:
        return None
    owner = (
        f"function {declaration.name}"
        if isinstance(declaration, ast.FunctionDecl)
        else f"operator {declaration.operator}"
    )
    source_unit = declaration.source_identity or context.scope.source_unit
    digest = (
        context.environment.source_digests.get(source_unit)
        if source_unit is not None
        else None
    )
    if digest is None and source_unit == context.scope.source_unit:
        digest = context.scope.source_digest
    return SourceOrigin(
        declaration.origin,
        f"{owner} declaration",
        source_unit,
        digest,
    )


def _annotate_callable_error(
    error: SemanticError,
    declaration: ast.FunctionDecl | ast.OperatorDecl,
    context: ExpressionContext,
    call_origin: SourceOrigin | None,
) -> SemanticError:
    """Attach call/declaration provenance without changing the legacy message."""

    declaration_origin = _callable_declaration_origin(declaration, context)
    notes = list(error.notes)
    if declaration_origin is not None:
        location = declaration_origin.render()
        if declaration_origin.source_unit is not None:
            location = f"{declaration_origin.source_unit}:{location}"
        note = f"callable declared at {location}"
        if note not in notes:
            notes.append(note)
    return SemanticError(
        str(error),
        code=error.code,
        primary=call_origin or error.primary,
        notes=notes,
        fixes=error.fixes,
        machine_fixes=error.machine_fixes,
    )


@dataclass(frozen=True)
class CallableSpecializer:
    """Own exact generic binding, functional admission, and specialization."""

    argument_binder: SpecializationArgumentBinder = field(
        default_factory=SpecializationArgumentBinder
    )
    functional_specializer: FunctionalCallableSpecializer = field(
        default_factory=FunctionalCallableSpecializer
    )

    @staticmethod
    def specialization_binding(
        name: str,
        value: ir_expr.Expression | StaticCallableBinding,
        dependency_identity: tuple[tuple[str, str], ...],
    ) -> ir_module.SpecializationBinding:
        """Seal one compiler-owned argument into a specialization identity."""

        schema = semantic_limits.COMPILE_TIME_EVALUATOR_SCHEMA
        if isinstance(value, StaticCallableBinding):
            content_hash = stable_digest({
                "schema": schema,
                "kind": ir_module.SpecializationBindingKind.CALLABLE.value,
                "parameters": tuple(map(str, value.parameter_types)),
                "return": str(value.return_type),
                "callee_identity": value.callee_identity,
                "dependencies": dependency_identity,
            })
            return ir_module.SpecializationBinding(
                name,
                ir_module.SpecializationBindingKind.CALLABLE,
                None,
                None,
                content_hash,
                parameter_types=value.parameter_types,
                return_type=value.return_type,
                callee_identity=value.callee_identity,
                dependency_identity=dependency_identity,
                evaluator_schema=schema,
            )

        runtime_value = _canonical_rom_runtime_values(
            value.type, constant_runtime_value(value)
        )
        content_hash = stable_digest({
            "schema": schema,
            "kind": ir_module.SpecializationBindingKind.CONSTANT.value,
            "type": str(value.type),
            "value": runtime_value,
            "dependencies": dependency_identity,
        })
        return ir_module.SpecializationBinding(
            name,
            ir_module.SpecializationBindingKind.CONSTANT,
            value.type,
            runtime_value,
            content_hash,
            dependency_identity=dependency_identity,
            evaluator_schema=schema,
        )

    @staticmethod
    def bind_inferred_types(
        parameters: dict[str, ast.ModuleParameter],
        type_bindings: dict[str, ir_types.HardwareType],
        value_bindings: dict[str, int],
        resolver: type_resolution.TypeResolver,
        pairs: tuple[tuple[ast.TypeSyntax, ir_types.HardwareType], ...],
        *,
        module_types: bool = False,
    ) -> None:
        """Apply exact generic inference without exposing its mutable binder."""

        binder = GenericBinder(parameters, type_bindings, value_bindings, resolver)
        bind = binder.bind_module_type if module_types else binder.bind_callable_type
        for formal, actual in pairs:
            bind(formal, actual)

    def bindings(
        self,
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
        return self.argument_binder.bindings(
            self,
            declaration,
            explicit,
            arguments,
            context,
            specialization_symbols,
        )

    def contextualize_integer_arguments(
        self,
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        syntax_arguments: tuple[ast.Expression, ...],
        typed_arguments: tuple[ir_expr.Expression, ...],
        explicit: tuple[ast.SpecializationArgument, ...],
        context: ExpressionContext,
    ) -> tuple[ir_expr.Expression, ...]:
        return self.argument_binder.contextualize_integer_arguments(
            declaration,
            syntax_arguments,
            typed_arguments,
            explicit,
            context,
        )

    @staticmethod
    def is_direct_integer_literal(expression: ast.Expression) -> bool:
        return SpecializationArgumentBinder.is_direct_integer_literal(expression)

    def close_nested_functional_template(
        self,
        value: ir_expr.Expression,
        binder: functional_regions.CompileTimeBinderRef,
    ) -> tuple[
        ir_expr.Expression,
        tuple[tuple[ir_expr.FunctionalCaptureRef, ir_expr.Expression], ...],
    ]:
        return self.functional_specializer.close_nested_template(value, binder)

    def try_functional(
        self,
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        arguments: tuple[ir_expr.Expression, ...],
        explicit: tuple[ast.SpecializationArgument, ...],
        context: ExpressionContext,
        *,
        specialization_symbols: dict[str, object] | None,
        call_origin: SourceOrigin | None,
    ) -> ir_expr.Expression | None:
        return self.functional_specializer.try_specialize(
            self,
            self.argument_binder,
            declaration,
            arguments,
            explicit,
            context,
            specialization_symbols=specialization_symbols,
            call_origin=call_origin,
        )

    def _specialize_unannotated(
        self,
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        arguments: tuple[ir_expr.Expression, ...],
        explicit: tuple[ast.SpecializationArgument, ...],
        context: ExpressionContext,
        *,
        specialization_symbols: dict[str, object] | None = None,
    ) -> ir_expr.Expression:
        (
            type_bindings,
            value_bindings,
            constant_bindings,
            callable_bindings,
        ) = self.bindings(
            declaration,
            explicit,
            arguments,
            context,
            specialization_symbols,
        )
        assert context.environment.type_resolver is not None
        # Generic source types are still represented as ``TypeSyntax`` while the
        # inferred arguments are already canonical hardware types.  Register the
        # canonical spellings as resolver aliases as well as the source parameter
        # names so nominal types (notably enums) survive substitution without being
        # mistaken for a user-written type name.
        callable_type_bindings = {
            **type_bindings,
            **{str(type_): type_ for type_ in type_bindings.values()},
        }
        resolver = type_resolution.TypeResolver(
            tuple(
                ast.TypeAlias(name, target)
                for name, target in context.environment.type_resolver._aliases.items()
            ),
            tuple(context.environment.type_resolver._structs.values()),
            tuple(context.environment.type_resolver._enum_declarations.values()),
            declaration.generic_parameters,
            {**context.environment.parameters, **value_bindings},
            callable_type_bindings,
            context.environment.type_resolver._identity_namespace,
            tagged_unions=tuple(
                context.environment.type_resolver._tagged_union_declarations.values()
            ),
        )

        def concrete_syntax(syntax: ast.TypeSyntax) -> ast.TypeSyntax:
            if isinstance(syntax, ast.VectorTypeName):
                length = (
                    value_bindings.get(syntax.length, syntax.length)
                    if isinstance(syntax.length, str)
                    else syntax.length
                )
                return ast.VectorTypeName(length, concrete_syntax(syntax.element_type))
            if isinstance(syntax, ast.TupleTypeName):
                return ast.TupleTypeName(
                    tuple(concrete_syntax(item) for item in syntax.elements)
                )
            text = syntax.text
            for name, type_ in sorted(
                type_bindings.items(), key=lambda item: -len(item[0])
            ):
                text = re.sub(rf"\b{re.escape(name)}\b", str(type_), text)
            for name, value in sorted(
                value_bindings.items(), key=lambda item: -len(item[0])
            ):
                text = re.sub(rf"\b{re.escape(name)}\b", str(value), text)
            return ast.TypeName(text)

        concrete_parameters = tuple(
            ir_module.FunctionParameter(
                parameter.name, resolver.resolve(concrete_syntax(parameter.type_name))
            )
            for parameter in declaration.parameters
        )
        for formal, actual in zip(concrete_parameters, arguments, strict=True):
            if formal.type != actual.type:
                raise SemanticError(
                    f"argument '{formal.name}' has type {actual.type}, expected exact {formal.type}"
                )
        specialization_bindings = tuple(
            self.specialization_binding(
                parameter.name,
                constant_bindings[parameter.name],
                context.environment.generic_dependency_identity,
            )
            if parameter.kind == "constant"
            else self.specialization_binding(
                parameter.name,
                callable_bindings[parameter.name],
                context.environment.generic_dependency_identity,
            )
            for parameter in declaration.generic_parameters
            if parameter.kind in {"constant", "callable"}
        )
        specialization_binding_by_name = {
            binding.name: binding for binding in specialization_bindings
        }
        constant_identities = {
            name: specialization_binding_by_name[name].content_hash
            for name in constant_bindings
        }
        rendered = tuple(
            (
                parameter.name,
                str(type_bindings[parameter.name])
                if parameter.kind == "type"
                else str(value_bindings[parameter.name])
                if parameter.kind == "value"
                else f"constant:{constant_identities[parameter.name]}"
                if parameter.kind == "constant"
                else f"callable:{callable_bindings[parameter.name].callee_identity}",
            )
            for parameter in declaration.generic_parameters
        )
        owner = (
            declaration.name
            if isinstance(declaration, ast.FunctionDecl)
            else f"operator{declaration.operator}"
        )
        generic_arguments = tuple(
            GenericArgument(
                parameter.name,
                parameter.kind,
                type_bindings[parameter.name]
                if parameter.kind == "type"
                else value_bindings[parameter.name]
                if parameter.kind == "value"
                else f"{constant_bindings[parameter.name].type}:{constant_identities[parameter.name]}"
                if parameter.kind == "constant"
                else callable_bindings[parameter.name].callee_identity,
            )
            for parameter in declaration.generic_parameters
        )
        identity = SpecializationIdentity.create(
            declaration=declaration,
            owner=owner,
            arguments=generic_arguments,
            source_identity=declaration.source_identity,
            dependency_identity=context.environment.generic_dependency_identity,
        ).digest
        definition_name = f"zlang_spec_{identity}"
        stack_key = f"{owner}:{identity}"
        if (
            stack_key in context.scope.resolution_stack
            or context.services.callables.is_specializing(identity)
        ):
            raise SemanticError(f"recursive generic specialization cycle for '{owner}'")

        cached = context.services.callables.definition(identity)
        if cached is not None:
            compile_time_evaluation.replay_specialization_budget(context, identity)
            context.services.callables.record_use(identity)
            return ir_expr.Call(
                cached.name,
                arguments,
                cached.return_type,
                cached.callee_identity,
                origin=cached.body.origin,
            )

        budget = context.services.compile_time_budget
        budget_before = (
            (budget.generated_elements, budget.operations)
            if budget is not None
            else None
        )
        if budget is not None:
            budget.call_depth += 1
            if budget.call_depth > semantic_limits.COMPILE_TIME_CALLS:
                budget.call_depth -= 1
                raise SemanticError(
                    f"compile-time function nesting exceeds {semantic_limits.COMPILE_TIME_CALLS} calls"
                )
        body_context = _context_for_callable_body(
            context,
            declaration.source_identity,
            identity,
        )
        body_context = body_context.with_environment(
            type_resolver=resolver,
            parameters={**context.environment.parameters, **value_bindings},
        ).with_scope(
            compile_time_constants={
                **context.scope.compile_time_constants,
                **constant_bindings,
            },
            static_callables={**context.scope.static_callables, **callable_bindings},
            resolution_stack=(*context.scope.resolution_stack, stack_key),
            allow_fixed_target_coercion=declaration.return_type is None,
        )
        symbols: dict[str, object] = {
            parameter.name: parameter for parameter in concrete_parameters
        }
        symbols.update(constant_bindings)
        expected_return = (
            resolver.resolve(concrete_syntax(declaration.return_type))
            if declaration.return_type is not None
            else None
        )
        with context.services.callables.specializing(identity):
            try:
                body = context.services.callable_bodies.check(
                    declaration,
                    symbols,
                    expected_return,
                    body_context,
                    typed_boundary=(
                        expected_return is not None
                        and isinstance(declaration, ast.FunctionDecl)
                    ),
                )
            finally:
                if budget is not None:
                    budget.call_depth -= 1
        if expected_return is not None and body.type != expected_return:
            raise SemanticError(
                f"'{owner}' returns {body.type}, expected {expected_return}"
            )

        kind = (
            ir_callables.CallableKind.FUNCTION
            if isinstance(declaration, ast.FunctionDecl)
            else ir_callables.CallableKind.OPERATOR
        )
        declaration_identity = (
            f"{declaration.source_identity or context.scope.source_unit or '<source>'}:"
            f"{owner}"
        )
        metadata = ir_module.CallableMetadata(
            kind,
            owner,
            declaration_identity,
            identity,
            rendered,
        )
        definition = ir_module.Function(
            name=definition_name,
            parameters=concrete_parameters,
            return_type=body.type,
            body=body,
            callee_identity=identity,
            metadata=metadata,
        )
        budget_cost = (
            (
                budget.generated_elements - budget_before[0],
                budget.operations - budget_before[1],
            )
            if budget is not None and budget_before is not None
            else None
        )
        record = ir_module.GenericSpecialization(
            kind.value,
            owner,
            identity,
            rendered,
            body.type,
            specialization_bindings,
        )
        context.services.callables.publish(
            identity,
            definition,
            record,
            budget_cost=budget_cost,
        )
        return ir_expr.Call(
            definition.name,
            arguments,
            definition.return_type,
            definition.callee_identity,
            origin=body.origin,
        )

    def specialize(
        self,
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        arguments: tuple[ir_expr.Expression, ...],
        explicit: tuple[ast.SpecializationArgument, ...],
        context: ExpressionContext,
        *,
        call_origin: SourceOrigin | None = None,
        specialization_symbols: dict[str, object] | None = None,
    ) -> ir_expr.Expression:
        try:
            return self._specialize_unannotated(
                declaration,
                arguments,
                explicit,
                context,
                specialization_symbols=specialization_symbols,
            )
        except SemanticError as error:
            raise _annotate_callable_error(
                error,
                declaration,
                context,
                call_origin,
            ) from error

    def annotate_error(
        self,
        error: SemanticError,
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        context: ExpressionContext,
        call_origin: SourceOrigin | None,
    ) -> SemanticError:
        return _annotate_callable_error(
            error,
            declaration,
            context,
            call_origin,
        )


__all__ = ["CallableSpecializer"]
