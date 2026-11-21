"""Semantic-analysis diagnostics shared by focused analysis subsystems."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zlang.diagnostics import DiagnosticError

if TYPE_CHECKING:
    from zlang.ast.nodes import NameExpr
    from zlang.source import SourceOrigin


class SemanticError(DiagnosticError):
    """A well-formed source program has invalid hardware semantics."""

    default_code = "ZL-SEMANTIC-001"
    unresolved_input: NameExpr | None = None

    @classmethod
    def unknown_input(cls, reference: NameExpr, primary: SourceOrigin | None) -> SemanticError:
        """Keep the exact occurrence for an enclosing range expression's scope diagnostic."""
        error = cls(f"unknown input '{reference.name}'", primary=primary)
        error.unresolved_input = reference
        return error


__all__ = ["SemanticError"]
