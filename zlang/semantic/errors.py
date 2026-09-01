"""Semantic-analysis diagnostics shared by focused analysis subsystems."""

from __future__ import annotations

from zlang.diagnostics import DiagnosticError


class SemanticError(DiagnosticError):
    """A well-formed source program has invalid hardware semantics."""

    default_code = "ZL-SEMANTIC-001"


__all__ = ["SemanticError"]
