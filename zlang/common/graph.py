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


__all__ = ["DependencyCycle", "dependency_postorder", "reachable"]
