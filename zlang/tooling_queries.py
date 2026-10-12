# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned hover, completion, signature, and semantic-token queries."""


from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from zlang.analysis_needs import AnalysisNeeds
from zlang.ast import nodes as ast_nodes
from zlang.completion_resolution import CompletionScope
from zlang.compiler import TopSelectionError, check_file_snapshot
from zlang.definition_resolution import DefinitionResolution
from zlang.dependencies import DependencyModelError
from zlang.diagnostics import DiagnosticError
from zlang.ir import expressions as ir_expressions
from zlang.ir.traversal import walk_expression
from zlang.module_resolver import ModuleResolutionError
from zlang.parser import ParseError
from zlang.project import ProjectModelError
from zlang.semantic import SemanticError
from zlang.signature_help_resolution import SignatureHelpCall
from zlang import workspace as workspace
from zlang import tooling_models as tooling_models
from zlang import tooling_session as tooling_session
from zlang import tooling_workspace as tooling_workspace


from zlang import tooling_symbols as tooling_symbols


_SEMANTIC_QUERY_ERRORS = (
    ParseError,
    TopSelectionError,
    SemanticError,
    DiagnosticError,
)
_INFRASTRUCTURE_QUERY_ERRORS = (
    ModuleResolutionError,
    workspace.WorkspaceError,
    ProjectModelError,
    DependencyModelError,
    OSError,
    ValueError,
)


def _valid_position(line: object, character: object) -> bool:
    return (
        isinstance(line, int)
        and not isinstance(line, bool)
        and line >= 0
        and isinstance(character, int)
        and not isinstance(character, bool)
        and character >= 0
    )


def _optional_query_snapshot(factory: Callable[[], object]) -> object | None:
    """Apply the one fail-closed policy shared by optional semantic queries."""

    try:
        return factory()
    except _SEMANTIC_QUERY_ERRORS:
        return None
    except _INFRASTRUCTURE_QUERY_ERRORS as error:
        raise tooling_models.ToolingError(str(error)) from error

def _indexed_symbol_line(
    index: Mapping[tuple[str | None, int], tuple[object, ...]],
    source_unit: str | None,
    line: int,
) -> tuple[object, ...]:
    if source_unit is not None:
        return index.get((source_unit, line), ())
    return tuple(
        item
        for (unit, indexed_line), records in index.items()
        if indexed_line == line
        for item in records
    )

def _semantic_snapshot(
    source: Path,
    source_text: str,
    *,
    analysis_needs: AnalysisNeeds,
    session: tooling_session.ToolingSession | None,
    source_digest: str | None = None,
    project: Path | str | None = None,
    profile: str | None = None,
    top: str | None = None,
) -> object:
    """Use a session cache when supplied, otherwise preserve direct behavior."""

    if session is not None:
        return session.semantic_snapshot(
            source,
            source_text,
            analysis_needs=analysis_needs,
            source_digest=source_digest,
            project=project,
            profile=profile,
            top=top,
        )
    return check_file_snapshot(
        source,
        source_text,
        source_digest=source_digest,
        project=project,
        profile=profile,
        top=top,
        analysis_needs=analysis_needs,
        allow_external_enum_inputs=True,
        allow_unsaved_root=True,
    )

def _definition_snapshot(
    source: Path,
    source_text: str,
    *,
    session: tooling_session.ToolingSession | None,
    project: Path | str | None = None,
    profile: str | None = None,
    top: str | None = None,
) -> object:
    """Prefer the normalized symbol cache before requesting typed analysis."""

    if session is not None:
        cached = session.symbol_snapshot(
            source,
            source_text,
            project=project,
            profile=profile,
            top=top,
        )
        if cached is not None:
            return cached
    return _semantic_snapshot(
        source,
        source_text,
        analysis_needs=AnalysisNeeds.DEFINITIONS,
        session=session,
        project=project,
        profile=profile,
        top=top,
    )

def _definition_target_path(
    source: Path,
    resolution: DefinitionResolution,
    physical_inputs: object,
    session: tooling_session.ToolingSession | None = None,
) -> Path | None:
    return tooling_workspace._source_path_for_origin(
        source, resolution.target, physical_inputs, session
    )

def _definition_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    session: tooling_session.ToolingSession | None = None,
) -> tooling_symbols.ToolingDefinition | None:
    root_unit = tooling_workspace._root_logical_source(source, session)
    indexed = getattr(result, "occurrences_by_line", None)
    resolutions = (
        _indexed_symbol_line(indexed, root_unit, line + 1)
        if indexed is not None
        else result.definition_resolutions
    )
    resolution = tooling_symbols._narrowest_source_record(
        resolutions,
        source_unit=root_unit,
        line=line,
        character=character,
        source_origin=lambda item: item.occurrence,
    )
    if resolution is None:
        return None
    target_path = _definition_target_path(
        source, resolution, result.physical_inputs, session
    )
    if target_path is None:
        raise tooling_models.ToolingError(
            "compiler resolved a definition whose locked source path is unavailable"
        )
    target_origin = tooling_symbols._origin_from_source(resolution.target)
    if target_origin is None:
        raise tooling_models.ToolingError("compiler resolved a definition without a source origin")
    return tooling_symbols.ToolingDefinition(
        resolution.name,
        resolution.kind,
        target_path,
        target_origin,
    )

def _definition_lookup_columns(
    source_text: str,
    line: int,
    character: int,
) -> tuple[int, ...]:
    """Return the exact column and a safe identifier-edge fallback.

    LSP positions are insertion points.  VS Code can send the exclusive right
    edge of a selected identifier, while compiler source origins are correctly
    half-open.  Only that immediate identifier boundary is retried; arbitrary
    whitespace and punctuation positions still resolve to no definition.
    """

    columns = [character]
    lines = source_text.splitlines()
    if not (0 <= line < len(lines)):
        return tuple(columns)
    text = lines[line]
    if not (0 < character <= len(text)):
        return tuple(columns)
    previous = text[character - 1]
    current = text[character] if character < len(text) else None
    previous_is_identifier = previous == "_" or previous.isalnum()
    current_is_identifier = (
        current is not None and (current == "_" or current.isalnum())
    )
    if previous_is_identifier and not current_is_identifier:
        columns.append(character - 1)
    return tuple(columns)

def _hover_specificity(origin: tooling_symbols.ToolingOrigin) -> tuple[int, int]:
    line_span = origin.end_line - origin.start_line
    column_span = (
        origin.end_column - origin.start_column
        if line_span == 0
        else 1_000_000
    )
    return line_span, column_span

def _callable_signature(function: object) -> str:
    metadata = getattr(function, "metadata", None)
    kind = "operator" if getattr(getattr(metadata, "kind", None), "value", None) == "operator" else "fn"
    source_name = getattr(metadata, "source_name", None) or getattr(function, "name")
    parameters = ", ".join(
        f"{parameter.name} : {parameter.type}"
        for parameter in getattr(function, "parameters", ())
    )
    return f"{kind} {source_name}({parameters}) -> {function.return_type}"

def _expression_hover(
    value: ir_expressions.TracedExpression,
    callables: dict[str, object],
) -> tooling_symbols.ToolingHover:
    name: str | None = None
    kind = "expression"
    signature: str | None = None
    if isinstance(value, ir_expressions.InputRef):
        name, kind = value.name, "value"
    elif isinstance(value, ir_expressions.ParameterRef):
        name, kind = value.name, "parameter"
    elif isinstance(value, ir_expressions.RegisterRef):
        name, kind = value.name, "register"
    elif isinstance(value, ir_expressions.Constant):
        kind = "value"
    elif isinstance(value, ir_expressions.ReadyValidRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.RequestResponseRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.CreditRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.PacketRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.VirtualChannelCreditRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.FifoRef):
        name, kind = value.fifo, "fifo"
    elif isinstance(value, ir_expressions.MemoryRef):
        name, kind = value.memory, "memory"
    elif isinstance(value, ir_expressions.RomRef):
        name, kind = value.rom, "rom"
    elif isinstance(value, ir_expressions.InstanceOutputRef):
        name, kind = f"{value.instance}.{value.port}", "instance_output"
    elif isinstance(value, ir_expressions.FieldAccess):
        name, kind = value.field, "field"
    elif isinstance(value, ir_expressions.Call):
        callable_value = (
            callables.get(value.callee_identity)
            if value.callee_identity is not None
            else None
        )
        if callable_value is not None:
            name = getattr(getattr(callable_value, "metadata", None), "source_name", None)
            name = name or getattr(callable_value, "name")
            signature = _callable_signature(callable_value)
            kind = (
                "operator"
                if signature.startswith("operator ")
                else "function"
            )
        else:
            name, kind = value.function, "function"
    elif isinstance(value, ir_expressions.Add):
        name, kind = "+", "operator"
    elif isinstance(value, ir_expressions.Binary):
        name, kind = value.operator.value, "operator"
    elif isinstance(value, ir_expressions.Dot):
        name = "dot"
    elif isinstance(value, ir_expressions.Reduce):
        name = "reduce"
    elif isinstance(value, ir_expressions.FixedConvert):
        name = "fixed conversion"
    elif isinstance(value, ir_expressions.Mux):
        name = "mux"
    return tooling_symbols._hover_record(
        name=name,
        kind=kind,
        type_value=getattr(value, "type", None),
        origin=getattr(value, "origin", None),
        signature=signature,
    )

def _walk_expressions(value: object) -> tuple[ir_expressions.TracedExpression, ...]:
    if not isinstance(value, ir_expressions.TracedExpression):
        return ()
    return tuple(walk_expression(value, deduplicate=False))

def _module_expression_roots(module: object) -> tuple[object, ...]:
    roots: list[object] = []
    roots.extend(getattr(item, "expression") for item in module.assignments)
    roots.extend(
        getattr(item, "expression")
        for item in getattr(module, "next_assignments", ())
    )
    roots.extend(
        getattr(item, "initial") for item in getattr(module, "registers", ())
    )
    roots.extend(
        getattr(item, "expression") for item in getattr(module, "locals", ())
    )
    roots.extend(getattr(item, "body") for item in module.functions)
    roots.extend(
        getattr(item, "body")
        for item in getattr(module, "callable_definitions", ())
    )
    for rule in getattr(module, "rules", ()):
        roots.append(rule.guard)
        roots.extend(action.expression for action in rule.actions)
        roots.extend(
            action.activation
            for action in rule.actions
            if action.activation is not None
        )
    return tuple(roots)

def _module_hover_candidates(
    ast_module: ast_nodes.Module,
    ir_module: object,
    line: int,
    character: int,
) -> list[tuple[tuple[int, int, int], tooling_symbols.ToolingHover]]:
    candidates: list[tuple[tuple[int, int, int], tooling_symbols.ToolingHover]] = []

    def add(value: tooling_symbols.ToolingHover, priority: int) -> None:
        if value.origin is None or not tooling_symbols._position_in_origin(
            value.origin, line, character
        ):
            return
        line_span, column_span = _hover_specificity(value.origin)
        candidates.append(((line_span, column_span, priority), value))

    semantic_ports = {
        port.name: port for port in getattr(ir_module, "ports", ())
    }
    for declaration in ast_module.ports:
        names = declaration.names or (declaration.name,)
        for name in names:
            port = semantic_ports.get(name)
            if port is None:
                continue
            add(
                tooling_symbols._hover_record(
                    name=name,
                    kind="port",
                    type_value=port.type,
                    origin=declaration.origin,
                    port_direction=port.direction.value,
                ),
                4,
            )

    callables = {
        function.callee_identity: function
        for function in (
            *getattr(ir_module, "functions", ()),
            *getattr(ir_module, "callable_definitions", ()),
        )
    }
    for declaration in (*ast_module.functions, *ast_module.operators):
        function = next(
            (
                value
                for value in callables.values()
                if (
                    isinstance(declaration, ast_nodes.FunctionDecl)
                    and value.name == declaration.name
                )
                or (
                    isinstance(declaration, ast_nodes.OperatorDecl)
                    and getattr(getattr(value, "metadata", None), "source_name", None)
                    == f"operator{declaration.operator}"
                )
            ),
            None,
        )
        if function is None:
            continue
        name = (
            declaration.name
            if isinstance(declaration, ast_nodes.FunctionDecl)
            else declaration.operator
        )
        kind = "function" if isinstance(declaration, ast_nodes.FunctionDecl) else "operator"
        add(
            tooling_symbols._hover_record(
                name=name,
                kind=kind,
                type_value=function.return_type,
                origin=declaration.origin,
                signature=_callable_signature(function),
            ),
            5,
        )

    for root in _module_expression_roots(ir_module):
        for expression in _walk_expressions(root):
            add(_expression_hover(expression, callables), 0)
    return candidates

def _hover_from_result(
    result: object,
    line: int,
    character: int,
) -> tooling_symbols.ToolingHover | None:
    candidates: list[tuple[tuple[int, int, int], tooling_symbols.ToolingHover]] = []
    ast_module = result.ast
    ir_module = result.ir
    candidates.extend(
        _module_hover_candidates(ast_module, ir_module, line, character)
    )
    for child_ast in getattr(ast_module, "submodules", ()):
        child_ir = next(
            (
                value
                for value in getattr(ir_module, "children", ())
                if getattr(value, "name", None) == child_ast.name
            ),
            None,
        )
        if child_ir is not None:
            candidates.extend(
                _module_hover_candidates(child_ast, child_ir, line, character)
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]

def hover_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    *,
    _session: tooling_session.ToolingSession | None = None,
) -> tooling_symbols.ToolingHover | None:
    """Return semantic hover facts for one zero-based source position.

    This function demands only the semantic check product.  Parse and semantic
    failures return no hover; resolver/environment failures remain explicit as
    ``ToolingError`` rather than being turned into a semantic result.
    """

    if not _valid_position(line, character):
        return None
    result = _optional_query_snapshot(
        lambda: _semantic_snapshot(
            Path(source).expanduser().resolve(),
            source_text,
            analysis_needs=AnalysisNeeds.NONE,
            session=_session,
        )
    )
    if result is None:
        return None
    return _hover_from_result(result, line, character)

def _completion_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    session: tooling_session.ToolingSession | None = None,
) -> tuple[tooling_symbols.ToolingCompletion, ...]:
    """Select the most specific compiler-recorded scope at a position."""

    root_unit = tooling_workspace._root_logical_source(source, session)
    candidates: list[tuple[tuple[int, int, int], CompletionScope]] = []
    for index, scope in enumerate(result.completion_scopes):
        origin = tooling_symbols._origin_from_source(scope.origin)
        if origin is None:
            continue
        if root_unit is not None and scope.origin.source_unit not in {None, root_unit}:
            continue
        if not tooling_symbols._position_in_origin(origin, line, character):
            continue
        line_span = origin.end_line - origin.start_line
        column_span = (
            origin.end_column - origin.start_column
            if line_span == 0
            else 1_000_000
        )
        candidates.append(((line_span, column_span, index), scope))
    if not candidates:
        return ()
    selected = min(candidates, key=lambda item: item[0])[1]
    projected: dict[tuple[str, str, str | None], tooling_symbols.ToolingCompletion] = {}
    for candidate in selected.candidates:
        key = (candidate.name, candidate.kind, candidate.detail)
        projected[key] = tooling_symbols.ToolingCompletion(
            candidate.name, candidate.kind, candidate.detail
        )
    return tuple(
        sorted(
            projected.values(),
            key=lambda item: (item.name, item.kind, item.detail or ""),
        )
    )

def completion_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    *,
    _session: tooling_session.ToolingSession | None = None,
) -> tuple[tooling_symbols.ToolingCompletion, ...]:
    """Return compiler-visible semantic candidates at one source position."""

    if not _valid_position(line, character):
        return ()
    path = Path(source).expanduser().resolve()
    result = _optional_query_snapshot(
        lambda: _semantic_snapshot(
            path,
            source_text,
            analysis_needs=AnalysisNeeds.DEFINITIONS | AnalysisNeeds.COMPLETION,
            session=_session,
        )
    )
    if result is None:
        return ()
    return _completion_from_result(path, result, line, character, _session)

def _position_key(line: int, character: int) -> tuple[int, int]:
    """Convert an editor position to the compiler's one-based coordinates."""

    return line + 1, character + 1

def _signature_active_parameter(
    call: SignatureHelpCall,
    line: int,
    character: int,
) -> int:
    """Select an argument using compiler-owned argument spans only."""

    if not call.parameters:
        return 0
    position = _position_key(line, character)
    known_arguments = [
        tooling_symbols._origin_from_source(origin)
        for origin in call.argument_origins
    ]
    for index, origin in enumerate(known_arguments):
        if origin is None:
            continue
        start = (origin.start_line, origin.start_column)
        if tooling_symbols._position_in_origin(origin, line, character) or position < start:
            return index
    # A cursor after the final argument and before the closing delimiter is
    # conservatively associated with the final parameter.  This uses the
    # authoritative call/argument spans, never source punctuation parsing.
    return len(call.parameters) - 1

def _signature_help_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    session: tooling_session.ToolingSession | None = None,
) -> tooling_symbols.ToolingSignatureHelp | None:
    """Project the most-specific compiler-resolved call at a position."""

    root_unit = tooling_workspace._root_logical_source(source, session)
    candidates: list[tuple[tuple[int, int, int, int, int], SignatureHelpCall]] = []
    for index, call in enumerate(result.signature_help_calls):
        origin = tooling_symbols._origin_from_source(call.call_origin)
        if origin is None:
            continue
        if root_unit is not None and call.call_origin.source_unit not in {
            None,
            root_unit,
        }:
            continue
        if not tooling_symbols._position_in_origin(origin, line, character):
            continue
        line_span, column_span = _hover_specificity(origin)
        candidates.append(
            (
                (
                    line_span,
                    column_span,
                    origin.start_line,
                    origin.start_column,
                    index,
                ),
                call,
            )
        )
    if not candidates:
        return None
    call = min(candidates, key=lambda item: item[0])[1]
    parameters = tuple(parameter.label for parameter in call.parameters)
    return tooling_symbols.ToolingSignatureHelp(
        call.label,
        parameters,
        _signature_active_parameter(call, line, character),
        tooling_symbols._origin_from_source(call.call_origin),
    )

def signature_help_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    *,
    _session: tooling_session.ToolingSession | None = None,
) -> tooling_symbols.ToolingSignatureHelp | None:
    """Return one compiler-resolved callable signature at an editor position."""

    if not _valid_position(line, character):
        return None
    path = Path(source).expanduser().resolve()
    result = _optional_query_snapshot(
        lambda: _semantic_snapshot(
            path,
            source_text,
            analysis_needs=AnalysisNeeds.SIGNATURE_HELP,
            session=_session,
        )
    )
    if result is None:
        return None
    return _signature_help_from_result(path, result, line, character, _session)

_SEMANTIC_DECLARATION_KIND_MAP = {
    "function": "function", "parameter": "parameter",
    "port": "property", "value": "variable",
}
_SEMANTIC_REFERENCE_KIND_MAP = {
    "function": "function", "parameter": "parameter", "port": "property",
    "symbol": "variable", "value": "variable",
}


def _origin_is_exact_name(origin: tooling_symbols.ToolingOrigin, name: str) -> bool:
    return (
        origin.start_line == origin.end_line
        and origin.end_column - origin.start_column == len(name)
    )


def _exact_semantic_token_origin(
    origin: object,
    name: str,
) -> tooling_symbols.ToolingOrigin | None:
    """Project only parser/compiler spans proven to cover one identifier."""

    projected = tooling_symbols._origin_from_source(origin)
    if projected is None or not _origin_is_exact_name(projected, name):
        return None
    return projected

def _semantic_tokens_from_result(
    source: Path,
    result: object,
    session: tooling_session.ToolingSession | None = None,
) -> tuple[tooling_symbols.ToolingSemanticToken, ...]:
    """Project exact root-document declarations and resolved occurrences."""

    root_unit = tooling_workspace._root_logical_source(source, session)
    root_names = {source.name, source.as_posix(), str(source.resolve())}

    def belongs_to_root(origin: object) -> bool:
        unit = getattr(origin, "source_unit", None)
        if unit is None:
            return True
        if root_unit is not None:
            return unit == root_unit
        return unit in root_names

    records: dict[
        tuple[object, ...], tooling_symbols.ToolingSemanticToken
    ] = {}

    def add(
        origin: object,
        name: str,
        kind: str | None,
        modifiers: tuple[str, ...],
    ) -> None:
        if kind is None or not belongs_to_root(origin):
            return
        projected = _exact_semantic_token_origin(origin, name)
        if projected is None:
            return
        token = tooling_symbols.ToolingSemanticToken(projected, kind, modifiers)
        key = (
            projected.source_unit,
            projected.start_line,
            projected.start_column,
            projected.end_line,
            projected.end_column,
            token.kind,
            token.modifiers,
        )
        records[key] = token

    for declaration in result.definition_declarations:
        add(
            declaration.target,
            declaration.name,
            _SEMANTIC_DECLARATION_KIND_MAP.get(declaration.kind),
            ("declaration",),
        )
    for resolution in result.definition_resolutions:
        add(
            resolution.occurrence,
            resolution.name,
            _SEMANTIC_REFERENCE_KIND_MAP.get(resolution.kind),
            (),
        )

    ordered = sorted(
        records.values(),
        key=lambda item: (
            item.origin.start_line,
            item.origin.start_column,
            item.origin.end_column - item.origin.start_column,
            0 if "declaration" in item.modifiers else 1,
            item.kind,
            item.modifiers,
        ),
    )
    # Exact identifier spans should never overlap.  If malformed observational
    # metadata does overlap, fail closed for the later record instead of
    # publishing two conflicting token classes for one source range.
    result_tokens: list[tooling_symbols.ToolingSemanticToken] = []
    for token in ordered:
        if result_tokens:
            previous = result_tokens[-1]
            if (
                previous.origin.start_line == token.origin.start_line
                and token.origin.start_column < previous.origin.end_column
            ):
                continue
        result_tokens.append(token)
    return tuple(result_tokens)

def semantic_tokens(
    source: Path | str,
    source_text: str,
    *,
    _session: tooling_session.ToolingSession | None = None,
    _top: str | None = None,
) -> tuple[tooling_symbols.ToolingSemanticToken, ...]:
    """Return exact compiler-classified occurrences for the root document."""

    path = Path(source).expanduser().resolve()
    def snapshot() -> object:
        result = (
            _session.symbol_snapshot_covering(
                path, source_text, required_module=_top
            )
            if _session is not None and _top is not None else None
        )
        if result is None:
            result = _definition_snapshot(
                path,
                source_text,
                session=_session,
            )
        return result

    result = _optional_query_snapshot(snapshot)
    if result is None:
        return ()
    return _semantic_tokens_from_result(path, result, _session)
