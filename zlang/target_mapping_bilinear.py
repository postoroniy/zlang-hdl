# SPDX-License-Identifier: Apache-2.0
"""Physical covering of an already-selected bounded bilinear value DAG."""

from __future__ import annotations

from dataclasses import replace

from zlang.bilinear_product import bilinear_products
from zlang.ir import expressions as e
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.scheduled import ScheduledValueGraph, ScheduledValueResourceBinding
from zlang.target_catalog import TargetArchitectureError, validate_inventory
from zlang.target_mapping_dsp import map_auto_multiply_configuration


def _joint_schedule(module, value, resources, assignment):
    operations, dependencies, frontiers = [], [], {}
    graph = ExpressionDagIndex((value,))
    known = {}
    for node in graph:
        inputs = tuple(
            dict.fromkeys(
                parent
                for child in graph.children(node)
                for parent in frontiers.get(id(child), ())
            )
        )
        if isinstance(node, (e.Add, e.Binary)):
            semantic = expression_semantic_identity(node)
            if semantic in known:
                frontiers[id(node)] = (known[semantic],)
                continue
            identity = f"bilinear_operation{len(operations)}"
            known[semantic] = identity
            operations.append((identity, semantic))
            dependencies.extend((source, identity, 0) for source in inputs)
            frontiers[id(node)] = (identity,)
        else:
            frontiers[id(node)] = inputs
    by_semantic = {semantic: identity for identity, semantic in operations}
    plan = assignment.expression.pipeline_plan
    source = (plan.source_expression or value) if plan is not None else value
    proof = plan.rewrite_certificate if plan is not None else ()
    bindings = tuple(
        ScheduledValueResourceBinding(
            by_semantic[
                expression_semantic_identity(
                    next(
                        item.expression
                        for item in resource.semantic_mappings
                        if item.resource_port == "p"
                    )
                )
            ],
            resource.identity,
            resource.resource_definition_identity,
        )
        for resource in resources
    )
    return ScheduledValueGraph(
        expression_semantic_identity(source),
        expression_semantic_identity(value),
        tuple(identity for identity, _ in operations),
        tuple(operations),
        tuple(dependencies),
        tuple((identity, 0) for identity, _ in operations),
        (0,),
        1,
        1,
        "unmeasured",
        resource_bindings=bindings,
        cut_identities=("joint_output",),
        rewrite_certificate=proof,
        clock_domain=module.clock,
    )


def map_parallel_bilinear(request):
    if request.configuration.name != "unregistered":
        raise TargetArchitectureError(
            "parallel bilinear covering supports only unregistered products"
        )
    value = request.source_expression
    products = bilinear_products(value)
    if len(products) != request.template.resource_count:
        raise TargetArchitectureError(
            "parallel product template does not match the selected exact DAG"
        )
    assignments = tuple(
        item
        for item in request.module.assignments
        if isinstance(item.expression, e.Pipeline)
        and item.expression.expression == value
    )
    latency = assignments[0].expression.stages if len(assignments) == 1 else 0
    if latency not in (0, 1):
        raise TargetArchitectureError(
            "parallel bilinear covering supports latency zero or one only"
        )
    if request.exact_latency is not None and request.exact_latency != latency:
        raise TargetArchitectureError(
            "parallel bilinear covering cannot alter the selected latency"
        )
    graphs = tuple(
        map_auto_multiply_configuration(
            request.module,
            request.target,
            request.family,
            request.resources,
            replace(request.template, operation="multiply", resource_count=1),
            request.configuration,
            source_expression=product,
        )
        for product in products
    )
    if not graphs:
        raise TargetArchitectureError("parallel bilinear region has no products")
    resource = graphs[0].resources[0].resource_definition_identity
    validate_inventory(request.target, ((resource, len(graphs)),))
    nodes = tuple(
        replace(
            graph.resources[0],
            identity=f"product{index}",
            configuration=(*graph.resources[0].configuration, ("parallel_bilinear", 1)),
        )
        for index, graph in enumerate(graphs)
    )
    return replace(
        graphs[0],
        resources=nodes,
        dedicated_edges=(),
        latency=latency,
        semantic_region_identity=expression_semantic_identity(value),
        quantization=value,
        timing_dag=None,
        scheduled_value_graph=_joint_schedule(
            request.module, value, nodes, assignments[0]
        )
        if latency == 1
        else None,
        legality_evidence=(
            "one physical resource per unique full-width product",
            "joint output register; II=1; no sharing or interleaving",
        ),
    )
