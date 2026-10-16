# SPDX-License-Identifier: Apache-2.0
"""Bounded spatial candidates for already-certified joint bilinear values.

This provider does not discover algebraic alternatives. It accounts unique
products and offers only combinational or one-output-register II=1 layouts.
Target mapping must subsequently validate/bind every advertised DSP resource.
"""

from __future__ import annotations

from zlang.ir import expressions as e
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.ir.pipelines import (
    MultiplierMapping,
    PipelineCandidate,
    PipelineEstimate,
    PipelineTree,
    PipelinePlan,
    RegisterPlacement,
)
from zlang.opt.bilinear_polynomial import read_bilinear, expression_term
from zlang.ir.joint_values import joint_value_boundary


def bilinear_products(value: e.Expression) -> tuple[e.Binary, ...]:
    try:
        boundary = joint_value_boundary(value)
        if boundary is None:
            return ()
        read_bilinear(expression_term(boundary.packed))
    except (TypeError, ValueError):
        return ()
    products = tuple(
        {
            expression_semantic_identity(item): item
            for item in (
                item
                for item in ExpressionDagIndex((value,))
                if isinstance(item, e.Binary)
                and item.operator is e.BinaryOperator.MULTIPLY
            )
        }.values()
    )
    if not 1 <= len(products) <= 8:
        return ()
    return products


def bilinear_pipeline_candidates(value, context, source=None, proof=()):
    products = bilinear_products(value)
    if not products:
        return ()
    # Exact DAG accounting: a product shared by output fields is charged once.
    nodes = tuple(ExpressionDagIndex((value,)))
    lut = sum(
        item.type.width
        for item in nodes
        if isinstance(item, e.Add)
        or (isinstance(item, e.Binary) and item.operator is e.BinaryOperator.SUBTRACT)
    )
    return tuple(
        PipelineCandidate(
            f"bilinear_parallel_{len(products)}dsp_l{latency}",
            value
            if latency == 0
            else e.Pipeline(
                1,
                value,
                context.allocate_instance(),
                value.type,
                pipeline_plan=PipelinePlan(
                    ("output",),
                    1,
                    source_expression=source or value,
                    source_expression_identity=expression_semantic_identity(
                        source or value
                    ),
                    selected_value_identity=expression_semantic_identity(value),
                    rewrite_certificate=tuple(
                        item for item in proof if item.startswith("checked-value:")
                    ),
                ),
                domain=context.clock_domain,
            ),
            PipelineTree.DAG,
            RegisterPlacement.OUTPUT,
            MultiplierMapping.DSP,
            (
                "exact_joint_value",
                "parallel_products",
                "one_resource_per_unique_product",
            ),
            latency,
            1,
            PipelineEstimate(lut, latency * value.type.width, len(products), 0),
        )
        for latency in (0, 1)
        if latency == 0 or context.clock_domain is not None
    )


def is_bilinear_pipeline(candidate) -> bool:
    return candidate.name.startswith(
        "bilinear_parallel_"
    ) and candidate.transformations == (
        "exact_joint_value",
        "parallel_products",
        "one_resource_per_unique_product",
    )
