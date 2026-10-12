# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Demand-driven, observational editor projections for semantic analysis.

The functions in this module consume already-resolved compiler objects.  They
never participate in name resolution or type checking and therefore cannot
change the typed program.  Observation owners supply compiler-native origins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zlang.analysis_needs import AnalysisNeeds
from zlang.ast import nodes as ast
from zlang.completion_resolution import CompletionCandidate, CompletionScope
from zlang.definition_resolution import DefinitionResolution, DefinitionTarget
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir.interfaces import InterfaceProtocol
from zlang.signature_help_resolution import (
    SignatureHelpCall,
    SignatureParameter,
)
from zlang.source import SourceOrigin, SourceSpan

from . import expression_origins

if TYPE_CHECKING:
    from .context import ExpressionContext


def declaration_origin(
    span: SourceSpan | None,
    construct: str,
    context: ExpressionContext,
    *,
    source_unit: str | None = None,
) -> SourceOrigin | None:
    """Create one authoritative declaration origin without source guessing."""

    if span is None:
        return None
    unit = source_unit if source_unit is not None else context.scope.source_unit
    return SourceOrigin(
        span,
        construct,
        unit,
        context.environment.source_digests.get(unit, context.scope.source_digest)
        if unit is not None
        else context.scope.source_digest,
    )


def remember_definition_target(
    context: ExpressionContext,
    symbol: object,
    origin: SourceOrigin | None,
    *,
    name: str | None = None,
    kind: str | None = None,
) -> None:
    """Associate the exact semantic symbol object with its declaration span."""

    if origin is not None and (
        context.services.tooling.analysis_needs.wants(AnalysisNeeds.DEFINITIONS)
        or context.services.tooling.analysis_needs.wants(AnalysisNeeds.COMPLETION)
    ):
        context.services.tooling.definition_targets[id(symbol)] = origin
        if (
            context.services.tooling.definition_declarations is not None
            and name is not None
            and kind is not None
        ):
            context.services.tooling.definition_declarations.append(
                DefinitionTarget(origin, name, kind)
            )


def definition_kind(symbol: object) -> str:
    """Return the stable editor-facing declaration category for a symbol."""

    if isinstance(symbol, ir_module.Port):
        return "port"
    if isinstance(symbol, ir_module.FunctionParameter):
        return "parameter"
    if isinstance(symbol, ir_module.Register):
        return "register"
    if isinstance(symbol, ir_module.LocalValue):
        return "value"
    return "symbol"


def record_definition(
    context: ExpressionContext,
    occurrence: SourceOrigin | None,
    target: SourceOrigin | None,
    *,
    name: str,
    kind: str,
) -> None:
    """Publish one already-resolved semantic occurrence/target relation."""

    if (
        context.services.tooling.definition_resolutions is None
        or occurrence is None
        or target is None
    ):
        return
    context.services.tooling.definition_resolutions.append(
        DefinitionResolution(occurrence, target, name, kind)
    )


def module_declaration_origin(
    declaration: ast.Module,
    context: ExpressionContext,
) -> SourceOrigin | None:
    """Return the resolver-owned exact source origin of a module name."""

    span = declaration.name_origin
    if span is None:
        return None
    source_unit = declaration.source_identity or context.scope.source_unit
    digest = (
        declaration.source_hash
        or (
            context.environment.source_digests.get(source_unit)
            if source_unit is not None
            else None
        )
        or context.scope.source_digest
    )
    return SourceOrigin(
        span,
        f"module {declaration.name}",
        source_unit,
        digest,
    )


def record_module_definition(
    context: ExpressionContext,
    instance: ast.InstanceDecl,
    target_module: ast.Module,
) -> None:
    """Record one resolved module-instance type without textual lookup."""

    if not context.services.tooling.analysis_needs.wants(AnalysisNeeds.DEFINITIONS):
        return
    if context.services.tooling.definition_resolutions is None:
        return
    occurrence = declaration_origin(
        instance.module_origin,
        f"module {instance.module}",
        context,
    )
    target = module_declaration_origin(target_module, context)
    if occurrence is None or target is None:
        return
    record_definition(
        context,
        occurrence,
        target,
        name=instance.module,
        kind="module",
    )
    if context.services.tooling.definition_declarations is not None:
        already_published = any(
            item.name == target_module.name
            and item.kind == "module"
            and item.target == target
            for item in context.services.tooling.definition_declarations
        )
        if not already_published:
            context.services.tooling.definition_declarations.append(
                DefinitionTarget(target, target_module.name, "module")
            )


def record_local_resource_definitions(
    context: ExpressionContext, module: ast.Module
) -> None:
    """Publish exact source-local resource declarations and resolved uses."""

    if context.services.tooling.definition_declarations is None:
        return
    declared: dict[str, list[SourceOrigin]] = {}
    for resource in module.resource_definitions:
        target = declaration_origin(
            resource.name_origin,
            f"resource {resource.name}",
            context,
        )
        if target is None:
            continue
        declared.setdefault(resource.name, []).append(target)
        remember_definition_target(
            context, resource, target, name=resource.name, kind="resource"
        )

    def record(name: str, span: SourceSpan | None) -> None:
        targets = declared.get(name, ())
        if len(targets) != 1:
            return
        occurrence = declaration_origin(span, f"resource {name}", context)
        record_definition(
            context, occurrence, targets[0], name=name, kind="resource"
        )

    for family in module.target_families:
        for name, span in zip(
            family.resources, family.resource_origins, strict=False
        ):
            record(name, span)
    for architecture in module.architecture_templates:
        record(architecture.resource, architecture.resource_origin)


def completion_function_detail(
    name: str,
    *,
    signature: object | None = None,
    declaration: ast.FunctionDecl | None = None,
) -> str:
    """Render the established stable source-facing callable detail."""

    if signature is not None:
        parameters = ", ".join(
            f"{parameter.name} : {parameter.type}"
            for parameter in signature.parameters
        )
        return f"fn {name}({parameters}) -> {signature.return_type}"
    if declaration is None:
        return f"fn {name}"
    parameters = ", ".join(
        f"{parameter.name} : {parameter.type_name}"
        for parameter in declaration.parameters
    )
    generic = ""
    if declaration.generic_parameters:
        generic = "<" + ", ".join(
            parameter.name for parameter in declaration.generic_parameters
        ) + ">"
    suffix = (
        f" -> {declaration.return_type}"
        if declaration.return_type is not None
        else ""
    )
    return f"fn {name}{generic}({parameters}){suffix}"


def completion_candidates(
    inputs: dict[str, object],
    context: object,
) -> tuple[CompletionCandidate, ...]:
    """Project the compiler's current semantic environment for one scope."""

    candidates: list[CompletionCandidate] = []
    for name, signature in context.environment.functions.items():
        candidates.append(
            CompletionCandidate(
                name,
                "function",
                completion_function_detail(name, signature=signature),
                context.services.tooling.definition_targets.get(id(signature.declaration)),
            )
        )
    for name, declaration in context.environment.generic_functions.items():
        candidates.append(
            CompletionCandidate(
                name,
                "function",
                completion_function_detail(name, declaration=declaration),
                context.services.tooling.definition_targets.get(id(declaration)),
            )
        )
    for name, symbol in inputs.items():
        kind: str | None = None
        detail: str | None = None
        if isinstance(symbol, ir_module.Port):
            if symbol.protocol is not InterfaceProtocol.WIRE:
                continue
            kind, detail = "port", str(symbol.type)
        elif isinstance(symbol, ir_module.FunctionParameter):
            kind, detail = "parameter", str(symbol.type)
        elif isinstance(symbol, ir_module.LocalValue):
            kind, detail = "value", str(symbol.type)
        elif isinstance(symbol, ir_expr.Expression):
            kind, detail = "value", str(symbol.type)
        if kind is None:
            continue
        candidates.append(
            CompletionCandidate(
                name,
                kind,
                detail,
                context.services.tooling.definition_targets.get(id(symbol)),
            )
        )
    for name in context.environment.parameters:
        candidates.append(
            CompletionCandidate(name, "parameter", "compile-time parameter")
        )
    for name in context.scope.index_bindings:
        candidates.append(
            CompletionCandidate(name, "parameter", "compile-time index")
        )
    for name, value in context.scope.compile_time_constants.items():
        candidates.append(CompletionCandidate(name, "parameter", str(value.type)))
    for name in context.scope.static_callables:
        candidates.append(CompletionCandidate(name, "function", f"fn {name}"))

    unique: dict[tuple[object, ...], CompletionCandidate] = {}
    for candidate in candidates:
        target = candidate.target
        target_key = (
            None
            if target is None
            else (
                target.source_unit,
                target.digest,
                target.span.start_line,
                target.span.start_column,
                target.span.end_line,
                target.span.end_column,
                target.construct,
            )
        )
        unique[(candidate.name, candidate.kind, candidate.detail, target_key)] = candidate
    return tuple(
        sorted(
            unique.values(),
            key=lambda candidate: (candidate.name, candidate.kind, candidate.detail or ""),
        )
    )


def record_completion_scope(
    scopes: list[CompletionScope] | None,
    expression: ast.Expression,
    inputs: dict[str, object],
    context: object,
) -> None:
    """Record visible candidates only when completion was requested."""

    if scopes is None or expression.origin is None:
        return
    origin = expression_origins.semantic_origin(expression, context)
    if origin is not None:
        scopes.append(CompletionScope(origin, completion_candidates(inputs, context)))


def record_signature_help_call(
    calls: list[SignatureHelpCall] | None,
    expression: ast.CallExpr,
    context: object,
    parameters: tuple[ir_module.FunctionParameter, ...],
    return_type: object,
) -> None:
    """Record one resolved call without affecting callable resolution."""

    if calls is None or expression.origin is None:
        return
    call_origin = expression_origins.semantic_origin(expression, context)
    if call_origin is None or len(parameters) != len(expression.arguments):
        return
    argument_origins = tuple(
        expression_origins.semantic_origin(argument, context)
        for argument in expression.arguments
    )
    record = SignatureHelpCall(
        call_origin,
        expression_origins.callable_reference_origin(expression, context),
        argument_origins,
        expression.function,
        tuple(
            SignatureParameter(parameter.name, str(parameter.type))
            for parameter in parameters
        ),
        str(return_type),
    )
    if record not in calls:
        calls.append(record)
