"""Compiler-owned semantic completion scope projections.

These records are an observational side channel from semantic analysis to the
Community tooling API.  They deliberately contain no AST, resolver, or typed
IR objects.  A scope is tied to an authoritative source expression origin;
the tooling layer may therefore answer a position query without rebuilding
ZLang visibility rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from zlang.source import SourceOrigin


@dataclass(frozen=True)
class CompletionCandidate:
    """One compiler-visible declaration suitable for semantic completion."""

    name: str
    kind: str
    detail: str | None = None
    target: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("completion candidate name must not be empty")
        if not self.kind:
            raise ValueError("completion candidate kind must not be empty")


@dataclass(frozen=True)
class CompletionScope:
    """Visible candidates for one authoritative source expression region."""

    origin: SourceOrigin
    candidates: tuple[CompletionCandidate, ...]


__all__ = ["CompletionCandidate", "CompletionScope"]
