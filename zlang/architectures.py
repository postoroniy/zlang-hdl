"""Bounded and reproducible FIR-like architecture exploration."""

from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.architectures import (
    ArchitectureCandidate,
    ArchitectureConstraint,
    ArchitectureEquivalence,
    ArchitectureExploration,
    ArchitectureMetric,
    FirArchitectureKind,
)
from zlang.ir.module import Module
from zlang.ir.product_reductions import (
    add_integer_terms,
    balanced_tree,
    coerce_integer_result,
    linear_tree,
    sum_fits,
    validate_full_precision_products,
)
from zlang.ir.types import HardwareType, SIntType, UIntType
from zlang.costs import CandidateCost, extract_best


ARCHITECTURE_MODEL = "zlang-fir-architecture-v1"
MAX_ARCHITECTURE_CANDIDATES = 32
MAX_FIR_TERMS = 32


class ArchitectureExplorationError(ValueError):
    """An automatic architecture region or bound is unsupported."""


def explore_architecture(
    output: str,
    expression: expr.Expression,
    result_type: HardwareType,
    constraints: tuple[ArchitectureConstraint, ...],
) -> ArchitectureExploration:
    """Explore direct, transposed, and folded-lane FIR reductions."""

    _validate_constraints(constraints)
    products = _flatten_products(expression)
    if len(products) < 4:
        raise ArchitectureExplorationError(
            "architecture(auto) requires a FIR-like sum of at least four products"
        )
    if len(products) > MAX_FIR_TERMS:
        raise ArchitectureExplorationError(
            f"architecture(auto) supports at most {MAX_FIR_TERMS} product terms"
        )
    if not isinstance(result_type, (UIntType, SIntType)):
        raise ArchitectureExplorationError(
            "architecture(auto) requires an integer scalar result"
        )
    validate_full_precision_products(
        products,
        result_type,
        error_type=ArchitectureExplorationError,
        context="architecture(auto)",
    )
    if not sum_fits(products, result_type):
        raise ArchitectureExplorationError(
            f"architecture(auto) cannot prove reassociation lossless at {result_type}"
        )

    limits = {constraint.metric: constraint.maximum for constraint in constraints}
    candidate_limit = limits.get(
        ArchitectureMetric.CANDIDATES,
        MAX_ARCHITECTURE_CANDIDATES,
    )
    specifications = [
        (
            "direct",
            FirArchitectureKind.DIRECT,
            len(products),
            balanced_tree(products, error_type=ArchitectureExplorationError),
            ("preserve_product_order", "balanced_direct_reduction"),
        ),
        (
            "transposed",
            FirArchitectureKind.TRANSPOSED,
            1,
            _transposed_tree(products),
            ("preserve_product_order", "transposed_accumulator_chain"),
        ),
        *(
            (
                f"folded_p{parallelism}",
                FirArchitectureKind.FOLDED,
                parallelism,
                _folded_tree(products, parallelism),
                (
                    "preserve_product_order",
                    f"partition_{parallelism}_accumulator_lanes",
                    "balanced_lane_reduction",
                ),
            )
            for parallelism in range(2, len(products))
        ),
    ]
    theoretical = len(specifications)
    bounded = specifications[:candidate_limit]
    candidates = tuple(
        _candidate(
            name,
            kind,
            parallelism,
            tree,
            transformations,
            products,
            result_type,
            constraints,
        )
        for name, kind, parallelism, tree, transformations in bounded
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
    legal = tuple(candidate for candidate in candidates if candidate.legal)
    if not legal:
        failures = "; ".join(
            f"{candidate.name} violates {', '.join(candidate.violations)}"
            for candidate in candidates
        )
        raise ArchitectureExplorationError(
            f"no legal FIR architecture for output '{output}': {failures}"
        )
    selected = extract_best(
        legal,
        objective="fmax_est",
        cost_fn=_architecture_cost,
    ).selected
    return ArchitectureExploration(
        output=output,
        result_type=result_type,
        source_expression=expression,
        constraints=constraints,
        candidates=candidates,
        selected=selected.name,
        theoretical_candidates=theoretical,
        search_bound=len(candidates),
        budget_pruned=theoretical - len(candidates),
        constraint_pruned=sum(not candidate.legal for candidate in candidates),
    )


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
            "reason=m28_maximize_estimated_frequency_with_deterministic_tie_break "
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


def _validate_constraints(
    constraints: tuple[ArchitectureConstraint, ...],
) -> None:
    metrics = [constraint.metric for constraint in constraints]
    if len(metrics) != len(set(metrics)):
        duplicate = next(metric for metric in metrics if metrics.count(metric) > 1)
        raise ArchitectureExplorationError(
            f"architecture(auto) repeats '{duplicate.value}' constraint"
        )
    for constraint in constraints:
        if constraint.maximum < 1:
            raise ArchitectureExplorationError(
                f"architecture constraint '{constraint.metric.value}' must be positive"
            )
        if (
            constraint.metric is ArchitectureMetric.CANDIDATES
            and constraint.maximum < 3
        ):
            raise ArchitectureExplorationError(
                "architecture candidate bound must be at least 3 to explore "
                "direct, transposed, and folded forms"
            )
        if (
            constraint.metric is ArchitectureMetric.CANDIDATES
            and constraint.maximum > MAX_ARCHITECTURE_CANDIDATES
        ):
            raise ArchitectureExplorationError(
                "architecture candidate bound exceeds the hard maximum of "
                f"{MAX_ARCHITECTURE_CANDIDATES}"
            )


def _candidate(
    name: str,
    kind: FirArchitectureKind,
    parallelism: int,
    tree: expr.Expression,
    transformations: tuple[str, ...],
    products: tuple[expr.Binary, ...],
    result_type: UIntType | SIntType,
    constraints: tuple[ArchitectureConstraint, ...],
) -> ArchitectureCandidate:
    expression = coerce_integer_result(
        tree, result_type, error_type=ArchitectureExplorationError
    )
    candidate = ArchitectureCandidate(
        name=name,
        expression=expression,
        kind=kind,
        parallelism=parallelism,
        add_depth=_add_depth(tree),
        multiplier_count=len(products),
        adder_count=len(products) - 1,
        transformations=transformations,
        equivalence=ArchitectureEquivalence.MATHEMATICAL,
    )
    values = {
        ArchitectureMetric.PARALLELISM: candidate.parallelism,
        ArchitectureMetric.DEPTH: candidate.add_depth,
    }
    violations = tuple(
        f"{constraint.metric.value}={values[constraint.metric]} "
        f"not <={constraint.maximum}"
        for constraint in constraints
        if constraint.metric is not ArchitectureMetric.CANDIDATES
        and values[constraint.metric] > constraint.maximum
    )
    return replace(candidate, violations=violations)


def _flatten_products(expression: expr.Expression) -> tuple[expr.Binary, ...]:
    terms: list[expr.Expression] = []

    def visit(node: expr.Expression) -> None:
        if isinstance(node, expr.Add):
            visit(node.left)
            visit(node.right)
        else:
            terms.append(node)

    visit(expression)
    if not all(
        isinstance(term, expr.Binary)
        and term.operator is expr.BinaryOperator.MULTIPLY
        for term in terms
    ):
        raise ArchitectureExplorationError(
            "architecture(auto) accepts only a FIR-like sum of full-precision products"
        )
    return tuple(term for term in terms if isinstance(term, expr.Binary))


def _transposed_tree(products: tuple[expr.Binary, ...]) -> expr.Expression:
    result: expr.Expression = products[-1]
    for product in reversed(products[:-1]):
        result = add_integer_terms(
            product, result, error_type=ArchitectureExplorationError
        )
    return result


def _folded_tree(
    products: tuple[expr.Binary, ...], parallelism: int
) -> expr.Expression:
    lanes = tuple(
        linear_tree(
            products[lane::parallelism], error_type=ArchitectureExplorationError
        )
        for lane in range(parallelism)
        if products[lane::parallelism]
    )
    return balanced_tree(lanes, error_type=ArchitectureExplorationError)


def _add_depth(expression: expr.Expression) -> int:
    if isinstance(expression, expr.Add):
        return 1 + max(_add_depth(expression.left), _add_depth(expression.right))
    if isinstance(expression, (expr.Extend, expr.Truncate, expr.FixedConvert)):
        return _add_depth(expression.expression)
    return 0
