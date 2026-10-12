"""Combinational ready/valid dependency checks for temporal child regions.

Only the non-interleaved temporal wrapper contributes an internal
``input.ready <- output.ready`` edge.  Ordinary hierarchy connection edges
are already compiler-owned IR; this focused check prevents that new edge from
closing a cross-instance ready/valid loop without becoming a general Boolean
or protocol solver.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass

from zlang.ast import nodes as ast
from zlang.common import stable_digest
from zlang.costs import CandidateCost, CostExtractionError, extract_best
from zlang import exploration
from zlang.ir import expressions as expr
from zlang.ir.interfaces import InterfaceProtocol, ReadyValidSignal
from zlang.ir import pipelines as ir_pipelines
from zlang.ir.temporal import TemporalImplementationGraph
from zlang.ir.types import HardwareType
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.shared_arithmetic import SharedArithmeticError, SharedArithmeticProvider

from .errors import SemanticError
from .implementation import ImplementationIntentAnalyzer


class TemporalReadyValidDependencyError(ValueError):
    """A temporal admission edge closes a combinational handshake loop."""


_Node = tuple[str, str, ReadyValidSignal]


@dataclass(frozen=True)
class TemporalReadyValidSelection:
    """Selected bounded shared-arithmetic implementation for one RV transform."""

    exploration: ir_pipelines.PipelineExploration
    temporal_graph: TemporalImplementationGraph | None
    semantic_identity: str


def select_temporal_shared_arithmetic(
    syntax: ast.ImplementExpr,
    operand: expr.Expression,
    *,
    source_name: str,
    destination_name: str,
    destination_type: HardwareType,
    input_type: HardwareType,
    transform_constraints: tuple[ast.PipelineConstraint, ...],
    pipeline_constraints: tuple[ir_pipelines.PipelineConstraint, ...],
    allocate_instance,
    domain: str,
) -> TemporalReadyValidSelection:
    """Select only the bounded ``a*b+c*d`` temporal/spatial RV alternatives."""

    intent = ImplementationIntentAnalyzer()
    if intent._contains_nested_site(syntax.expression):
        raise SemanticError("nested implementation selection is not supported")
    intent._validate_objective(syntax.objective)
    objective = intent._objective_metric(syntax.objective)
    policy_constraints = exploration.constraints_from_syntax((
        *transform_constraints,
        *syntax.constraints,
    ))
    semantic_identity = "elastic:" + stable_digest({
        "source": source_name,
        "destination": destination_name,
        "kernel": expression_semantic_identity(operand),
        "constraints": tuple(item.render() for item in pipeline_constraints),
    })
    provider = SharedArithmeticProvider()
    try:
        shared = provider.candidate(
            operand,
            semantic_region_identity=semantic_identity,
            constraints=pipeline_constraints,
            input_type=input_type,
        )
        spatial = provider.spatial_candidate(
            operand,
            allocate_instance=allocate_instance,
            domain=domain,
            constraints=pipeline_constraints,
        )
        candidates = (spatial, shared.pipeline_candidate)

        def temporal_cost(candidate: ir_pipelines.PipelineCandidate) -> CandidateCost:
            return CandidateCost.estimate(
                lut=candidate.estimate.lut,
                ff=candidate.estimate.ff,
                dsp=candidate.estimate.dsp,
                latency=candidate.latency,
                ii=candidate.initiation_interval,
                fmax_est=candidate.estimate.fmax_mhz,
                structural_cost=3,
            )

        selected = extract_best(
            candidates,
            objective,
            policy_constraints,
            cost_fn=temporal_cost,
        ).selected
    except (SharedArithmeticError, CostExtractionError) as error:
        raise SemanticError(str(error)) from error

    selected_temporal = selected is shared.pipeline_candidate
    selected_candidates = (
        (shared.pipeline_candidate,) if selected_temporal else (spatial,)
    )
    return TemporalReadyValidSelection(
        ir_pipelines.PipelineExploration(
            f"{destination_name}.payload",
            destination_type,
            operand,
            pipeline_constraints,
            selected_candidates,
            selected.name,
            len(candidates),
        ),
        shared.temporal_graph if selected_temporal else None,
        semantic_identity,
    )


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
