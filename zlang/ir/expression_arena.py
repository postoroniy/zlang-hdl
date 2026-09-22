"""Compilation-local hash-consing for exact typed expression DAGs.

The arena changes host representation only.  Language budgets still count
logical source work, and source provenance is retained separately from the
origin-insensitive semantic node key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import math
from typing import get_args

from zlang.ir import expressions as expr
from zlang.source import SourceOrigin


SEMANTIC_EXPRESSION_ARENA_SCHEMA = "zlang-semantic-expression-arena-v2"


class ExpressionArenaKeyError(TypeError):
    """A semantic node contains metadata without an explicit key policy."""


@dataclass(frozen=True)
class ExpressionArenaStatistics:
    requests: int
    hits: int
    unique_nodes: int
    provenance_occurrences: int


class ExpressionProvenanceTable:
    """Compact source-occurrence side table for canonical lowering."""

    def __init__(
        self,
        entries: tuple[tuple[expr.Expression, tuple[SourceOrigin, ...]], ...],
    ) -> None:
        self._entries = entries
        self._by_id = {id(expression): origins for expression, origins in entries}

    def origins(self, expression: expr.Expression) -> tuple[SourceOrigin, ...]:
        return self._by_id.get(id(expression), ())


_EXPRESSION_TYPES = tuple(get_args(expr.Expression))
_BARRIER_TYPES = (
    # Call occurrences own source-site multiplicity used by bounded inlining
    # and callable DCE.  Arguments are still interned recursively.
    expr.Call,
    expr.Delay,
    expr.Pipeline,
    expr.ImplementationChoice,
)


class SemanticExpressionArena:
    """Intern one compilation's immutable pure expression nodes."""

    def __init__(self) -> None:
        self._pool: dict[object, expr.Expression] = {}
        self._node_ids: dict[int, int] = {}
        self._nodes_by_id: dict[int, expr.Expression] = {}
        # Retain the original object beside its result.  A bare ``id`` cache
        # is unsound because short-lived dataclass nodes may be collected and
        # Python can then reuse their addresses during the same compilation.
        self._memo: dict[int, tuple[expr.Expression, expr.Expression]] = {}
        self._origins: dict[int, list[SourceOrigin]] = {}
        self._requests = 0
        self._hits = 0
        self._next_node_id = 0

    @property
    def statistics(self) -> ExpressionArenaStatistics:
        return ExpressionArenaStatistics(
            requests=self._requests,
            hits=self._hits,
            unique_nodes=len(self._node_ids),
            provenance_occurrences=sum(len(items) for items in self._origins.values()),
        )

    def origins(self, expression: expr.Expression) -> tuple[SourceOrigin, ...]:
        return tuple(self._origins.get(id(expression), ()))

    @property
    def provenance_table(self) -> ExpressionProvenanceTable:
        return ExpressionProvenanceTable(tuple(
            (self._nodes_by_id[node_id], tuple(self._origins.get(node_id, ())))
            for node_id, _ordinal in sorted(
                self._node_ids.items(), key=lambda item: item[1]
            )
            if self._origins.get(node_id)
        ))

    def intern(self, expression: expr.Expression) -> expr.Expression:
        """Return the unique exact typed node for ``expression``."""

        cached = self._memo.get(id(expression))
        if cached is not None and cached[0] is expression:
            self._record_origin(cached[1], expression.origin)
            return cached[1]

        rebuilt = self._rewrite_expression_children(expression)
        self._requests += 1
        if isinstance(rebuilt, _BARRIER_TYPES):
            selected = rebuilt
        else:
            key = self._key(rebuilt)
            selected = self._pool.get(key)
            if selected is None:
                selected = rebuilt
                self._pool[key] = selected
            else:
                self._hits += 1

        if id(selected) not in self._node_ids:
            self._node_ids[id(selected)] = self._next_node_id
            self._nodes_by_id[id(selected)] = selected
            self._next_node_id += 1
        self._memo[id(expression)] = (expression, selected)
        self._memo[id(rebuilt)] = (rebuilt, selected)
        self._record_origin(selected, expression.origin)
        return selected

    def _record_origin(
        self,
        expression: expr.Expression,
        origin: SourceOrigin | None,
    ) -> None:
        if origin is None:
            return
        retained = self._origins.setdefault(id(expression), [])
        if origin not in retained:
            retained.append(origin)

    def _rewrite_expression_children(
        self,
        expression: expr.Expression,
    ) -> expr.Expression:
        updates: dict[str, object] = {}
        for descriptor in fields(expression):
            if not descriptor.init or descriptor.name in {"origin", "source_origin"}:
                continue
            current = getattr(expression, descriptor.name)
            rewritten = self._rewrite_value(current)
            if rewritten is not current:
                updates[descriptor.name] = rewritten
        return replace(expression, **updates) if updates else expression

    def _rewrite_value(self, value: object) -> object:
        if isinstance(value, _EXPRESSION_TYPES):
            return self.intern(value)
        if isinstance(value, tuple):
            rewritten = tuple(self._rewrite_value(item) for item in value)
            return value if all(a is b for a, b in zip(value, rewritten, strict=True)) else rewritten
        if is_dataclass(value) and not isinstance(value, type):
            updates: dict[str, object] = {}
            for descriptor in fields(value):
                if not descriptor.init or descriptor.name in {"origin", "source_origin"}:
                    continue
                current = getattr(value, descriptor.name)
                rewritten = self._rewrite_value(current)
                if rewritten is not current:
                    updates[descriptor.name] = rewritten
            return replace(value, **updates) if updates else value
        return value

    def _key(self, expression: expr.Expression) -> object:
        return (
            type(expression).__module__,
            type(expression).__qualname__,
            tuple(
                (descriptor.name, self._key_value(getattr(expression, descriptor.name)))
                for descriptor in fields(expression)
                if descriptor.init
                and descriptor.name not in {"origin", "source_origin"}
            ),
        )

    def _key_value(self, value: object) -> object:
        if isinstance(value, _EXPRESSION_TYPES):
            node_id = self._node_ids.get(id(value))
            if node_id is None:
                value = self.intern(value)
                node_id = self._node_ids[id(value)]
            return ("expression", node_id)
        if isinstance(value, tuple):
            return tuple(self._key_value(item) for item in value)
        if isinstance(value, list):
            return ("list", tuple(self._key_value(item) for item in value))
        if isinstance(value, Mapping):
            entries = tuple(
                (self._key_value(key), self._key_value(item))
                for key, item in value.items()
            )
            encoded = tuple(sorted(entries, key=str))
            return ("mapping", encoded)
        if isinstance(value, (set, frozenset)):
            return (
                "set",
                tuple(sorted((self._key_value(item) for item in value), key=str)),
            )
        if isinstance(value, Enum):
            return (type(value).__module__, type(value).__qualname__, value.value)
        if is_dataclass(value) and not isinstance(value, type):
            return (
                type(value).__module__,
                type(value).__qualname__,
                tuple(
                    (descriptor.name, self._key_value(getattr(value, descriptor.name)))
                    for descriptor in fields(value)
                    if descriptor.init
                    and descriptor.name not in {"origin", "source_origin"}
                ),
            )
        if value is None or isinstance(value, (bool, int, str, bytes)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ExpressionArenaKeyError(
                    "semantic expression metadata contains a non-finite float"
                )
            return value
        raise ExpressionArenaKeyError(
            "semantic expression metadata has no explicit key policy for "
            f"{type(value).__module__}.{type(value).__qualname__}"
        )


__all__ = [
    "ExpressionArenaStatistics",
    "ExpressionArenaKeyError",
    "ExpressionProvenanceTable",
    "SEMANTIC_EXPRESSION_ARENA_SCHEMA",
    "SemanticExpressionArena",
]
