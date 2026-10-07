"""Combinational ready/valid dependency checks for temporal child regions.

Only the non-interleaved temporal wrapper contributes an internal
``input.ready <- output.ready`` edge.  Ordinary hierarchy connection edges
are already compiler-owned IR; this focused check prevents that new edge from
closing a cross-instance ready/valid loop without becoming a general Boolean
or protocol solver.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass

from zlang.ir import expressions as expr
from zlang.ir.interfaces import InterfaceProtocol, ReadyValidSignal


class TemporalReadyValidDependencyError(ValueError):
    """A temporal admission edge closes a combinational handshake loop."""


_Node = tuple[str, str, ReadyValidSignal]


def _ready_references(value: object) -> tuple[str, ...]:
    """Return direct compiler-IR ready reads without resolving source text."""

    found: set[str] = set()

    def visit(current: object) -> None:
        if isinstance(current, expr.ReadyValidRef):
            if current.signal is ReadyValidSignal.READY:
                found.add(current.interface)
            return
        if isinstance(current, tuple):
            for item in current:
                visit(item)
            return
        if not is_dataclass(current):
            return
        for description in fields(current):
            if description.name in {"origin", "type"} or not description.init:
                continue
            visit(getattr(current, description.name))

    visit(value)
    return tuple(sorted(found))


def reject_temporal_ready_valid_dependency_cycles(module: object) -> None:
    """Fail closed when temporal child admission participates in a cycle."""

    graph: dict[_Node, set[_Node]] = {}
    temporal_nodes: set[_Node] = set()

    def node(owner: str, name: str, signal: ReadyValidSignal) -> _Node:
        value = (owner, name, signal)
        graph.setdefault(value, set())
        return value

    for connection in getattr(module, "hierarchical_connections", ()):
        source = connection.source
        destination = connection.destination
        if (
            source.protocol is not InterfaceProtocol.READY_VALID
            or destination.protocol is not InterfaceProtocol.READY_VALID
        ):
            continue
        # Forward payload/valid and reverse ready are the exact typed
        # connection semantics.  Edges point from a driven signal to the
        # current-cycle signal it reads.
        for signal in (ReadyValidSignal.PAYLOAD, ReadyValidSignal.VALID):
            target = node(destination.owner, destination.name, signal)
            graph[target].add(node(source.owner, source.name, signal))
        target = node(source.owner, source.name, ReadyValidSignal.READY)
        graph[target].add(
            node(destination.owner, destination.name, ReadyValidSignal.READY)
        )

    for child, elaborated in zip(
        getattr(module, "children", ()),
        getattr(module, "elaborated_instances", ()),
        strict=True,
    ):
        path = getattr(elaborated, "semantic_path", ())
        if not path:
            continue
        owner = path[-1]
        for assignment in getattr(child, "assignments", ()):
            if getattr(assignment, "signal", None) is not ReadyValidSignal.READY:
                continue
            target = node(owner, assignment.target.name, ReadyValidSignal.READY)
            for interface in _ready_references(assignment.expression):
                graph[target].add(node(owner, interface, ReadyValidSignal.READY))
        for region in getattr(child, "elastic_pipeline_regions", ()):
            if getattr(region, "temporal_graph", None) is None:
                continue
            admission = node(
                owner, region.source_endpoint, ReadyValidSignal.READY,
            )
            dependency = node(
                owner, region.destination_endpoint, ReadyValidSignal.READY,
            )
            graph[admission].add(dependency)
            temporal_nodes.add(admission)

    active: list[_Node] = []
    visited: set[_Node] = set()

    def visit(current: _Node) -> None:
        if current in active:
            start = active.index(current)
            cycle = (*active[start:], current)
            if any(item in temporal_nodes for item in cycle):
                rendered = " -> ".join(
                    f"{owner}.{name}.{signal.value}"
                    for owner, name, signal in cycle
                )
                raise TemporalReadyValidDependencyError(
                    "combinational ready/valid dependency cycle through "
                    f"temporal admission: {rendered}"
                )
            return
        if current in visited:
            return
        active.append(current)
        for dependency in sorted(graph[current], key=lambda item: (
            item[0], item[1], item[2].value,
        )):
            visit(dependency)
        active.pop()
        visited.add(current)

    for item in sorted(graph, key=lambda value: (
        value[0], value[1], value[2].value,
    )):
        visit(item)


__all__ = [
    "TemporalReadyValidDependencyError",
    "reject_temporal_ready_valid_dependency_cycles",
]
