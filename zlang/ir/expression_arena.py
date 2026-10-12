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
from zlang.ir.types import HardwareType
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
_EXPRESSION_TYPE_SET = frozenset(_EXPRESSION_TYPES)
_HARDWARE_TYPE_SET = frozenset(get_args(HardwareType))


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
        self._field_cache: dict[type, tuple[object, ...]] = {}
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

        updates = self._rewritten_children(expression)
        self._requests += 1
        if isinstance(expression, _BARRIER_TYPES):
            selected = replace(expression, **updates) if updates else expression
        else:
            key = self._key(expression, updates)
            selected = self._pool.get(key)
            if selected is None:
                selected = replace(expression, **updates) if updates else expression
                self._pool[key] = selected
            else:
                self._hits += 1

        if id(selected) not in self._node_ids:
            self._node_ids[id(selected)] = self._next_node_id
            self._nodes_by_id[id(selected)] = selected
            self._next_node_id += 1
        self._memo[id(expression)] = (expression, selected)
        self._memo[id(selected)] = (selected, selected)
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

    def _semantic_fields(self, type_: type) -> tuple[object, ...]:
        selected = self._field_cache.get(type_)
        if selected is None:
            selected = tuple(
                descriptor for descriptor in fields(type_)
                if descriptor.init
                and descriptor.name not in {"origin", "source_origin"}
            )
            self._field_cache[type_] = selected
        return selected

    def _rewritten_children(
        self,
        expression: expr.Expression,
    ) -> dict[str, object]:
        updates: dict[str, object] = {}
        for descriptor in self._semantic_fields(type(expression)):
            current = getattr(expression, descriptor.name)
            rewritten = self._rewrite_value(current)
            if rewritten is not current:
                updates[descriptor.name] = rewritten
        return updates

    def _rewrite_value(self, value: object) -> object:
        if type(value) in _EXPRESSION_TYPE_SET:
            return self.intern(value)
        if value is None or type(value) in {bool, int, str, bytes, float}:
            return value
        # Hardware type records contain no typed expression children.
        if type(value) in _HARDWARE_TYPE_SET:
            return value
        if isinstance(value, tuple):
            rewritten: list[object] = []
            changed = False
            for item in value:
                selected = self._rewrite_value(item)
                rewritten.append(selected)
                changed |= selected is not item
            return tuple(rewritten) if changed else value
        if is_dataclass(value) and not isinstance(value, type):
            updates: dict[str, object] = {}
            for descriptor in self._semantic_fields(type(value)):
                current = getattr(value, descriptor.name)
                rewritten = self._rewrite_value(current)
                if rewritten is not current:
                    updates[descriptor.name] = rewritten
            return replace(value, **updates) if updates else value
        return value

    def _key(
        self, expression: expr.Expression, updates: Mapping[str, object]
    ) -> object:
        return (
            type(expression),
            tuple(
                self._key_value(
                    updates.get(descriptor.name, getattr(expression, descriptor.name))
                )
                for descriptor in self._semantic_fields(type(expression))
            ),
        )

    def _key_value(self, value: object) -> object:
        if type(value) in _EXPRESSION_TYPE_SET:
            node_id = self._node_ids.get(id(value))
            if node_id is None:
                value = self.intern(value)
                node_id = self._node_ids[id(value)]
            return ("expression", node_id)
        if value is None or type(value) in {bool, int, str, bytes}:
            return value
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
        # Hardware types are frozen, origin-free value records.  Their own
        # equality/hash contract is exactly the field-wise semantic key.
        if type(value) in _HARDWARE_TYPE_SET:
            return value
        if is_dataclass(value) and not isinstance(value, type):
            return (
                type(value),
                tuple(
                    self._key_value(getattr(value, descriptor.name))
                    for descriptor in self._semantic_fields(type(value))
                ),
            )
        if isinstance(value, (bool, int, str, bytes)):
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
