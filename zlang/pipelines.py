"""Bounded, explainable automatic pipeline exploration."""

from __future__ import annotations

from dataclasses import replace
from math import log2
from typing import Callable

from zlang.ir import expressions as expr
from zlang.ir.interfaces import ReadyValidSignal
from zlang.ir.module import Module
from zlang.ir.pipelines import (
    MultiplierMapping,
    PipelineCandidate,
    PipelineConstraint,
    PipelineCostSource,
    PipelineEstimate,
    PipelineExploration,
    PipelineMetric,
    PipelineRelation,
    PipelineTree,
    RegisterPlacement,
    PipelinePlan,
)
from zlang.ir.product_reductions import (
    add_integer_terms,
    balanced_tree,
    coerce_integer_result,
    linear_tree,
    sum_fits,
    validate_full_precision_products,
)
from zlang.ir.types import FixedType, HardwareType, SIntType, UFixedType, UIntType
from zlang.ir.signed_reductions import recognize_signed_product_reduction
from zlang.costs import CandidateCost, UnifiedConstraint, extract_best
from zlang.timing import validate_timed_candidate


PIPELINE_COST_MODEL = "zlang-pipeline-estimate-v1"
_FREQUENCY_NUMERATOR_MHZ = 800


class PipelineExplorationError(ValueError):
    """An automatic pipeline region or its constraints are unsupported."""


def explore_pipeline(
    output: str,
    expression: expr.Expression,
    result_type: HardwareType,
    constraints: tuple[PipelineConstraint, ...],
    allocate_instance: Callable[[], int],
) -> PipelineExploration:
    """Generate, validate, and select a bounded multi-product pipeline."""

    _validate_constraints(constraints)
    if isinstance(result_type, (FixedType, UFixedType)):
        return _explore_fixed_output(
            output, expression, result_type, constraints, allocate_instance,
        )
    products = _flatten_products(expression)
    if len(products) < 4:
        raise PipelineExplorationError(
            "pipeline(auto) requires a sum of at least four products"
        )
    if len(products) & (len(products) - 1):
        raise PipelineExplorationError(
            "pipeline(auto) currently requires a power-of-two product count"
        )
    if not isinstance(result_type, (UIntType, SIntType)):
        raise PipelineExplorationError(
            "pipeline(auto) currently requires an integer scalar result"
        )
    validate_full_precision_products(
        products,
        result_type,
        error_type=PipelineExplorationError,
        context="pipeline(auto)",
    )
    if not sum_fits(products, result_type):
        raise PipelineExplorationError(
            f"pipeline(auto) cannot prove reassociation lossless at {result_type}"
        )

    linear = coerce_integer_result(
        linear_tree(products, error_type=PipelineExplorationError),
        result_type,
        error_type=PipelineExplorationError,
    )
    balanced = coerce_integer_result(
        balanced_tree(products, error_type=PipelineExplorationError),
        result_type,
        error_type=PipelineExplorationError,
    )
    depth = 1 + int(log2(len(products)))
    logic_lut = _logic_lut(products, result_type)
    add_lut = result_type.width * (len(products) - 1)

    candidates = (
        _candidate(
            "linear_output_logic",
            _output_pipeline(linear, allocate_instance),
            PipelineTree.LINEAR,
            RegisterPlacement.OUTPUT,
            MultiplierMapping.LOGIC,
            ("preserve_linear_tree", "register_output"),
            1,
            logic_lut,
            result_type.width,
            0,
            _frequency(3 + len(products) - 1),
            constraints,
        ),
        _candidate(
            "balanced_output_logic",
            _output_pipeline(balanced, allocate_instance),
            PipelineTree.BALANCED,
            RegisterPlacement.OUTPUT,
            MultiplierMapping.LOGIC,
            ("reassociate_balanced_tree", "register_output", "latency_balance"),
            1,
            logic_lut,
            result_type.width,
            0,
            _frequency(depth + 2),
            constraints,
        ),
        _candidate(
            "balanced_levels_logic",
            coerce_integer_result(
                _registered_balanced_tree(products, allocate_instance),
                result_type,
                error_type=PipelineExplorationError,
            ),
            PipelineTree.BALANCED,
            RegisterPlacement.BALANCED_LEVELS,
            MultiplierMapping.LOGIC,
            (
                "reassociate_balanced_tree",
                "register_each_operator_level",
                "latency_balance",
            ),
            depth,
            logic_lut,
            result_type.width * depth,
            0,
            _frequency(3),
            constraints,
        ),
        _candidate(
            "balanced_output_dsp",
            _output_pipeline(balanced, allocate_instance),
            PipelineTree.BALANCED,
            RegisterPlacement.OUTPUT,
            MultiplierMapping.DSP,
            (
                "reassociate_balanced_tree",
                "map_products_to_dsp",
                "register_output",
                "latency_balance",
            ),
            1,
            add_lut,
            result_type.width,
            len(products),
            _frequency(depth),
            constraints,
        ),
        _candidate(
            "balanced_levels_dsp",
            coerce_integer_result(
                _registered_balanced_tree(products, allocate_instance),
                result_type,
                error_type=PipelineExplorationError,
            ),
            PipelineTree.BALANCED,
            RegisterPlacement.BALANCED_LEVELS,
            MultiplierMapping.DSP,
            (
                "reassociate_balanced_tree",
                "map_products_to_dsp",
                "register_each_operator_level",
                "latency_balance",
            ),
            depth,
            add_lut,
            result_type.width * depth,
            len(products),
            _frequency(1),
            constraints,
        ),
    )
    if expression.origin is not None:
        candidates = tuple(
            replace(
                candidate,
                expression=replace(
                    candidate.expression,
                    origin=expression.origin,
                ),
            )
            for candidate in candidates
        )
    for candidate in candidates:
        actual_latency = _expression_latency(candidate.expression)
        if actual_latency != candidate.latency:
            raise PipelineExplorationError(
                f"candidate '{candidate.name}' reports latency "
                f"{candidate.latency} but has cycle latency {actual_latency}"
            )
        if candidate.expression.type != result_type:
            raise PipelineExplorationError(
                f"candidate '{candidate.name}' has type "
                f"{candidate.expression.type}, expected {result_type}"
            )

    for candidate in candidates:
        relation = validate_timed_candidate(
            expression, candidate.expression,
            value_equivalent=True,
        )
        if not relation.equivalent:
            raise PipelineExplorationError(
                f"candidate '{candidate.name}' failed timed-equivalence validation: {relation.proof}"
            )
    unified_constraints = tuple(_unified_constraint(item) for item in constraints)
    try:
        extraction = extract_best(
            candidates,
            objective="fmax_est",
            constraints=unified_constraints,
            cost_fn=lambda candidate: CandidateCost.estimate(
                lut=candidate.estimate.lut,
                ff=candidate.estimate.ff,
                dsp=candidate.estimate.dsp,
                latency=candidate.latency,
                ii=candidate.initiation_interval,
                fmax_est=candidate.estimate.fmax_mhz,
                structural_cost=len(candidate.transformations),
            ),
        )
    except ValueError as error:
        failures = "; ".join(
            f"{candidate.name} violates {', '.join(candidate.violations) or 'unified constraints'}"
            for candidate in candidates
        )
        raise PipelineExplorationError(
            f"no legal pipeline architecture for output '{output}': {failures}"
        ) from error
    selected = extraction.selected
    return PipelineExploration(
        output,
        result_type,
        expression,
        constraints,
        candidates,
        selected.name,
        len(candidates),
    )


def _unified_constraint(constraint: PipelineConstraint) -> UnifiedConstraint:
    from zlang.ir.expressions import CostMetric
    if constraint.metric is PipelineMetric.LATENCY:
        return UnifiedConstraint(CostMetric.LATENCY, maximum=constraint.value)
    if constraint.metric is PipelineMetric.THROUGHPUT:
        return UnifiedConstraint(CostMetric.INITIATION_INTERVAL, maximum=constraint.value,
                                 minimum=constraint.value)
    if constraint.metric is PipelineMetric.DSP:
        return UnifiedConstraint(CostMetric.DSP, maximum=constraint.value)
    return UnifiedConstraint(CostMetric.FMAX_EST, minimum=constraint.value)


def render_pipeline_report(module: Module) -> str:
    """Explain bounded candidates, legality, transforms, and selection."""

    if not module.pipeline_explorations and not module.elastic_pipeline_regions:
        return ""
    lines = [
        f"module {module.name}",
        f"pipeline_model={PIPELINE_COST_MODEL}+m31-unified cost_source=structural_estimate measured=false",
    ]
    for exploration in module.pipeline_explorations:
        constraints = ",".join(
            constraint.render() for constraint in exploration.constraints
        )
        lines.append(
            f"exploration output={exploration.output} result={exploration.result_type} "
            f"constraints=[{constraints}] bounded=true "
            f"search_bound={exploration.search_bound}"
        )
        for candidate in exploration.candidates:
            transformations = ",".join(candidate.transformations)
            violations = ",".join(candidate.violations) or "none"
            lines.append(
                f"  candidate name={candidate.name} legal="
                f"{str(candidate.legal).lower()} tree={candidate.tree.value} "
                f"registers={candidate.register_placement.value} "
                f"multipliers={candidate.multiplier_mapping.value} "
                f"latency={candidate.latency} "
                f"initiation_interval={candidate.initiation_interval} "
                f"transformations=[{transformations}] estimated "
                f"lut={candidate.estimate.lut} ff={candidate.estimate.ff} "
                f"dsp={candidate.estimate.dsp} "
                f"fmax_mhz={candidate.estimate.fmax_mhz} "
                f"cost_source={candidate.cost_source.value} "
                f"violations=[{violations}]"
            )
        selected = exploration.selected_candidate
        lines.append(
            f"  selected name={selected.name} reason="
            "best_candidate_in_explored_bounded_search maximize_fmax_est_then_latency "
            f"latency={selected.latency} "
            f"initiation_interval={selected.initiation_interval} "
            f"cost_source={selected.cost_source.value} timed_equivalence=verified "
            f"pipeline_plan_registers={selected.pipeline_plan.inserted_registers}"
        )
        if exploration.formal_records:
            lines.append(
                f"  formal records={len(exploration.formal_records)}"
            )
            lines.extend(
                f"    candidate {record}"
                for record in exploration.formal_records
            )
    for region in module.elastic_pipeline_regions:
        constraints = ",".join(item.render() for item in region.constraints)
        lines.append(
            f"elastic_region id={region.semantic_id} "
            f"source={region.source_endpoint} destination={region.destination_endpoint} "
            f"constraints=[{constraints}] selected={region.selected} "
            f"minimum_unstalled_latency={region.timing.minimum_unstalled_latency} "
            f"ii_no_stall={region.timing.ii_no_stall} "
            f"capacity={region.timing.capacity} wall_clock_latency=variable "
            f"stall_policy={region.timing.stall_policy.value} "
            f"valid_ff_estimate={region.plan.valid_stage_count} "
            f"ready_control_lut_estimate={region.plan.ready_control_lut_estimate} "
            "m30_relation=not_applicable m36=unsupported m38=unsupported"
        )
        selected = region.selected_candidate
        lines.append(
            "  selected_estimate "
            f"lut={selected.estimate.lut} ff={selected.estimate.ff} "
            f"dsp={selected.estimate.dsp} fmax_mhz={selected.estimate.fmax_mhz} "
            f"cost_source={selected.cost_source.value}"
        )
        for record in region.formal_records:
            lines.append(f"  formal {record}")
    return "\n".join(lines) + "\n"


def _candidate(
    name: str,
    expression: expr.Expression,
    tree: PipelineTree,
    placement: RegisterPlacement,
    mapping: MultiplierMapping,
    transformations: tuple[str, ...],
    latency: int,
    lut: int,
    ff: int,
    dsp: int,
    fmax_mhz: int,
    constraints: tuple[PipelineConstraint, ...],
) -> PipelineCandidate:
    estimate = PipelineEstimate(lut, ff, dsp, fmax_mhz)
    candidate = PipelineCandidate(
        name,
        expression,
        tree,
        placement,
        mapping,
        transformations,
        latency,
        1,
        estimate,
        PipelineCostSource.ESTIMATE,
        pipeline_plan=PipelinePlan(
            tuple(transformations),
            latency,
        ),
    )
    return replace(candidate, violations=_violations(candidate, constraints))


def _validate_constraints(constraints: tuple[PipelineConstraint, ...]) -> None:
    metrics = [constraint.metric for constraint in constraints]
    if len(metrics) != len(set(metrics)):
        duplicate = next(metric for metric in metrics if metrics.count(metric) > 1)
        raise PipelineExplorationError(
            f"pipeline(auto) repeats '{duplicate.value}' constraint"
        )
    required_relations = {
        PipelineMetric.THROUGHPUT: PipelineRelation.EXACT,
        PipelineMetric.DSP: PipelineRelation.MAXIMUM,
        PipelineMetric.FMAX: PipelineRelation.MINIMUM,
    }
    for constraint in constraints:
        if constraint.metric is PipelineMetric.LATENCY:
            if constraint.relation not in {
                PipelineRelation.MAXIMUM, PipelineRelation.EXACT,
            }:
                raise PipelineExplorationError(
                    "pipeline constraint 'latency' requires '<=' or '=='"
                )
            if constraint.value < 1:
                raise PipelineExplorationError(
                    "pipeline constraint 'latency' must be positive"
                )
            continue
        required = required_relations[constraint.metric]
        if constraint.relation is not required:
            raise PipelineExplorationError(
                f"pipeline constraint '{constraint.metric.value}' requires "
                f"'{required.value}', got '{constraint.relation.value}'"
            )
        if constraint.metric in {PipelineMetric.THROUGHPUT, PipelineMetric.FMAX}:
            if constraint.value < 1:
                raise PipelineExplorationError(
                    f"pipeline constraint '{constraint.metric.value}' must be positive"
                )


def _explore_fixed_output(
    output: str,
    expression: expr.Expression,
    result_type: FixedType | UFixedType,
    constraints: tuple[PipelineConstraint, ...],
    allocate_instance: Callable[[], int],
) -> PipelineExploration:
    """Compatibility candidate for exact fixed reductions.

    Target-aware planning consumes ``source_expression`` and replaces this
    generic output-boundary plan with resource-local cuts.  Keeping one generic
    candidate here preserves targetless compilation and the old backend path.
    """
    if not isinstance(expression, expr.FixedConvert) or expression.type != result_type:
        raise PipelineExplorationError(
            "fixed pipeline(auto) requires one explicit final FixedConvert"
        )
    reduction = recognize_signed_product_reduction(expression.expression)
    if reduction is None:
        products = _legacy_positive_fixed_products(expression.expression)
    else:
        products = tuple(term.product_expression for term in reduction.terms)
    if len(products) < 2:
        raise PipelineExplorationError(
            "fixed pipeline(auto) requires a full-precision product reduction"
        )
    exact = next((item.value for item in constraints
                  if item.metric is PipelineMetric.LATENCY
                  and item.relation is PipelineRelation.EXACT), None)
    latency = exact or 1
    candidate_expression = expr.Pipeline(
        latency, expression, allocate_instance(), result_type,
    )
    # This is deliberately only structural evidence.  Target planning may
    # replace it with synthesis/routed evidence but must not promote it.
    lut = sum(item.left.type.width * item.right.type.width for item in products)
    lut += expression.expression.type.width * max(0, len(products) - 1)
    preservation = (
        "preserve_exact_signed_product_reduction"
        if reduction is not None and reduction.has_subtraction
        else "preserve_exact_fixed_reduction"
    )
    candidate = _candidate(
        "fixed_output_generic",
        candidate_expression,
        PipelineTree.LINEAR,
        RegisterPlacement.OUTPUT,
        MultiplierMapping.LOGIC,
        (preservation, "preserve_original_join_tree", "register_output_boundary"),
        latency,
        lut,
        result_type.width * latency,
        0,
        100,
        constraints,
    )
    relation = validate_timed_candidate(
        expression, candidate.expression, value_equivalent=True,
    )
    if not relation.equivalent:
        raise PipelineExplorationError(
            f"fixed generic candidate failed timed validation: {relation.proof}"
        )
    if candidate.violations:
        raise PipelineExplorationError(
            "no legal generic fixed pipeline candidate: "
            + ", ".join(candidate.violations)
        )
    return PipelineExploration(
        output, result_type, expression, constraints, (candidate,),
        candidate.name, 1,
    )


def _legacy_positive_fixed_products(value: expr.Expression) -> tuple[expr.Binary, ...]:
    """Preserve the pre-freeze positive M32 accumulator-resize recognition."""

    terms: list[expr.Expression] = []

    def visit(node: expr.Expression) -> None:
        if isinstance(node, expr.Add):
            visit(node.left)
            visit(node.right)
            return
        if isinstance(node, (expr.Extend, expr.Truncate, expr.FixedConvert)):
            visit(node.expression)
            return
        terms.append(node)

    visit(value)
    if not all(
        isinstance(item, expr.Binary)
        and item.operator is expr.BinaryOperator.MULTIPLY
        for item in terms
    ):
        return ()
    return tuple(item for item in terms if isinstance(item, expr.Binary))


def _violations(
    candidate: PipelineCandidate,
    constraints: tuple[PipelineConstraint, ...],
) -> tuple[str, ...]:
    values = {
        PipelineMetric.LATENCY: candidate.latency,
        PipelineMetric.THROUGHPUT: candidate.throughput,
        PipelineMetric.DSP: candidate.estimate.dsp,
        PipelineMetric.FMAX: candidate.estimate.fmax_mhz,
    }
    violations: list[str] = []
    for constraint in constraints:
        actual = values[constraint.metric]
        satisfied = {
            PipelineRelation.MAXIMUM: actual <= constraint.value,
            PipelineRelation.EXACT: actual == constraint.value,
            PipelineRelation.MINIMUM: actual >= constraint.value,
        }[constraint.relation]
        if not satisfied:
            violations.append(
                f"{constraint.metric.value}={actual} "
                f"not {constraint.relation.value}{constraint.value}"
            )
    return tuple(violations)


def _flatten_products(expression: expr.Expression) -> tuple[expr.Binary, ...]:
    terms: list[expr.Expression] = []

    def visit(node: expr.Expression) -> None:
        if isinstance(node, expr.Add):
            visit(node.left)
            visit(node.right)
        elif isinstance(node, (expr.Extend, expr.Truncate, expr.FixedConvert)) and isinstance(
            node.expression, expr.Binary
        ) and node.expression.operator is expr.BinaryOperator.MULTIPLY:
            # M32 reduction candidates explicitly coerce every product to the
            # canonical accumulator width.  The product itself remains the
            # full-precision value that M31 validates and rebuilds.
            terms.append(node.expression)
        else:
            terms.append(node)

    visit(expression)
    if not all(
        isinstance(term, expr.Binary)
        and term.operator is expr.BinaryOperator.MULTIPLY
        for term in terms
    ):
        raise PipelineExplorationError(
            "pipeline(auto) currently accepts only a sum of full-precision products"
        )
    return tuple(term for term in terms if isinstance(term, expr.Binary))


def _registered_balanced_tree(
    products: tuple[expr.Binary, ...],
    allocate_instance: Callable[[], int],
) -> expr.Expression:
    level: tuple[expr.Expression, ...] = tuple(
        expr.Pipeline(1, product, allocate_instance(), product.type)
        for product in products
    )
    while len(level) > 1:
        next_level: list[expr.Expression] = []
        for index in range(0, len(level), 2):
            summed = add_integer_terms(
                level[index], level[index + 1], error_type=PipelineExplorationError
            )
            next_level.append(
                expr.Pipeline(1, summed, allocate_instance(), summed.type)
            )
        level = tuple(next_level)
    return level[0]


def _output_pipeline(
    expression: expr.Expression,
    allocate_instance: Callable[[], int],
) -> expr.Pipeline:
    return expr.Pipeline(1, expression, allocate_instance(), expression.type)


def _logic_lut(
    products: tuple[expr.Binary, ...],
    result_type: UIntType | SIntType,
) -> int:
    multiply = sum(
        product.left.type.width * product.right.type.width
        for product in products
    )
    return multiply + result_type.width * (len(products) - 1)


def _frequency(delay_units: int) -> int:
    return _FREQUENCY_NUMERATOR_MHZ // delay_units


def _expression_latency(expression: expr.Expression) -> int:
    if isinstance(expression, (expr.InputRef, expr.ParameterRef, expr.Constant)):
        return 0
    if (
        isinstance(expression, expr.ReadyValidRef)
        and expression.signal is ReadyValidSignal.PAYLOAD
    ):
        return 0
    if isinstance(expression, (expr.Add, expr.Binary)):
        left = _expression_latency(expression.left)
        right = _expression_latency(expression.right)
        if left != right:
            raise PipelineExplorationError(
                f"generated candidate is cycle-misaligned: {left} versus {right}"
            )
        return left
    if isinstance(
        expression,
        (
            expr.Extend,
            expr.Truncate,
            expr.FixedConvert,
            expr.FieldAccess,
            expr.VectorIndex,
            expr.Slice,
            expr.Bitcast,
            expr.Reshape,
            expr.Pack,
            expr.Unpack,
        ),
    ):
        return _expression_latency(expression.expression)
    if isinstance(expression, (expr.Concat, expr.VectorConcat)):
        latencies = tuple(
            _expression_latency(operand) for operand in expression.operands
        )
        if len(set(latencies)) != 1:
            raise PipelineExplorationError(
                "generated concatenation operands are cycle-misaligned: "
                + " versus ".join(str(latency) for latency in latencies)
            )
        return latencies[0]
    if isinstance(expression, expr.RuntimeIndex):
        vector_latency = _expression_latency(expression.expression)
        index_latency = _expression_latency(expression.index)
        if vector_latency != index_latency:
            raise PipelineExplorationError(
                "runtime vector index operands are cycle-misaligned: "
                f"{vector_latency} versus {index_latency}"
            )
        return vector_latency
    if isinstance(expression, expr.VectorUpdate):
        latencies = (
            _expression_latency(expression.expression),
            _expression_latency(expression.index),
            _expression_latency(expression.value),
        )
        if len(set(latencies)) != 1:
            raise PipelineExplorationError(
                "vector update operands are cycle-misaligned: "
                + " versus ".join(str(latency) for latency in latencies)
            )
        return latencies[0]
    if isinstance(expression, expr.Pipeline):
        return _expression_latency(expression.expression) + expression.stages
    raise PipelineExplorationError(
        f"generated candidate contains unsupported node {type(expression).__name__}"
    )
