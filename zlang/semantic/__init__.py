"""Semantic analysis from AST to typed IR."""

from zlang.semantic.analyze import analyze
from zlang.semantic.errors import SemanticError

__all__ = ["SemanticError", "analyze"]
