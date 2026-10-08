# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned definition, references, and rename queries."""


from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
from pathlib import Path
from typing import Iterator

from zlang.analysis_needs import AnalysisNeeds
from zlang.ast import nodes as ast_nodes
from zlang.compiler import TopSelectionError
from zlang.dependencies import DependencyModelError
from zlang.diagnostics import DiagnosticError
from zlang.module_resolver import ModuleResolutionError
from zlang.parser import ParseError, is_valid_identifier, parse
from zlang.project import ProjectModelError
from zlang.semantic import SemanticError
from zlang import workspace as workspace
from zlang import tooling_models as tooling_models
from zlang import tooling_session as tooling_session
from zlang import tooling_workspace as tooling_workspace


from zlang import tooling_symbols as tooling_symbols
from zlang import tooling_queries as tooling_queries

_REFERENCE_MAX_PROJECT_ROOTS = 128
_REFERENCE_MAX_PROJECT_CANDIDATES = 128

_RENAMEABLE_KINDS = frozenset({"port", "value", "function", "parameter"})

def definition_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    *,
    _session: tooling_session.ToolingSession | None = None,
) -> tooling_symbols.ToolingDefinition | None:
    """Resolve one source position through the compiler's name resolver."""

    if not tooling_queries._valid_position(line, character):
        return None
    path = Path(source).expanduser().resolve()
    columns = tooling_queries._definition_lookup_columns(source_text, line, character)
    for result in _navigation_results_at(path, source_text, line, character, _session):
        for column in columns:
            definition = tooling_queries._definition_from_result(path, result, line, column, _session)
            if definition is not None:
                return definition
    return None

def _navigation_results_at(
    path: Path,
    source_text: str,
    line: int,
    character: int,
    session: tooling_session.ToolingSession | None,
) -> Iterator[object]:
    """Yield exact compiler symbol contexts for both navigation methods.

    The parser selects a possible top only; no parser result can become a
    Location.  A warm selected top is tried first, then the default, then the
    enclosing earlier top if needed.  The iterator stops when its caller has
    a compiler-owned occurrence, so a warm hit avoids unrelated analysis.
    """

    seen: set[str | None] = set()

    def snapshot(top: str | None) -> object | None:
        try:
            return tooling_queries._definition_snapshot(path, source_text, session=session, top=top)
        except (ParseError, TopSelectionError, SemanticError, DiagnosticError):
            return None
        except (
            ModuleResolutionError, workspace.WorkspaceError, ProjectModelError,
            DependencyModelError, OSError, ValueError,
        ) as error:
            raise tooling_models.ToolingError(str(error)) from error

    cached_top = (
        _definition_top_at(
            source_text, line, character, source=path, session=session,
            cached_only=True,
        )
        if session is not None else None
    )
    for top in (cached_top, None):
        if top in seen:
            continue
        seen.add(top)
        result = snapshot(top)
        if result is not None:
            yield result
    try:
        selected_top = _definition_top_at(
            source_text, line, character, source=path, session=session
        )
    except ParseError:
        return
    if selected_top is not None and selected_top not in seen:
        result = snapshot(selected_top)
        if result is not None:
            yield result

def _definition_top_at(
    source_text: str,
    line: int,
    character: int,
    *,
    source: Path | None = None,
    session: tooling_session.ToolingSession | None = None,
    cached_only: bool = False,
) -> str | None:
    """Select the enclosing parsed module, not the last module in a file.

    A module name span begins each source module.  Its next name span ends
    the current module's navigation region.  Declaration-only units retain
    the ordinary compiler top selection.  This is only a compilation selector;
    compiler definition resolutions remain the sole navigation authority.
    """

    key = (
        (source, hashlib.sha256(source_text.encode("utf-8")).hexdigest())
        if source is not None and session is not None else None
    )
    regions = session._navigation_regions.get(key) if key is not None else None
    if regions is None:
        if cached_only:
            return None
        syntax = parse(source_text)
        modules = (*syntax.submodules, syntax)
        regions = tuple(
            ((module.name_origin.start_line, module.name_origin.start_column),
             module.name)
            for module in modules
            if module.name_origin is not None
        )
        if key is not None:
            session._navigation_regions[key] = regions
            session._navigation_regions.move_to_end(key)
            while len(session._navigation_regions) > 64:
                session._navigation_regions.popitem(last=False)
    elif key is not None:
        session._navigation_regions.move_to_end(key)
    if len(regions) < 2:
        return None
    position = (line + 1, character + 1)
    preceding = tuple(
        (start, name) for start, name in regions if start <= position
    )
    if not preceding:
        return None
    selected = max(preceding, key=lambda item: item[0])[1]
    return selected if selected != regions[-1][1] else None

def _reference_target_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    session: tooling_session.ToolingSession | None = None,
) -> object | None:
    """Resolve the target identity under a cursor using compiler records."""

    root_unit = tooling_workspace._root_logical_source(source, session)
    occurrence_index = getattr(result, "occurrences_by_line", None)
    resolutions = (
        tooling_queries._indexed_symbol_line(occurrence_index, root_unit, line + 1)
        if occurrence_index is not None
        else result.definition_resolutions
    )
    resolution = tooling_symbols._narrowest_source_record(
        resolutions,
        source_unit=root_unit,
        line=line,
        character=character,
        source_origin=lambda item: item.occurrence,
    )
    if resolution is not None:
        return resolution.target

    # A declaration may have no usages.  Declaration targets are still
    # compiler-owned and allow includeDeclaration queries to resolve without
    # inventing a name match in the LSP.
    declaration_index = getattr(result, "declarations_by_line", None)
    declarations = (
        tooling_queries._indexed_symbol_line(declaration_index, root_unit, line + 1)
        if declaration_index is not None
        else result.definition_declarations
    )
    declaration = tooling_symbols._narrowest_source_record(
        declarations,
        source_unit=root_unit,
        line=line,
        character=character,
        source_origin=lambda item: item.target,
        include_column_span=False,
    )
    return None if declaration is None else declaration.target

def _references_for_target(
    source: Path,
    result: object,
    target: object,
    include_declaration: bool,
    session: tooling_session.ToolingSession | None = None,
) -> tuple[tooling_symbols.ToolingReference, ...]:
    """Project occurrences matching one compiler-resolved declaration."""

    target_key = tooling_models._declaration_coordinate_key(target)
    records: list[tooling_symbols.ToolingReference] = []
    seen: set[tuple[object, ...]] = set()

    def add(origin: object) -> None:
        path = tooling_workspace._source_path_for_origin(
            source, origin, result.physical_inputs, session
        )
        projected = tooling_symbols._origin_from_source(origin)
        if path is None or projected is None:
            raise tooling_models.ToolingError(
                "compiler resolved a reference whose locked source path is unavailable"
            )
        key = (
            path.resolve().as_posix(),
            projected.start_line,
            projected.start_column,
            projected.end_line,
            projected.end_column,
        )
        if key in seen:
            return
        seen.add(key)
        records.append(tooling_symbols.ToolingReference(path.resolve(), projected))

    if include_declaration:
        add(target)
    occurrence_index = getattr(result, "occurrences_by_declaration", None)
    resolutions = (
        occurrence_index.get(target_key, ())
        if occurrence_index is not None
        else result.definition_resolutions
    )
    for resolution in resolutions:
        if tooling_models._declaration_coordinate_key(resolution.target) != target_key:
            continue
        add(resolution.occurrence)
    records.sort(
        key=lambda item: (
            item.source_path.as_posix(),
            item.origin.start_line,
            item.origin.start_column,
            item.origin.end_line,
            item.origin.end_column,
        )
    )
    return tuple(records)

def _project_reference_compilations(
    source: Path,
    source_text: str,
    target: object,
    reference_name: str,
) -> tuple[tuple[Path, str | None, str], ...]:
    """Return bounded root/top/text snapshots whose closure can use target."""

    target_unit = getattr(target, "source_unit", None)
    if not isinstance(target_unit, str):
        return ()
    location = tooling_workspace.discover_project(source)
    if location is None:
        return ()
    # A reference sweep must see the current manifest, lock and import graph,
    # not a long-lived index retained for ordinary F12 path projection.
    index = tooling_workspace.workspace_index(location.manifest_path)
    root_names = {item.logical_path for item in index.root_modules}
    if target_unit not in root_names and target_unit not in {
        item.logical_path for item in index.dependency_modules
    }:
        return ()
    closures = {
        item.logical_path: frozenset(index.dependency_closure(item.logical_path))
        for item in index.root_modules
    }
    eligible = tuple(
        item
        for item in index.root_modules
        if item.logical_path == target_unit
        or target_unit in closures[item.logical_path]
    )
    if len(eligible) > _REFERENCE_MAX_PROJECT_ROOTS:
        raise tooling_models.ToolingError("project reference root candidate limit exceeded")
    result: list[tuple[Path, str | None, str]] = []
    for item in eligible:
        text = (
            source_text if item.source_path.resolve() == source.resolve()
            else item.source_path.read_text(encoding="utf-8")
        )
        try:
            syntax = parse(text)
        except ParseError:
            # Keep malformed roots eligible so the semantic pass below remains
            # the single authority for accepting or rejecting their records.
            result.append((item.source_path, None, text))
            continue
        matching_submodules = tuple(
            module.name
            for module in syntax.submodules
            if _syntax_mentions_reference_name(module, reference_name)
        )
        if _syntax_mentions_reference_name(syntax, reference_name) or matching_submodules:
            result.append((item.source_path, None, text))
        result.extend(
            (item.source_path, name, text) for name in matching_submodules
        )
        if len(result) > _REFERENCE_MAX_PROJECT_CANDIDATES:
            raise tooling_models.ToolingError("project reference top candidate limit exceeded")
    return tuple(result)

def _syntax_mentions_reference_name(value: object, name: str) -> bool:
    """Parser prefilter only; this predicate never publishes a reference."""

    if isinstance(value, ast_nodes.TypeName):
        names = tuple(component for component, _ in value.named_origins)
        return (
            value.text == name
            or value.text.endswith("." + name)
            or any(
                component == name or component.endswith("." + name)
                for component in names
            )
        )
    if isinstance(value, str):
        return value == name or value.endswith("." + name)
    if isinstance(value, tuple):
        return any(_syntax_mentions_reference_name(item, name) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return any(
            _syntax_mentions_reference_name(getattr(value, item.name), name)
            for item in fields(value)
            if item.name not in {
                "origin", "name_origin", "named_origins", "source_hash",
                "source_identity", "submodules",
            }
        )
    return False

def _same_file_reference_tops(source_text: str, name: str) -> tuple[str, ...]:
    """Select sibling modules that may contain an exact semantic use."""

    syntax = parse(source_text)
    matches = tuple(
        module.name for module in syntax.submodules
        if _syntax_mentions_reference_name(module, name)
    )
    if len(matches) > 64:
        raise tooling_models.ToolingError("same-file reference candidate limit exceeded")
    return matches

def _target_matches_disk_snapshot(
    source: Path,
    target: object,
    result: object,
    session: tooling_session.ToolingSession | None,
) -> bool:
    """Reject project-wide projection from an unsaved declaration snapshot."""

    digest = getattr(target, "digest", None)
    if digest is None:
        return False
    path = tooling_workspace._source_path_for_origin(
        source, target, result.physical_inputs, session
    )
    if path is None:
        return False
    try:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return current == digest

def references_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    include_declaration: bool = False,
    *,
    _session: tooling_session.ToolingSession | None = None,
) -> tuple[tooling_symbols.ToolingReference, ...]:
    """Return compiler-resolved references for one source position."""

    if not tooling_queries._valid_position(line, character) or not isinstance(
        include_declaration, bool
    ):
        return ()
    path = Path(source).expanduser().resolve()
    columns = tooling_queries._definition_lookup_columns(source_text, line, character)
    target = None
    result = None
    for candidate in _navigation_results_at(
        path, source_text, line, character, _session
    ):
        for column in columns:
            target = _reference_target_from_result(
                path, candidate, line, column, _session
            )
            if target is not None:
                result = candidate
                break
        if target is not None:
            break
    if target is None or result is None:
        return ()

    metadata = _rename_target_metadata(result, target)
    reference_name = metadata[0] if metadata is not None else None

    records = list(_references_for_target(
        path,
        result,
        target,
        include_declaration,
        _session,
    ))
    if reference_name is not None:
        try:
            same_file_tops = _same_file_reference_tops(
                source_text, reference_name
            )
        except ParseError:
            same_file_tops = ()
        for top in same_file_tops:
            try:
                sibling = tooling_queries._definition_snapshot(
                    path, source_text, session=_session, top=top
                )
            except (
                ParseError, TopSelectionError, SemanticError, DiagnosticError,
                ModuleResolutionError, workspace.WorkspaceError, ProjectModelError,
                DependencyModelError, OSError, ValueError,
            ):
                # An invalid sibling cannot supply trustworthy occurrences.
                continue
            records.extend(_references_for_target(
                path, sibling, target, False, _session
            ))
    if (
        reference_name is not None
        and _target_matches_disk_snapshot(path, target, result, _session)
    ):
        for root, root_top, root_text in _project_reference_compilations(
            path, source_text, target, reference_name
        ):
            try:
                project_result = tooling_queries._definition_snapshot(
                    root,
                    root_text,
                    session=_session,
                    top=root_top,
                )
            except (
                ParseError,
                TopSelectionError,
                SemanticError,
                DiagnosticError,
                ModuleResolutionError,
                workspace.WorkspaceError,
                ProjectModelError,
                DependencyModelError,
                OSError,
                ValueError,
            ):
                # A broken independent root cannot provide trustworthy
                # semantic references, but it must not invalidate records
                # already resolved from other roots.
                continue
            if root.resolve() != path:
                try:
                    current_digest = hashlib.sha256(root.read_bytes()).hexdigest()
                except OSError as error:
                    raise tooling_models.ToolingError(
                        "project reference source changed during lookup"
                    ) from error
                if current_digest != hashlib.sha256(root_text.encode("utf-8")).hexdigest():
                    raise tooling_models.ToolingError("project reference source changed during lookup")
            records.extend(_references_for_target(
                root,
                project_result,
                target,
                False,
                _session,
            ))

    source_cache: dict[Path, tuple[str, ...]] = {
        path: tuple(source_text.splitlines())
    }

    def exact_spelling(item: tooling_symbols.ToolingReference) -> bool:
        if reference_name is None:
            return True
        resolved = item.source_path.resolve()
        lines = source_cache.get(resolved)
        if lines is None:
            editor_text = (
                None
                if _session is None
                else _session.editor_text_for(resolved)
            )
            if editor_text is not None:
                lines = tuple(editor_text.splitlines())
            else:
                try:
                    lines = tuple(
                        resolved.read_text(encoding="utf-8").splitlines()
                    )
                except OSError:
                    return False
            source_cache[resolved] = lines
        origin = item.origin
        if (
            origin.start_line != origin.end_line
            or not 1 <= origin.start_line <= len(lines)
        ):
            return False
        line_text = lines[origin.start_line - 1]
        return (
            line_text[origin.start_column - 1 : origin.end_column - 1]
            == reference_name
        )

    unique = {
        (
            item.source_path.as_posix(),
            item.origin.start_line,
            item.origin.start_column,
            item.origin.end_line,
            item.origin.end_column,
        ): item
        for item in records
        if exact_spelling(item)
    }
    return tuple(unique[key] for key in sorted(unique))

def _origin_location_key(origin: object) -> tuple[object, ...]:
    span = getattr(origin, "span", origin)
    return (
        getattr(origin, "source_unit", None),
        int(span.start_line),
        int(span.start_column),
    )

def _source_offset(source_text: str, line: int, column: int) -> int:
    lines = source_text.splitlines(keepends=True)
    if line < 1 or line > len(lines) + 1 or column < 1:
        raise tooling_models.ToolingRenameError("rename source origin is outside the document")
    return sum(len(item) for item in lines[: line - 1]) + column - 1

def _offset_to_position(source_text: str, offset: int) -> tuple[int, int]:
    if offset < 0 or offset > len(source_text):
        raise tooling_models.ToolingRenameError("rename source edit is outside the document")
    before = source_text[:offset]
    line = before.count("\n") + 1
    last_newline = before.rfind("\n")
    column = offset - last_newline
    return line, column

def _apply_rename_text(
    source_text: str,
    edits: tuple[tooling_symbols.ToolingRenameEdit, ...],
) -> str:
    positioned: list[tuple[int, int, str]] = []
    for edit in edits:
        start = _source_offset(
            source_text, edit.origin.start_line, edit.origin.start_column
        )
        end = _source_offset(
            source_text, edit.origin.end_line, edit.origin.end_column
        )
        if end < start:
            raise tooling_models.ToolingRenameError("rename source edit has an invalid range")
        positioned.append((start, end, edit.new_text))
    result = source_text
    for start, end, replacement in sorted(positioned, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result

def _shifted_position(
    source_text: str,
    origin: object,
    edits: tuple[tooling_symbols.ToolingRenameEdit, ...],
    updated_text: str | None = None,
) -> tuple[int, int]:
    span = getattr(origin, "span", origin)
    offset = _source_offset(source_text, int(span.start_line), int(span.start_column))
    delta = 0
    for edit in edits:
        edit_start = _source_offset(
            source_text, edit.origin.start_line, edit.origin.start_column
        )
        edit_end = _source_offset(
            source_text, edit.origin.end_line, edit.origin.end_column
        )
        if edit_end <= offset:
            delta += len(edit.new_text) - (edit_end - edit_start)
    return _offset_to_position(
        source_text if updated_text is None else updated_text,
        offset + delta,
    )

def _rename_target_metadata(
    result: object,
    target: object,
) -> tuple[str, str] | None:
    target_key = tooling_models._origin_key(target)
    for declaration in result.definition_declarations:
        if tooling_models._origin_key(declaration.target) == target_key:
            return declaration.name, declaration.kind
    for resolution in result.definition_resolutions:
        if tooling_models._origin_key(resolution.target) == target_key:
            return resolution.name, resolution.kind
    return None

def _rename_edits_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    new_name: str,
    session: tooling_session.ToolingSession | None = None,
) -> tuple[tooling_symbols.ToolingRenameEdit, ...] | None:
    target = _reference_target_from_result(
        source, result, line, character, session
    )
    if target is None:
        return None
    metadata = _rename_target_metadata(result, target)
    if metadata is None:
        return None
    old_name, kind = metadata
    if kind not in _RENAMEABLE_KINDS:
        return None

    target_path = tooling_workspace._source_path_for_origin(
        source, target, result.physical_inputs, session
    )
    if target_path is None or target_path.resolve() != source.resolve():
        raise tooling_models.ToolingRenameError(
            "cross-file rename is not supported without an authoritative editable snapshot"
        )
    target_origin = tooling_symbols._origin_from_source(target)
    if target_origin is None or not tooling_queries._origin_is_exact_name(target_origin, old_name):
        return None

    origins: list[object] = [target]
    target_key = tooling_models._origin_key(target)
    for resolution in result.definition_resolutions:
        if tooling_models._origin_key(resolution.target) == target_key:
            origins.append(resolution.occurrence)

    edits: list[tooling_symbols.ToolingRenameEdit] = []
    seen: set[tuple[int, int, int, int]] = set()
    for origin in origins:
        projected = tooling_symbols._origin_from_source(origin)
        path = tooling_workspace._source_path_for_origin(
            source, origin, result.physical_inputs, session
        )
        if projected is None or path is None:
            raise tooling_models.ToolingRenameError(
                "symbol cannot be renamed safely because an exact source span is unavailable"
            )
        if path.resolve() != source.resolve() or not tooling_queries._origin_is_exact_name(projected, old_name):
            raise tooling_models.ToolingRenameError(
                "symbol cannot be renamed safely because an exact editable identifier span is unavailable"
            )
        key = (
            projected.start_line,
            projected.start_column,
            projected.end_line,
            projected.end_column,
        )
        if key in seen:
            continue
        seen.add(key)
        edits.append(tooling_symbols.ToolingRenameEdit(source.resolve(), projected, new_name))
    edits.sort(
        key=lambda item: (
            item.source_path.as_posix(),
            item.origin.start_line,
            item.origin.start_column,
        )
    )
    return tuple(edits)

def _validate_renamed_semantics(
    source: Path,
    source_text: str,
    original_target: object,
    edits: tuple[tooling_symbols.ToolingRenameEdit, ...],
    new_name: str,
    session: tooling_session.ToolingSession | None = None,
) -> None:
    """Recheck the edited root and preserve target resolution at each use."""

    candidate_text = _apply_rename_text(source_text, edits)
    try:
        candidate = tooling_queries._semantic_snapshot(
            source,
            candidate_text,
            analysis_needs=AnalysisNeeds.DEFINITIONS,
            session=session,
        )
    except (
        ParseError,
        TopSelectionError,
        SemanticError,
        DiagnosticError,
    ) as error:
        raise tooling_models.ToolingRenameError(
            "new name would change or invalidate ZLang name resolution"
        ) from error
    except (
        ModuleResolutionError,
        workspace.WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        OSError,
        ValueError,
    ) as error:
        raise tooling_models.ToolingError(str(error)) from error

    shifted_target_line, shifted_target_column = _shifted_position(
        source_text, original_target, edits, candidate_text
    )
    target = _reference_target_from_result(
        source,
        candidate,
        shifted_target_line - 1,
        shifted_target_column - 1,
        session,
    )
    if target is None:
        raise tooling_models.ToolingRenameError("new name would change the resolved rename target")
    original_location = _origin_location_key(original_target)
    candidate_location = _origin_location_key(target)
    if original_location != candidate_location:
        raise tooling_models.ToolingRenameError("new name would change the resolved rename target")

    expected_starts = {
        _shifted_position(source_text, edit.origin, edits, candidate_text)
        for edit in edits
    }
    matched = {
        (
            resolution.occurrence.span.start_line,
            resolution.occurrence.span.start_column,
        )
        for resolution in candidate.definition_resolutions
        if resolution.name == new_name
        and _origin_location_key(resolution.target) == candidate_location
    }
    expected_starts.discard((shifted_target_line, shifted_target_column))
    if not expected_starts.issubset(matched):
        raise tooling_models.ToolingRenameError("new name would change the resolved rename target")

def rename_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    new_name: str,
    *,
    _session: tooling_session.ToolingSession | None = None,
) -> tuple[tooling_symbols.ToolingRenameEdit, ...] | None:
    """Return exact semantic rename edits, or no result when unsupported."""

    if not tooling_queries._valid_position(line, character):
        return None
    if not isinstance(new_name, str) or not is_valid_identifier(new_name):
        raise tooling_models.ToolingRenameError("new name is not a valid ZLang identifier")
    path = Path(source).expanduser().resolve()
    result = tooling_queries._optional_query_snapshot(
        lambda: tooling_queries._semantic_snapshot(
            path,
            source_text,
            analysis_needs=AnalysisNeeds.DEFINITIONS,
            session=_session,
        )
    )
    if result is None:
        return None
    edits = _rename_edits_from_result(
        path, result, line, character, new_name, _session
    )
    if edits is None:
        return None
    target = _reference_target_from_result(
        source, result, line, character, _session
    )
    if target is None:
        return None
    _validate_renamed_semantics(
        path,
        source_text,
        target,
        edits,
        new_name,
        _session,
    )
    return edits
