"""Backend-independent timing DAG construction for selected target graphs."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Iterable

from zlang.ir import expressions as expr
from zlang.ir.target import (
    ImplementationDelay,
    ImplementationGraph,
    PipelineConfiguration,
    ResourceDefinition,
    TimingCut,
    TimingDAG,
    TimingEdge,
    TimingNode,
)
from zlang.ir.signed_reductions import SignedProductReduction, expression_semantic_identity
from zlang.timing import align_operands


def _identity(*items: object) -> str:
    return sha256(repr(items).encode()).hexdigest()


def alignment_delays(
    operands: Iterable[expr.Expression],
    node_identities: Iterable[str],
    *,
    width: int,
    destination_node: str,
    semantic_identity: str | None = None,
    source_origin: str | None = None,
) -> tuple[ImplementationDelay, ...]:
    """Turn M30's minimum-latency alignment plan into explicit graph objects."""
    values = tuple(operands)
    nodes = tuple(node_identities)
    if len(values) != len(nodes):
        raise ValueError("alignment operand/node counts differ")
    plan = align_operands(values)
    result = []
    for index, (node, cycles) in enumerate(zip(nodes, plan.adjustments, strict=True)):
        if cycles == 0:
            continue
        result.append(ImplementationDelay(
            identity=_identity("alignment", node, destination_node, cycles, width, index),
            kind="alignment",
            source_node=node,
            destination_node=destination_node,
            cycles=cycles,
            width=width,
            ff_cost=cycles * width,
            semantic_identity=semantic_identity,
            source_origin=source_origin,
        ))
    return tuple(result)


def add_compensation(
    dag: TimingDAG,
    *,
    required_latency: int | None,
    width: int,
    semantic_identity: str,
    source_origin: str | None,
) -> TimingDAG:
    """Reach an exact output contract without changing useful internal cuts."""
    if required_latency is None:
        return dag
    if required_latency < dag.output_latency:
        raise ValueError(
            f"implementation latency {dag.output_latency} exceeds exact latency {required_latency}"
        )
    cycles = required_latency - dag.output_latency
    if cycles == 0:
        return dag
    output = next((item for item in dag.nodes if item.kind == "output_boundary"), None)
    if output is None:
        raise ValueError("timing DAG has no legal output compensation boundary")
    delay = ImplementationDelay(
        identity=_identity("compensation", output.identity, cycles, width),
        kind="compensation",
        source_node=output.identity,
        destination_node=output.identity,
        cycles=cycles,
        width=width,
        ff_cost=cycles * width,
        semantic_identity=semantic_identity,
        source_origin=source_origin,
    )
    return replace(
        dag,
        compensation_delays=(*dag.compensation_delays, delay),
        output_latency=required_latency,
    )


def build_dsp_cascade_timing_dag(
    graph: ImplementationGraph,
    resource: ResourceDefinition,
    configuration: PipelineConfiguration,
    *,
    structural_latency: int,
    output_width: int,
    exact_latency: int | None = None,
) -> TimingDAG:
    """Build the bounded FIR timing graph from semantic/resource data.

    The function knows resource instances, generic sites and edge kinds.  It
    never inspects a vendor primitive or emitted RTL register name.
    """
    origins = graph.source_origin.render() if hasattr(graph.source_origin, "render") else (
        str(graph.source_origin) if graph.source_origin is not None else None
    )
    nodes: list[TimingNode] = []
    input_id = _identity(graph.semantic_region_identity, "input_boundary")
    nodes.append(TimingNode(
        input_id, "input_boundary", "input", graph.semantic_region_identity,
        source_origin=origins, target_identity=graph.target_identity,
    ))
    resource_node_ids: dict[str, str] = {}
    for instance in graph.resources:
        node_id = _identity(graph.semantic_region_identity, "resource", instance.identity)
        resource_node_ids[instance.identity] = node_id
        instance_configuration = dict(instance.configuration)
        accumulator_mode = instance_configuration.get("accumulator_mode")
        kind = {
            "accumulator_plus_product": "signed_product_add_segment",
            "accumulator_minus_product": "signed_product_subtract_segment",
            "product_minus_accumulator": "signed_product_subtract_segment",
        }.get(accumulator_mode, "resource_segment")
        semantic_mapping = (
            next(
                (item.semantic_identity for item in instance.semantic_mappings
                 if item.resource_port == "p"),
                graph.semantic_region_identity,
            )
            if accumulator_mode is not None else graph.semantic_region_identity
        )
        nodes.append(TimingNode(
            node_id, kind, instance.identity,
            semantic_mapping, instance.identity,
            latency=0, estimated_delay_ps=0, source_origin=origins,
            target_identity=graph.target_identity,
        ))
    quant_id = _identity(graph.semantic_region_identity, "fixed_quantization")
    output_id = _identity(graph.semantic_region_identity, "output_boundary")
    nodes.extend((
        TimingNode(
            quant_id, "fixed_quantization", "quantization",
            graph.semantic_region_identity, latency=0, estimated_delay_ps=0,
            source_origin=origins, target_identity=graph.target_identity,
        ),
        TimingNode(
            output_id, "output_boundary", "output",
            graph.semantic_region_identity, latency=structural_latency,
            source_origin=origins, target_identity=graph.target_identity,
        ),
    ))
    edges: list[TimingEdge] = []
    if graph.resources:
        edges.append(TimingEdge(
            _identity(input_id, resource_node_ids[graph.resources[0].identity], "fabric"),
            "fabric", input_id, resource_node_ids[graph.resources[0].identity],
        ))
    for physical in graph.dedicated_edges:
        edges.append(TimingEdge(
            _identity("dedicated", physical.identity), "dedicated",
            resource_node_ids[physical.source_instance],
            resource_node_ids[physical.destination_instance],
            physical.latency, 0, physical.identity,
        ))
    if graph.resources:
        edges.extend((
            TimingEdge(
                _identity(resource_node_ids[graph.resources[-1].identity], quant_id, "fabric"),
                "fabric", resource_node_ids[graph.resources[-1].identity], quant_id,
            ),
            TimingEdge(
                _identity(quant_id, output_id, "fabric"),
                "fabric", quant_id, output_id,
            ),
        ))
    site_by_name = {item.name: item for item in resource.pipeline_sites}
    cuts: list[TimingCut] = []
    for site_name in configuration.sites:
        site = site_by_name[site_name]
        selected_instances = (
            (graph.resources[-1],)
            if site.semantic_location == "accumulate_output"
            else graph.resources
        )
        for instance in selected_instances:
            cuts.append(TimingCut(
                _identity("resource_cut", instance.identity, site_name),
                resource_node_ids[instance.identity], "resource_local",
                site.latency_delta, instance.identity,
                f"{resource.identity}.{site.name}",
            ))
    dag = TimingDAG(
        tuple(nodes), tuple(edges), tuple(cuts), output_latency=(
            structural_latency + configuration.latency
        ),
    )
    return add_compensation(
        dag, required_latency=exact_latency, width=output_width,
        semantic_identity=graph.semantic_region_identity,
        source_origin=origins,
    )


def build_signed_product_timing_dag(
    reduction: SignedProductReduction,
    *,
    quantization: expr.Expression,
    output_latency: int,
    target_identity: str | None,
) -> TimingDAG:
    """Describe the unchanged product/join/quantization path for a generic plan."""

    origin = (
        reduction.source_origin.render()
        if reduction.source_origin is not None else None
    )
    input_id = _identity(reduction.semantic_identity, "input_boundary")
    nodes: list[TimingNode] = [TimingNode(
        input_id, "input_boundary", "input", reduction.semantic_identity,
        source_origin=origin, target_identity=target_identity,
    )]
    product_nodes: dict[int, str] = {}
    for term in reduction.terms:
        node_id = _identity(reduction.semantic_identity, "product", term.ordinal)
        product_nodes[term.ordinal] = node_id
        nodes.append(TimingNode(
            node_id, "product_segment", f"product{term.ordinal}",
            term.semantic_identity, source_origin=(
                term.source_origin.render() if term.source_origin else None
            ), target_identity=target_identity,
        ))
    join_nodes: dict[int, str] = {}
    for join in reduction.joins:
        node_id = _identity(reduction.semantic_identity, "join", join.ordinal)
        join_nodes[join.ordinal] = node_id
        nodes.append(TimingNode(
            node_id,
            "subtract_join" if join.operator.value == "subtract" else "add_join",
            f"join{join.ordinal}", join.semantic_identity,
            source_origin=(join.source_origin.render() if join.source_origin else None),
            target_identity=target_identity,
        ))
    boundary_kind = (
        "fixed_quantization"
        if isinstance(quantization, expr.FixedConvert)
        else "exact_value_projection"
    )
    quant_id = _identity(reduction.semantic_identity, boundary_kind)
    output_id = _identity(reduction.semantic_identity, "output_boundary")
    nodes.extend((
        TimingNode(
            quant_id, boundary_kind, "quantization",
            expression_semantic_identity(quantization),
            source_origin=(quantization.origin.render() if quantization.origin else None),
            target_identity=target_identity,
        ),
        TimingNode(
            output_id, "output_boundary", "output", reduction.semantic_identity,
            latency=output_latency, source_origin=origin,
            target_identity=target_identity,
        ),
    ))
    edges: list[TimingEdge] = []
    for term in reduction.terms:
        product_node = product_nodes[term.ordinal]
        edges.append(TimingEdge(
            _identity(input_id, product_node, "semantic_input"),
            "semantic", input_id, product_node,
        ))
    for join in reduction.joins:
        destination = join_nodes[join.ordinal]
        left = product_nodes[0] if join.ordinal == 0 else join_nodes[join.ordinal - 1]
        right = product_nodes[join.ordinal + 1]
        edges.extend((
            TimingEdge(_identity(left, destination, "left"), "semantic", left, destination),
            TimingEdge(_identity(right, destination, "right"), "semantic", right, destination),
        ))
    root = join_nodes[len(reduction.joins) - 1]
    edges.extend((
        TimingEdge(_identity(root, quant_id, "quantization"), "semantic", root, quant_id),
        TimingEdge(_identity(quant_id, output_id, "output"), "semantic", quant_id, output_id),
    ))
    cuts = () if output_latency == 0 else (TimingCut(
        _identity("generic_output_cut", output_id, output_latency),
        output_id, "generic", output_latency,
    ),)
    return TimingDAG(tuple(nodes), tuple(edges), cuts, output_latency=output_latency)


def delay_ff_cost(dag: TimingDAG | None) -> int:
    if dag is None:
        return 0
    return sum(item.ff_cost for item in (
        *dag.alignment_delays, *dag.compensation_delays,
    ))


__all__ = [
    "add_compensation", "alignment_delays", "build_dsp_cascade_timing_dag",
    "build_signed_product_timing_dag", "delay_ff_cost",
]
