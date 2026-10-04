"""Stable, read-only integration surface for compiler-aware tooling.

This module deliberately exposes immutable records rather than parser,
workspace, or semantic implementation objects.  External tooling may depend on
``TOOLING_API_SCHEMA`` and these functions without importing compiler internals.
"""

from __future__ import annotations

from zlang.tooling_models import (
    TOOLING_API_SCHEMA,
    TOOLING_COMPLETION_SCHEMA,
    TOOLING_DEFINITION_SCHEMA,
    TOOLING_DIAGNOSTIC_EDIT_SCHEMA,
    TOOLING_DOCUMENT_SYMBOL_SCHEMA,
    TOOLING_HOVER_SCHEMA,
    TOOLING_REFERENCE_SCHEMA,
    TOOLING_RENAME_SCHEMA,
    TOOLING_SEMANTIC_TOKEN_SCHEMA,
    TOOLING_SIGNATURE_HELP_SCHEMA,
    EditorDocumentSnapshot,
    EditorWorkspaceSnapshot,
    ToolingError,
    ToolingRenameError,
    ProjectLocation,
    ResolvedImport,
    ToolingIdentity,
    WorkspaceIndex,
    WorkspaceModule,
    tooling_identity,
)
from zlang.tooling_session import ToolingSession
from zlang.tooling_symbol_cache import SYMBOL_CACHE_SCHEMA
from zlang.tooling_workspace import (
    discover_project,
    resolve_direct_imports,
    workspace_index,
)

from zlang.tooling_symbols import (
    SourceFacts,
    ToolingCompletion,
    ToolingDefinition,
    ToolingDiagnostic,
    ToolingDiagnosticEdit,
    ToolingDiagnosticFix,
    ToolingHover,
    ToolingOrigin,
    ToolingReference,
    ToolingRenameEdit,
    ToolingSemanticToken,
    ToolingSignatureHelp,
    ToolingSymbol,
    document_symbols,
)

from zlang.tooling_queries import (
    completion_at,
    hover_at,
    semantic_tokens,
    signature_help_at,
)
from zlang.tooling_navigation import (
    definition_at,
    references_at,
    rename_at,
)
from zlang.tooling_diagnostics import (
    SemanticCheckRecord,
    check_snapshot,
    is_unspecialized_generic_diagnostic,
    source_facts,
    unwritten_register_warnings,
)

__all__ = [
    "SYMBOL_CACHE_SCHEMA",
    "TOOLING_API_SCHEMA",
    "TOOLING_DOCUMENT_SYMBOL_SCHEMA",
    "TOOLING_HOVER_SCHEMA",
    "TOOLING_DEFINITION_SCHEMA",
    "TOOLING_REFERENCE_SCHEMA",
    "TOOLING_RENAME_SCHEMA",
    "TOOLING_COMPLETION_SCHEMA",
    "TOOLING_SIGNATURE_HELP_SCHEMA",
    "TOOLING_SEMANTIC_TOKEN_SCHEMA",
    "TOOLING_DIAGNOSTIC_EDIT_SCHEMA",
    "EditorDocumentSnapshot",
    "EditorWorkspaceSnapshot",
    "ProjectLocation",
    "ResolvedImport",
    "SemanticCheckRecord",
    "SourceFacts",
    "ToolingDiagnostic",
    "ToolingDiagnosticEdit",
    "ToolingDiagnosticFix",
    "ToolingError",
    "ToolingSession",
    "ToolingHover",
    "ToolingDefinition",
    "ToolingReference",
    "ToolingRenameEdit",
    "ToolingRenameError",
    "ToolingCompletion",
    "ToolingSignatureHelp",
    "ToolingSemanticToken",
    "ToolingIdentity",
    "ToolingOrigin",
    "ToolingSymbol",
    "WorkspaceIndex",
    "WorkspaceModule",
    "check_snapshot",
    "is_unspecialized_generic_diagnostic",
    "document_symbols",
    "discover_project",
    "resolve_direct_imports",
    "source_facts",
    "unwritten_register_warnings",
    "tooling_identity",
    "hover_at",
    "definition_at",
    "references_at",
    "rename_at",
    "completion_at",
    "signature_help_at",
    "semantic_tokens",
    "workspace_index",
]
