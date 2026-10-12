from __future__ import annotations

from zlang.ir import expressions as expr
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.types import UIntType


def _shared_diamond(depth: int) -> expr.Expression:
    type_ = UIntType(depth + 2)
    value: expr.Expression = expr.InputRef("value", type_)
    for _ in range(depth):
        value = expr.Add(value, value, type_)
    return value


def test_dag_index_separates_unique_nodes_from_logical_occurrences() -> None:
    root = _shared_diamond(20)
    graph = ExpressionDagIndex((root,))

    assert graph.unique_node_count == 21
    assert graph.logical_occurrences() == (2 ** 21) - 1
    assert graph.logic_depth(lambda value: isinstance(value, expr.Add)) == (20,)


def test_dag_fold_visits_each_unique_node_once() -> None:
    root = _shared_diamond(32)
    graph = ExpressionDagIndex((root,))
    visited: list[int] = []

    results = graph.fold(
        lambda value, children: (
            visited.append(id(value)),
            1 + sum(item[1] for item in children),
        )
    )

    assert len(visited) == graph.unique_node_count
    assert len(set(visited)) == graph.unique_node_count
    assert results[id(root)][1] == (2 ** 33) - 1
