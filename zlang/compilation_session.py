"""Demand-driven orchestration for one isolated ZLang compilation.

The session owns every computed value and failure.  It deliberately contains
no process-global cache: two sessions compiling identical source are still
independent compilation attempts.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Callable, Iterable, Mapping

from zlang.architectures import render_architecture_report
from zlang.ast.nodes import Module as AstModule
from zlang.backend.systemverilog import emit_contracts
from zlang.costs import (
    CostExtractionError,
    SourcePolicy,
    extract_estimated_costs,
    render_cost_report,
)
from zlang.candidate_sites import (
    CandidateRewriteKind,
    CandidateSiteError,
    CandidateSiteLedger,
    build_candidate_site_ledger,
    gate_retained_explorations,
    gate_structured_candidate_sites,
    module_candidate_owner_identity,
    exploration_site_key,
    pipeline_site_key,
)
from zlang.compilation_inputs import PhysicalCompilationInputs
from zlang.compilation_products import CompilationResult
from zlang.csr import emit_csr_json, emit_csr_markdown
from zlang.exploration import ExplorationResult, render_exploration_report
from zlang.formal import (
    build_formal_design,
    build_recursive_formal_design,
    emit_harness,
    emit_recursive_harness,
)
from zlang.formal_artifact_provider import FormalArtifactProvider
from zlang.formal_tooling import FormalToolResolver
from zlang.formal_exploration import (
    CACHE_STATE_NOT_RUN,
    FormalExplorationConfig,
    FormalExplorationRecord,
    FormalPolicy,
)
from zlang.implementation_plans import (
    BackendImplementationPlanningResult,
    plan_backend_implementations,
)
from zlang.implementation_policy import (
    ModuleImplementationPolicy,
    apply_external_region_exploration,
    normalize_implementation_policy,
)
from zlang.implementation_request import (
    ArchitectureRequest,
    BackendKind,
    BackendRequest,
    ImplementationContribution,
    ImplementationRequest,
    PolicyOrigin,
    RequirementMode,
    merge_implementation_contributions,
)
from zlang.implementations import render_implementation_report
from zlang.ir import expressions as ir_expr
from zlang.ir.formal import FormalDesign, FormalStatus, ProofMode
from zlang.ir.module import Module as IrModule, dependency_context_identity
from zlang.ir.signed_reductions import (
    expression_semantic_identity,
    selection_expression_semantic_identity,
)
from zlang.opt import CanonicalModule, OptimizationStage, lower, restore
from zlang.parser import parse
from zlang.pipelines import render_pipeline_report
from zlang.semantic import SemanticError, analyze
from zlang.target_planner import TargetPlanningResult
from zlang.targets import ArchitectureSelectionMode, select_implementation_graph
from zlang.stdlib import track_resolved_stdlib_source_paths


class SessionTopSelectionError(ValueError):
    """A requested source top does not exist.

    The public compiler facade translates this internal exception to its
    longstanding ``TopSelectionError`` type.
    """


_UNSUPPORTED_RESET_FORMAL_REASON = (
    "M39 authoritative M36 route requires one physical domain with "
    "power_up unspecified"
)


def _nondefault_reset_formal_record(
    candidate_identity: str,
    config: FormalExplorationConfig,
    origin=None,
    *,
    reason: str = _UNSUPPORTED_RESET_FORMAL_REASON,
) -> FormalExplorationRecord:
    """Publish one explicit not-run record without constructing a verifier."""

    return FormalExplorationRecord(
        candidate_identity=candidate_identity,
        rank=1,
        semantic_legality="typed_legal",
        formal_route="unsupported_physical_reset_contract",
        policy=FormalPolicy.AVAILABLE,
        mode=ProofMode.BMC,
        depth=config.bmc_depth,
        status=FormalStatus.SKIPPED,
        cache_state=CACHE_STATE_NOT_RUN,
        eligible=True,
        reason=reason,
        source_origin=origin,
    )


def _map_modules(module: IrModule, transform) -> IrModule:
    """Apply one selection-phase transform to every specialization once."""

    children = tuple(_map_modules(child, transform) for child in module.children)
    # Formal records are deliberately ``compare=False`` metadata.  Equality
    # therefore cannot tell whether a descendant received selection evidence;
    # always reconnect the recursively transformed children.
    current = replace(module, children=children)
    return transform(current)


def _gate_all_standalone_pipelines(
    module: IrModule,
    config: FormalExplorationConfig,
    verifier: object | None,
    canonical_site_keys: Iterable[tuple[str, str | None, str]] = (),
    *,
    backend: str = "direct_systemverilog",
) -> IrModule:
    from zlang.formal_candidate import gate_standalone_pipelines

    keys = frozenset(canonical_site_keys)
    return _map_modules(
        module,
        lambda item: gate_standalone_pipelines(
            item, config, verifier, canonical_site_keys=keys, backend=backend
        ),
    )


def _defer_one_root_pipeline_to_physical_m39(module: IrModule) -> bool:
    """Return whether planning can construct one complete physical candidate.

    The pre-planning M39 route must not prove a raw ``Pipeline`` expression
    which has not yet received its internal schedule.  The target planner's
    bounded physical route currently owns exactly one root scalar pipeline;
    all other standalone forms retain the historical gate/diagnostic.
    """

    pipelines = tuple(
        item.expression
        for item in module.assignments
        if isinstance(item.expression, ir_expr.Pipeline)
    )
    return len(pipelines) == 1 and len(module.pipeline_explorations) <= 1


def _attach_unified_pipeline_formal_records(
    module: IrModule,
    results: Iterable[ExplorationResult],
) -> IrModule:
    """Mirror one unified M39 record into planner-only pipeline metadata.

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
                result for result in retained if exploration_site_key(result) == key
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
                    if candidates != pipeline.candidates:
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

    return _map_modules(module, transform)


def _with_elastic_formal_records(
    module: IrModule,
    config: FormalExplorationConfig,
    *,
    nondefault_reset: bool,
    domain_reason: str | None = None,
) -> IrModule:
    def transform(item: IrModule) -> IrModule:
        regions = tuple(
            replace(
                region,
                formal_records=((
                    _nondefault_reset_formal_record(
                        region.selected,
                        config,
                        region.source_origin,
                        reason=(
                            domain_reason or _UNSUPPORTED_RESET_FORMAL_REASON
                        ),
                    )
                    if nondefault_reset
                    else FormalExplorationRecord(
                        candidate_identity=region.selected,
                        rank=1,
                        semantic_legality="typed_legal",
                        formal_route="unsupported_variable_latency_elastic",
                        policy=FormalPolicy.AVAILABLE,
                        mode=ProofMode.BMC,
                        depth=config.bmc_depth,
                        status=FormalStatus.SKIPPED,
                        cache_state=CACHE_STATE_NOT_RUN,
                        eligible=True,
                        reason=(
                            "M36 fixed-latency equivalence does not apply "
                            "to a stalled elastic relation"
                        ),
                        source_origin=region.source_origin,
                    )
                ),),
            )
            for region in item.elastic_pipeline_regions
        )
        return replace(item, elastic_pipeline_regions=regions)

    return _map_modules(module, transform)


def _with_pipeline_reset_skip_records(
    module: IrModule,
    config: FormalExplorationConfig,
    *,
    reason: str = _UNSUPPORTED_RESET_FORMAL_REASON,
) -> IrModule:
    def transform(item: IrModule) -> IrModule:
        pipelines = tuple(
            replace(
                pipeline,
                formal_records=(_nondefault_reset_formal_record(
                    pipeline.selected,
                    config,
                    pipeline.source_expression.origin,
                    reason=reason,
                ),),
            )
            for pipeline in item.pipeline_explorations
        )
        return replace(item, pipeline_explorations=pipelines)

    return _map_modules(module, transform)


def _with_structured_reset_skip_records(
    module: IrModule,
    config: FormalExplorationConfig,
    *,
    reason: str = _UNSUPPORTED_RESET_FORMAL_REASON,
) -> IrModule:
    """Attach explicit non-default-reset skips to choice/architecture sites."""

    def transform(item: IrModule) -> IrModule:
        ledger = build_candidate_site_ledger(item)
        identities = {
            (site.kind.value, site.output): site.selected_candidate_identity
            for site in ledger.sites
            if site.owner_identity == module_candidate_owner_identity(item)
        }
        assignments = tuple(
            replace(
                assignment,
                expression=replace(
                    assignment.expression,
                    formal_records=(_nondefault_reset_formal_record(
                        identities[("choice_auto", assignment.target.name)],
                        config,
                        assignment.expression.origin,
                        reason=reason,
                    ),),
                ),
            )
            if assignment.signal is None
            and assignment.channel is None
            and isinstance(assignment.expression, ir_expr.ImplementationChoice)
            and assignment.expression.cost_policy is not None
            else assignment
            for assignment in item.assignments
        )
        architectures = tuple(
            replace(
                exploration,
                formal_records=(_nondefault_reset_formal_record(
                    identities[("architecture_auto", exploration.output)],
                    config,
                    exploration.source_expression.origin,
                    reason=reason,
                ),),
            )
            for exploration in item.architecture_explorations
        )
        return replace(
            item,
            assignments=assignments,
            architecture_explorations=architectures,
        )

    return _map_modules(module, transform)


def _restore_selection_formal_records(
    reference: IrModule,
    restored: IrModule,
) -> IrModule:
    """Reconnect compare-false orchestration evidence after canonical restore."""

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
    architecture_records = {
        item.output: item.formal_records
        for item in reference.architecture_explorations
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
            tuple(
                (architecture.output, architecture.formal_records)
                for architecture in item.architecture_explorations
            ),
        )

    reference_children: dict[str, IrModule] = {}
    for item in reference.children:
        owner = module_candidate_owner_identity(item)
        previous = reference_children.get(owner)
        if (
            previous is not None
            and selection_record_payload(previous) != selection_record_payload(item)
        ):
            raise CandidateSiteError(
                "selection formal records contain incompatible children for "
                f"specialization owner '{owner}'"
            )
        reference_children.setdefault(owner, item)
    children = tuple(
        _restore_selection_formal_records(reference_children[owner], child)
        if (owner := module_candidate_owner_identity(child)) in reference_children
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
        architecture_explorations=tuple(
            replace(item, formal_records=architecture_records.get(item.output, ()))
            for item in restored.architecture_explorations
        ),
        children=children,
    )


@dataclass(frozen=True)
class CompilationSessionOptions:
    """Immutable snapshot of every option which can affect a session product."""

    formal_policy: FormalPolicy | str | None
    formal_depth: int
    formal_max_candidates: int
    formal_timeout: int
    formal_cache: Path | None
    formal_work_directory: Path | None
    formal_verifier: object | None
    top: str | None
    target: str | None
    architecture: str | None
    architecture_mode: ArchitectureSelectionMode | str | None
    target_evidence_policy: SourcePolicy | str | None
    target_evidence: tuple[object, ...] | None
    target_evidence_path: Path | None
    target_tool: str
    target_tool_version: str
    target_clock_period_ns: float
    source_unit: str | None
    module_resolver: object | None
    root_module_identity: object | None
    dependency_closure: object | None
    implementation_backend: BackendKind | str | None
    implementation_backend_mode: RequirementMode | str
    implementation_contributions: tuple[ImplementationContribution, ...]


@dataclass(frozen=True)
class _AnalysisProduct:
    module: IrModule
    exploration_results: tuple[ExplorationResult, ...]


@dataclass(frozen=True)
class _SelectionProduct:
    module: IrModule
    high_level_ir: CanonicalModule
    optimization_ir: CanonicalModule
    extraction: object
    implementation_policy: ModuleImplementationPolicy
    implementation_request: ImplementationRequest
    exploration_results: tuple[ExplorationResult, ...]
    candidate_site_ledger: CandidateSiteLedger


@dataclass(frozen=True)
class _FormalProduct:
    design: FormalDesign
    harness: str
    recursive_design: object | None
    recursive_harness: str


@dataclass(frozen=True)
class _PlanningProduct:
    module: IrModule
    backend_plans: BackendImplementationPlanningResult
    target_planning_result: TargetPlanningResult | None
    implementation_graph: object | None
    physical_formal_records: tuple[FormalExplorationRecord, ...] = ()


def _gate_physical_target_candidates(
    module: IrModule,
    plans: BackendImplementationPlanningResult,
    config: FormalExplorationConfig,
    injected_verifier: object | None,
) -> tuple[
    BackendImplementationPlanningResult,
    TargetPlanningResult | None,
    object | None,
    tuple[FormalExplorationRecord, ...],
]:
    """Apply M39 to complete value+schedule+resource candidates.

    Earlier M39 sites validate typed value alternatives.  This bounded
    planning-phase gate additionally validates the exact physical graph which
    direct-SV will publish.  It is intentionally limited to the current
    single-output scalar target planner; unsupported shapes remain explicit.
    """

    result = plans.target_planning_result
    if config.policy is FormalPolicy.OFF or result is None:
        graph = result.selected_graph if result is not None else None
        return plans, result, graph, ()
    if result.extraction is None:
        raise SemanticError(
            "physical formal policy requires a ranked target candidate set",
            code="ZL-FORMAL-PHYSICAL-CANDIDATE",
        )

    from zlang.formal_candidate import (
        M36DirectSystemVerilogCandidateVerifier,
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
            "physical M39 gate requires exactly one target-planned scalar "
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
    if selected_quantization is not None and reference != selected_quantization:
        if selected_value != selected_quantization:
            raise SemanticError(
                "physical M39 selected value does not match the target region",
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
    verifier = injected_verifier or M36DirectSystemVerilogCandidateVerifier(
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
        route="M36_direct_systemverilog",
    )
    selected_identity = (
        result.selected_candidate.implementation_identity
        if config.policy is FormalPolicy.AVAILABLE
        else gate.eligible[0].implementation_identity
    )
    selected = target_by_identity[selected_identity]
    extraction = replace(
        result.extraction,
        selected=selected,
        selected_cost=selected.cost,
        reason=(
            result.extraction.reason
            + "; complete physical candidate passed M39 policy "
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


@dataclass(frozen=True)
class _DocumentProduct:
    csr_markdown: str
    csr_json: str
    contracts_sva: str


@dataclass(frozen=True)
class _ReportProduct:
    implementation: str
    cost: str
    pipeline: str
    architecture: str
    exploration: str


# This table documents the stable orchestration DAG.  Conditional reuse (the
# configured semantic product aliases the check product when formal policy is
# off) never adds an undeclared downstream dependency.
COMPILATION_PRODUCT_DEPENDENCIES: Mapping[str, tuple[str, ...]] = {
    "syntax": (),
    "semantic": ("syntax",),
    "configured_semantic": ("syntax",),
    "selection": ("configured_semantic",),
    "formal": ("selection",),
    "planning": ("selection",),
    "target_instance": ("selection",),
    "documents": ("selection",),
    "reports": ("selection", "planning"),
    "materialized": (
        "syntax",
        "selection",
        "formal",
        "planning",
        "target_instance",
        "documents",
        "reports",
    ),
}


class CompilationSession:
    """One lazy, deterministic compilation dependency graph.

    ``semantic_ir`` and :meth:`check` intentionally use formal policy ``off``.
    They validate source semantics without executing formal candidate gates or
    constructing selected/backend/report products.  :meth:`materialize` uses
    the configured policy and reproduces the eager ``compile_source`` facade.
    """

    def __init__(
        self,
        source: str,
        *,
        formal_policy: FormalPolicy | str | None = None,
        formal_depth: int = 32,
        formal_max_candidates: int = 8,
        formal_timeout: int = 120,
        formal_cache: Path | str | None = None,
        formal_work_directory: Path | str | None = None,
        formal_verifier=None,
        top: str | None = None,
        target: str | None = None,
        architecture: str | None = None,
        architecture_mode: ArchitectureSelectionMode | str | None = None,
        target_evidence_policy: SourcePolicy | str | None = None,
        target_evidence: Iterable[object] | None = None,
        target_evidence_path: Path | str | None = None,
        target_tool: str = "Vivado",
        target_tool_version: str = "2024.2",
        target_clock_period_ns: float = 10.0,
        source_unit: str | None = None,
        module_resolver=None,
        root_module_identity=None,
        dependency_closure=None,
        implementation_backend: BackendKind | str | None = None,
        implementation_backend_mode: RequirementMode | str = RequirementMode.REQUIRED,
        implementation_contributions: tuple[ImplementationContribution, ...] = (),
        physical_inputs: PhysicalCompilationInputs | None = None,
    ) -> None:
        if not isinstance(source, str):
            raise TypeError("source must be text")
        self._source = source
        self._options = CompilationSessionOptions(
            formal_policy=formal_policy,
            formal_depth=formal_depth,
            formal_max_candidates=formal_max_candidates,
            formal_timeout=formal_timeout,
            formal_cache=None if formal_cache is None else Path(formal_cache),
            formal_work_directory=(
                None
                if formal_work_directory is None
                else Path(formal_work_directory)
            ),
            formal_verifier=formal_verifier,
            top=top,
            target=target,
            architecture=architecture,
            architecture_mode=architecture_mode,
            target_evidence_policy=target_evidence_policy,
            target_evidence=(
                None if target_evidence is None else tuple(target_evidence)
            ),
            target_evidence_path=(
                None if target_evidence_path is None else Path(target_evidence_path)
            ),
            target_tool=target_tool,
            target_tool_version=target_tool_version,
            target_clock_period_ns=target_clock_period_ns,
            source_unit=source_unit,
            module_resolver=module_resolver,
            root_module_identity=root_module_identity,
            dependency_closure=dependency_closure,
            implementation_backend=implementation_backend,
            implementation_backend_mode=implementation_backend_mode,
            implementation_contributions=tuple(implementation_contributions),
        )

        self._values: dict[str, object] = {}
        self._failures: dict[str, Exception] = {}
        self._evaluating: set[str] = set()
        self._lock = RLock()
        self._physical_inputs = physical_inputs or PhysicalCompilationInputs()
        self._formal_artifact_provider = FormalArtifactProvider(
            None
            if self._options.formal_cache is None
            else self._options.formal_cache / "artifacts"
        )
        # Construction is side-effect free.  Host tools are inspected only
        # when a requested M39 route first needs them.
        self._formal_tool_resolver = FormalToolResolver()

    @property
    def source(self) -> str:
        return self._source

    @property
    def options(self) -> CompilationSessionOptions:
        return self._options

    @property
    def physical_inputs(self) -> PhysicalCompilationInputs:
        """Physical inputs discovered by products demanded so far."""

        return self._physical_inputs

    @property
    def formal_artifact_provider(self) -> FormalArtifactProvider:
        """Session-owned memoization for formal preparation recipes."""

        return self._formal_artifact_provider

    @property
    def formal_tool_resolver(self) -> FormalToolResolver:
        """Lazy compiler-session-owned formal/backend discovery."""

        return self._formal_tool_resolver

    def __setattr__(self, name: str, value) -> None:
        if "_options" in self.__dict__ and (
            name in CompilationSessionOptions.__dataclass_fields__
            or name in {
                "source",
                "options",
                "physical_inputs",
                "formal_artifact_provider",
                "formal_tool_resolver",
                "semantic_candidate_site_ledger",
                "candidate_site_ledger",
            }
        ):
            raise AttributeError(f"compilation session option '{name}' is read-only")
        object.__setattr__(self, name, value)

    def __getattr__(self, name: str):
        # Preserve readable option attributes without exposing mutation.  This
        # hook runs only after ordinary instance lookup fails.
        options = self.__dict__.get("_options")
        if (
            options is not None
            and name in CompilationSessionOptions.__dataclass_fields__
        ):
            return getattr(options, name)
        raise AttributeError(name)

    @property
    def computed_products(self) -> tuple[str, ...]:
        """Products successfully computed so far, in dependency-table order."""

        return tuple(
            name for name in COMPILATION_PRODUCT_DEPENDENCIES if name in self._values
        )

    @property
    def failed_products(self) -> tuple[str, ...]:
        """Products whose original exception is cached by this session."""

        return tuple(
            name for name in COMPILATION_PRODUCT_DEPENDENCIES if name in self._failures
        )

    def _demand(self, name: str, builder: Callable[[], object]):
        if name not in COMPILATION_PRODUCT_DEPENDENCIES:
            raise KeyError(f"unknown compilation product '{name}'")
        with self._lock:
            if name in self._values:
                return self._values[name]
            if name in self._failures:
                raise self._failures[name]
            if name in self._evaluating:
                raise RuntimeError(f"compilation product dependency cycle at '{name}'")
            self._evaluating.add(name)
            try:
                value = builder()
            except Exception as error:
                self._failures[name] = error
                raise
            finally:
                self._evaluating.remove(name)
            self._values[name] = value
            return value

    @property
    def syntax(self) -> AstModule:
        return self._demand("syntax", self._build_syntax)

    def _build_syntax(self) -> AstModule:
        syntax = parse(self.source)
        if self.top is None:
            return syntax
        candidates = (syntax, *syntax.submodules)
        selected = next((item for item in candidates if item.name == self.top), None)
        if selected is None:
            raise SessionTopSelectionError(f"top module '{self.top}' was not found")
        return replace(
            selected,
            type_aliases=syntax.type_aliases,
            enums=syntax.enums,
            structs=syntax.structs,
            functions=syntax.functions,
            operators=syntax.operators,
            equivalences=syntax.equivalences,
            imports=syntax.imports,
            protocols=syntax.protocols,
            module_interfaces=syntax.module_interfaces,
            submodules=tuple(item for item in candidates if item.name != self.top),
        )

    def _contributions(self) -> tuple[ImplementationContribution, ...]:
        explicit = ImplementationContribution(
            PolicyOrigin("explicit compiler options"),
            backend=(
                None
                if self.implementation_backend is None
                else BackendRequest(
                    BackendKind(self.implementation_backend),
                    RequirementMode(self.implementation_backend_mode),
                )
            ),
            target=self.target,
            architecture=(
                None
                if self.architecture is None and self.architecture_mode is None
                else ArchitectureRequest(
                    self.architecture,
                    ArchitectureSelectionMode(
                        self.architecture_mode
                        if self.architecture_mode is not None
                        else ArchitectureSelectionMode.PREFERRED
                    ),
                )
            ),
            evidence_policy=(
                None
                if self.target_evidence_policy is None
                else SourcePolicy(self.target_evidence_policy)
            ),
            formal_policy=(
                None if self.formal_policy is None else FormalPolicy(self.formal_policy)
            ),
        )
        return (*self.implementation_contributions, explicit)

    def _formal_config(self, *, check_only: bool) -> FormalExplorationConfig:
        preliminary = merge_implementation_contributions(*self._contributions())
        dependency_identity = dependency_context_identity(
            SimpleNamespace(
                root_module_identity=self.root_module_identity,
                dependency_closure=self.dependency_closure,
            )
        )
        return FormalExplorationConfig(
            policy=(FormalPolicy.OFF if check_only else preliminary.formal_policy),
            bmc_depth=self.formal_depth,
            max_formal_candidates=self.formal_max_candidates,
            timeout_seconds=self.formal_timeout,
            cache_directory=self.formal_cache,
            work_directory=self._options.formal_work_directory,
            dependency_identity=dependency_identity,
            artifact_provider=self.formal_artifact_provider,
            tool_resolver=self.formal_tool_resolver,
            backend="direct_systemverilog",
        )

    def _analyze(self, *, check_only: bool) -> _AnalysisProduct:
        exploration_results: list[ExplorationResult] = []
        stdlib_sources: set[Path] = set()
        try:
            with track_resolved_stdlib_source_paths() as tracked_sources:
                stdlib_sources = tracked_sources
                module = analyze(
                    self.syntax,
                    exploration_results=exploration_results,
                    # Semantic typing always generates and ranks candidates
                    # statically.  M39 execution is owned by selection below.
                    formal_config=self._formal_config(check_only=True),
                    formal_verifier=None,
                    source_unit=self.source_unit,
                    source_digest=(
                        hashlib.sha256(self.source.encode()).hexdigest()
                        if self.source_unit is not None
                        else None
                    ),
                    module_resolver=self.module_resolver,
                    root_module_identity=self.root_module_identity,
                    dependency_closure=self.dependency_closure,
                )
        finally:
            self._physical_inputs = self._physical_inputs.with_stdlib_sources(
                stdlib_sources
            )
        if self.root_module_identity is not None or self.dependency_closure is not None:
            module = replace(
                module,
                root_module_identity=self.root_module_identity,
                dependency_closure=self.dependency_closure,
            )
        return _AnalysisProduct(module, tuple(exploration_results))

    @property
    def semantic_ir(self) -> IrModule:
        """Typed semantic IR without formal execution or later products."""

        product = self._demand("semantic", lambda: self._analyze(check_only=True))
        return product.module

    def check(self) -> IrModule:
        """Validate syntax and semantics, demanding no downstream product."""

        return self.semantic_ir

    @property
    def semantic_candidate_site_ledger(self) -> CandidateSiteLedger:
        """OFF-policy candidate catalog without selection or verifier work."""

        product = self._demand("semantic", lambda: self._analyze(check_only=True))
        return self._candidate_site_ledger(
            product.module, product.exploration_results
        )

    @staticmethod
    def _candidate_site_ledger(
        module: IrModule,
        exploration_results: Iterable[ExplorationResult],
    ) -> CandidateSiteLedger:
        """Build one ledger while preserving established source diagnostics."""

        try:
            return build_candidate_site_ledger(
                module, exploration_results
            )
        except CostExtractionError:
            # Candidate-site ranking uses the common M28 extractor.  For an
            # impossible legacy ``choice(auto)`` its generic error is less
            # useful than the established source-level diagnostic, which
            # names the output and every failed candidate constraint.
            extract_estimated_costs(module)
            raise
        except CandidateSiteError as error:
            raise SemanticError(str(error)) from error

    def _configured_analysis(self) -> _AnalysisProduct:
        # Compatibility product name retained for the stable demand graph.
        # It is now an alias of the one OFF-policy semantic analysis; all
        # configured formal work happens in ``_build_selection``.
        return self._demand("semantic", lambda: self._analyze(check_only=True))

    @property
    def _selection(self) -> _SelectionProduct:
        return self._demand("selection", self._build_selection)

    def _build_selection(self) -> _SelectionProduct:
        analysis = self._demand("configured_semantic", self._configured_analysis)
        semantic_ir = analysis.module
        exploration_results = analysis.exploration_results
        configured_formal = self._formal_config(check_only=False)
        static_ledger = self._candidate_site_ledger(
            semantic_ir, exploration_results
        )
        static_only = tuple(
            item for item in static_ledger.sites
            if item.rewrite_kind is CandidateRewriteKind.STATIC_ONLY
        )
        if (
            static_only
            and configured_formal.policy in {
                FormalPolicy.REQUIRED_BMC,
                FormalPolicy.REQUIRED_PROVEN,
            }
        ):
            kinds = ", ".join(sorted({item.kind.value for item in static_only}))
            raise SemanticError(
                "formal-required policy cannot gate retained candidate sites "
                f"without an M39 evidence attachment: {kinds}"
            )
        if configured_formal.policy is not FormalPolicy.OFF:
            has_elastic = any(
                item.kind.value == "elastic_pipeline"
                for item in static_ledger.sites
            )
            if has_elastic and configured_formal.policy in {
                FormalPolicy.REQUIRED_BMC,
                FormalPolicy.REQUIRED_PROVEN,
            }:
                raise SemanticError(
                    "formal-required policy has no M36 route for variable-latency "
                    "elastic pipeline(auto); M35 ready/valid safety remains available"
                )
            try:
                defer_physical = (
                    _defer_one_root_pipeline_to_physical_m39(semantic_ir)
                    and len(exploration_results) == 1
                    and exploration_results[0].site_kind == "implement"
                    and self.options.target not in {None, "generic"}
                )
                if not defer_physical:
                    semantic_ir, exploration_results = gate_retained_explorations(
                        semantic_ir,
                        exploration_results,
                        configured_formal,
                        self.formal_verifier,
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
                semantic_ir = _attach_unified_pipeline_formal_records(
                    semantic_ir,
                    exploration_results,
                )
                if not defer_physical and not _defer_one_root_pipeline_to_physical_m39(semantic_ir):
                    semantic_ir = _gate_all_standalone_pipelines(
                        semantic_ir,
                        configured_formal,
                        self.formal_verifier,
                        canonical_site_keys=(
                            exploration_site_key(item)
                            for item in exploration_results
                            if item.site_kind == "implement"
                        ),
                        backend=configured_formal.backend,
                    )
                semantic_ir = gate_structured_candidate_sites(
                    semantic_ir,
                    configured_formal,
                    self.formal_verifier,
                    backend=configured_formal.backend,
                )
            except ValueError as error:
                raise SemanticError(str(error)) from error
            if has_elastic and configured_formal.policy is FormalPolicy.AVAILABLE:
                semantic_ir = _with_elastic_formal_records(
                    semantic_ir,
                    configured_formal,
                    nondefault_reset=False,
                )
        backend_ir = inline_locals(semantic_ir)
        contributions = self._contributions()
        implementation_policy = normalize_implementation_policy(
            backend_ir,
            source_module=semantic_ir,
            exploration_results=exploration_results,
            external_contributions=contributions,
        )
        external_formal = replace(configured_formal, policy=FormalPolicy.OFF)
        backend_ir, external_explorations = apply_external_region_exploration(
            backend_ir,
            implementation_policy,
            external_contributions=contributions,
            formal_config=external_formal,
            formal_verifier=None,
        )
        if (
            external_explorations
            and configured_formal.policy is not FormalPolicy.OFF
            # A concrete target turns this external value site into a complete
            # value+schedule+resource candidate during planning. Gating the
            # pre-planning expression here would prove a different artifact
            # and, for newly partitioned DAGs, cannot emit the nested physical
            # boundaries. The planning-phase M39 gate below owns that route.
            and implementation_policy.request.target in {None, "generic"}
            and not _defer_one_root_pipeline_to_physical_m39(backend_ir)
        ):
            try:
                backend_ir, external_explorations = gate_retained_explorations(
                    backend_ir,
                    external_explorations,
                    configured_formal,
                    self.formal_verifier,
                    backend=configured_formal.backend,
                )
            except ValueError as error:
                raise SemanticError(str(error)) from error
        exploration_results = (*exploration_results, *external_explorations)
        high_level_ir = lower(backend_ir, stage=OptimizationStage.HIGH_LEVEL)
        source_typed_ir = restore(high_level_ir)
        if source_typed_ir != backend_ir:
            raise RuntimeError("canonical optimization IR did not restore semantic IR")
        source_typed_ir = _restore_selection_formal_records(
            backend_ir, source_typed_ir
        )
        extraction = extract_estimated_costs(source_typed_ir)
        optimization_ir = lower(
            extraction.module,
            stage=OptimizationStage.SELECTED_ARCHITECTURE,
        )
        typed_ir = restore(optimization_ir)
        if typed_ir != extraction.module:
            raise RuntimeError("extracted optimization IR did not restore semantic IR")
        # Solver/cache records are orchestration evidence and intentionally do
        # not participate in canonical IR identity.  Restore them recursively
        # after the semantic round-trip for reports and callers.
        typed_ir = _restore_selection_formal_records(backend_ir, typed_ir)
        candidate_site_ledger = self._candidate_site_ledger(
            typed_ir, exploration_results
        )
        return _SelectionProduct(
            module=typed_ir,
            high_level_ir=high_level_ir,
            optimization_ir=optimization_ir,
            extraction=extraction,
            implementation_policy=implementation_policy,
            implementation_request=implementation_policy.request,
            exploration_results=tuple(exploration_results),
            candidate_site_ledger=candidate_site_ledger,
        )

    @property
    def selected_ir(self) -> IrModule:
        return self._selection.module

    @property
    def high_level_ir(self) -> CanonicalModule:
        return self._selection.high_level_ir

    @property
    def optimization_ir(self) -> CanonicalModule:
        return self._selection.optimization_ir

    @property
    def implementation_policy(self) -> ModuleImplementationPolicy:
        return self._selection.implementation_policy

    @property
    def implementation_request(self) -> ImplementationRequest:
        return self._selection.implementation_request

    @property
    def candidate_site_ledger(self) -> CandidateSiteLedger:
        """Deterministic retained candidate sites after configured selection."""

        return self._selection.candidate_site_ledger

    @property
    def formal_products(self) -> _FormalProduct:
        return self._demand("formal", self._build_formal)

    def _build_formal(self) -> _FormalProduct:
        design = build_formal_design(self.selected_ir)
        recursive = build_recursive_formal_design(self.selected_ir)
        return _FormalProduct(
            design,
            emit_harness(design),
            recursive,
            emit_recursive_harness(recursive),
        )

    @property
    def planning(self) -> _PlanningProduct:
        return self._demand("planning", self._build_planning)

    @property
    def backend_implementation_plans(self) -> BackendImplementationPlanningResult:
        return self.planning.backend_plans

    def _build_planning(self) -> _PlanningProduct:
        selection = self._selection
        request = selection.implementation_request
        from zlang.pipeline_scheduling import (
            PipelineSchedulingError,
            TargetResourceOperationCostModel,
            schedule_module_fixed_pipelines,
        )

        try:
            cost_model = None
            if request.target not in {None, "generic"}:
                from zlang.targets import load_target

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
        plans = plan_backend_implementations(
            planned_module,
            backend_requests=(
                (request.backend,) if request.backend is not None else ()
            ),
            target=request.target,
            architecture=request.architecture.identity,
            architecture_mode=request.architecture.mode,
            source_policy=request.evidence_policy,
            evidence=self.target_evidence,
            evidence_path=self.target_evidence_path,
            tool=self.target_tool,
            tool_version=self.target_tool_version,
            clock_period_ns=self.target_clock_period_ns,
            strict_target_planning=request.backend is None,
        )
        target_result = plans.target_planning_result
        physical_formal_records: tuple[FormalExplorationRecord, ...] = ()
        if (
            target_result is not None
            and request.formal_policy is not FormalPolicy.OFF
            # The bounded single-root path owns one complete
            # value+schedule+resource gate here. Multi-site designs retain the
            # established selection-time M39 route and never receive a second
            # solver invocation.
            and _defer_one_root_pipeline_to_physical_m39(planned_module)
            and (
                not planned_module.pipeline_explorations
                or request.target not in {None, "generic"}
            )
        ):
            try:
                plans, target_result, _, physical_formal_records = (
                    _gate_physical_target_candidates(
                        planned_module,
                        plans,
                        self._formal_config(check_only=False),
                        self.formal_verifier,
                    )
                )
            except ValueError as error:
                raise SemanticError(str(error)) from error
        if request.backend is None:
            graph = (
                target_result.selected_graph
                if target_result is not None
                else select_implementation_graph(
                    planned_module,
                    target=request.target,
                    architecture=request.architecture.identity,
                    mode=request.architecture.mode,
                )
            )
        else:
            graph = plans.plan_for(request.backend.kind).graph
        return _PlanningProduct(
            planned_module,
            plans,
            target_result,
            graph,
            physical_formal_records,
        )

    @property
    def target_instance(self):
        return self._demand("target_instance", self._build_target_instance)

    def _build_target_instance(self):
        request = self._selection.implementation_request
        if request.target is None or request.target == "generic":
            return None
        from zlang.targets import load_target

        return load_target(request.target)[0]

    @property
    def documents(self) -> _DocumentProduct:
        return self._demand("documents", self._build_documents)

    def _build_documents(self) -> _DocumentProduct:
        module = self.selected_ir
        return _DocumentProduct(
            emit_csr_markdown(module) if module.csr_blocks else "",
            emit_csr_json(module) if module.csr_blocks else "",
            emit_contracts(module)
            if module.contracts or module.verification_scopes else "",
        )

    @property
    def reports(self) -> _ReportProduct:
        return self._demand("reports", self._build_reports)

    def _build_reports(self) -> _ReportProduct:
        selection = self._selection
        planning = self.planning
        implementation_report = render_implementation_report(planning.module)
        if not implementation_report and selection.exploration_results:
            implementation_report = render_exploration_report(
                selection.exploration_results
            )
        architecture_report = render_architecture_report(planning.module)
        if not architecture_report and selection.exploration_results:
            # ``--architecture-report`` remains a compatibility output alias,
            # but canonical ``implement`` regions no longer manufacture a
            # source-level ArchitectureExploration node.  Expose the same
            # unified candidate report instead of returning an empty artifact.
            architecture_report = render_exploration_report(
                selection.exploration_results
            )
        return _ReportProduct(
            implementation_report,
            render_cost_report(selection.extraction),
            render_pipeline_report(planning.module)
            + (
                planning.target_planning_result.report
                if planning.target_planning_result
                else ""
            ),
            architecture_report,
            render_exploration_report(selection.exploration_results),
        )

    def materialize(self) -> CompilationResult:
        """Demand every product needed by the eager compatibility facade."""

        return self._demand("materialized", self._build_materialized)

    def _build_materialized(self) -> CompilationResult:
        selection = self._selection
        formal = self.formal_products
        planning = self.planning
        target_instance = self.target_instance
        documents = self.documents
        reports = self.reports
        return CompilationResult(
            ast=self.syntax,
            ir=planning.module,
            optimization_ir=selection.optimization_ir,
            csr_markdown=documents.csr_markdown,
            csr_json=documents.csr_json,
            contracts_sva=documents.contracts_sva,
            implementation_report=reports.implementation,
            cost_report=reports.cost,
            pipeline_report=reports.pipeline,
            architecture_report=reports.architecture,
            high_level_ir=selection.high_level_ir,
            exploration_report=reports.exploration,
            exploration_results=selection.exploration_results,
            formal_design=formal.design,
            formal_harness=formal.harness,
            recursive_formal_design=formal.recursive_design,
            recursive_formal_harness=formal.recursive_harness,
            target_instance=target_instance,
            implementation_graph=planning.implementation_graph,
            target_planning_result=planning.target_planning_result,
            target_planner_report=(
                planning.target_planning_result.report
                if planning.target_planning_result
                else ""
            ),
            implementation_policy=selection.implementation_policy,
            implementation_request=selection.implementation_request,
            implementation_regions=tuple(
                item.region for item in selection.implementation_policy.regions
            ),
            implementation_policy_report=selection.implementation_policy.report,
            backend_implementation_plans=planning.backend_plans,
            backend_implementation_report=planning.backend_plans.report,
            physical_inputs=self.physical_inputs,
            formal_artifact_provider=self.formal_artifact_provider,
            formal_tool_resolver=self.formal_tool_resolver,
            candidate_site_ledger=selection.candidate_site_ledger,
            physical_formal_records=planning.physical_formal_records,
        )


def inline_locals(module: IrModule) -> IrModule:
    """Erase pure local bindings before canonical and backend stages."""

    children = tuple(inline_locals(child) for child in module.children)
    if not module.locals:
        return (
            replace(module, children=children)
            if children != module.children
            else module
        )
    values = {local.name: local.expression for local in module.locals}

    def walk(value):
        if isinstance(value, ir_expr.InputRef) and value.name in values:
            return walk(values[value.name])
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        if is_dataclass(value):
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
                return replace(value, **updates)
            except (TypeError, ValueError):
                return value
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
            replace(register, initial=walk(register.initial))
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


__all__ = [
    "COMPILATION_PRODUCT_DEPENDENCIES",
    "CompilationSession",
    "CompilationSessionOptions",
    "SessionTopSelectionError",
    "inline_locals",
]
