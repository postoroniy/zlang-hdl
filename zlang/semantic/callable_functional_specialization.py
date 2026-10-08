# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Symbolic functional-region callable specialization and capture closure."""

from __future__ import annotations

import ast as pyast
from dataclasses import fields, is_dataclass, replace
import re
from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.common import stable_digest
from zlang.ir import expressions as ir_expr
from zlang.ir import functional_regions
from zlang.ir import module as ir_module
from zlang.ir import types as ir_types
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.source import SourceOrigin

from . import type_resolution
from .callables import _context_for_callable_body
from .callable_specialization_bindings import (
    CallableSpecializationOwner,
    SpecializationArgumentBinder,
)
from .errors import SemanticError

if TYPE_CHECKING:
    from .context import ExpressionContext


class _FunctionalSpecializationRejected(Exception):
    """A symbolic generic is not in the compiler's closed liftable subset."""


def _compile_time_expr_from_python(
    node: pyast.AST,
    context: ExpressionContext,
) -> functional_regions.CompileTimeExpr:
    if (
        isinstance(node, pyast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return functional_regions.CompileTimeExpr.literal(node.value)
    if isinstance(node, pyast.Name):
        symbolic = context.scope.functional_symbolic_values.get(node.id)
        if symbolic is not None:
            return symbolic
        if node.id in context.environment.parameters:
            return functional_regions.CompileTimeExpr.literal(
                context.environment.parameters[node.id]
            )
        raise _FunctionalSpecializationRejected(
            f"compile-time name '{node.id}' is not a liftable functional value"
        )
    if isinstance(node, pyast.UnaryOp) and isinstance(node.op, pyast.USub):
        return functional_regions.CompileTimeExpr(
            functional_regions.CompileTimeOperator.NEGATE,
            (_compile_time_expr_from_python(node.operand, context),),
        )
    operators: dict[type[pyast.operator], functional_regions.CompileTimeOperator] = {
        pyast.Add: functional_regions.CompileTimeOperator.ADD,
        pyast.Sub: functional_regions.CompileTimeOperator.SUBTRACT,
        pyast.Mult: functional_regions.CompileTimeOperator.MULTIPLY,
        pyast.FloorDiv: functional_regions.CompileTimeOperator.FLOOR_DIVIDE,
        pyast.Mod: functional_regions.CompileTimeOperator.MODULO,
    }
    if isinstance(node, pyast.BinOp):
        operator = operators.get(type(node.op))
        if operator is not None:
            return functional_regions.CompileTimeExpr(
                operator,
                (
                    _compile_time_expr_from_python(node.left, context),
                    _compile_time_expr_from_python(node.right, context),
                ),
            )
    raise _FunctionalSpecializationRejected(
        "generic value expression is outside bounded functional arithmetic"
    )


def _symbolic_specialization_value(
    value: object,
    context: ExpressionContext,
) -> functional_regions.CompileTimeExpr | None:
    if isinstance(value, int):
        return None
    text = value.text if isinstance(value, ast.TypeName) else str(value)
    try:
        parsed = pyast.parse(text, mode="eval")
    except SyntaxError:
        return None
    try:
        expression = _compile_time_expr_from_python(parsed.body, context)
    except _FunctionalSpecializationRejected:
        return None
    binder_ids = {
        item.identity
        for item in context.scope.functional_symbolic_values.values()
        for item in _compile_time_binders(item)
    }
    expression_binders = {item.identity for item in _compile_time_binders(expression)}
    return expression if expression_binders & binder_ids else None


def _compile_time_binders(
    value: object,
) -> tuple[functional_regions.CompileTimeBinderRef, ...]:
    result: list[functional_regions.CompileTimeBinderRef] = []

    def walk(item: object) -> None:
        if isinstance(item, functional_regions.CompileTimeBinderRef):
            result.append(item)
        elif isinstance(item, functional_regions.CompileTimeExpr):
            for operand in item.operands:
                walk(operand)

    walk(value)
    return tuple(result)


def _type_syntax_mentions(
    syntax: ast.TypeSyntax | None,
    names: set[str],
) -> bool:
    if syntax is None:
        return False
    if isinstance(syntax, ast.VectorTypeName):
        return (
            isinstance(syntax.length, str)
            and syntax.length in names
            or _type_syntax_mentions(syntax.element_type, names)
        )
    if isinstance(syntax, ast.TupleTypeName):
        return any(_type_syntax_mentions(item, names) for item in syntax.elements)
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", syntax.text)
        for name in names
    )


def _substitute_functional_parameters(
    value: object,
    bindings: dict[str, ir_expr.Expression],
) -> object:
    if isinstance(value, ir_expr.ParameterRef) and value.name in bindings:
        replacement = bindings[value.name]
        if value.type != replacement.type:
            raise _FunctionalSpecializationRejected(
                f"parameter '{value.name}' changed type during functional lifting"
            )
        return replacement
    if isinstance(value, tuple):
        return tuple(
            _substitute_functional_parameters(item, bindings) for item in value
        )
    if is_dataclass(value) and not isinstance(value, type):
        updates = {
            item.name: _substitute_functional_parameters(
                getattr(value, item.name), bindings
            )
            for item in fields(value)
            if item.init and item.name not in {"type", "origin"}
        }
        try:
            return replace(value, **updates) if updates else value
        except (TypeError, ValueError) as error:
            raise _FunctionalSpecializationRejected(str(error)) from error
    return value


class FunctionalCallableSpecializer:
    """Own functional admission, substitution, captures, and rollback."""

    @staticmethod
    def close_nested_template(
        value: ir_expr.Expression,
        binder: functional_regions.CompileTimeBinderRef,
    ) -> tuple[
        ir_expr.Expression,
        tuple[tuple[ir_expr.FunctionalCaptureRef, ir_expr.Expression], ...],
    ]:
        """Capture expressions owned by enclosing functional binders.

        Nested regions own only their local binder.  A value depending exclusively
        on an enclosing binder is therefore an immutable capture, while a single
        compile-time expression mixing local and foreign binders remains outside
        the closed representable subset and falls back to eager elaboration.
        """

        captures: list[tuple[ir_expr.FunctionalCaptureRef, ir_expr.Expression]] = []
        binder_memo: dict[
            tuple[int, frozenset[str]], tuple[object, frozenset[str]]
        ] = {}

        def direct_binder_ids(item: object) -> set[str]:
            def walk(current: object, bound: frozenset[str]) -> frozenset[str]:
                key = (id(current), bound)
                cached = binder_memo.get(key)
                if cached is not None and cached[0] is current:
                    return cached[1]
                if isinstance(current, functional_regions.CompileTimeBinderRef):
                    result = (
                        frozenset((current.identity,))
                        if current.identity not in bound
                        else frozenset()
                    )
                elif isinstance(current, ir_expr.FunctionalRegion):
                    nested_bound = bound | {current.binder.identity}
                    result = walk(current.template, nested_bound)
                    result |= walk(
                        tuple(table.values for table in current.tables), nested_bound
                    )
                    result |= walk(
                        tuple(captured for _, captured in current.captures), bound
                    )
                elif isinstance(current, tuple):
                    result = frozenset().union(
                        *(walk(child, bound) for child in current)
                    )
                elif is_dataclass(current) and not isinstance(current, type):
                    result = frozenset().union(
                        *(
                            walk(getattr(current, field_.name), bound)
                            for field_ in fields(current)
                            if field_.name not in {"type", "origin"}
                        )
                    )
                else:
                    result = frozenset()
                binder_memo[key] = (current, result)
                return result

            return set(walk(item, frozenset()))

        def capture(expression: ir_expr.Expression) -> ir_expr.FunctionalCaptureRef:
            for reference, existing in captures:
                if existing == expression:
                    return reference
            ordinal = len(captures)
            reference = ir_expr.FunctionalCaptureRef(
                f"{binder.identity}:capture:{ordinal}",
                f"{binder.display_name}_capture_{ordinal}",
                expression.type,
            )
            captures.append((reference, expression))
            return reference

        def rewrite(expression: ir_expr.Expression) -> ir_expr.Expression:
            if isinstance(expression, ir_expr.FunctionalRegion):
                return expression
            binder_ids = direct_binder_ids(expression)
            if binder_ids and binder.identity not in binder_ids:
                return capture(expression)
            if isinstance(expression, ir_expr.FunctionalValue) and (
                binder_ids - {binder.identity}
            ):
                raise _FunctionalSpecializationRejected(
                    "one functional value mixes local and enclosing binders"
                )
            if not binder_ids and not isinstance(
                expression,
                (
                    ir_expr.Constant,
                    ir_expr.FunctionalCaptureRef,
                    ir_expr.FunctionalTableLookup,
                    ir_expr.FunctionalValue,
                ),
            ):
                return capture(expression)
            updates = {
                field_.name: rewrite_value(getattr(expression, field_.name))
                for field_ in fields(expression)
                if field_.init and field_.name not in {"type", "origin"}
            }
            try:
                return replace(expression, **updates) if updates else expression
            except (TypeError, ValueError) as error:
                raise _FunctionalSpecializationRejected(str(error)) from error

        def rewrite_value(item: object) -> object:
            if isinstance(item, ir_expr.Expression):
                return rewrite(item)
            if isinstance(item, tuple):
                return tuple(rewrite_value(child) for child in item)
            if is_dataclass(item) and not isinstance(item, type):
                updates = {
                    field_.name: rewrite_value(getattr(item, field_.name))
                    for field_ in fields(item)
                    if field_.init and field_.name not in {"type", "origin"}
                }
                try:
                    return replace(item, **updates) if updates else item
                except (TypeError, ValueError) as error:
                    raise _FunctionalSpecializationRejected(str(error)) from error
            return item

        return rewrite(value), tuple(captures)

    def try_specialize(
        self,
        specializer: CallableSpecializationOwner,
        argument_binder: SpecializationArgumentBinder,
        declaration: ast.FunctionDecl | ast.OperatorDecl,
        arguments: tuple[ir_expr.Expression, ...],
        explicit: tuple[ast.SpecializationArgument, ...],
        context: ExpressionContext,
        *,
        specialization_symbols: dict[str, object] | None,
        call_origin: SourceOrigin | None,
    ) -> ir_expr.Expression | None:
        """Inline one provably value-only binder-dependent specialization.

        This admission path runs only while checking a symbolic functional-region
        template.  A failure restores every mutable specialization registry and
        lets the established eager elaborator produce the authoritative result or
        diagnostic.
        """

        if not context.scope.functional_symbolic_values or not explicit:
            return None
        parameters = {item.name: item for item in declaration.generic_parameters}
        positional = [item for item in explicit if item.name is None]
        named = [item for item in explicit if item.name is not None]
        if positional and named:
            return None
        lifted: dict[str, functional_regions.CompileTimeExpr] = {}
        concrete_explicit: list[ast.SpecializationArgument] = []
        binder_values = {
            binder.identity: binder.start
            for expression in context.scope.functional_symbolic_values.values()
            for binder in _compile_time_binders(expression)
        }
        for index, item in enumerate(explicit):
            name = item.name or (
                declaration.generic_parameters[index].name
                if index < len(declaration.generic_parameters)
                else ""
            )
            symbolic = _symbolic_specialization_value(item.value, context)
            if symbolic is None:
                concrete_explicit.append(item)
                continue
            parameter = parameters.get(name)
            if parameter is None or parameter.kind != "value":
                return None
            lifted[name] = symbolic
            try:
                representative = functional_regions.evaluate_compile_time(
                    symbolic, binder_values
                )
            except ValueError:
                return None
            concrete_explicit.append(
                ast.SpecializationArgument(item.name, representative)
            )
        if not lifted:
            return None
        lifted_names = set(lifted)
        if any(
            _type_syntax_mentions(parameter.type_name, lifted_names)
            for parameter in declaration.parameters
        ) or _type_syntax_mentions(declaration.return_type, lifted_names):
            return None

        saved = context.services.callables.snapshot()
        saved_certificate_count = len(
            context.scope.functional_specialization_certificates
        )
        budget = context.services.compile_time_budget
        saved_budget = (
            (budget.generated_elements, budget.operations, budget.call_depth)
            if budget is not None
            else None
        )
        try:
            (
                type_bindings,
                value_bindings,
                constant_bindings,
                callable_bindings,
            ) = argument_binder.bindings(
                specializer,
                declaration,
                tuple(concrete_explicit),
                arguments,
                context,
                specialization_symbols,
            )
            if constant_bindings or callable_bindings:
                raise _FunctionalSpecializationRejected(
                    "functional lifting does not cross constant/callable parameters"
                )
            assert context.environment.type_resolver is not None
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
                {
                    **context.environment.parameters,
                    **{
                        name: value
                        for name, value in value_bindings.items()
                        if name not in lifted
                    },
                },
                callable_type_bindings,
                context.environment.type_resolver._identity_namespace,
                tagged_unions=tuple(
                    context.environment.type_resolver._tagged_union_declarations.values()
                ),
            )
            concrete_parameters = tuple(
                ir_module.FunctionParameter(
                    parameter.name, resolver.resolve(parameter.type_name)
                )
                for parameter in declaration.parameters
            )
            if any(
                formal.type != actual.type
                for formal, actual in zip(concrete_parameters, arguments, strict=True)
            ):
                raise _FunctionalSpecializationRejected(
                    "functional callable arguments changed exact type"
                )
            expected_return = (
                resolver.resolve(declaration.return_type)
                if declaration.return_type is not None
                else None
            )
            identity = stable_digest(
                {
                    "schema": "zlang-functional-specialization-v1",
                    "source": declaration.source_identity,
                    "owner": getattr(
                        declaration, "name", getattr(declaration, "operator", "")
                    ),
                    "types": tuple(
                        sorted(
                            (name, str(value)) for name, value in type_bindings.items()
                        )
                    ),
                    "values": tuple(
                        sorted(
                            (name, value)
                            for name, value in value_bindings.items()
                            if name not in lifted
                        )
                    ),
                    "lifted": tuple(sorted(lifted.items())),
                    "dependency": context.environment.generic_dependency_identity,
                }
            )
            stack_key = f"functional:{identity}"
            if stack_key in context.scope.resolution_stack:
                raise _FunctionalSpecializationRejected(
                    "recursive functional specialization cycle"
                )
            body_context = _context_for_callable_body(
                context, declaration.source_identity, identity
            )
            body_context = body_context.with_environment(
                type_resolver=resolver,
                parameters={
                    **context.environment.parameters,
                    **{
                        name: value
                        for name, value in value_bindings.items()
                        if name not in lifted
                    },
                },
            ).with_scope(
                functional_symbolic_values={
                    **context.scope.functional_symbolic_values,
                    **lifted,
                },
                index_types={
                    **body_context.scope.index_types,
                    **{
                        name: ir_types.UIntType(max(1, upper.bit_length()))
                        for name, expression in lifted.items()
                        for lower, upper in (
                            functional_regions.compile_time_range(expression),
                        )
                        if lower >= 0
                    },
                },
                resolution_stack=(*context.scope.resolution_stack, stack_key),
                allow_fixed_target_coercion=declaration.return_type is None,
            )
            symbols: dict[str, object] = {
                parameter.name: parameter for parameter in concrete_parameters
            }
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
            if expected_return is not None and body.type != expected_return:
                raise _FunctionalSpecializationRejected(
                    "functional callable return type depends on lifted value"
                )
            substituted = _substitute_functional_parameters(
                body,
                {
                    parameter.name: argument
                    for parameter, argument in zip(
                        concrete_parameters, arguments, strict=True
                    )
                },
            )
            assert isinstance(substituted, ir_expr.Expression)
            if call_origin is not None:
                substituted = replace(substituted, origin=call_origin)
            unique_binders = {
                binder.identity: binder
                for expression in lifted.values()
                for binder in _compile_time_binders(expression)
            }
            virtual_instances = 1
            for binder in unique_binders.values():
                virtual_instances *= binder.stop - binder.start
            owner_binder_identity = next(
                (
                    owned_binder.identity
                    for symbolic_value in context.scope.functional_symbolic_values.values()
                    for owned_binder in _compile_time_binders(symbolic_value)
                    if owned_binder.identity in unique_binders
                ),
                "",
            )
            if not owner_binder_identity:
                raise _FunctionalSpecializationRejected(
                    "functional specialization has no enclosing owner binder"
                )
            declaration_owner = getattr(
                declaration, "name", f"operator{getattr(declaration, 'operator', '')}"
            )
            context.scope.functional_specialization_certificates.append(
                functional_regions.FunctionalSpecializationCertificate(
                    declaration_identity=(
                        f"{declaration.source_identity or context.scope.source_unit or '<source>'}:"
                        f"{declaration_owner}"
                    ),
                    dependency_identity=(
                        context.environment.generic_dependency_identity
                    ),
                    invariant_arguments=tuple(
                        sorted(
                            (name, str(value))
                            for name, value in (
                                *type_bindings.items(),
                                *(
                                    item
                                    for item in value_bindings.items()
                                    if item[0] not in lifted
                                ),
                            )
                        )
                    ),
                    lifted_arguments=tuple(sorted(lifted.items())),
                    owner_binder_identity=owner_binder_identity,
                    parameter_types=tuple(item.type for item in concrete_parameters),
                    return_type=substituted.type,
                    body_identity=expression_semantic_identity(substituted),
                    virtual_instances=virtual_instances,
                )
            )
            return substituted
        except (SemanticError, ValueError, _FunctionalSpecializationRejected):
            context.services.callables.restore(saved)
            del context.scope.functional_specialization_certificates[
                saved_certificate_count:
            ]
            if budget is not None and saved_budget is not None:
                (
                    budget.generated_elements,
                    budget.operations,
                    budget.call_depth,
                ) = saved_budget
            return None


__all__ = ["FunctionalCallableSpecializer"]
