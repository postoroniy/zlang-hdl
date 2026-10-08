# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Deterministic candidate space for retained standalone pipeline sites."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.common import stable_digest
from zlang.costs import CandidateCost, extract_best
from zlang.ir.expressions import CostMetric
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.pipelines import pipeline_constraint_to_unified
from zlang.timing import validate_timed_candidate


@dataclass(frozen=True)
class PipelineFormalCandidate:
    pipeline_candidate: object
    expression: object
    semantic_identity: str
    implementation_identity: str
    cost: CandidateCost
    timing_relation: object | None = None
    candidate_class: str = "pipeline_scheduler"


def standalone_pipeline_candidate_space(exploration: object):
    """Reconstruct the frozen deterministic selection space for a pipeline."""

    wrapped = tuple(
        PipelineFormalCandidate(
            candidate,
            candidate.expression,
            expression_semantic_identity(exploration.source_expression),
            stable_digest({
                "schema": "zlang-formal_selection-pipeline-candidate-v1",
                "source": expression_semantic_identity(
                    exploration.source_expression
                ),
                "name": candidate.name,
                "expression": expression_semantic_identity(candidate.expression),
                "latency": candidate.latency,
                "ii": candidate.initiation_interval,
                "transformations": list(candidate.transformations),
            }),
            CandidateCost.estimate(
                lut=candidate.estimate.lut,
                ff=candidate.estimate.ff,
                dsp=candidate.estimate.dsp,
                latency=candidate.latency,
                ii=candidate.initiation_interval,
                fmax_est=candidate.estimate.fmax_mhz,
                structural_cost=len(candidate.transformations),
            ),
            timing_relation=validate_timed_candidate(
                exploration.source_expression,
                candidate.expression,
                value_equivalent=True,
            ),
        )
        for candidate in exploration.candidates
    )
    constraints = tuple(
        pipeline_constraint_to_unified(item) for item in exploration.constraints
    )
    extraction = extract_best(
        wrapped,
        objective=CostMetric.FMAX_EST,
        constraints=constraints,
        cost_fn=lambda item: item.cost,
    )
    return wrapped, constraints, extraction


__all__ = ["PipelineFormalCandidate", "standalone_pipeline_candidate_space"]
