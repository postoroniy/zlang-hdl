"""Selection and formal-aware planning algorithms for compilation sessions."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Iterable

from zlang import candidate_sites
from zlang import costs
from zlang import formal_exploration
from zlang import implementation_policy as implementation_policy_api
from zlang import implementation_plans
from zlang.candidate_identity import pipeline_site_key
from zlang.exploration import ExplorationResult
from zlang.formal_temporal_stream import build_capacity_one_transaction_relation
from zlang.compilation_products import (
    AnalysisProduct,
    PlanningProduct,
    SelectionProduct,
)
from zlang.ir import expressions as ir_expr
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.ir.module import Module as IrModule
from zlang.ir.signed_reductions import selection_expression_semantic_identity
from zlang.opt.ir import CanonicalModule, OptimizationStage
from zlang.opt.module_lowering import lower
from zlang.opt.module_restoration import restore
from zlang.opt.render import canonical_identity_matches
from zlang.semantic import SemanticError
from zlang.target_planner import TargetPlanningResult


_UNSUPPORTED_RESET_FORMAL_REASON = (
    "formal-aware selection authoritative semantic-reference equivalence route "
    "requires one physical domain with power_up unspecified"
)


def _nondefault_reset_formal_record(
    candidate_identity: str,
    config: formal_exploration.FormalExplorationConfig,
    origin=None,
    *,
    reason: str = _UNSUPPORTED_RESET_FORMAL_REASON,
) -> formal_exploration.FormalExplorationRecord:
    """Record why fixed-latency equivalence cannot model this reset contract."""

    return formal_exploration.FormalExplorationRecord(
        candidate_identity=candidate_identity,
        rank=1,
        semantic_legality="typed_legal",
        formal_route="unsupported_physical_reset_contract",
        policy=formal_exploration.FormalPolicy.AVAILABLE,
        mode=ProofMode.BMC,
        depth=config.bmc_depth,
        status=FormalStatus.SKIPPED,
        cache_state=formal_exploration.CACHE_STATE_NOT_RUN,
        eligible=True,
        reason=reason,
        source_origin=origin,
    )



def candidate_site_ledger(
    module: IrModule,
    exploration_results: Iterable[ExplorationResult],
) -> candidate_sites.CandidateSiteLedger:
    """Build one ledger while preserving established source diagnostics."""

    try:
        return candidate_sites.build_candidate_site_ledger(
            module, exploration_results
        )
    except costs.CostExtractionError:
        costs.extract_estimated_costs(module)
        raise
    except candidate_sites.CandidateSiteError as error:
        raise SemanticError(str(error)) from error


class SelectionBuilder:
    """Build the selected typed/canonical products from explicit inputs."""

    def build(
        self,
        analysis: AnalysisProduct,
        configured_formal: formal_exploration.FormalExplorationConfig,
        formal_verifier: object | None,
        target: str | None,
        contributions: tuple[object, ...],
    ) -> SelectionProduct:
        semantic_ir = analysis.module
        exploration_results = analysis.exploration_results
        static_ledger = candidate_site_ledger(
            semantic_ir, exploration_results
        )
        static_only = tuple(
            item for item in static_ledger.sites
            if item.rewrite_kind is candidate_sites.CandidateRewriteKind.STATIC_ONLY
        )
        if (
            static_only
            and configured_formal.policy in {
                formal_exploration.FormalPolicy.REQUIRED_BMC,
                formal_exploration.FormalPolicy.REQUIRED_PROVEN,
            }
        ):
            kinds = ", ".join(sorted({item.kind.value for item in static_only}))
            raise SemanticError(
                "formal-required policy cannot gate retained candidate sites "
                f"without an formal-aware selection evidence attachment: {kinds}"
            )
        if configured_formal.policy is not formal_exploration.FormalPolicy.OFF:
            has_elastic = any(
                item.kind.value == "elastic_pipeline"
                for item in static_ledger.sites
            )
            if has_elastic and configured_formal.policy in {
                formal_exploration.FormalPolicy.REQUIRED_BMC,
                formal_exploration.FormalPolicy.REQUIRED_PROVEN,
            }:
                raise SemanticError(
                    "formal-required policy has no semantic-reference equivalence route for variable-latency "
                    "transform pipeline(auto); safety verification ready/valid safety remains available"
                )
            try:
                defer_physical = (
                    defer_one_root_pipeline_to_physical_formal_selection(
                        semantic_ir
                    )
                    and len(exploration_results) == 1
                    and exploration_results[0].site_kind == "implement"
                    and target not in {None, "generic"}
                )
                if not defer_physical:
                    semantic_ir, exploration_results = candidate_sites.gate_retained_explorations(
                        semantic_ir,
                        exploration_results,
                        configured_formal,
                        formal_verifier,
                        backend=configured_formal.backend,
                    )
                else:
                    # Policy belongs to the implementation site even though
                    # execution is deferred until target planning has formed
                    # the complete value+schedule+resource candidates.
                    exploration_results = tuple(
                        replace(
                            item,
                            request=replace(
                                item.request,
                                formal_config=configured_formal,
                                formal_verifier=None,
                            ),
                        )
                        for item in exploration_results
                    )
                semantic_ir = attach_unified_pipeline_formal_records(
                    semantic_ir,
                    exploration_results,
                )
                semantic_ir = candidate_sites.gate_structured_candidate_sites(
                    semantic_ir,
                    configured_formal,
                    formal_verifier,
                    backend=configured_formal.backend,
                )
            except ValueError as error:
                raise SemanticError(str(error)) from error
            if has_elastic and configured_formal.policy is formal_exploration.FormalPolicy.AVAILABLE:
                semantic_ir = with_elastic_formal_records(
                    semantic_ir,
                    configured_formal,
                    nondefault_reset=False,
                )
        backend_ir = inline_locals(semantic_ir)
        implementation_policy = implementation_policy_api.normalize_implementation_policy(
            backend_ir,
            source_module=semantic_ir,
            exploration_results=exploration_results,
            external_contributions=contributions,
        )
        external_formal = replace(configured_formal, policy=formal_exploration.FormalPolicy.OFF)
        backend_ir, external_explorations = implementation_policy_api.apply_external_region_exploration(
            backend_ir,
            implementation_policy,
            external_contributions=contributions,
            formal_config=external_formal,
            formal_verifier=None,
        )
        if (
            external_explorations
            and configured_formal.policy is not formal_exploration.FormalPolicy.OFF
            # A concrete target turns this external value site into a complete
            # value+schedule+resource candidate during planning. Gating the
            # pre-planning expression here would prove a different artifact
            # and, for newly partitioned DAGs, cannot emit the nested physical
            # boundaries. The planning-phase formal-aware selection gate below owns that route.
            and implementation_policy.request.target in {None, "generic"}
            and not defer_one_root_pipeline_to_physical_formal_selection(
                backend_ir
            )
        ):
            try:
                backend_ir, external_explorations = candidate_sites.gate_retained_explorations(
                    backend_ir,
                    external_explorations,
                    configured_formal,
                    formal_verifier,
                    backend=configured_formal.backend,
                )
            except ValueError as error:
                raise SemanticError(str(error)) from error
        exploration_results = (*exploration_results, *external_explorations)
        high_level_ir = lower(backend_ir, stage=OptimizationStage.HIGH_LEVEL)
        source_typed_ir = restore(high_level_ir)
        # Semantic IR is a DAG.  Dataclass equality recursively follows every
        # incoming edge and therefore turns shared expressions into their
        # exponentially large tree expansion.  Validate the round trip in the
        # flat canonical representation instead, where node references are
        # integer IDs and comparison is bounded by unique DAG size.
        if not canonical_round_trip_matches(
            high_level_ir,
            lower(
                source_typed_ir,
                stage=OptimizationStage.HIGH_LEVEL,
            ),
        ):
            raise RuntimeError("canonical optimization IR did not restore semantic IR")
        source_typed_ir = restore_selection_formal_records(
            backend_ir, source_typed_ir
        )
        extraction = costs.extract_estimated_costs(source_typed_ir)
        optimization_ir = lower(
            extraction.module,
            stage=OptimizationStage.SELECTED_ARCHITECTURE,
        )
        typed_ir = restore(optimization_ir)
        if not canonical_round_trip_matches(
            optimization_ir,
            lower(
                typed_ir,
                stage=OptimizationStage.SELECTED_ARCHITECTURE,
            ),
        ):
            raise RuntimeError("extracted optimization IR did not restore semantic IR")
        # Solver/cache records are orchestration evidence and intentionally do
        # not participate in canonical IR identity.  Restore them recursively
        # after the semantic round-trip for reports and callers.
        typed_ir = restore_selection_formal_records(
            backend_ir, typed_ir
        )
        ledger = candidate_site_ledger(
            typed_ir, exploration_results
        )
        return SelectionProduct(
            module=typed_ir,
            high_level_ir=high_level_ir,
            optimization_ir=optimization_ir,
            extraction=extraction,
            implementation_policy=implementation_policy,
            implementation_request=implementation_policy.request,
            exploration_results=tuple(exploration_results),
            candidate_site_ledger=ledger,
        )


def canonical_round_trip_matches(
    expected: CanonicalModule,
    restored: CanonicalModule,
) -> bool:
    """Compare canonical semantics without recursively expanding a typed DAG.

    ``restore`` necessarily reconstructs diagnostic provenance from the
    canonical expression table.  Re-lowering can therefore merge a different
    (but equivalent) set of source occurrences into a node.  The old typed-IR
    dataclass equality already ignored that provenance, but recursively walked
    every incoming expression edge and expanded a shared DAG as a tree.

    The canonical identity renderer is the complete, origin-insensitive
    semantic representation used by build identities.  Both inputs are flat
    node tables, so this comparison is bounded by unique canonical nodes.
    Comparing the rendered bytes rather than only their digest also keeps this
    a strict round-trip validation rather than a hash-collision assumption.
    """

    return canonical_identity_matches(expected, restored)


def map_modules(module: IrModule, transform) -> IrModule:
    """Apply one selection-phase transform to every specialization once."""

    children = tuple(map_modules(child, transform) for child in module.children)
    # Formal records are deliberately ``compare=False`` metadata.  Equality
    # therefore cannot tell whether a descendant received selection evidence;
    # always reconnect the recursively transformed children.
    current = replace(module, children=children)
    return transform(current)


def defer_one_root_pipeline_to_physical_formal_selection(
    module: IrModule,
) -> bool:
    """Return whether planning can build the complete physical candidate."""

    pipelines = tuple(
        item.expression
        for item in module.assignments
        if isinstance(item.expression, ir_expr.Pipeline)
    )
    return len(pipelines) == 1 and len(module.pipeline_explorations) <= 1


def attach_unified_pipeline_formal_records(
    module: IrModule,
    results: Iterable[ExplorationResult],
) -> IrModule:
    """Mirror one unified formal-aware selection record into planner-only pipeline metadata.

    ``implement`` owns the formal candidate site.  Its retained pipeline
    table is still useful to reports and target planning, but must not trigger
    another proof route.  Copying the already-produced records keeps those
    reports informative without changing the candidate/cache identity.
    """

    retained = tuple(results)

    def transform(current: IrModule) -> IrModule:
        pipelines = []
        changed = False
        for pipeline in current.pipeline_explorations:
            key = pipeline_site_key(current, pipeline)
            matches = tuple(
                result for result in retained if candidate_sites.exploration_site_key(result) == key
            )
            if not matches:
                pipelines.append(pipeline)
                continue
            if len(matches) == 1:
                result = matches[0]
                # The unified implement gate may select a different
                # architecture than the planner's initial catalog choice.
                # Rebind the catalog's selected name by stable implementation
                # identity before comparing expressions, so only the actually
                # emitted candidate receives the retained formal record.
                emitted_identity = result.selected_candidate.implementation_identity
                matching_candidate = next(
                    (
                        candidate
                        for candidate in pipeline.candidates
                        if selection_expression_semantic_identity(
                            candidate.expression
                        )
                        == selection_expression_semantic_identity(
                            result.selected_candidate.expression
                        )
                    ),
                    None,
                )
                if matching_candidate is None:
                    # Unified exploration records carry the architecture name
                    # in their deterministic stage provenance even when the
                    # implementation-site wrapper has a distinct identity
                    # schema.  Use that typed name as a conservative fallback.
                    stage_names = tuple(
                        str(stage).split(":", 1)[1]
                        for stage in getattr(result.selected_candidate, "stages", ())
                        if str(stage).startswith("pipeline:")
                    )
                    matching_candidate = next(
                        (
                            candidate for candidate in pipeline.candidates
                            if candidate.name in stage_names
                        ),
                        None,
                    )
                selected_name_changed = (
                    matching_candidate is not None
                    and pipeline.selected != matching_candidate.name
                )
                if selected_name_changed:
                    pipeline = replace(pipeline, selected=matching_candidate.name)
                # Unified implementation candidates carry the enclosing
                # Pipeline value, whereas planner catalog entries carry the
                # inner value DAG.  Keep the catalog's selected entry aligned
                # with the emitted implementation so report emission checks
                # compare the same typed value (without changing candidate
                # identity or scheduling semantics).
                if matching_candidate is not None:
                    selected_value = (
                        result.selected_candidate.expression.expression
                        if isinstance(
                            result.selected_candidate.expression,
                            ir_expr.Pipeline,
                        )
                        else result.selected_candidate.expression
                    )
                    candidates = tuple(
                        replace(candidate, expression=selected_value)
                        if candidate.name == matching_candidate.name
                        else candidate
                        for candidate in pipeline.candidates
                    )
                    # Avoid dataclass equality here: candidate expressions are
                    # shared DAGs and recursive equality expands them as trees.
                    # Replacing this tiny catalog tuple is deterministic and
                    # cheaper than asking whether the selected value object is
                    # structurally equal to its predecessor.
                    pipeline = replace(pipeline, candidates=candidates)
                # A planner catalog is allowed to carry evidence only when
                # its exact selected expression is the one emitted by the
                # unified implementation result.  Matching only the region
                # would attach proof for a different (often zero-cycle)
                # candidate to a positive-latency catalog entry.
                selected_matches = (
                    matching_candidate is not None
                    or
                    selection_expression_semantic_identity(
                        result.selected_candidate.expression
                    )
                    == selection_expression_semantic_identity(
                        pipeline.selected_candidate.expression
                    )
                )
                records = (
                    tuple(
                        record
                        for record in result.formal_records
                        if getattr(record, "candidate_identity", None)
                        == emitted_identity
                    )
                    if selected_matches else ()
                )
                updated = replace(pipeline, formal_records=records)
                pipelines.append(updated)
                # Pipeline formal metadata is intentionally compare=False, so
                # dataclass equality cannot detect this report-only update.
                changed = changed or selected_name_changed or (
                    pipeline.formal_records != records
                )
            else:
                # A duplicate typed site is malformed; leave metadata empty so
                # a report cannot claim evidence whose owner is ambiguous.
                pipelines.append(replace(pipeline, formal_records=()))
                changed = changed or bool(pipeline.formal_records)
        return replace(
            current,
            pipeline_explorations=tuple(pipelines),
        ) if changed else current

    return map_modules(module, transform)


def with_elastic_formal_records(
    module: IrModule,
    config: formal_exploration.FormalExplorationConfig,
    *,
    nondefault_reset: bool,
    domain_reason: str | None = None,
) -> IrModule:
    def records(region: object) -> tuple[formal_exploration.FormalExplorationRecord, ...]:
        if nondefault_reset:
            return (_nondefault_reset_formal_record(
                getattr(region, "selected"),
                config,
                getattr(region, "source_origin"),
                reason=(domain_reason or _UNSUPPORTED_RESET_FORMAL_REASON),
            ),)
        temporal_graph = getattr(region, "temporal_graph", None)
        relation = (
            build_capacity_one_transaction_relation(region)
            if temporal_graph is not None
            else None
        )
        return (formal_exploration.FormalExplorationRecord(
            candidate_identity=(
                temporal_graph.implementation_identity
                if temporal_graph is not None
                else getattr(region, "selected")
            ),
            rank=1,
            semantic_legality="typed_legal",
            formal_route=(
                "transaction_stream_equivalence_bmc_unbound"
                if relation is not None
                else "unsupported_variable_latency_elastic"
            ),
            policy=formal_exploration.FormalPolicy.AVAILABLE,
            mode=ProofMode.BMC,
            depth=config.bmc_depth,
            status=FormalStatus.SKIPPED,
            cache_state=formal_exploration.CACHE_STATE_NOT_RUN,
            eligible=True,
            reason=(
                "capacity-one transaction-stream BMC miter exists, but no "
                "candidate-selection evidence route is attached"
                if relation is not None
                else "semantic-reference equivalence fixed-latency equivalence "
                "does not apply to a stalled elastic relation"
            ),
            property_identity=(None if relation is None else relation.property_identity),
            source_origin=getattr(region, "source_origin"),
        ),)

    def transform(item: IrModule) -> IrModule:
        regions = tuple(
            replace(
                region,
                formal_records=records(region),
            )
            for region in item.elastic_pipeline_regions
        )
        return replace(item, elastic_pipeline_regions=regions)

    return map_modules(module, transform)


def restore_selection_formal_records(
    reference: IrModule,
    restored: IrModule,
) -> IrModule:
    """Reconnect compare-false orchestration evidence after canonical restore."""

    def has_records(item: IrModule) -> bool:
        return bool(
            any(
                assignment.expression.formal_records
                or assignment.expression.formal_eligible
                for assignment in item.assignments
                if assignment.signal is None
                and assignment.channel is None
                and isinstance(
                    assignment.expression,
                    ir_expr.ImplementationChoice,
                )
            )
            or any(value.formal_records for value in item.pipeline_explorations)
            or any(value.formal_records for value in item.elastic_pipeline_regions)
            or any(has_records(child) for child in item.children)
        )

    if not has_records(reference):
        return restored

    reference_choices = {
        assignment.target.name: (
            assignment.expression.formal_records,
            assignment.expression.formal_eligible,
        )
        for assignment in reference.assignments
        if assignment.signal is None
        and assignment.channel is None
        and isinstance(assignment.expression, ir_expr.ImplementationChoice)
    }
    assignments = tuple(
        replace(
            assignment,
            expression=replace(
                assignment.expression,
                formal_records=reference_choices.get(
                    assignment.target.name,
                    (assignment.expression.formal_records, ()),
                )[0],
                formal_eligible=reference_choices.get(
                    assignment.target.name,
                    ((), assignment.expression.formal_eligible),
                )[1],
            ),
        )
        if assignment.signal is None
        and assignment.channel is None
        and isinstance(assignment.expression, ir_expr.ImplementationChoice)
        else assignment
        for assignment in restored.assignments
    )
    pipeline_records = {
        item.output: item.formal_records
        for item in reference.pipeline_explorations
    }
    elastic_records = {
        item.semantic_id: item.formal_records
        for item in reference.elastic_pipeline_regions
    }
    def selection_record_payload(item: IrModule) -> tuple[object, ...]:
        return (
            tuple(
                (
                    assignment.target.name,
                    assignment.expression.formal_records,
                    assignment.expression.formal_eligible,
                )
                for assignment in item.assignments
                if assignment.signal is None
                and assignment.channel is None
                and isinstance(assignment.expression, ir_expr.ImplementationChoice)
            ),
            tuple(
                (pipeline.output, pipeline.formal_records)
                for pipeline in item.pipeline_explorations
            ),
            tuple(
                (region.semantic_id, region.formal_records)
                for region in item.elastic_pipeline_regions
            ),
        )

    reference_children: dict[str, IrModule] = {}
    for item in reference.children:
        owner = candidate_sites.module_candidate_owner_identity(item)
        previous = reference_children.get(owner)
        if (
            previous is not None
            and selection_record_payload(previous) != selection_record_payload(item)
        ):
            raise candidate_sites.CandidateSiteError(
                "selection formal records contain incompatible children for "
                f"specialization owner '{owner}'"
            )
        reference_children.setdefault(owner, item)
    children = tuple(
        restore_selection_formal_records(reference_children[owner], child)
        if (owner := candidate_sites.module_candidate_owner_identity(child)) in reference_children
        else child
        for child in restored.children
    )
    return replace(
        restored,
        assignments=assignments,
        pipeline_explorations=tuple(
            replace(item, formal_records=pipeline_records.get(item.output, ()))
            for item in restored.pipeline_explorations
        ),
        elastic_pipeline_regions=tuple(
            replace(item, formal_records=elastic_records.get(item.semantic_id, ()))
            for item in restored.elastic_pipeline_regions
        ),
        children=children,
    )

def gate_physical_target_candidates(
    module: IrModule,
    plans: implementation_plans.BackendImplementationPlanningResult,
    config: formal_exploration.FormalExplorationConfig,
    injected_verifier: object | None,
) -> tuple[
    implementation_plans.BackendImplementationPlanningResult,
    TargetPlanningResult | None,
    object | None,
    tuple[formal_exploration.FormalExplorationRecord, ...],
]:
    """Apply formal-aware selection to complete value+schedule+resource candidates.

    Earlier formal-aware selection sites validate typed value alternatives.  This bounded
    planning-phase gate additionally validates the exact physical graph which
    direct-SV will publish.  It is intentionally limited to the current
    single-output scalar target planner; unsupported shapes remain explicit.
    """

    result = plans.target_planning_result
    if config.policy is formal_exploration.FormalPolicy.OFF or result is None:
        graph = result.selected_graph if result is not None else None
        return plans, result, graph, ()
    if result.extraction is None:
        raise SemanticError(
            "physical formal policy requires a ranked target candidate set",
            code="ZL-FORMAL-PHYSICAL-CANDIDATE",
        )

    from zlang.formal_candidate import (
        SemanticEquivalenceDirectSystemVerilogCandidateVerifier,
        PhysicalTargetFormalCandidate,
    )
    from zlang.formal_exploration import gate_candidates
    from zlang.pipeline_scheduling import erase_pipeline_timing

    assignments = tuple(
        assignment
        for assignment in module.assignments
        if isinstance(assignment.expression, ir_expr.Pipeline)
        and hasattr(assignment.target, "name")
    )
    selected_quantization = result.selected_graph.quantization
    if selected_quantization is not None:
        assignments = tuple(
            assignment
            for assignment in assignments
            if erase_pipeline_timing(assignment.expression)
            == selected_quantization
        )
    if len(assignments) != 1:
        raise SemanticError(
            "physical formal-aware selection gate requires exactly one target-planned scalar "
            "pipeline output",
            code="ZL-FORMAL-PHYSICAL-CANDIDATE",
        )
    source_assignment = assignments[0]
    plan = source_assignment.expression.pipeline_plan
    reference = (
        plan.source_expression
        if plan is not None and plan.source_expression is not None
        else erase_pipeline_timing(source_assignment.expression)
    )
    selected_value = erase_pipeline_timing(source_assignment.expression)
    if (
        selected_quantization is not None
        and selection_expression_semantic_identity(reference)
        != selection_expression_semantic_identity(selected_quantization)
    ):
        if selection_expression_semantic_identity(
            selected_value
        ) != selection_expression_semantic_identity(selected_quantization):
            raise SemanticError(
                "physical formal-aware selection selected value does not match the target region",
                code="ZL-FORMAL-PHYSICAL-CANDIDATE",
            )

    target_by_identity = {
        candidate.implementation_identity: candidate
        for candidate in result.generated_candidates
    }
    wrappers = []
    evaluations = []
    for evaluation in result.extraction.evaluations:
        target_candidate = evaluation.candidate
        implementation = source_assignment.expression
        if implementation.stages != target_candidate.graph.latency:
            implementation = replace(
                implementation,
                stages=target_candidate.graph.latency,
                expression=selected_value,
                pipeline_plan=None,
            )
        physical_module = replace(
            module,
            assignments=tuple(
                replace(item, expression=implementation)
                if item is source_assignment else item
                for item in module.assignments
            ),
        )
        wrapper = PhysicalTargetFormalCandidate(
            expression=implementation,
            module=physical_module,
            implementation_graph=target_candidate.graph,
            semantic_identity=target_candidate.graph.semantic_region_identity,
            implementation_identity=target_candidate.implementation_identity,
            cost=target_candidate.cost,
        )
        wrappers.append(wrapper)
        evaluations.append(replace(evaluation, candidate=wrapper))

    domain = module.clock_domains[0] if len(module.clock_domains) == 1 else None
    verifier = injected_verifier or SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        reference,
        candidate_class="pipeline",
        artifact_provider=getattr(config, "artifact_provider", None),
        clock_domain_contract=domain,
        unavailable_reason=(
            None
            if domain is not None
            else "physical target candidate requires exactly one formal domain"
        ),
    )
    gate = gate_candidates(
        tuple(wrappers),
        tuple(evaluations),
        config,
        verifier,
        route="semantic_equivalence_direct_systemverilog",
    )
    selected_identity = (
        result.selected_candidate.implementation_identity
        if config.policy is formal_exploration.FormalPolicy.AVAILABLE
        else gate.eligible[0].implementation_identity
    )
    selected = target_by_identity[selected_identity]
    extraction = replace(
        result.extraction,
        selected=selected,
        selected_cost=selected.cost,
        reason=(
            result.extraction.reason
            + "; complete physical candidate passed formal-aware selection policy "
            + config.policy.value
        ),
    )
    result = replace(
        result,
        selected_candidate=selected,
        extraction=extraction,
        formal_records=gate.records,
    )
    previous_graph = plans.target_planning_result.selected_graph
    updated_plans = tuple(
        replace(plan, graph=selected.graph)
        if plan.graph is not None
        and plan.backend == "systemverilog"
        and plan.graph.identity == previous_graph.identity
        else plan
        for plan in plans.plans
    )
    plans = replace(
        plans,
        plans=updated_plans,
        target_planning_result=result,
    )
    return plans, result, selected.graph, gate.records

def inline_locals(module: IrModule) -> IrModule:
    """Erase pure local bindings before canonical and backend stages."""

    children = tuple(inline_locals(child) for child in module.children)
    if not module.locals:
        return (
            replace(module, children=children)
            if any(
                updated is not original
                for updated, original in zip(
                    children, module.children, strict=True
                )
            )
            else module
        )
    values = {local.name: local.expression for local in module.locals}
    local_cache: dict[str, object] = {}
    node_cache: dict[int, object] = {}

    def walk(value):
        if isinstance(value, ir_expr.InputRef) and value.name in values:
            cached_local = local_cache.get(value.name)
            if cached_local is not None:
                return cached_local
            expanded_local = walk(values[value.name])
            local_cache[value.name] = expanded_local
            return expanded_local
        if isinstance(value, tuple):
            cache_key = id(value)
            cached_node = node_cache.get(cache_key)
            if cached_node is not None:
                return cached_node
            expanded_tuple = tuple(walk(item) for item in value)
            node_cache[cache_key] = expanded_tuple
            return expanded_tuple
        if is_dataclass(value):
            cache_key = id(value)
            cached_node = node_cache.get(cache_key)
            if cached_node is not None:
                return cached_node
            updates = {}
            for item in fields(value):
                current = getattr(value, item.name)
                if item.name == "origin" or not item.init:
                    continue
                if isinstance(current, tuple):
                    updates[item.name] = tuple(walk(v) for v in current)
                elif is_dataclass(current):
                    updates[item.name] = walk(current)
                else:
                    updates[item.name] = current
            try:
                expanded_value = replace(value, **updates)
            except (TypeError, ValueError):
                expanded_value = value
            node_cache[cache_key] = expanded_value
            return expanded_value
        return value

    return replace(
        module,
        assignments=tuple(
            replace(item, expression=walk(item.expression))
            for item in module.assignments
        ),
        next_assignments=tuple(
            replace(
                item,
                expression=walk(item.expression),
                activation=(
                    walk(item.activation)
                    if item.activation is not None else None
                ),
            )
            for item in module.next_assignments
        ),
        rules=tuple(
            replace(
                rule,
                guard=walk(rule.guard),
                actions=tuple(
                    replace(
                        action,
                        expression=walk(action.expression),
                        activation=(
                            walk(action.activation)
                            if action.activation is not None else None
                        ),
                    )
                    for action in rule.actions
                ),
            )
            for rule in module.rules
        ),
        registers=tuple(
            replace(
                register,
                initial=(
                    walk(register.initial)
                    if register.initial is not None else None
                ),
            )
            for register in module.registers
        ),
        instance_bindings=tuple(
            replace(binding, expression=walk(binding.expression))
            for binding in module.instance_bindings
        ),
        pipeline_explorations=tuple(
            replace(
                exploration,
                source_expression=walk(exploration.source_expression),
                candidates=tuple(
                    replace(candidate, expression=walk(candidate.expression))
                    for candidate in exploration.candidates
                ),
            )
            for exploration in module.pipeline_explorations
        ),
        resolved_transition=walk(module.resolved_transition),
        locals=(),
        children=children,
    )
class PlanningBuilder:
    """Build physical implementation planning from one selected product."""

    def build(
        self,
        selection: SelectionProduct,
        *,
        target_evidence: tuple[object, ...] | None,
        target_evidence_path: Path | None,
        target_tool: str,
        target_tool_version: str,
        target_clock_period_ns: float,
        formal_config: formal_exploration.FormalExplorationConfig,
        formal_verifier: object | None,
    ) -> PlanningProduct:
        request = selection.implementation_request
        from zlang.pipeline_scheduling import (
            PipelineSchedulingError,
            TargetResourceOperationCostModel,
            schedule_module_fixed_pipelines,
        )

        try:
            cost_model = None
            if request.target not in {None, "generic"}:
                from zlang.target_catalog import load_target

                _, _, resources = load_target(request.target)
                cost_model = TargetResourceOperationCostModel(resources)
            planned_module = schedule_module_fixed_pipelines(
                selection.module,
                cost_model=cost_model,
            )
        except PipelineSchedulingError as error:
            raise SemanticError(
                f"fixed pipeline cannot schedule the typed expression: {error}",
                code="ZL-PIPELINE-SCHEDULE",
                notes=(
                    "fixed pipeline scheduling accepts only pure combinational "
                    "typed values and never moves state, protocol, storage, or "
                    "CDC effects",
                ),
            ) from error
        plans = implementation_plans.plan_backend_implementations(
            planned_module,
            backend_requests=(
                (request.backend,) if request.backend is not None else ()
            ),
            target=request.target,
            architecture=request.architecture.identity,
            architecture_mode=request.architecture.mode,
            source_policy=request.evidence_policy,
            evidence=target_evidence,
            evidence_path=target_evidence_path,
            tool=target_tool,
            tool_version=target_tool_version,
            clock_period_ns=target_clock_period_ns,
            strict_target_planning=request.backend is None,
        )
        target_result = plans.target_planning_result
        physical_formal_records: tuple[formal_exploration.FormalExplorationRecord, ...] = ()
        if (
            target_result is not None
            and request.formal_policy is not formal_exploration.FormalPolicy.OFF
            # The bounded single-root path owns one complete
            # value+schedule+resource gate here. Multi-site designs retain the
            # established selection-time formal-aware selection route and never receive a second
            # solver invocation.
            and defer_one_root_pipeline_to_physical_formal_selection(
                planned_module
            )
            and (
                not planned_module.pipeline_explorations
                or request.target not in {None, "generic"}
            )
        ):
            try:
                plans, target_result, _, physical_formal_records = (
                    gate_physical_target_candidates(
                        planned_module,
                        plans,
                        formal_config,
                        formal_verifier,
                    )
                )
            except ValueError as error:
                raise SemanticError(str(error)) from error
        if request.backend is None:
            graph = (
                target_result.selected_graph
                if target_result is not None
                else plans.selected_graph
            )
            if graph is None:
                raise AssertionError("selected SystemVerilog plan has no graph")
        else:
            graph = plans.plan_for(request.backend.kind).graph
        return PlanningProduct(
            planned_module,
            plans,
            target_result,
            graph,
            physical_formal_records,
        )
