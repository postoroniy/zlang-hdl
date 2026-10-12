# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned diagnostics and semantic-check projection."""


from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import hashlib
from pathlib import Path

from zlang.analysis_needs import AnalysisNeeds
from zlang.ast import nodes as ast_nodes
from zlang.common.serialization import stable_digest
from zlang.compiler import TopSelectionError
from zlang.dependencies import DependencyModelError
from zlang.diagnostics import Diagnostic, DiagnosticError, DiagnosticFix
from zlang.ir.hierarchy import specialization_fingerprint
from zlang.module_resolver import ModuleResolutionError
from zlang.parser import ParseError, parse
from zlang.project import ProjectModelError
from zlang.semantic import SemanticError
from zlang import workspace as workspace
from zlang import tooling_models as tooling_models
from zlang import tooling_session as tooling_session


from zlang import tooling_symbols as tooling_symbols
from zlang import tooling_queries as tooling_queries


@dataclass(frozen=True)
class SemanticCheckRecord:
    status: str
    phase: str
    resolved_top: str | None
    module_identity: str | None
    diagnostics: tuple[tooling_symbols.ToolingDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed"}:
            raise ValueError("tooling semantic status is unsupported")
        if self.phase not in {
            "parse",
            "resolution",
            "semantic",
            "top_selection",
            "complete",
        }:
            raise ValueError("tooling semantic phase is unsupported")
        if self.status == "passed" and (
            self.phase != "complete"
            or self.resolved_top is None
            or self.module_identity is None
            or self.diagnostics
        ):
            raise ValueError("passed tooling semantic record is inconsistent")
        if self.status == "failed" and not self.diagnostics:
            raise ValueError("failed tooling semantic record requires diagnostics")

def source_facts(source_text: str) -> tooling_symbols.SourceFacts:
    """Return deterministic declaration/import facts without making validity claims."""

    try:
        syntax = parse(source_text)
    except ParseError:
        return tooling_symbols.SourceFacts((), (), False)
    return tooling_symbols.SourceFacts(
        tuple(item.path for item in syntax.imports),
        tuple(sorted({syntax.name, *(item.name for item in syntax.submodules)})),
        True,
    )

def unwritten_register_warnings(source_text: str) -> tuple[tooling_symbols.ToolingDiagnostic, ...]:
    """Report only source-declared registers with no possible source update.

    A register without an update is valid ZLang and holds its reset value.
    This narrow editor observation uses the compiler parser's declaration and
    action records; it never changes semantic checking or searches identifier
    text for a matching assignment.  An update in any compile-time branch
    suppresses the warning, so unspecialized source cannot get a false alarm.
    """

    # A negative prefilter avoids reparsing most navigation targets.  Actual
    # classification below is exclusively parser-owned.
    if "reg" not in source_text:
        return ()
    try:
        syntax = parse(source_text)
    except ParseError:
        return ()

    def write_targets(module: ast_nodes.Module) -> set[str]:
        targets: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, ast_nodes.NextAssignment):
                target = value.target
                targets.add(
                    target if isinstance(target, str) else target.register
                )
                return
            if isinstance(value, ast_nodes.Module) or isinstance(
                value, ast_nodes.Expression
            ):
                return
            if isinstance(value, (tuple, list)):
                for item in value:
                    visit(item)
            elif is_dataclass(value) and not isinstance(value, type):
                for item in fields(value):
                    if item.name not in {"origin", "name_origin", "name_origins"}:
                        visit(getattr(value, item.name))

        visit(
            module.ordered_items
            if module.ordered_items
            else (
                *module.next_assignments,
                *module.rules,
                *module.fsms,
                *module.compile_time_ifs,
                *module.generate_blocks,
            )
        )
        return targets

    warnings: list[tooling_symbols.ToolingDiagnostic] = []

    def collect(module: ast_nodes.Module) -> None:
        targets = write_targets(module)
        for declaration in module.registers:
            if declaration.name in targets:
                continue
            origin = tooling_symbols._origin_from_source(
                declaration.name_origin, construct=f"register {declaration.name}"
            )
            if origin is None:
                continue
            warnings.append(tooling_symbols.ToolingDiagnostic(
                "ZL-REGISTER-NEVER-WRITTEN",
                f"register '{declaration.name}' has no next-state or rule "
                "assignment; it will hold its reset value",
                origin,
                (),
                (),
                severity="warning",
            ))
        for child in module.submodules:
            collect(child)

    collect(syntax)
    warnings.sort(key=lambda item: (
        item.primary.start_line if item.primary is not None else 0,
        item.primary.start_column if item.primary is not None else 0,
        item.message,
    ))
    return tuple(warnings)

def _phase(error: BaseException) -> str:
    if isinstance(error, ParseError):
        return "parse"
    if isinstance(error, TopSelectionError):
        return "top_selection"
    diagnostic = getattr(error, "diagnostic", None)
    code = getattr(diagnostic, "code", None)
    if isinstance(code, str) and code.startswith("ZL-IMPORT-"):
        return "resolution"
    if isinstance(
        error,
        (
            ModuleResolutionError,
            workspace.WorkspaceError,
            ProjectModelError,
            DependencyModelError,
        ),
    ):
        return "resolution"
    if isinstance(error, (SemanticError, DiagnosticError)):
        return "semantic"
    return "resolution"

def _diagnostic_position_offset(
    source_text: str,
    line: int,
    column: int,
) -> int | None:
    """Return one exact compiler position offset, rejecting stale ranges."""

    if line < 1 or column < 1:
        return None
    lines = source_text.splitlines(keepends=True)
    if not lines:
        return 0 if (line, column) == (1, 1) else None
    if line == len(lines) + 1 and source_text.endswith(("\n", "\r")):
        return len(source_text) if column == 1 else None
    if line > len(lines):
        return None
    selected = lines[line - 1]
    content = selected.rstrip("\r\n")
    if column > len(content) + 1:
        return None
    return sum(len(item) for item in lines[: line - 1]) + column - 1

def _tooling_diagnostic_fix(
    fix: DiagnosticFix,
    *,
    source_path: Path,
    source_text: str,
) -> tooling_symbols.ToolingDiagnosticFix | None:
    """Project one complete current-source fix, or reject it atomically."""

    digest = hashlib.sha256(source_text.encode()).hexdigest()
    projected: list[tooling_symbols.ToolingDiagnosticEdit] = []
    for edit in fix.edits:
        origin = edit.origin
        if origin.digest != digest:
            return None
        tooling_origin = tooling_symbols._origin_from_source(origin)
        if tooling_origin is None:
            return None
        start = _diagnostic_position_offset(
            source_text,
            tooling_origin.start_line,
            tooling_origin.start_column,
        )
        end = _diagnostic_position_offset(
            source_text,
            tooling_origin.end_line,
            tooling_origin.end_column,
        )
        if start is None or end is None or end < start:
            return None
        projected.append(
            tooling_symbols.ToolingDiagnosticEdit(source_path, tooling_origin, edit.replacement)
        )
    return tooling_symbols.ToolingDiagnosticFix(fix.title, tuple(projected))

def _diagnostic(
    value: Diagnostic,
    *,
    machine_fixes: tuple[DiagnosticFix, ...] = (),
    source_path: Path | None = None,
    source_text: str | None = None,
) -> tooling_symbols.ToolingDiagnostic:
    origin = value.primary
    primary = tooling_symbols._origin_from_source(origin)
    projected_fixes: tuple[tooling_symbols.ToolingDiagnosticFix, ...] = ()
    if source_path is not None and source_text is not None:
        projected_fixes = tuple(
            projected
            for fix in machine_fixes
            if (
                projected := _tooling_diagnostic_fix(
                    fix,
                    source_path=source_path,
                    source_text=source_text,
                )
            )
            is not None
        )
    return tooling_symbols.ToolingDiagnostic(
        value.code,
        value.message,
        primary,
        value.notes,
        value.fixes,
        projected_fixes,
    )

def _module_identity(module: object) -> str:
    if getattr(module, "parameters", None) is not None:
        return specialization_fingerprint(module)
    signature = getattr(module, "module_signature", None)
    identity = getattr(signature, "identity", None)
    if isinstance(identity, str) and identity:
        return identity
    name = getattr(module, "name", None)
    if isinstance(name, str) and name:
        return name
    return "module:" + stable_digest({"type": type(module).__name__})

def check_snapshot(
    source: Path | str,
    source_text: str,
    *,
    source_digest: str | None = None,
    project: Path | str | None = None,
    top: str | None = None,
    _session: tooling_session.ToolingSession | None = None,
) -> SemanticCheckRecord:
    """Run one semantic-only check and return a stable integration record."""

    source_path = Path(source).expanduser().resolve()
    try:
        result = tooling_queries._semantic_snapshot(
            source_path,
            source_text,
            source_digest=source_digest,
            project=project,
            top=top,
            analysis_needs=AnalysisNeeds.NONE,
            session=_session,
        )
    except (
        ParseError,
        TopSelectionError,
        ModuleResolutionError,
        workspace.WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        SemanticError,
        DiagnosticError,
    ) as error:
        diagnostic = getattr(error, "diagnostic", None)
        if not isinstance(diagnostic, Diagnostic):
            raise tooling_models.ToolingError(str(error)) from error
        return SemanticCheckRecord(
            "failed",
            _phase(error),
            None,
            None,
            (
                _diagnostic(
                    diagnostic,
                    machine_fixes=tuple(getattr(error, "machine_fixes", ())),
                    source_path=source_path,
                    source_text=source_text,
                ),
            ),
        )
    module = result.ir
    return SemanticCheckRecord(
        "passed",
        "complete",
        getattr(module, "name", None),
        _module_identity(module),
        (),
    )

def is_unspecialized_generic_diagnostic(
    source_text: str, diagnostic: tooling_symbols.ToolingDiagnostic
) -> bool:
    """Classify editor-only template diagnostics without weakening a build.

    The ordinary compiler check still rejects an unbound template.  Only an
    exact unresolved *declared* value parameter in its parsed constraint is
    treated like the existing generic-type specialization requirement while
    viewing the declaration without a specialization.
    """

    if diagnostic.code == "ZL-GENERIC-SPECIALIZATION-REQUIRED":
        return True
    if diagnostic.code != "ZL-SEMANTIC-PARAMETER-CONSTRAINT":
        return False
    try:
        module = parse(source_text)
    except ParseError:
        return False
    if module.parameter_constraint is None or diagnostic.primary is None:
        return False
    origin = module.parameter_constraint.origin
    if origin is None or diagnostic.primary.start_line != origin.start_line:
        return False
    prefix = (
        f"module '{module.name}' parameter constraint cannot be discharged: "
        "compile-time condition references runtime value "
    )
    return diagnostic.message.startswith(prefix) and any(
        f"runtime value '{parameter.name}'" in diagnostic.message
        for parameter in module.parameters
        if parameter.kind == "value" and parameter.default is None
    )
