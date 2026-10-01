"""Reporting and cost support for persisted legacy architecture records.

New source constructs use the unified :mod:`zlang.exploration` path.  The
records rendered here remain readable for artifact and evidence replay.
"""

from __future__ import annotations

from zlang.ir.architectures import ArchitectureCandidate
from zlang.ir.module import Module
from zlang.costs import CandidateCost


ARCHITECTURE_MODEL = "zlang-fir-architecture-v1"


def render_architecture_report(module: Module) -> str:
    """Report bounds, pruning, topology, and deterministic selection."""

    if not module.architecture_explorations:
        return ""
    lines = [
        f"module {module.name}",
        f"architecture_model={ARCHITECTURE_MODEL} "
        "equivalence=mathematical temporal_folding=false",
    ]
    for exploration in module.architecture_explorations:
        constraints = ",".join(
            constraint.render() for constraint in exploration.constraints
        )
        lines.append(
            f"exploration output={exploration.output} "
            f"result={exploration.result_type} constraints=[{constraints}] "
            f"theoretical_candidates={exploration.theoretical_candidates} "
            f"search_bound={exploration.search_bound} "
            f"explored={len(exploration.candidates)} "
            f"budget_pruned={exploration.budget_pruned} "
            f"constraint_pruned={exploration.constraint_pruned}"
        )
        for candidate in exploration.candidates:
            transformations = ",".join(candidate.transformations)
            violations = ",".join(candidate.violations) or "none"
            lines.append(
                f"  candidate name={candidate.name} kind={candidate.kind.value} "
                f"legal={str(candidate.legal).lower()} "
                f"parallelism={candidate.parallelism} "
                f"add_depth={candidate.add_depth} "
                f"multipliers={candidate.multiplier_count} "
                f"adders={candidate.adder_count} "
                f"equivalence={candidate.equivalence.value} "
                f"transformations=[{transformations}] "
                f"violations=[{violations}]"
            )
        selected = exploration.selected_candidate
        lines.append(
            f"  selected name={selected.name} kind={selected.kind.value} "
            "reason=cost_selection_maximize_estimated_frequency_with_deterministic_tie_break "
            f"parallelism={selected.parallelism} "
            f"add_depth={selected.add_depth} "
            f"equivalence={selected.equivalence.value}"
        )
    return "\n".join(lines) + "\n"


def _architecture_cost(candidate: ArchitectureCandidate) -> CandidateCost:
    width = candidate.expression.type.width
    return CandidateCost.estimate(
        lut=width * candidate.adder_count,
        dsp=0,
        latency=0,
        ii=1,
        fmax_est=max(1, 800 // max(1, candidate.add_depth)),
        structural_cost=candidate.parallelism + candidate.add_depth,
    )
