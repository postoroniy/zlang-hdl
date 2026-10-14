"""Shared rendering for compiler-owned command-line diagnostics."""

from __future__ import annotations

from pathlib import Path
import sys

from zlang import compiler as compiler_api
from zlang import diagnostics as diagnostic_api
from zlang import source_identity as source_api
from zlang import workspace as workspace_api


def fallback_diagnostic(error: BaseException) -> diagnostic_api.Diagnostic:
    """Attach stable codes to public exceptions not migrated at their source."""

    if isinstance(error, source_api.SourceExtensionError):
        return diagnostic_api.Diagnostic(
            "ZL-SOURCE-EXTENSION",
            str(error),
            fixes=(f"rename the source file to use '{source_api.SOURCE_SUFFIX}'",),
        )
    if isinstance(error, compiler_api.TopSelectionError):
        code = "ZL-TOP-001"
    elif isinstance(error, workspace_api.WorkspaceError):
        code = "ZL-PROJECT-001"
    elif isinstance(error, OSError):
        code = "ZL-IO-001"
    else:
        code = "ZL-ERROR-001"
    return diagnostic_api.diagnostic_from_exception(error, code=code)


def diagnostic_location(
    diagnostic: diagnostic_api.Diagnostic,
    *,
    source: Path | None,
    project: Path | None,
    physical_source: Path | None = None,
) -> str | None:
    """Resolve a logical compiler origin to a physical CLI source location."""

    origin = diagnostic.primary
    if origin is None:
        return None
    source_path = physical_source or source
    source_unit = origin.source_unit
    if (
        physical_source is None
        and source_path is not None
        and source_unit not in {None, source_path.name}
    ):
        resolved_source: Path | None = None
        try:
            workspace = workspace_api.load_project_workspace(source_path, project=project)
        except (OSError, ValueError):
            workspace = None
        if workspace is not None:
            resolved_source = workspace.source_path_for_unit(source_unit)
        source_path = resolved_source
    rendered_source = (
        str(source_path)
        if source_path is not None
        else (source_unit or "<unknown-source>")
    )
    return f"{rendered_source}:{origin.span.start_line}:{origin.span.start_column}"


def print_cli_diagnostic(
    error: BaseException,
    diagnostic_format: str = "text",
    *,
    legacy_message: str | None = None,
    source: Path | None = None,
    project: Path | None = None,
    program: str = source_api.CLI_NAME,
) -> None:
    """Write one diagnostic with a compiler-owned code and source location."""

    diagnostic = fallback_diagnostic(error)
    if legacy_message is not None:
        diagnostic = diagnostic_api.Diagnostic(
            diagnostic.code,
            legacy_message,
            diagnostic.primary,
            diagnostic.notes,
            diagnostic.fixes,
        )
    if diagnostic_format == "json":
        print(diagnostic.to_json(), file=sys.stderr)
        return

    location = diagnostic_location(
        diagnostic,
        source=source,
        project=project,
        physical_source=getattr(error, "physical_source", None),
    )
    prefix = f"{program}: "
    if location is not None:
        prefix += f"{location}: "
    print(
        f"{prefix}error[{diagnostic.code}]: {diagnostic.message}",
        file=sys.stderr,
    )


__all__ = ["diagnostic_location", "fallback_diagnostic", "print_cli_diagnostic"]
