# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Stateless Lark rule mixins and the one owned parser source context."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re

from zlang.ast.nodes import CostMetric
from zlang.source import SourceSpan


def _cost_metric(value: str) -> CostMetric:
    return CostMetric.FMAX_EST if value == "fmax" else CostMetric(value)


def _tagged(item: object, tag: str) -> bool:
    return isinstance(item, tuple) and len(item) >= 2 and item[0] == tag


def _tagged_position(item: object) -> bool:
    return isinstance(item, tuple) and len(item) == 3 and item[0] == "position"


@dataclass(frozen=True)
class _ParsedHierarchicalEndpoint:
    """One endpoint spelling plus exact name-token spans for editor tooling."""

    text: str
    name_origins: tuple[SourceSpan | None, ...]


@dataclass(frozen=True)
class _ParsedRuleTarget:
    """One scalar rule target plus its exact lexer-owned name span."""

    name: str
    name_origin: SourceSpan | None


_TYPE_NAME_COMPONENT = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*"
)


@dataclass(frozen=True)
class BuilderSourceContext:
    """Immutable source text and line index owned by one AST builder."""

    source: str
    line_starts: tuple[int, ...]

    @classmethod
    def create(cls, source: str) -> BuilderSourceContext:
        return cls(
            source,
            (0, *(match.end() for match in re.finditer(r"\n", source))),
        )

    def offset(self, line: int, column: int) -> int:
        return self.line_starts[line - 1] + column - 1

    def position(self, offset: int) -> tuple[int, int]:
        line_index = bisect_right(self.line_starts, offset) - 1
        return line_index + 1, offset - self.line_starts[line_index] + 1

    def type_name_components(
        self,
        origin: SourceSpan,
    ) -> tuple[tuple[str, SourceSpan], ...]:
        start = self.offset(origin.start_line, origin.start_column)
        end = self.offset(origin.end_line, origin.end_column)
        result: list[tuple[str, SourceSpan]] = []
        for match in _TYPE_NAME_COMPONENT.finditer(self.source[start:end]):
            absolute_start = start + match.start()
            absolute_end = start + match.end()
            start_line, start_column = self.position(absolute_start)
            end_line, end_column = self.position(absolute_end)
            result.append((
                match.group(0),
                SourceSpan(start_line, start_column, end_line, end_column),
            ))
        return tuple(result)


class SourceRuleMixin:
    """Source-coordinate helpers for parser callbacks that need exact spans."""

    _source_context: BuilderSourceContext

    def _initialize_source_context(self, source: str) -> None:
        self._source_context = BuilderSourceContext.create(source)

    def _source_offset(self, line: int, column: int) -> int:
        return self._source_context.offset(line, column)

    def _type_name_components(
        self,
        origin: SourceSpan,
    ) -> tuple[tuple[str, SourceSpan], ...]:
        return self._source_context.type_name_components(origin)

__all__ = [
    "BuilderSourceContext",
    "SourceRuleMixin",
]
