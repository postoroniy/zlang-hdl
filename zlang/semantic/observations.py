# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Demand-driven, observational editor projections for semantic analysis.

The functions in this module consume already-resolved compiler objects.  They
never participate in name resolution or type checking and therefore cannot
change the typed program.  The orchestration facade retains the small wrappers
which supply compiler-native source origins.
"""

from __future__ import annotations

from collections.abc import Callable

from zlang.ast import nodes as ast
from zlang.completion_resolution import CompletionCandidate, CompletionScope
from zlang.ir import expressions as ir_expr
from zlang.ir import module as ir_module
from zlang.ir.interfaces import InterfaceProtocol
from zlang.signature_help_resolution import (
    SignatureHelpCall,
    SignatureParameter,
)
from zlang.source import SourceOrigin


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
    detail_renderer: Callable[..., str] = completion_function_detail,
) -> tuple[CompletionCandidate, ...]:
    """Project the compiler's current semantic environment for one scope."""

    candidates: list[CompletionCandidate] = []
    for name, signature in context.functions.items():
        candidates.append(
            CompletionCandidate(
                name,
                "function",
                detail_renderer(name, signature=signature),
                context.definition_targets.get(id(signature.declaration)),
            )
        )
    for name, declaration in context.generic_functions.items():
        candidates.append(
            CompletionCandidate(
                name,
                "function",
                detail_renderer(name, declaration=declaration),
                context.definition_targets.get(id(declaration)),
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
                context.definition_targets.get(id(symbol)),
            )
        )
    for name in context.parameters:
        candidates.append(
            CompletionCandidate(name, "parameter", "compile-time parameter")
        )
    for name in context.index_bindings:
        candidates.append(
            CompletionCandidate(name, "parameter", "compile-time index")
        )
    for name, value in context.compile_time_constants.items():
        candidates.append(CompletionCandidate(name, "parameter", str(value.type)))
    for name in context.static_callables:
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
    semantic_origin: Callable[[ast.Expression, object], SourceOrigin | None],
    candidate_provider: Callable[
        [dict[str, object], object], tuple[CompletionCandidate, ...]
    ] = completion_candidates,
) -> None:
    """Record visible candidates only when completion was requested."""

    if scopes is None or expression.origin is None:
        return
    origin = semantic_origin(expression, context)
    if origin is not None:
        scopes.append(CompletionScope(origin, candidate_provider(inputs, context)))


def record_signature_help_call(
    calls: list[SignatureHelpCall] | None,
    expression: ast.CallExpr,
    context: object,
    parameters: tuple[ir_module.FunctionParameter, ...],
    return_type: object,
    semantic_origin: Callable[[ast.Expression, object], SourceOrigin | None],
    callable_origin: Callable[[ast.CallExpr, object], SourceOrigin | None],
) -> None:
    """Record one resolved call without affecting callable resolution."""

    if calls is None or expression.origin is None:
        return
    call_origin = semantic_origin(expression, context)
    if call_origin is None or len(parameters) != len(expression.arguments):
        return
    argument_origins = tuple(
        semantic_origin(argument, context) for argument in expression.arguments
    )
    record = SignatureHelpCall(
        call_origin,
        callable_origin(expression, context),
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
