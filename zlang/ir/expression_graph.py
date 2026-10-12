"""Bounded, object-identity DAG queries for typed expressions.

The typed expression representation is immutable and may share complete
subgraphs.  Consumers must therefore distinguish the number of unique nodes
from the number of logical tree occurrences.  This module builds one
root-scoped index and answers both questions without expanding shared paths.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from zlang.ir import expressions as expr
from zlang.ir.traversal import expression_children


class ExpressionGraphError(ValueError):
    """A typed expression graph violates the immutable-DAG contract."""


@dataclass(frozen=True)
class ExpressionDagNode:
    """One unique expression and its root-scoped graph relationships."""

    expression: expr.Expression
    children: tuple[expr.Expression, ...]
    parents: tuple[expr.Expression, ...]
    fanout: int
    root_uses: int


_Result = TypeVar("_Result")


class ExpressionDagIndex:
    """Index one or more expression roots in dependency-first order.

    Uniqueness is deliberately based on retained Python object identity.  The
    semantic arena is responsible for hash-consing equivalent pure nodes;
    graph queries must not recursively invoke structural dataclass equality or
    invent a second semantic identity implementation.
    """

    def __init__(
        self,
        roots: Iterable[expr.Expression],
        *,
        children: Callable[[expr.Expression], tuple[expr.Expression, ...]] = (
            expression_children
        ),
    ) -> None:
        self.roots = tuple(roots)
        self._children_of = children
        self._expressions: dict[int, expr.Expression] = {}
        self._children: dict[int, tuple[expr.Expression, ...]] = {}
        self._parents: dict[int, list[expr.Expression]] = {}
        self._fanout: dict[int, int] = {}
        self._root_uses: dict[int, int] = {}
        self._dependency_order: list[expr.Expression] = []
        self._state: dict[int, int] = {}

        for root in self.roots:
            root_id = id(root)
            self._root_uses[root_id] = self._root_uses.get(root_id, 0) + 1
            self._visit(root)

        self.nodes = tuple(
            ExpressionDagNode(
                expression=value,
                children=self._children[id(value)],
                parents=tuple(self._parents.get(id(value), ())),
                fanout=self._fanout.get(id(value), 0),
                root_uses=self._root_uses.get(id(value), 0),
            )
            for value in self._dependency_order
        )
        self.unique_node_count = len(self.nodes)

    def _visit(self, value: expr.Expression) -> None:
        identity = id(value)
        previous = self._expressions.get(identity)
        if previous is not None and previous is not value:
            raise ExpressionGraphError(
                "expression object identity was reused while building one DAG"
            )
        status = self._state.get(identity, 0)
        if status == 2:
            return
        if status == 1:
            raise ExpressionGraphError("typed expression graph contains a cycle")

        self._state[identity] = 1
        self._expressions[identity] = value
        children = tuple(self._children_of(value))
        self._children[identity] = children
        for child in children:
            child_id = id(child)
            self._fanout[child_id] = self._fanout.get(child_id, 0) + 1
            parents = self._parents.setdefault(child_id, [])
            if all(parent is not value for parent in parents):
                parents.append(value)
            self._visit(child)
        self._state[identity] = 2
        self._dependency_order.append(value)

    def __iter__(self) -> Iterator[expr.Expression]:
        """Iterate unique nodes in deterministic dependency-first order."""

        return (node.expression for node in self.nodes)

    def preorder(self) -> Iterator[expr.Expression]:
        """Iterate unique nodes in deterministic root-first order."""

        return reversed(self._dependency_order)

    def children(self, value: expr.Expression) -> tuple[expr.Expression, ...]:
        try:
            retained = self._expressions[id(value)]
        except KeyError as error:
            raise ExpressionGraphError("expression is not part of this DAG") from error
        if retained is not value:
            raise ExpressionGraphError("expression is not part of this DAG")
        return self._children[id(value)]

    def fold(
        self,
        reducer: Callable[[expr.Expression, tuple[_Result, ...]], _Result],
    ) -> dict[int, _Result]:
        """Evaluate one pure query exactly once per unique node."""

        results: dict[int, _Result] = {}
        for value in self._dependency_order:
            results[id(value)] = reducer(
                value,
                tuple(results[id(child)] for child in self._children[id(value)]),
            )
        return results

    def root_results(
        self,
        reducer: Callable[[expr.Expression, tuple[_Result, ...]], _Result],
    ) -> tuple[_Result, ...]:
        results = self.fold(reducer)
        return tuple(results[id(root)] for root in self.roots)

    def logical_occurrences(self) -> int:
        """Return logical tree-node occurrences without materializing paths."""

        sizes = self.fold(lambda _value, children: 1 + sum(children))
        return sum(sizes[id(root)] for root in self.roots)

    def logic_depth(
        self,
        contributes: Callable[[expr.Expression], bool],
    ) -> tuple[int, ...]:
        """Return maximum contributing-node depth for every retained root."""

        return self.root_results(
            lambda value, children: (
                (1 if contributes(value) else 0) + max(children, default=0)
            )
        )


__all__ = [
    "ExpressionDagIndex",
    "ExpressionDagNode",
    "ExpressionGraphError",
]
