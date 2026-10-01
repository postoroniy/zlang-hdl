# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Shared fail-closed graph validation for typed module connectivity."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TypeVar

from .errors import SemanticError

_Node = TypeVar("_Node")


def reject_dependency_cycles(
    graph: Mapping[_Node, Iterable[_Node]],
    *,
    render_node: Callable[[_Node], str],
    description: Callable[[tuple[_Node, ...]], str] | str,
    stable_sort: bool = True,
) -> None:
    """Reject a deterministic dependency cycle without changing graph meaning."""

    visited: set[_Node] = set()
    active: list[_Node] = []

    def visit(node: _Node) -> None:
        if node in active:
            start = active.index(node)
            cycle_nodes = (*active[start:], node)
            label = (
                description(cycle_nodes)
                if callable(description)
                else description
            )
            cycle = " -> ".join(render_node(item) for item in cycle_nodes)
            raise SemanticError(f"combinational {label} dependency cycle: {cycle}")
        if node in visited or node not in graph:
            return
        active.append(node)
        dependencies = graph[node]
        for dependency in sorted(dependencies) if stable_sort else dependencies:
            visit(dependency)
        active.pop()
        visited.add(node)

    nodes = sorted(graph) if stable_sort else graph
    for node in nodes:
        visit(node)
