"""Normalize legacy source policy and external implementation profiles.

This is an orchestration adapter, not a new explorer.  Existing source forms
continue to select candidates in their frozen implementations; this module
gives those forms and external policy one deterministic representation and
detects incompatible attempts to control the same semantic output region.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Iterable

from zlang.exploration import (
    ExplorationContext,
    ExplorationRequest,
    ExplorationResult,
    TransformFamily,
    explore,
)
from zlang.implementation_regions import (
    ImplementationRegion,
    ImplementationRegionError,
    discover_implementation_regions,
    match_implementation_regions,
)
from zlang.implementation_request import (
    ConstraintRelation,
    ImplementationConstraint,
    ImplementationContribution,
    ImplementationObjective,
    ImplementationRequest,
    ObjectiveDirection,
    PolicyOrigin,
    TransformPolicy,
    apply_exact_timing_contract,
    merge_implementation_contributions,
)
from zlang.ir import expressions as ir_expr
from zlang.ir import architectures as ir_architectures
from zlang.ir import pipelines as ir_pipelines
from zlang.ir.module import Module
from zlang.ir.signed_reductions import expression_semantic_identity
from zlang.candidate_sites import module_candidate_owner_identity


@dataclass(frozen=True)
class RegionImplementationPolicy:
    region: ImplementationRegion
    request: ImplementationRequest
    source_form: str | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "region": self.region.to_data(),
            "request": self.request.to_data(),
            "source_form": self.source_form,
        }


@dataclass(frozen=True)
class ModuleImplementationPolicy:
    request: ImplementationRequest
    regions: tuple[RegionImplementationPolicy, ...]

    @property
    def identity(self) -> str:
        from zlang.common import stable_digest

        return stable_digest(self.to_identity_data())

    def to_identity_data(self) -> dict[str, object]:
        return {
            "schema": "zlang-module-implementation-policy-v1",
            "request": self.request.to_identity_data(),
            "regions": [
                {
                    "identity": item.region.identity,
                    "request": item.request.to_identity_data(),
                    "source_form": item.source_form,
                }
                for item in self.regions
            ],
        }

    def to_data(self) -> dict[str, object]:
        return {
            **self.to_identity_data(),
            "regions": [item.to_data() for item in self.regions],
            "identity": self.identity,
        }

    @property
    def report(self) -> str:
        lines = [
            "Implementation policy (zlang-module-implementation-policy-v1)",
            f"request: {self.request.identity}",
            f"backend: {self.request.backend.kind.value if self.request.backend else 'unspecified'}",
            f"target: {self.request.target or 'generic'}",
            f"formal: {self.request.formal_policy.value}",
        ]
        for item in self.regions:
            lines.append(
                f"region {item.region.module_name}.{item.region.output_name}: "
                f"{item.region.identity}; request={item.request.identity}; "
                f"source={item.source_form or 'none'}"
            )
        lines.append(f"policy identity: {self.identity}")
        return "\n".join(lines) + "\n"


def normalize_implementation_policy(
    module: Module,
    *,
    source_module: Module | None = None,
    exploration_results: Iterable[ExplorationResult] = (),
    external_contributions: Iterable[ImplementationContribution] = (),
) -> ModuleImplementationPolicy:
    """Normalize module and per-output policy without changing selected IR."""

    external = tuple(external_contributions)
    global_request = merge_implementation_contributions(*external)
    if module.timing_contract is not None:
        global_request = apply_exact_timing_contract(
            global_request,
            module.timing_contract,
            origin=PolicyOrigin(
                "module timing contract", module.timing_contract.source_origin
            ),
        )

    regions = discover_implementation_regions(module)
    selectors = global_request.regions
    try:
        selected_regions = (
            match_implementation_regions(regions, selectors)
            if selectors else regions
        )
    except ImplementationRegionError:
        raise
    selected_ids = {item.identity for item in selected_regions}
    source = source_module or module
    source_policy = _source_contributions(source, tuple(exploration_results))
    policies: list[RegionImplementationPolicy] = []
    for region in regions:
        source_item = source_policy.get(region.output_name)
        contributions = []
        source_form = None
        if source_item is not None:
            source_form, source_contribution = source_item
            contributions.append(source_contribution)
        if region.identity in selected_ids:
            contributions.extend(external)
        request = merge_implementation_contributions(*contributions)
        if module.timing_contract is not None:
            request = apply_exact_timing_contract(
                request,
                module.timing_contract,
                origin=PolicyOrigin(
                    "module timing contract", module.timing_contract.source_origin
                ),
            )
        policies.append(RegionImplementationPolicy(region, request, source_form))
    return ModuleImplementationPolicy(
        global_request,
        tuple(sorted(policies, key=lambda item: item.region.identity)),
    )


def apply_external_region_exploration(
    module: Module,
    policy: ModuleImplementationPolicy,
    *,
    external_contributions: Iterable[ImplementationContribution],
    formal_config=None,
    formal_verifier=None,
) -> tuple[Module, tuple[ExplorationResult, ...]]:
    """Execute profile/API M34 policy only for plain selected wire regions.

    Legacy source forms have already executed in semantic analysis.  They are
    represented and conflict-checked by :func:`normalize_implementation_policy`
    and are never silently run a second time.
    """

    contributions = tuple(external_contributions)
    active = any(
        item.transforms is not None
        or item.constraints is not None
        or item.objective is not None
        or (
            item.formal_policy is not None
            and str(getattr(item.formal_policy, "value", item.formal_policy)) != "off"
        )
        for item in contributions
    )
    if not active:
        return module, ()

    selected = set(policy.request.regions) or {
        item.region.identity for item in policy.regions
    }
    next_instance = _next_delay_instance(module)

    def allocate() -> int:
        nonlocal next_instance
        result = next_instance
        next_instance += 1
        return result

    assignments = list(module.assignments)
    results: list[ExplorationResult] = []
    by_output = {
        item.region.output_name: item
        for item in policy.regions
        if item.region.identity in selected
    }
    for index, assignment in enumerate(assignments):
        item = by_output.get(assignment.target.name)
        if item is None or item.source_form is not None:
            continue
        request = item.request
        constraints = list(request.unified_constraints())
        if request.exact_timing is not None:
            from zlang.costs import UnifiedConstraint

            constrained = {value.metric for value in constraints}
            if ir_expr.CostMetric.LATENCY not in constrained:
                constraints.append(UnifiedConstraint(
                    ir_expr.CostMetric.LATENCY,
                    minimum=request.exact_timing.latency,
                    maximum=request.exact_timing.latency,
                ))
            if ir_expr.CostMetric.INITIATION_INTERVAL not in constrained:
                constraints.append(UnifiedConstraint(
                    ir_expr.CostMetric.INITIATION_INTERVAL,
                    minimum=request.exact_timing.initiation_interval,
                    maximum=request.exact_timing.initiation_interval,
                ))
        try:
            result = explore(
                ExplorationRequest(
                    assignment.expression,
                    request.transforms.allowed,
                    request.transforms.avoided,
                    tuple(constraints),
                    request.objective.metric,
                    request.evidence_policy,
                    source_origin=assignment.expression.origin,
                    equivalences=module.equivalences,
                    formal_config=formal_config,
                    formal_verifier=formal_verifier,
                ),
                ExplorationContext(
                    assignment.target.name,
                    assignment.expression.type,
                    allocate,
                    module_candidate_owner_identity(module),
                    "external_profile",
                ),
            )
        except ValueError as error:
            from zlang.implementation_request import ImplementationRequestError

            raise ImplementationRequestError(
                f"implementation profile rejected region "
                f"'{module.name}.{assignment.target.name}': {error}"
            ) from error
        assignments[index] = replace(
            assignment, expression=result.selected_candidate.expression
        )
        results.append(result)
    return replace(module, assignments=tuple(assignments)), tuple(results)


def _source_contributions(
    module: Module,
    exploration_results: tuple[ExplorationResult, ...],
) -> dict[str, tuple[str, ImplementationContribution]]:
    result: dict[str, tuple[str, ImplementationContribution]] = {}
    assignments = {
        assignment.target.name: assignment
        for assignment in module.assignments
        if assignment.signal is None and assignment.channel is None
    }

    for output, assignment in assignments.items():
        expression = assignment.expression
        if isinstance(expression, ir_expr.ImplementationChoice):
            policy = expression.cost_policy
            if policy is None:
                continue
            result[output] = (
                "choice(auto)",
                ImplementationContribution(
                    PolicyOrigin("source choice(auto)", expression.origin),
                    transforms=TransformPolicy(
                        (TransformFamily.DSP,)
                        if any(
                            item.kind is ir_expr.ImplementationKind.DSP_MAC
                            for item in expression.alternatives
                        )
                        else ()
                    ),
                    constraints=tuple(
                        ImplementationConstraint(
                            item.metric, ConstraintRelation.MAXIMUM, item.maximum
                        )
                        for item in policy.constraints
                    ),
                    objective=ImplementationObjective(
                        ObjectiveDirection.MINIMIZE, policy.goal
                    ),
                ),
            )

    for item in module.pipeline_explorations:
        constraints = tuple(_pipeline_constraint(value) for value in item.constraints)
        result[item.output] = (
            "pipeline(auto)",
            ImplementationContribution(
                PolicyOrigin("source pipeline(auto)", item.source_expression.origin),
                transforms=TransformPolicy((TransformFamily.PIPELINE,)),
                constraints=constraints,
                objective=ImplementationObjective(
                    ObjectiveDirection.MAXIMIZE, ir_expr.CostMetric.FMAX_EST
                ),
            ),
        )

    for item in module.architecture_explorations:
        # Legacy architecture bounds use compatibility-only parallelism/depth
        # metrics which are not external profile metrics.  The common request
        # records the legal reduction family and leaves those frozen bounds in
        # the existing ArchitectureExploration record.
        result[item.output] = (
            "architecture(auto)",
            ImplementationContribution(
                PolicyOrigin(
                    "source architecture(auto)", item.source_expression.origin
                ),
                transforms=TransformPolicy((TransformFamily.REDUCTION,)),
            ),
        )

    for exploration in exploration_results:
        selected_identity = expression_semantic_identity(
            exploration.selected_candidate.expression
        )
        matching = tuple(
            output
            for output, assignment in assignments.items()
            if expression_semantic_identity(assignment.expression) == selected_identity
        )
        if len(matching) != 1:
            # Output binding is intentionally required for a public policy
            # region.  Ambiguous structural twins remain separately addressable
            # and are not guessed here.
            continue
        request = exploration.request
        direction = (
            ObjectiveDirection.MAXIMIZE
            if request.objective is ir_expr.CostMetric.FMAX_EST
            else ObjectiveDirection.MINIMIZE
        )
        formal_policy = getattr(request.formal_config, "policy", None)
        result[matching[0]] = (
            "explore",
            ImplementationContribution(
                PolicyOrigin("source explore", request.source_origin),
                transforms=TransformPolicy(request.allowed, request.avoided),
                constraints=tuple(
                    _unified_constraint(item) for item in request.constraints
                ),
                objective=ImplementationObjective(direction, request.objective),
                evidence_policy=request.source_policy,
                formal_policy=formal_policy,
            ),
        )
    return result


def _next_delay_instance(value: object) -> int:
    maximum = -1

    def visit(item: object) -> None:
        nonlocal maximum
        if isinstance(item, (ir_expr.Delay, ir_expr.Pipeline)):
            maximum = max(maximum, item.instance)
        if isinstance(item, tuple):
            for child in item:
                visit(child)
        elif is_dataclass(item):
            for child_field in fields(item):
                if child_field.name != "origin":
                    visit(getattr(item, child_field.name))

    visit(value)
    return maximum + 1


def _pipeline_constraint(
    item: ir_pipelines.PipelineConstraint,
) -> ImplementationConstraint:
    metric = {
        ir_pipelines.PipelineMetric.LATENCY: ir_expr.CostMetric.LATENCY,
        ir_pipelines.PipelineMetric.THROUGHPUT: ir_expr.CostMetric.INITIATION_INTERVAL,
        ir_pipelines.PipelineMetric.DSP: ir_expr.CostMetric.DSP,
        ir_pipelines.PipelineMetric.FMAX: ir_expr.CostMetric.FMAX_EST,
    }[item.metric]
    relation = {
        ir_pipelines.PipelineRelation.MAXIMUM: ConstraintRelation.MAXIMUM,
        ir_pipelines.PipelineRelation.MINIMUM: ConstraintRelation.MINIMUM,
        ir_pipelines.PipelineRelation.EXACT: ConstraintRelation.EXACT,
    }[item.relation]
    return ImplementationConstraint(metric, relation, item.value)


def _unified_constraint(item) -> ImplementationConstraint:
    if item.minimum is not None and item.maximum is not None:
        if item.minimum == item.maximum:
            return ImplementationConstraint(
                item.metric, ConstraintRelation.EXACT, item.minimum
            )
        raise ValueError(
            "a bounded minimum/maximum pair cannot be represented as one "
            "implementation constraint"
        )
    if item.minimum is not None:
        return ImplementationConstraint(
            item.metric, ConstraintRelation.MINIMUM, item.minimum
        )
    if item.maximum is not None:
        return ImplementationConstraint(
            item.metric, ConstraintRelation.MAXIMUM, item.maximum
        )
    raise ValueError("empty unified implementation constraint")


__all__ = [
    "ModuleImplementationPolicy",
    "RegionImplementationPolicy",
    "apply_external_region_exploration",
    "normalize_implementation_policy",
]
