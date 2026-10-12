"""Small deterministic graph algorithms shared by compiler infrastructure.

The functions return data rather than formatting domain diagnostics.  Callers
retain ownership of error wording and of the ordering supplied by their roots
and successor collections.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Generic, TypeVar


Node = TypeVar("Node", bound=Hashable)


@dataclass(frozen=True)
class DependencyCycle(Generic[Node], ValueError):
    """A stable closed path discovered during dependency traversal."""

    nodes: tuple[Node, ...]

    def __str__(self) -> str:
        return " -> ".join(str(node) for node in self.nodes)


class ReachabilityIndex(Generic[Node]):
    """Precompute reachability for repeated queries over one directed graph.

    This is intentionally a small query object rather than a policy-bearing
    graph model.  Compiler stages retain ownership of what an edge means while
    avoiding repeated edge-set scans and depth-first searches for every pair.
    """

    def __init__(self, edges: Iterable[tuple[Node, Node]]) -> None:
        adjacency_sets: dict[Node, set[Node]] = {}
        nodes: set[Node] = set()
        for source, target in edges:
            nodes.update((source, target))
            adjacency_sets.setdefault(source, set()).add(target)
        self._adjacency = {
            source: tuple(targets)
            for source, targets in adjacency_sets.items()
        }
        self._descendants: dict[Node, frozenset[Node]] = {}

        indegree = dict.fromkeys(nodes, 0)
        for targets in self._adjacency.values():
            for target in targets:
                indegree[target] += 1
        pending = [node for node, count in indegree.items() if count == 0]
        completed = 0
        while pending:
            source = pending.pop()
            completed += 1
            for target in self._adjacency.get(source, ()):
                indegree[target] -= 1
                if indegree[target] == 0:
                    pending.append(target)
        self._has_cycle = completed != len(nodes)

    def reaches(self, source: Node, target: Node) -> bool:
        """Return whether ``target`` is reachable, including a zero-edge path."""

        if source == target:
            return True
        descendants = self._descendants.get(source)
        if descendants is None:
            pending = list(self._adjacency.get(source, ()))
            visited: set[Node] = set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(self._adjacency.get(current, ()))
            descendants = frozenset(visited)
            self._descendants[source] = descendants
        return target in descendants

    @property
    def has_cycle(self) -> bool:
        """Return whether any node is reachable from itself through an edge."""

        return self._has_cycle


def dependency_postorder(
    roots: Iterable[Node],
    successors: Callable[[Node], Iterable[Node]],
) -> tuple[Node, ...]:
    """Return dependency-first nodes reachable from ``roots``.

    Root and successor iteration order is preserved.  Each node appears once;
    a cycle raises :class:`DependencyCycle` containing a closed stable path.
    """

    ordered: list[Node] = []
    complete: set[Node] = set()
    active: list[Node] = []
    active_set: set[Node] = set()

    def visit(node: Node) -> None:
        if node in complete:
            return
        if node in active_set:
            start = active.index(node)
            raise DependencyCycle((*active[start:], node))
        active.append(node)
        active_set.add(node)
        for successor in successors(node):
            visit(successor)
        active.pop()
        active_set.remove(node)
        complete.add(node)
        ordered.append(node)

    for root in roots:
        visit(root)
    return tuple(ordered)


def reachable(
    source: Node,
    target: Node,
    successors: Callable[[Node], Iterable[Node]],
) -> bool:
    """Return whether ``target`` is reachable from ``source``."""

    pending = [source]
    visited: set[Node] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(successors(current))
    return False


__all__ = [
    "DependencyCycle",
    "ReachabilityIndex",
    "dependency_postorder",
    "reachable",
]
