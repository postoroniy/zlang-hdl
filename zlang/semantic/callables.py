# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned callable catalogs and shared callable representations.

CallableSpecializer owns specialization policy while this module retains
callable identities, ordinary-function lookup, shared binding records, and
retained-call-graph utilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import callables as ir_callables
from zlang.ir import expressions as ir_expr, module as ir_module
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from .callable_state import CallableSpecializationCache
from . import compile_time_evaluation
from .errors import SemanticError


@dataclass(frozen=True)
class FunctionSignature:
    """One compiler-resolved ordinary function signature."""

    declaration: ast.FunctionDecl
    parameters: tuple[ir_module.FunctionParameter, ...]
    return_type: ir_types.HardwareType


@dataclass(frozen=True)
class FunctionPrototype:
    """One ordinary function before an omitted return has been inferred."""

    declaration: ast.FunctionDecl
    parameters: tuple[ir_module.FunctionParameter, ...]
    declared_return_type: ir_types.HardwareType | None


@dataclass(frozen=True)
class StaticCallableBinding:
    """One compile-time function parameter after exact specialization."""

    reference: ast.CallableRef
    parameter_types: tuple[ir_types.HardwareType, ...]
    return_type: ir_types.HardwareType
    callee_identity: str
    definitions: tuple[ir_module.Function, ...] = ()


@dataclass
class CallableRepository:
    """Compilation-local demand-driven ordinary-function signatures."""

    prototypes: dict[str, FunctionPrototype]
    signatures: dict[str, FunctionSignature]
    context: object | None = None
    _resolving: list[str] = field(default_factory=list)

    def resolve(
        self,
        name: str,
        *,
        call_origin: SourceOrigin | None = None,
    ) -> FunctionSignature | None:
        existing = self.signatures.get(name)
        if existing is not None:
            return existing
        prototype = self.prototypes.get(name)
        if prototype is None:
            return None
        if name in self._resolving:
            start = self._resolving.index(name)
            cycle_names = (*self._resolving[start:], name)
            cycle = " -> ".join(cycle_names)
            notes: list[str] = []
            for item in dict.fromkeys(cycle_names):
                declaration = self.prototypes[item].declaration
                if declaration.origin is None:
                    continue
                source_prefix = (
                    f"{declaration.source_identity}:"
                    if declaration.source_identity is not None
                    else ""
                )
                notes.append(
                    f"function '{item}' declared at {source_prefix}"
                    f"{declaration.origin.render()}"
                )
            raise SemanticError(
                f"recursive inferred return cycle: {cycle}",
                code="ZL-FUNCTION-RETURN-CYCLE",
                primary=call_origin,
                notes=tuple(notes),
            )
        if self.context is None:
            raise SemanticError("internal function catalog has no expression context")

        self._resolving.append(name)
        try:
            return_type = _infer_callable_return(
                prototype,
                self.context,
                f"return-probe:{name}",
            )
            signature = FunctionSignature(
                prototype.declaration,
                prototype.parameters,
                return_type,
            )
            self.signatures[name] = signature
            return signature
        finally:
            self._resolving.pop()


class CallableDependencyValidator:
    """Reject recursion in the retained typed callable graph."""

    def validate(self, functions: tuple[ir_module.Function, ...]) -> None:
        dependencies = {
            function.name: {
                use.function
                for use in ir_callables.callable_uses(function.body)
            }
            for function in functions
        }
        visited: set[str] = set()
        active: list[str] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in active:
                start = active.index(name)
                cycle = " -> ".join((*active[start:], name))
                raise SemanticError(f"recursive function call: {cycle}")
            active.append(name)
            for dependency in dependencies[name]:
                # Name/type resolution owns the unknown-function diagnostic.  The
                # recursion pass follows only concrete definitions participating
                # in this module's retained call graph.
                if dependency in dependencies:
                    visit(dependency)
            active.pop()
            visited.add(name)

        for function in functions:
            visit(function.name)

if TYPE_CHECKING:
    from .context import ExpressionContext


def _rom_aggregate_values(
    type_: ir_types.HardwareType,
    value: object,
) -> tuple[str, tuple[tuple[str | None, ir_types.HardwareType, object], ...]] | None:
    """Validate and enumerate one aggregate ROM value in source order."""

    mismatch = f"constant ROM value does not match {type_}"
    if isinstance(type_, ir_types.StructType):
        if not isinstance(value, dict):
            raise SemanticError(mismatch)
        return "struct", tuple(
            (field.name, field.type, value[field.name])
            for field in type_.fields
        )
    if isinstance(type_, ir_types.TupleType):
        if not isinstance(value, tuple) or len(value) != len(type_.elements):
            raise SemanticError(mismatch)
        return "tuple", tuple(
            (None, element_type, element)
            for element_type, element in zip(type_.elements, value, strict=True)
        )
    if isinstance(type_, ir_types.VecType):
        if not isinstance(value, (tuple, list)) or len(value) != type_.length:
            raise SemanticError(mismatch)
        return "vec", tuple(
            (None, type_.element_type, element) for element in value
        )
    return None


def _canonical_rom_runtime_values(
    type_: ir_types.HardwareType,
    value: object,
) -> object:
    """Canonical, type-directed payload used for initialized-ROM hashing."""

    aggregate = _rom_aggregate_values(type_, value)
    if aggregate is not None:
        kind, items = aggregate
        return (
            kind,
            str(type_),
            tuple(
                (
                    _canonical_rom_runtime_values(item_type, item_value)
                    if name is None
                    else (
                        name,
                        _canonical_rom_runtime_values(item_type, item_value),
                    )
                )
                for name, item_type, item_value in items
            ),
        )
    return ("scalar", str(type_), value)


def _rom_constant_expression(
    type_: ir_types.HardwareType,
    value: object,
    origin: SourceOrigin | None,
) -> ir_expr.Expression:
    """Rebuild one bounded ROM word from its evaluated immutable value."""

    aggregate = _rom_aggregate_values(type_, value)
    if aggregate is None:
        return ir_expr.Constant(value, type_, origin=origin)
    kind, items = aggregate
    children = tuple(
        _rom_constant_expression(item_type, item_value, origin)
        for _name, item_type, item_value in items
    )
    if kind == "struct":
        return ir_expr.StructConstruct(
            type_.name,
            tuple(
                (item[0], child)
                for item, child in zip(items, children, strict=True)
            ),
            type_,
            origin=origin,
        )
    if kind == "tuple":
        return ir_expr.TupleConstruct(
            children,
            type_,
            origin=origin,
        )
    assert isinstance(type_, ir_types.VecType)
    return ir_expr.Generate(
        "rom_init",
        0,
        type_.length,
        children,
        type_,
        origin=origin,
    )


def _infer_callable_return(
    prototype: FunctionPrototype,
    context: object,
    callable_identity: str,
) -> ir_types.HardwareType:
    # Return probing must not publish generic specializations, consume the
    # selected module's logical elaboration budget, or retain a temporary
    # functional-binder identity.
    probe_context = context.with_services(
        callables=CallableSpecializationCache(
            callable_definitions=dict(context.services.callables.callable_definitions)
        ),
        compile_time_real_quantize_cache={},
        compile_time_budget=compile_time_evaluation.CompileTimeBudget(),
        exploration_results=None,
    ).with_scope(
        functional_binder_ordinals={},
        next_functional_binder_ordinal=[0],
        functional_binder_nesting=(),
    )
    probe_context = _context_for_callable_body(
        probe_context,
        prototype.declaration.source_identity,
        callable_identity,
    )
    symbols = {parameter.name: parameter for parameter in prototype.parameters}
    return probe_context.services.callable_bodies.check(
        prototype.declaration,
        symbols,
        None,
        probe_context,
        typed_boundary=False,
    ).type


def _static_callable_definition_closure(
    root: ir_expr.Call,
    context: ExpressionContext,
) -> tuple[ir_module.Function, ...]:
    """Return the exact definition closure selected by a static callable.

    A callable module argument crosses a module-analysis boundary.  Its root
    ordinary definition and any monomorphic generic/operator dependencies
    therefore travel together under their compiler-owned identities.
    """

    definitions = tuple((
        *context.services.callables.function_definitions.values(),
        *context.services.callables.callable_definitions.values(),
    ))
    try:
        reachable = ir_callables.reachable_callable_definitions(definitions, (root,))
    except ir_callables.CallableReachabilityError as error:
        raise SemanticError(
            f"invalid statically selected callable graph: {error}"
        ) from error
    return reachable


def _inherited_static_callable_definitions(
    bindings: dict[str, StaticCallableBinding] | None,
) -> dict[str, ir_module.Function]:
    """Merge exact callable closures carried by module parameters."""

    result: dict[str, ir_module.Function] = {}
    for binding in (bindings or {}).values():
        for definition in binding.definitions:
            previous = result.get(definition.callee_identity)
            if previous is not None and previous != definition:
                raise SemanticError(
                    "statically selected callable identity has conflicting "
                    "concrete definitions"
                )
            result[definition.callee_identity] = definition
    return result


def _context_for_source_declaration(
    context: ExpressionContext,
    source_unit: str | None,
) -> ExpressionContext:
    """Select provenance for a declaration body without changing its semantics."""

    if source_unit is None or source_unit == context.scope.source_unit:
        return context
    return context.with_scope(
        source_unit=source_unit,
        source_digest=context.environment.source_digests.get(source_unit),
    )


def _context_for_callable_body(
    context: ExpressionContext,
    source_unit: str | None,
    callable_identity: str,
) -> ExpressionContext:
    """Give one callable a declaration-stable functional-region namespace.

    Ordinary function declarations are typed independently for every selected
    module in a hierarchy.  A module-global functional ordinal therefore made
    an otherwise identical function body depend on unrelated declarations and
    import order.  Callable bodies instead start their own ordinal/nesting
    sequence under the exact typed callable identity.  Diagnostic provenance
    remains selected by ``_context_for_source_declaration`` and does not enter
    the semantic binder identity.
    """

    selected = _context_for_source_declaration(context, source_unit)
    return selected.with_scope(
        functional_binder_ordinals={},
        next_functional_binder_ordinal=[0],
        functional_binder_nesting=(),
        functional_binder_callable_identity=callable_identity,
        # Candidate sites inside a retained callable body must be addressable
        # after typing.  The exact callee identity is the semantic boundary;
        # source names are not unique across generic specializations.
        candidate_site_owner=f"callable:{callable_identity}",
    )


def _lookup_function_signature(
    context: ExpressionContext,
    name: str,
    *,
    call_origin: SourceOrigin | None = None,
) -> FunctionSignature | None:
    """Resolve one ordinary function without using caller result context."""

    signature = context.environment.functions.get(name)
    if signature is not None or context.environment.function_catalog is None:
        return signature
    return context.environment.function_catalog.resolve(
        name, call_origin=call_origin
    )




def _validate_tuple_destructure_assignment(
    declaration: ast.Assignment,
    value: ir_expr.Expression,
) -> None:
    """Validate the syntax-only hidden value before projections are typed."""

    arity = declaration.tuple_destructure_arity
    if arity is None:
        return
    if not isinstance(value.type, ir_types.TupleType):
        raise SemanticError(
            f"tuple destructuring requires a tuple value, got {value.type}"
        )
    if len(value.type.elements) != arity:
        raise SemanticError(
            f"tuple destructuring has {arity} bindings, but {value.type} has "
            f"{len(value.type.elements)} components"
        )


def _expand_analysis_calls(
    expression: ir_expr.Expression,
    context: ExpressionContext,
    *,
    purpose: str,
    functions: tuple[ir_module.Function, ...] | list[ir_module.Function] = (),
) -> ir_expr.Expression:
    """Create an exact temporary analysis view of retained callable bodies."""

    if functions and not context.services.callables.function_definitions:
        context.services.callables.function_definitions.update(
            (function.name, function) for function in functions
        )
    # The input may already be an immutable DAG with large shared local
    # vectors.  Callable expansion reconstructs expression nodes while it
    # walks them, so doing that for a call-free index can multiply the same
    # subtree thousands of times.  The compiler-owned use walker visits each
    # shared node once; with no callable edge the exact analysis view is the
    # original typed expression.
    if not ir_callables.callable_uses(expression):
        return expression

    function_definitions = tuple(
        context.services.callables.function_definitions[name]
        for name in sorted(context.services.callables.function_definitions)
    )
    callable_definitions = tuple(
        context.services.callables.callable_definitions[identity]
        for identity in sorted(context.services.callables.callable_definitions)
    )
    try:
        return ir_callables.expand_callable_calls(
            expression,
            (*function_definitions, *callable_definitions),
        )
    except ir_callables.CallableExpansionError as error:
        raise SemanticError(f"cannot analyze {purpose}: {error}") from error
