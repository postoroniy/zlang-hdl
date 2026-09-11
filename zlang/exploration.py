"""Backend-independent, bounded exploration orchestration for M34.

This module coordinates the already-frozen value, architecture, reduction,
timing, pipeline, and cost layers.  It does not create new equivalence rules.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from hashlib import sha256
from typing import Any, Callable, Iterable

from zlang.costs import (
    CandidateCost,
    CostExtractionError,
    ExtractionResult,
    SourcePolicy,
    UnifiedConstraint,
    extract_best,
)
from zlang.ir import expressions as ir_expr
from zlang.ir.cdc import ClockDomain
from zlang.ir.traversal import (
    ExpressionTraversalPolicy,
    expression_children as typed_expression_children,
)
from zlang.timing import TimingRelation, timing_info, validate_timed_candidate


class TransformFamily(str, Enum):
    PIPELINE = "pipeline"
    DSP = "dsp"
    REDUCTION = "reduction"
    REASSOCIATE = "reassociate"
    ADAPTER = "adapter"


@dataclass(frozen=True)
class ExplorationBounds:
    max_candidates: int = 64
    max_value_alternatives: int = 16
    max_architectures: int = 16
    max_reductions: int = 16
    max_pipeline_candidates: int = 32

    def __post_init__(self) -> None:
        values = tuple(getattr(self, item.name) for item in fields(self))
        if any(value < 1 or value > 256 for value in values):
            raise ValueError("exploration bounds must be between 1 and 256")


@dataclass(frozen=True)
class ExplorationContext:
    output: str
    result_type: object
    allocate_instance: Callable[[], int]
    # A complete output exploration has a stable rewrite boundary.  Retaining
    # that boundary lets compiler orchestration apply M39 after semantic
    # typing, without re-running the analyzer with a live verifier.
    site_owner: str | None = None
    site_kind: str = "source_explore"


@dataclass(frozen=True)
class ExplorationRequest:
    root: ir_expr.Expression
    allowed: tuple[TransformFamily, ...] = ()
    avoided: tuple[TransformFamily, ...] = ()
    constraints: tuple[UnifiedConstraint, ...] = ()
    objective: ir_expr.CostMetric = ir_expr.CostMetric.LUT
    source_policy: SourcePolicy = SourcePolicy.ESTIMATE_ONLY
    bounds: ExplorationBounds = ExplorationBounds()
    source_origin: object | None = None
    equivalences: tuple[object, ...] = ()
    formal_config: object | None = None
    formal_verifier: Callable[[Any, object], Any] | None = None
    # Direct API callers may request M39 during exploration rather than through
    # CompilationSession selection.  Carry the already-typed physical contract
    # explicitly so that path never fabricates a legacy reset domain.
    clock_domain_contract: ClockDomain | None = None

    def validate(self) -> None:
        overlap = set(self.allowed) & set(self.avoided)
        if overlap:
            names = ", ".join(sorted(item.value for item in overlap))
            raise ValueError(
                f"exploration family is both allowed and avoided: {names}"
            )
        metrics = [item.metric for item in self.constraints]
        if len(metrics) != len(set(metrics)):
            duplicate = next(item for item in metrics if metrics.count(item) > 1)
            raise ValueError(
                f"exploration repeats '{duplicate.value}' constraint"
            )


@dataclass(frozen=True)
class ExplorationCandidate:
    expression: ir_expr.Expression
    semantic_identity: str
    implementation_identity: str
    stages: tuple[str, ...]
    cost: CandidateCost
    value_relation: str = "exact_source_value"
    timing_relation: TimingRelation | None = None
    architecture: object | None = None
    protocol_relation: object | None = None
    provenance: tuple[str, ...] = ()

    @property
    def family(self) -> str:
        return self.stages[-1] if self.stages else "source"


class ExplorationSelectionError(ValueError):
    """Bounded, source-facing failure for an impossible implementation policy.

    ``CostExtractionError`` historically included ``repr(candidate)`` in its
    message.  Candidates contain complete typed expression trees, which made
    one impossible policy capable of producing an enormous and unstable
    diagnostic.  Keep the original error as the cause while exposing a small,
    deterministic summary for semantic diagnostics and CLI users.
    """

    def __init__(
        self,
        constraints: tuple[UnifiedConstraint, ...],
        candidates: Iterable[ExplorationCandidate],
        cause: CostExtractionError,
        rejected: Iterable[RejectedCandidate] = (),
    ) -> None:
        self.constraints = constraints
        self.candidate_summaries = tuple(
            _candidate_summary(candidate) for candidate in candidates
        )
        constraint_text = ", ".join(_constraint_summary(item) for item in constraints)
        candidates_text = "; ".join(self.candidate_summaries[:16])
        if len(self.candidate_summaries) > 16:
            candidates_text += f"; ... +{len(self.candidate_summaries) - 16} candidates"
        message = (
            "no implementation satisfies constraints"
            f" [{constraint_text or 'none'}]"
        )
        if candidates_text:
            message += f"; candidates: {candidates_text}"
        rejection_reasons = tuple(
            dict.fromkeys(
                item.reason[:800]
                for item in rejected
                if item.reason
            )
        )
        if rejection_reasons:
            message += "; rejected transformations: " + "; ".join(
                rejection_reasons[:4]
            )
        super().__init__(message)
        self.__cause__ = cause


def _constraint_summary(constraint: UnifiedConstraint) -> str:
    metric = constraint.metric.value
    if (
        constraint.minimum is not None
        and constraint.maximum is not None
        and constraint.minimum == constraint.maximum
    ):
        return f"{metric} == {constraint.minimum}"
    if constraint.maximum is not None:
        return f"{metric} <= {constraint.maximum}"
    if constraint.minimum is not None:
        return f"{metric} >= {constraint.minimum}"
    return metric


def _metric_value(value: object, metric: str) -> str:
    cost = getattr(value, metric)
    raw = getattr(cost, "value", None)
    return "unknown" if raw is None else str(raw)


def _candidate_summary(candidate: ExplorationCandidate) -> str:
    cost = candidate.cost
    stages = "/".join(candidate.stages) or "source"
    return (
        f"{stages}"
        f"[lut={_metric_value(cost, 'lut')}"
        f",ff={_metric_value(cost, 'ff')}"
        f",dsp={_metric_value(cost, 'dsp')}"
        f",bram={_metric_value(cost, 'bram')}"
        f",latency={_metric_value(cost, 'latency')}"
        f",ii={_metric_value(cost, 'ii')}"
        f",fmax={_metric_value(cost, 'fmax_est')}]"
    )


@dataclass(frozen=True)
class RejectedCandidate:
    candidate: ExplorationCandidate | None
    stage: str
    reason: str


@dataclass(frozen=True)
class ExplorationResult:
    request: ExplorationRequest
    source_semantic_identity: str
    generated_candidates: tuple[ExplorationCandidate, ...]
    rejected_candidates: tuple[RejectedCandidate, ...]
    selected_candidate: ExplorationCandidate
    extraction: ExtractionResult
    stage_counts: tuple[tuple[str, int], ...]
    search_complete: bool
    termination_reason: str
    formal_records: tuple[object, ...] = ()
    # Selection-only locator metadata.  Expression-local explores deliberately
    # leave ``site_output`` unset and are rewritten by exact retained object
    # identity; complete output/profile regions retain their public boundary.
    site_owner: str | None = None
    site_output: str | None = None
    site_kind: str = "expression_explore"

    @property
    def selected(self) -> ExplorationCandidate:
        return self.selected_candidate

    @property
    def candidates(self) -> tuple[ExplorationCandidate, ...]:
        return self.generated_candidates

    @property
    def report(self) -> str:
        return render_result(self)


def _general_pipeline_latencies(request: ExplorationRequest) -> tuple[int, ...]:
    """Return a deterministic bounded latency search set for DAG scheduling.

    Scalar ``implement`` only enables pipeline candidates after the source has
    supplied a positive latency constraint.  Exact constraints therefore map
    to one latency, bounded ranges retain both endpoints, and an open upper
    bound receives a deliberately small finite search horizon.  The M28
    constraint evaluator remains authoritative for candidate legality.
    """

    constraint = next(
        (
            item
            for item in request.constraints
            if item.metric is ir_expr.CostMetric.LATENCY
        ),
        None,
    )
    if constraint is None:
        return ()
    lower = max(1, int(constraint.minimum or 1))
    if constraint.maximum is not None:
        upper = int(constraint.maximum)
    else:
        # Open-ended implementation intent must still produce a bounded,
        # reproducible catalog.  Four neighbouring depths are sufficient for
        # this first structural scheduler and match the established bounded
        # automatic-pipeline search scale.
        upper = lower + 3
    if upper < lower:
        return ()
    count = upper - lower + 1
    budget = min(request.bounds.max_pipeline_candidates, 16)
    if count <= budget:
        return tuple(range(lower, upper + 1))
    # Preserve both semantic boundary candidates and sample the interior
    # evenly.  Integer arithmetic and first-occurrence deduplication make the
    # result stable across hosts.
    selected = {
        lower + (index * (count - 1)) // (budget - 1)
        for index in range(budget)
    }
    return tuple(sorted(selected))


def explore(
    request: ExplorationRequest,
    context: ExplorationContext | None = None,
) -> ExplorationResult:
    """Run one staged pass and delegate final filtering to M28."""

    request.validate()
    allowed = set(request.allowed) - set(request.avoided)
    if TransformFamily.ADAPTER in allowed:
        raise ValueError(
            "allow adapter is valid only for protocol-connection exploration; "
            "automatic connection adaptation is not implemented"
        )
    if TransformFamily.PIPELINE in allowed and context is None:
        raise ValueError(
            "allow pipeline requires a complete clocked wire-output assignment"
        )

    rejected: list[RejectedCandidate] = []
    complete = True
    termination = "all enabled bounded stages completed"
    source = _candidate(request.root, ("source",), estimate_expression_cost(request.root))
    candidates = [source]
    counts: list[tuple[str, int]] = [("source", 1)]

    # M26/M27 exact, type-preserving alternatives are always safe defaults.
    value_candidates, value_truncated, value_rejections = _value_candidates(request)
    candidates = _deduplicate([*candidates, *value_candidates])
    rejected.extend(value_rejections)
    counts.append(("value", len(candidates)))
    complete, termination = _merge_completion(
        complete, termination, value_truncated, "value expansion"
    )

    if TransformFamily.DSP in allowed:
        expanded: list[ExplorationCandidate] = list(candidates)
        from zlang.architecture import (
            ArchitectureImplementation,
            architecture_cost,
            expand_architectures,
        )
        for parent in candidates:
            for architecture in expand_architectures(parent.expression):
                if architecture.implementation is ArchitectureImplementation.GENERIC:
                    continue
                expanded.append(
                    _candidate(
                        architecture.value_root,
                        (*parent.stages, architecture.implementation.value),
                        architecture_cost(architecture),
                        architecture=architecture,
                        value_relation="M29 exact typed value semantics",
                        provenance=(*parent.provenance, "M29 architecture"),
                    )
                )
        candidates, truncated = _bounded_deduplicate(
            expanded, request.bounds.max_architectures, request.bounds.max_candidates
        )
        counts.append(("architecture", len(candidates)))
        complete, termination = _merge_completion(
            complete, termination, truncated, "architecture expansion"
        )

    if TransformFamily.REDUCTION in allowed:
        expanded = list(candidates)
        from zlang.reductions import expand_reduction, reduction_cost
        for parent in candidates:
            try:
                reductions = expand_reduction(parent.expression)
            except ValueError as error:
                rejected.append(
                    RejectedCandidate(parent, "reduction", str(error))
                )
                continue
            for reduction in reductions:
                if not reduction.legal:
                    rejected.append(
                        RejectedCandidate(parent, "reduction", reduction.legality.reason)
                    )
                    continue
                if (
                    reduction.implementation_policy == "dsp_preferred"
                    and TransformFamily.DSP not in allowed
                ):
                    continue
                expanded.append(
                    _candidate(
                        reduction.expression,
                        (*parent.stages, f"reduction:{reduction.topology.value}",
                         reduction.implementation_policy),
                        reduction_cost(reduction),
                        architecture=reduction,
                        value_relation="M32 exact canonical reduction semantics",
                        provenance=(*parent.provenance, "M32 reduction"),
                    )
                )
        candidates, truncated = _bounded_deduplicate(
            expanded, request.bounds.max_reductions, request.bounds.max_candidates
        )
        counts.append(("reduction", len(candidates)))
        complete, termination = _merge_completion(
            complete, termination, truncated, "reduction expansion"
        )

    if TransformFamily.PIPELINE in allowed:
        assert context is not None
        if timing_info(request.root).latency != 0:
            raise ValueError("explore pipeline source must be combinational")
        # Project the generic implementation policy through one shared,
        # fail-closed conversion boundary.  The pipeline model intentionally
        # covers only timing/throughput/DSP/frequency; M28 retains ownership of
        # LUT/FF/BRAM and lower-only latency constraints.
        from zlang.pipelines import (
            PipelineExplorationError,
            explore_general_pipeline,
            explore_pipeline,
            pipeline_constraints_from_unified,
        )

        pipeline_constraints = pipeline_constraints_from_unified(
            request.constraints
        )
        general_latencies = _general_pipeline_latencies(request)
        expanded = list(candidates)
        from zlang.ir.pipelines import MultiplierMapping
        for parent in candidates:
            pipeline_candidates = []
            pipeline_failures: list[str] = []
            try:
                generated = explore_pipeline(
                    context.output,
                    parent.expression,
                    context.result_type,
                    pipeline_constraints,
                    context.allocate_instance,
                )
            except PipelineExplorationError as error:
                pipeline_failures.append(f"specialized: {error}")
            else:
                pipeline_candidates.extend(generated.candidates)
            # Preserve the validated sum/product and target-DSP catalog as the
            # specialized architecture family.  The general DAG scheduler is
            # a bounded fallback, not a duplicate replacement catalog.
            if not pipeline_candidates and general_latencies:
                try:
                    general = explore_general_pipeline(
                        context.output,
                        parent.expression,
                        context.result_type,
                        pipeline_constraints,
                        general_latencies,
                        context.allocate_instance,
                    )
                except PipelineExplorationError as error:
                    pipeline_failures.append(f"general DAG: {error}")
                else:
                    pipeline_candidates.extend(general.candidates)
            if not pipeline_candidates:
                rejected.append(
                    RejectedCandidate(
                        parent,
                        "pipeline",
                        "; ".join(pipeline_failures)
                        or "no bounded pipeline candidate was generated",
                    )
                )
                continue
            for pipeline in pipeline_candidates:
                if (
                    pipeline.multiplier_mapping is MultiplierMapping.DSP
                    and TransformFamily.DSP not in allowed
                ):
                    continue
                if (
                    "reassociate_balanced_tree" in pipeline.transformations
                    and TransformFamily.REASSOCIATE not in allowed
                ):
                    continue
                relation = validate_timed_candidate(
                    request.root, pipeline.expression, value_equivalent=True
                )
                if not relation.equivalent:
                    rejected.append(
                        RejectedCandidate(parent, "pipeline", relation.proof)
                    )
                    continue
                expanded.append(
                    _candidate(
                        pipeline.expression,
                        (*parent.stages, f"pipeline:{pipeline.name}"),
                        CandidateCost.estimate(
                            lut=pipeline.estimate.lut,
                            ff=pipeline.estimate.ff,
                            dsp=pipeline.estimate.dsp,
                            latency=pipeline.latency,
                            ii=pipeline.initiation_interval,
                            fmax_est=pipeline.estimate.fmax_mhz,
                            structural_cost=len(pipeline.transformations),
                        ),
                        timing_relation=relation,
                        architecture=pipeline,
                        value_relation=parent.value_relation,
                        provenance=(*parent.provenance, "M31 pipeline/M30 timing"),
                    )
                )
        candidates, truncated = _bounded_deduplicate(
            expanded,
            request.bounds.max_pipeline_candidates,
            request.bounds.max_candidates,
        )
        counts.append(("pipeline", len(candidates)))
        complete, termination = _merge_completion(
            complete, termination, truncated, "pipeline expansion"
        )

    try:
        extraction = extract_best(
            candidates,
            objective=request.objective,
            constraints=request.constraints,
            source_policy=request.source_policy,
            cost_fn=lambda item: item.cost,
        )
    except CostExtractionError as error:
        raise ExplorationSelectionError(
            request.constraints,
            candidates,
            error,
            rejected,
        ) from error
    formal_records: tuple[object, ...] = ()
    if request.formal_config is not None:
        from zlang.formal_exploration import gate_candidates
        verifier = request.formal_verifier
        if (
            verifier is None
            and request.formal_config.policy.value != "off"
        ):
            from zlang.formal_candidate import (
                M36ClashCandidateVerifier,
                M36DirectSystemVerilogCandidateVerifier,
            )
            verifier_type = (
                M36DirectSystemVerilogCandidateVerifier
                if getattr(request.formal_config, "backend", "clash")
                == "direct_systemverilog"
                else M36ClashCandidateVerifier
            )
            verifier = verifier_type(
                request.root,
                artifact_provider=getattr(
                    request.formal_config,
                    "artifact_provider",
                    None,
                ),
                clock_domain_contract=request.clock_domain_contract,
            )
        gate = gate_candidates(tuple(candidates), extraction.evaluations,
                               request.formal_config, verifier)
        formal_records = gate.records
        if request.formal_config.policy.value != "off":
            extraction = extract_best(
                gate.eligible, objective=request.objective,
                constraints=request.constraints, source_policy=request.source_policy,
                cost_fn=lambda item: item.cost,
            )
    for evaluation in extraction.evaluations:
        if not evaluation.legal:
            reasons = tuple(
                item.reason for item in evaluation.constraints
                if item.status != "satisfied"
            )
            rejected.append(
                RejectedCandidate(
                    evaluation.candidate,
                    "extraction",
                    "; ".join(reasons) or "objective metric is unknown",
                )
            )
    return ExplorationResult(
        request,
        source.semantic_identity,
        tuple(candidates),
        tuple(rejected),
        extraction.selected,
        extraction,
        tuple(counts),
        complete,
        termination,
        formal_records,
        None if context is None else context.site_owner,
        None if context is None else context.output,
        "expression_explore" if context is None else context.site_kind,
    )


def estimate_expression_cost(value: ir_expr.Expression) -> CandidateCost:
    """Conservative structural estimate with explicit estimate provenance."""
    children = tuple(_expression_children(value))
    child_costs = tuple(estimate_expression_cost(item) for item in children)
    lut = sum(int(item.lut.value or 0) for item in child_costs)
    ff = sum(int(item.ff.value or 0) for item in child_costs)
    dsp = sum(int(item.dsp.value or 0) for item in child_costs)
    structural = 1 + sum(item.structural_cost for item in child_costs)
    width = getattr(value.type, "width", 1)
    if isinstance(value, ir_expr.Add):
        lut += width
    elif isinstance(value, ir_expr.Binary):
        if value.operator is ir_expr.BinaryOperator.MULTIPLY:
            lut += value.left.type.width * value.right.type.width
        elif value.operator not in {
            ir_expr.BinaryOperator.SHIFT_LEFT,
            ir_expr.BinaryOperator.SHIFT_RIGHT,
        }:
            lut += width
    elif isinstance(value, (ir_expr.Mux, ir_expr.Switch)):
        lut += width
    latency = timing_info(value).latency
    if isinstance(value, ir_expr.Delay):
        ff += value.type.width * value.cycles
    elif isinstance(value, ir_expr.Pipeline):
        ff += value.type.width * value.stages
    logic_depth = max(1, _logic_depth(value))
    return CandidateCost.estimate(
        lut=lut,
        ff=ff,
        dsp=dsp,
        latency=latency,
        ii=1,
        fmax_est=max(1, 800 // logic_depth),
        structural_cost=structural,
    )


def constraints_from_syntax(items: Iterable[Any]) -> tuple[UnifiedConstraint, ...]:
    result = []
    for item in items:
        metric = ir_expr.CostMetric(item.metric.value)
        relation = item.relation.value
        if relation == "<=":
            result.append(UnifiedConstraint(metric, maximum=item.value))
        elif relation == ">=":
            result.append(UnifiedConstraint(metric, minimum=item.value))
        else:
            result.append(
                UnifiedConstraint(metric, minimum=item.value, maximum=item.value)
            )
    return tuple(result)


def render_result(result: ExplorationResult) -> str:
    request = result.request
    allowed = ",".join(item.value for item in request.allowed) or "none"
    avoided = ",".join(item.value for item in request.avoided) or "none"
    counts = " ".join(f"{name}={count}" for name, count in result.stage_counts)
    rejected = len(result.rejected_candidates)
    selected = result.selected_candidate
    return "\n".join(
        (
            "Implementation selection: result"
            if result.site_kind == "implement"
            else "Exploration: result",
            f"allowed: {allowed}; avoided: {avoided}",
            f"objective: {'maximize' if request.objective is ir_expr.CostMetric.FMAX_EST else 'minimize'} {request.objective.value}",
            f"search: {counts} legal={sum(item.legal for item in result.extraction.evaluations)} rejected={rejected}",
            f"search complete: {'yes' if result.search_complete else 'no'}; {result.termination_reason}",
            f"selected: {'/'.join(selected.stages)} identity={selected.implementation_identity}",
            f"semantics: value={selected.value_relation}; timing={_timing_text(selected.timing_relation)}; protocol={'not applicable' if selected.protocol_relation is None else selected.protocol_relation.reason}",
            "architecture intent: backend-independent metadata; physical DSP mapping is not claimed",
            f"cost source: {request.source_policy.value}; metrics retain per-field provenance",
            f"formal: policy={request.formal_config.policy.value if request.formal_config else 'off'} "
            f"records={len(result.formal_records)}",
            *(f"formal candidate: {record}" for record in result.formal_records),
            f"reason: {result.extraction.reason}; best candidate in explored bounded search space",
        )
    ) + "\n"


def render_exploration_report(results: Iterable[ExplorationResult]) -> str:
    rendered = tuple(render_result(item).rstrip() for item in results)
    return "\n".join(rendered) + ("\n" if rendered else "")


def _value_candidates(
    request: ExplorationRequest,
) -> tuple[list[ExplorationCandidate], bool, list[RejectedCandidate]]:
    try:
        from zlang.ir.module import Assignment, Module, Port, PortDirection
        from zlang.opt import lower, saturate, term_to_expression

        inputs = _input_refs(request.root)
        output_name = "__zlang_explore_result"
        while output_name in inputs:
            output_name += "_"
        ports = tuple(
            Port(PortDirection.INPUT, name, type_)
            for name, type_ in sorted(inputs.items())
        )
        output = Port(PortDirection.OUTPUT, output_name, request.root.type)
        module = Module(
            "__ZLangExplore",
            (*ports, output),
            (Assignment(output, request.root),),
            equivalences=request.equivalences,
        )
        canonical = lower(module)
        root = canonical.assignments[0].expression
        saturation = saturate(
            canonical,
            root,
            max_terms=request.bounds.max_value_alternatives,
        )
        candidates = []
        for term in saturation.alternatives:
            expression = term_to_expression(term)
            if request.root.origin is not None:
                expression = replace(expression, origin=request.root.origin)
            candidates.append(
                _candidate(
                    expression,
                    ("value",),
                    estimate_expression_cost(expression),
                    value_relation="M26/M27 same-cycle exact typed equality",
                    provenance=("egglog M26/M27",),
                )
            )
        return candidates, saturation.truncated, []
    except (TypeError, ValueError) as error:
        # Ineligible roots are not a compiler failure: source remains a legal
        # candidate.  Keep the structured reason for explainability.
        return [], False, [RejectedCandidate(None, "value", str(error))]


def _candidate(
    expression: ir_expr.Expression,
    stages: tuple[str, ...],
    cost: CandidateCost,
    *,
    value_relation: str = "exact_source_value",
    timing_relation: TimingRelation | None = None,
    architecture: object | None = None,
    protocol_relation: object | None = None,
    provenance: tuple[str, ...] = (),
) -> ExplorationCandidate:
    semantic = _semantic_identity(expression)
    architecture_identity = getattr(architecture, "identity", None)
    payload = repr((semantic, stages, architecture_identity, timing_info(expression)))
    implementation = sha256(payload.encode()).hexdigest()
    return ExplorationCandidate(
        expression,
        semantic,
        implementation,
        stages,
        cost,
        value_relation,
        timing_relation,
        architecture,
        protocol_relation,
        provenance,
    )


def _semantic_identity(value: object) -> str:
    def normalize(item: object) -> object:
        if is_dataclass(item):
            return (
                type(item).__name__,
                tuple(
                    (field.name, normalize(getattr(item, field.name)))
                    for field in fields(item)
                    if field.name not in {"origin", "source_origin", "instance"}
                ),
            )
        if isinstance(item, tuple):
            return tuple(normalize(child) for child in item)
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, (str, int, float, type(None))):
            return item
        return repr(item)
    return sha256(repr(normalize(value)).encode()).hexdigest()


def _deduplicate(candidates: Iterable[ExplorationCandidate]) -> list[ExplorationCandidate]:
    unique: dict[str, ExplorationCandidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.implementation_identity, candidate)
    # Every producer is deterministic; preserving first occurrence keeps the
    # canonical source candidate first while remaining independent of sets.
    return list(unique.values())


def _bounded_deduplicate(
    candidates: Iterable[ExplorationCandidate],
    stage_limit: int,
    global_limit: int,
) -> tuple[list[ExplorationCandidate], bool]:
    ordered = _deduplicate(candidates)
    limit = min(stage_limit, global_limit)
    return ordered[:limit], len(ordered) > limit


def _merge_completion(
    complete: bool,
    reason: str,
    truncated: bool,
    stage: str,
) -> tuple[bool, str]:
    if truncated:
        return False, f"search truncated at {stage}"
    return complete, reason


def _expression_children(value: ir_expr.Expression) -> tuple[ir_expr.Expression, ...]:
    # Exploration keeps exact reductions compact and follows only a selected
    # implementation.  All other node coverage is owned by the closed-union
    # traversal, so a future expression kind cannot silently become a leaf.
    if isinstance(value, ir_expr.Reduce):
        return (value.collection,)
    return typed_expression_children(
        value,
        policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
    )


def _input_refs(value: ir_expr.Expression) -> dict[str, object]:
    result: dict[str, object] = {}
    def visit(item: ir_expr.Expression) -> None:
        if isinstance(item, ir_expr.InputRef):
            previous = result.get(item.name)
            if previous is not None and previous != item.type:
                raise ValueError(f"input '{item.name}' has inconsistent types")
            result[item.name] = item.type
        for child in _expression_children(item):
            visit(child)
    visit(value)
    return result


def _logic_depth(value: ir_expr.Expression) -> int:
    children = _expression_children(value)
    own = 1 if isinstance(value, (ir_expr.Add, ir_expr.Binary, ir_expr.Mux, ir_expr.Switch)) else 0
    return own + max((_logic_depth(item) for item in children), default=0)


def _timing_text(relation: TimingRelation | None) -> str:
    if relation is None:
        return "same-cycle"
    return f"{relation.kind.value} delta={relation.delta} proof={relation.proof}"
