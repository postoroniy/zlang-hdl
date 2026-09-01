"""Deterministic, estimate-based extraction of implementation alternatives."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256
from math import inf
from typing import Any, Callable, Iterable, Mapping

from zlang.ir import expressions as expr
from zlang.ir.module import Module


COST_MODEL = "zlang-initial-v1"


class MetricSource(str, Enum):
    STRUCTURAL_ESTIMATE = "structural_estimate"
    SYNTHESIS_MEASUREMENT = "synthesis_measurement"
    ROUTED_MEASUREMENT = "routed_measurement"


class SourcePolicy(str, Enum):
    ESTIMATE_ONLY = "estimate_only"
    MEASURED_PREFERRED = "measured_preferred"
    MEASURED_REQUIRED = "measured_required"


@dataclass(frozen=True)
class MetricValue:
    value: int | float | None
    source: MetricSource


@dataclass(frozen=True)
class CandidateCost:
    """Unified cost vector; resource metrics retain their provenance."""

    lut: MetricValue
    ff: MetricValue
    dsp: MetricValue
    bram: MetricValue
    latency: MetricValue
    ii: MetricValue
    fmax_est: MetricValue
    structural_cost: int

    @classmethod
    def estimate(cls, *, lut: int, ff: int = 0, dsp: int = 0, bram: int = 0,
                 latency: int | None = 0, ii: int = 1, fmax_est: float | None = None,
                 structural_cost: int = 0) -> "CandidateCost":
        source = MetricSource.STRUCTURAL_ESTIMATE
        return cls(*(MetricValue(value, source) for value in
                     (lut, ff, dsp, bram, latency, ii, fmax_est)), structural_cost)

    def metric(self, metric: expr.CostMetric) -> MetricValue:
        return {
            expr.CostMetric.LUT: self.lut, expr.CostMetric.FF: self.ff,
            expr.CostMetric.DSP: self.dsp, expr.CostMetric.BRAM: self.bram,
            expr.CostMetric.LATENCY: self.latency,
            expr.CostMetric.INITIATION_INTERVAL: self.ii,
            expr.CostMetric.FMAX_EST: self.fmax_est,
        }[metric]


@dataclass(frozen=True)
class UnifiedConstraint:
    metric: expr.CostMetric
    maximum: int | float | None = None
    minimum: int | float | None = None


@dataclass(frozen=True)
class ConstraintResult:
    constraint: UnifiedConstraint
    status: str
    reason: str


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate: Any
    cost: CandidateCost
    constraints: tuple[ConstraintResult, ...]
    legal: bool
    objective: MetricValue
    objective_key: tuple[Any, ...]


@dataclass(frozen=True)
class ExtractionResult:
    selected: Any
    selected_cost: CandidateCost
    objective: expr.CostMetric
    constraints: tuple[UnifiedConstraint, ...]
    source_policy: SourcePolicy
    evaluations: tuple[CandidateEvaluation, ...]
    reason: str


def extract_best(
    candidates: Iterable[Any] | Mapping[Any, CandidateCost],
    objective: expr.CostMetric | str = expr.CostMetric.LUT,
    constraints: Iterable[UnifiedConstraint | expr.CostConstraint] = (),
    source_policy: SourcePolicy | str = SourcePolicy.ESTIMATE_ONLY,
    cost_fn: Callable[[Any], CandidateCost] | None = None,
) -> ExtractionResult:
    """Select one already-equivalent candidate deterministically.

    This API is deliberately independent of equality and never invokes a
    synthesis tool. Candidates may be ``(candidate, CandidateCost)`` pairs,
    objects carrying ``cost``, or be scored by ``cost_fn``.
    """
    objective = expr.CostMetric(objective)
    policy = SourcePolicy(source_policy)
    normalized_constraints = tuple(_normalize_constraint(item) for item in constraints)
    if isinstance(candidates, Mapping):
        items = list(candidates.items())
    else:
        items = []
        for candidate in candidates:
            if isinstance(candidate, tuple) and len(candidate) == 2 and isinstance(candidate[1], CandidateCost):
                items.append(candidate)
            else:
                cost = cost_fn(candidate) if cost_fn else getattr(candidate, "cost", None)
                if not isinstance(cost, CandidateCost):
                    raise CostExtractionError("candidate cost is missing")
                items.append((candidate, cost))
    evaluations: list[CandidateEvaluation] = []
    for candidate, raw_cost in items:
        cost = _apply_policy(raw_cost, policy)
        checks = tuple(_check_constraint(cost, item) for item in normalized_constraints)
        value = cost.metric(objective)
        objective_check = value.value is not None
        legal = objective_check and all(item.status == "satisfied" for item in checks)
        key = _sort_key(candidate, cost, objective, value)
        evaluations.append(CandidateEvaluation(candidate, cost, checks, legal, value, key))
    legal = [item for item in evaluations if item.legal]
    if not legal:
        details = "; ".join(
            f"{item.candidate!r}: " + ", ".join(check.reason for check in item.constraints)
            for item in evaluations
        )
        raise CostExtractionError("no legal candidate satisfies constraints" + (f" ({details})" if details else ""))
    selected = min(legal, key=lambda item: item.objective_key)
    return ExtractionResult(selected.candidate, selected.cost, objective,
                            normalized_constraints, policy, tuple(evaluations),
                            _selection_reason(selected, objective))


def extract_best_eclass(
    saturation_result: Any,
    objective: expr.CostMetric | str = expr.CostMetric.LUT,
    constraints: Iterable[UnifiedConstraint | expr.CostConstraint] = (),
    source_policy: SourcePolicy | str = SourcePolicy.ESTIMATE_ONLY,
) -> ExtractionResult:
    """Apply M28 extraction to an M26/M27 ``SaturationResult`` e-class."""
    eclass = saturation_result.equivalence_class
    return extract_best(
        eclass.terms,
        objective,
        constraints,
        source_policy,
        cost_fn=_term_cost,
    )


def candidate_cost_from_alternative(alternative: expr.ImplementationAlternative) -> CandidateCost:
    """Adapt the existing M20 estimate object to the unified M28 vector."""
    estimate = estimate_cost(alternative)
    structural = estimate.lut + estimate.ff + estimate.dsp + estimate.bram + 1
    return CandidateCost.estimate(
        lut=estimate.lut, ff=estimate.ff, dsp=estimate.dsp,
        bram=estimate.bram, latency=estimate.latency,
        ii=estimate.initiation_interval, fmax_est=estimate.fmax_est,
        structural_cost=structural,
    )


def _term_cost(term: Any) -> CandidateCost:
    """Coarse deterministic scalar estimator for pure e-graph terms."""
    op = getattr(term, "op", None)
    operands = tuple(getattr(term, "operands", ()))
    width = getattr(getattr(term, "type", None), "width", 1) or 1
    child = [_term_cost(item) for item in operands]
    structural = 1 + sum(item.structural_cost for item in child)
    lut = 0
    if getattr(op, "value", op) in {"binary", "add"}:
        lut = width
    elif getattr(op, "value", op) == "mux":
        lut = width
    elif getattr(op, "value", op) in {"extend", "truncate"}:
        lut = 0
    return CandidateCost.estimate(lut=lut, ff=0, dsp=0, bram=0,
                                  latency=0, ii=1,
                                  structural_cost=structural)


def _normalize_constraint(value: UnifiedConstraint | expr.CostConstraint) -> UnifiedConstraint:
    if isinstance(value, UnifiedConstraint):
        return value
    return UnifiedConstraint(value.metric, maximum=value.maximum)


def _apply_policy(cost: CandidateCost, policy: SourcePolicy) -> CandidateCost:
    # CandidateCost already carries the selected provenance. A required policy
    # is enforced by constraint/objective validation rather than guessing.
    if policy is SourcePolicy.ESTIMATE_ONLY:
        return cost
    fields = []
    measured_resource_metrics = {
        expr.CostMetric.LUT, expr.CostMetric.FF, expr.CostMetric.DSP,
        expr.CostMetric.BRAM,
    }
    for metric in (expr.CostMetric.LUT, expr.CostMetric.FF, expr.CostMetric.DSP, expr.CostMetric.BRAM, expr.CostMetric.LATENCY, expr.CostMetric.INITIATION_INTERVAL, expr.CostMetric.FMAX_EST):
        value = cost.metric(metric)
        if policy is SourcePolicy.MEASURED_REQUIRED:
            if metric in measured_resource_metrics and value.source not in {
                MetricSource.SYNTHESIS_MEASUREMENT,
                MetricSource.ROUTED_MEASUREMENT,
            }:
                value = MetricValue(None, value.source)
            elif metric is expr.CostMetric.FMAX_EST and value.source is not MetricSource.ROUTED_MEASUREMENT:
                value = MetricValue(None, value.source)
        fields.append(value)
    return CandidateCost(*fields, cost.structural_cost)


def _check_constraint(cost: CandidateCost, constraint: UnifiedConstraint) -> ConstraintResult:
    value = cost.metric(constraint.metric)
    if value.value is None:
        return ConstraintResult(constraint, "unknown", f"{constraint.metric.value} requirement cannot be proven")
    if constraint.maximum is not None and value.value > constraint.maximum:
        return ConstraintResult(constraint, "violated", f"{constraint.metric.value} {value.value} > {constraint.maximum}")
    if constraint.minimum is not None and value.value < constraint.minimum:
        return ConstraintResult(constraint, "violated", f"{constraint.metric.value} {value.value} < {constraint.minimum}")
    return ConstraintResult(constraint, "satisfied", f"{constraint.metric.value} constraint satisfied")


def _sort_key(candidate: Any, cost: CandidateCost, objective: expr.CostMetric, value: MetricValue) -> tuple[Any, ...]:
    primary = value.value
    if primary is None:
        primary = inf if objective is not expr.CostMetric.FMAX_EST else -inf
    order = [expr.CostMetric.LATENCY, expr.CostMetric.INITIATION_INTERVAL,
             expr.CostMetric.DSP, expr.CostMetric.BRAM, expr.CostMetric.LUT,
             expr.CostMetric.FF]
    tie = tuple(_known(cost.metric(item).value) for item in order if item is not objective)
    structural = cost.structural_cost
    canonical = str(
        getattr(
            candidate,
            "implementation_identity",
            getattr(candidate, "identity", repr(candidate)),
        )
    )
    digest = sha256(canonical.encode()).hexdigest()
    return ((-primary if objective is expr.CostMetric.FMAX_EST else primary), *tie, structural, canonical, digest)


def _known(value: int | float | None) -> float:
    return inf if value is None else float(value)


def _selection_reason(selected: CandidateEvaluation, objective: expr.CostMetric) -> str:
    return f"selected by {'maximize' if objective is expr.CostMetric.FMAX_EST else 'minimize'} {objective.value} with deterministic tie-break"


class CostExtractionError(ValueError):
    """No candidate satisfies an automatic choice's hard constraints."""


@dataclass(frozen=True)
class CandidateAssessment:
    kind: expr.ImplementationKind
    estimate: expr.ImplementationCostEstimate
    violations: tuple[str, ...]

    @property
    def legal(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class ExtractionDecision:
    output: str
    goal: expr.CostMetric
    constraints: tuple[expr.CostConstraint, ...]
    candidates: tuple[CandidateAssessment, ...]
    selected: expr.ImplementationKind


@dataclass(frozen=True)
class CostExtractionResult:
    module: Module
    decisions: tuple[ExtractionDecision, ...]


def estimate_cost(
    alternative: expr.ImplementationAlternative,
) -> expr.ImplementationCostEstimate:
    """Apply the documented target-independent Milestone 20 cost model."""

    applicability = alternative.applicability
    semantics = alternative.semantics
    result_width = applicability.result_type.width
    if alternative.kind is expr.ImplementationKind.MUL_ADD:
        lut = (
            applicability.multiplier_left_type.width
            * applicability.multiplier_right_type.width
            + result_width
        )
        dsp = 0
    elif alternative.kind is expr.ImplementationKind.DSP_MAC:
        lut = result_width
        dsp = 1
    else:  # pragma: no cover - enum exhaustiveness guard
        raise CostExtractionError(
            f"the {COST_MODEL} model does not support '{alternative.kind.value}'"
        )
    return expr.ImplementationCostEstimate(
        lut=lut,
        ff=result_width * semantics.latency,
        dsp=dsp,
        bram=0,
        latency=semantics.latency,
        initiation_interval=semantics.initiation_interval,
    )


def extract_estimated_costs(module: Module) -> CostExtractionResult:
    """Select every automatic choice using estimates and hard constraints."""

    assignments = list(module.assignments)
    decisions: list[ExtractionDecision] = []
    for index, assignment in enumerate(assignments):
        choice = assignment.expression
        if not isinstance(choice, expr.ImplementationChoice):
            continue
        policy = choice.cost_policy
        if policy is None:
            continue

        alternatives = tuple(
            replace(alternative, estimate=estimate_cost(alternative))
            for alternative in choice.alternatives
        )
        assessments_list: list[CandidateAssessment] = []
        for alternative in alternatives:
            estimate = _require_estimate(alternative)
            violations = tuple(
                f"{constraint.metric.value}="
                f"{estimate.value(constraint.metric)} > {constraint.maximum}"
                for constraint in policy.constraints
                if estimate.value(constraint.metric) > constraint.maximum
            )
            assessments_list.append(
                CandidateAssessment(alternative.kind, estimate, violations)
            )
        assessments = tuple(assessments_list)
        legal = tuple(assessment for assessment in assessments if assessment.legal)
        if not legal:
            failures = "; ".join(
                f"{assessment.kind.value} violates "
                + ", ".join(assessment.violations)
                for assessment in sorted(
                    assessments, key=lambda item: item.kind.value
                )
            )
            raise CostExtractionError(
                f"no legal implementation for output '{assignment.target.name}' "
                f"under {COST_MODEL} estimated costs: {failures}"
            )
        # M34 compatibility normalization: legacy choice(auto) retains its
        # syntax and report objects, but selection uses the same M28 policy as
        # every other candidate producer.
        legal_alternatives = tuple(
            alternative
            for alternative in alternatives
            if any(
                assessment.kind is alternative.kind and assessment.legal
                for assessment in assessments
            )
            and (
                not choice.formal_eligible
                or alternative.kind in choice.formal_eligible
            )
        )
        unified = extract_best(
            legal_alternatives,
            objective=policy.goal,
            constraints=tuple(
                UnifiedConstraint(item.metric, maximum=item.maximum)
                for item in policy.constraints
            ),
            cost_fn=candidate_cost_from_alternative,
        )
        selected = next(
            assessment
            for assessment in legal
            if assessment.kind is unified.selected.kind
        )
        assignments[index] = replace(
            assignment,
            expression=replace(
                choice,
                selected=selected.kind,
                alternatives=alternatives,
            ),
        )
        decisions.append(
            ExtractionDecision(
                assignment.target.name,
                policy.goal,
                policy.constraints,
                assessments,
                selected.kind,
            )
        )
    return CostExtractionResult(
        replace(module, assignments=tuple(assignments)),
        tuple(decisions),
    )


def render_cost_report(result: CostExtractionResult) -> str:
    """Explain constraints, estimates, legality, and deterministic selection."""

    if not result.decisions:
        return ""
    lines = [
        f"module {result.module.name}",
        f"cost_model={COST_MODEL} cost_source=estimate measured=false",
    ]
    for decision in result.decisions:
        constraints = ",".join(
            f"{constraint.metric.value}<={constraint.maximum}"
            for constraint in decision.constraints
        )
        lines.append(
            f"choice output={decision.output} goal=minimize_"
            f"{decision.goal.value} constraints=[{constraints}]"
        )
        for candidate in sorted(
            decision.candidates, key=lambda item: item.kind.value
        ):
            estimate = candidate.estimate
            violations = ",".join(candidate.violations) or "none"
            lines.append(
                f"  candidate kind={candidate.kind.value} legal="
                f"{str(candidate.legal).lower()} estimated "
                f"lut={estimate.lut} ff={estimate.ff} dsp={estimate.dsp} "
                f"bram={estimate.bram} latency={estimate.latency} "
                f"ii={estimate.initiation_interval} violations=[{violations}]"
            )
        selected = next(
            candidate
            for candidate in decision.candidates
            if candidate.kind is decision.selected
        )
        lines.append(
            f"  selected kind={decision.selected.value} reason="
            f"minimum_estimated_{decision.goal.value}="
            f"{selected.estimate.value(decision.goal)} "
            "tie_break=implementation_kind"
        )
    return "\n".join(lines) + "\n"


def _require_estimate(
    alternative: expr.ImplementationAlternative,
) -> expr.ImplementationCostEstimate:
    assert alternative.estimate is not None
    return alternative.estimate
