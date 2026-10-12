"""Compiler-owned source-definition resolution records.

The records in this module are a deliberately small semantic side channel for
editor tooling.  They carry source origins only; no AST, resolver, or typed-IR
objects escape to tooling clients.
"""

from __future__ import annotations

from dataclasses import dataclass

from zlang.source import SourceOrigin


@dataclass(frozen=True)
class DefinitionResolution:
    """One compiler-resolved occurrence and its declaration target.

    The same occurrence-to-target relation is reused by the references tooling
    projection; the LSP never interprets the target identity itself.
    """

    occurrence: SourceOrigin
    target: SourceOrigin
    name: str
    kind: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("definition resolution name must not be empty")
        if not self.kind:
            raise ValueError("definition resolution kind must not be empty")


@dataclass(frozen=True)
class DefinitionTarget:
    """One compiler-owned declaration target available for editor queries."""

    target: SourceOrigin
    name: str
    kind: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("definition target name must not be empty")
        if not self.kind:
            raise ValueError("definition target kind must not be empty")


__all__ = ["DefinitionResolution", "DefinitionTarget"]
