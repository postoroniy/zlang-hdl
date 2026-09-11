"""Compiler-owned direct-SystemVerilog M36 evidence for selected candidates.

The source semantics and reference model remain backend-independent. This
module prepares and executes the single production RTL leg.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock

from zlang.backend.manifest import MANIFEST_VERSION
from zlang.candidate_sites import (
    CandidateSiteError,
    SelectedCandidateSite,
    candidate_formal_record_sites,
    candidate_owner_formal_domain,
    selected_candidate_sites,
)
from zlang.common import stable_digest
from zlang.equivalence import run_equivalence_formal
from zlang.equivalence_result_codec import (
    equivalence_result_from_data,
    equivalence_result_to_data,
)
from zlang.formal import FormalToolchainContext, use_formal_toolchain
from zlang.formal_artifact_provider import (
    FormalArtifactNamespace,
    FormalArtifactProvider,
    FormalArtifactRecipe,
    decisive_formal_cacheable,
    formal_backend_artifact_ref,
)
from zlang.formal_candidate import (
    FormalCandidateUnavailable,
    M36DirectSystemVerilogCandidateVerifier,
    PreparedCandidateEquivalence,
    prepared_candidate_equivalence_from_data,
    prepared_candidate_equivalence_to_data,
    validate_prepared_equivalence_domains,
)
from zlang.formal_exploration import (
    FormalExplorationConfig,
    FormalExplorationRecord,
    FormalPolicy,
    formal_execution_recipe_identity,
)
from zlang.formal_orchestration import (
    CandidateEquivalenceExecutionReport,
    CandidateEquivalencePlanReference,
    CompilerFormalExecutionPlan,
    FormalOrchestrationError,
)
from zlang.ir.cdc import ClockDomain
from zlang.ir.equivalence import (
    EquivalenceMode,
    EquivalenceProperty,
    EquivalenceResult,
    EquivalenceStatus,
)
from zlang.ir.formal import ProofMode
from zlang.ir.formal_planning import (
    FormalExecutableRoute,
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalRouteKind,
)


@dataclass(frozen=True)
class PreparedCandidateEquivalenceSite:
    """Prepared, solver-unexecuted direct-SV M36 plan for one site."""

    selected: SelectedCandidateSite
    plan: CandidateEquivalencePlanReference
    property: EquivalenceProperty
    direct_systemverilog: PreparedCandidateEquivalence

    def __post_init__(self) -> None:
        if self.plan.site_identity != self.selected.site.identity:
            raise FormalOrchestrationError(
                "prepared candidate plan references a different semantic site"
            )
        if self.plan.candidate_identity != self.selected.site.selected_candidate_identity:
            raise FormalOrchestrationError(
                "prepared candidate plan references a different selected candidate"
            )
        if self.property.id != self.plan.direct_systemverilog_m36.property_identity:
            raise FormalOrchestrationError(
                "prepared candidate property differs from its typed M36 plan"
            )
        if self.property != self.direct_systemverilog.property:
            raise FormalOrchestrationError(
                "prepared direct-SystemVerilog artifact uses a different property"
            )

    def freeze(self) -> "FrozenCandidateEquivalenceSite":
        return FrozenCandidateEquivalenceSite(
            self.plan, self.property, self.direct_systemverilog
        )


@dataclass(frozen=True)
class FrozenCandidateEquivalenceSite:
    """Immutable direct-SV M36 inputs for replay without source selection."""

    plan: CandidateEquivalencePlanReference
    property: EquivalenceProperty
    direct_systemverilog: PreparedCandidateEquivalence

    def __post_init__(self) -> None:
        prepared = self.direct_systemverilog
        plan = self.plan.direct_systemverilog_m36
        if self.property.id != plan.property_identity:
            raise FormalOrchestrationError(
                "frozen M36 property differs from its compiler plan"
            )
        if self.property.implementation_root != self.plan.candidate_identity:
            raise FormalOrchestrationError(
                "frozen M36 property references a different selected candidate"
            )
        if prepared.property != self.property or prepared.backend != "direct_systemverilog":
            raise FormalOrchestrationError(
                "frozen M36 input is not the planned direct-SystemVerilog leg"
            )
        try:
            validate_prepared_equivalence_domains(
                self.property,
                prepared.reference_artifact,
                prepared.implementation_artifact,
            )
        except ValueError as error:
            raise FormalOrchestrationError(
                f"frozen M36 physical domain is invalid: {error}"
            ) from error
        if prepared.implementation_artifact is None or plan.route is None:
            raise FormalOrchestrationError(
                "frozen direct-SystemVerilog M36 route is incomplete"
            )
        if plan.route.reference_identity != prepared.reference_artifact_hash:
            raise FormalOrchestrationError(
                "frozen M36 reference hash differs from its plan"
            )
        if plan.route.artifacts != (
            formal_backend_artifact_ref(prepared.implementation_artifact),
        ):
            raise FormalOrchestrationError("frozen M36 artifact differs from its plan")

    def _payload_data(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_data(),
            "direct_systemverilog": prepared_candidate_equivalence_to_data(
                self.direct_systemverilog
            ),
        }

    @property
    def replay_identity(self) -> str:
        return "candidate-equivalence-replay:" + stable_digest(self._payload_data())

    def to_data(self) -> dict[str, object]:
        return {
            "schema": "zlang-frozen-candidate-equivalence-site-v3",
            **self._payload_data(),
            "replay_identity": self.replay_identity,
        }

    @classmethod
    def from_data(cls, value: object) -> "FrozenCandidateEquivalenceSite":
        if not isinstance(value, dict) or set(value) != {
            "schema", "plan", "direct_systemverilog", "replay_identity",
        }:
            raise FormalOrchestrationError(
                "frozen candidate equivalence fields differ from the current schema"
            )
        if value["schema"] != "zlang-frozen-candidate-equivalence-site-v3":
            raise FormalOrchestrationError(
                "unsupported frozen candidate equivalence schema"
            )
        try:
            plan = CandidateEquivalencePlanReference.from_data(value["plan"])
            prepared = prepared_candidate_equivalence_from_data(
                value["direct_systemverilog"]
            )
        except (TypeError, ValueError) as error:
            raise FormalOrchestrationError(str(error)) from error
        restored = cls(plan, prepared.property, prepared)
        if value["replay_identity"] != restored.replay_identity:
            raise FormalOrchestrationError(
                "frozen candidate replay identity does not match its contents"
            )
        return restored


def _m36_plan(
    site: SelectedCandidateSite,
    property_: EquivalenceProperty,
    prepared: PreparedCandidateEquivalence,
) -> FormalGoalPlan:
    if prepared.implementation_artifact is None:
        raise FormalOrchestrationError(
            "prepared direct-SystemVerilog M36 route has no implementation artifact"
        )
    route = FormalExecutableRoute(
        FormalRouteKind.SEMANTIC_EQUIVALENCE,
        (formal_backend_artifact_ref(prepared.implementation_artifact),),
        reference_identity=prepared.reference_artifact_hash,
    )
    observations = tuple(dict.fromkeys((
        *property_.inputs, property_.implementation_output,
    )))
    return FormalGoalPlan(
        "goal:m36:" + stable_digest({
            "site": site.site.identity,
            "candidate": site.site.selected_candidate_identity,
            "backend": "direct_systemverilog",
            "property": property_.id,
        }),
        property_.id,
        FormalPlanGoalKind.M36_EQUIVALENCE,
        property_.implementation_clock,
        property_.implementation_reset,
        (),
        observations,
        site.site.selected_candidate_identity,
        property_.comparison_window,
        property_.comparison_window.minimum_bmc_depth,
        route=route,
        source_origin=property_.source_origin,
    )


def _selected_m39_record(
    compilation: object,
    site: SelectedCandidateSite,
    mode: EquivalenceMode,
) -> FormalExplorationRecord | None:
    module = getattr(compilation, "ir", None)
    if module is None:
        return None
    expected = ProofMode.BMC if mode is EquivalenceMode.BMC else ProofMode.PROVE
    matches = tuple(
        record
        for derived, record in candidate_formal_record_sites(
            module, getattr(compilation, "exploration_results", ())
        )
        if derived.identity == site.site.identity
        and isinstance(record, FormalExplorationRecord)
        and record.candidate_identity == site.site.selected_candidate_identity
        and record.mode is expected
    )
    if len(matches) > 1:
        raise FormalOrchestrationError(
            f"candidate site '{site.site.identity}' retains duplicate selected M39 records"
        )
    return matches[0] if matches else None


def _selected_owner_domain_for_reuse(
    compilation: object,
    selected: SelectedCandidateSite,
    prepared: PreparedCandidateEquivalence,
) -> ClockDomain | None:
    if prepared.property.clock_domain_contract is not None:
        return prepared.property.clock_domain_contract
    try:
        domain, limitation = candidate_owner_formal_domain(
            getattr(compilation, "ir", None), selected.site.owner_identity
        )
    except (AttributeError, CandidateSiteError, TypeError):
        return None
    return None if limitation is not None else domain


def _m39_recipe_matches(
    record: FormalExplorationRecord | None,
    prepared: PreparedCandidateEquivalence,
    selected: SelectedCandidateSite,
    config: FormalExplorationConfig,
    mode: EquivalenceMode,
    provider: FormalArtifactProvider,
    clock_domain_contract: ClockDomain | None = None,
) -> bool:
    if record is None or record.status is None:
        return False
    expected_mode = ProofMode.BMC if mode is EquivalenceMode.BMC else ProofMode.PROVE
    if not (
        record.formal_route == "M36_direct_systemverilog"
        and record.candidate_identity == prepared.property.implementation_root
        and record.policy is config.policy
        and record.mode is expected_mode
        and record.depth == config.bmc_depth
        and record.engine == config.engine
        and record.solver == config.solver
        and record.backend == "direct_systemverilog"
        and record.property_identity == prepared.property_identity
        and record.reference_artifact_hash == prepared.reference_artifact_hash
        and record.implementation_artifact_hash == prepared.implementation_artifact_hash
        and record.artifact_hash == prepared.implementation_artifact_hash
        and record.harness_hash == prepared.harness_hash
        and record.assumptions_identity == prepared.assumptions_identity
        and record.backend_identity == prepared.backend_identity
    ):
        return False
    stage_policy = (
        FormalPolicy.REQUIRED_BMC
        if record.policy is FormalPolicy.REQUIRED_PROVEN and mode is EquivalenceMode.BMC
        else record.policy
    )
    verifier = M36DirectSystemVerilogCandidateVerifier(
        selected.reference_expression,
        candidate_class=selected.candidate_class,
        artifact_provider=provider,
        clock_domain_contract=clock_domain_contract,
    )
    expected_recipe = formal_execution_recipe_identity(
        selected.candidate,
        replace(config, policy=stage_policy),
        verifier,
        {
            "property_identity": prepared.property_identity,
            "artifact_hash": prepared.implementation_artifact_hash,
            "reference_artifact_hash": prepared.reference_artifact_hash,
            "implementation_artifact_hash": prepared.implementation_artifact_hash,
            "harness_hash": prepared.harness_hash,
            "assumptions_identity": prepared.assumptions_identity,
            "backend_identity": prepared.backend_identity,
        },
    )
    return record.execution_recipe_identity == expected_recipe


def _equivalence_from_m39(
    record: FormalExplorationRecord,
    prepared: PreparedCandidateEquivalence,
) -> EquivalenceResult:
    assert record.status is not None and record.mode is not None
    return EquivalenceResult(
        prepared.property.id,
        EquivalenceStatus(record.status.value),
        EquivalenceMode(record.mode.value),
        record.engine,
        record.solver,
        record.depth,
        prepared.property.relation_kind,
        prepared.property.latency_delta,
        "direct_systemverilog",
        prepared.reference_artifact_hash,
        prepared.implementation_artifact_hash,
        MANIFEST_VERSION,
        prepared.property.implementation_root,
        record.source_origin or prepared.property.source_origin,
        record.selected_origin or prepared.property.selected_origin,
        record.counterexample,
        record.proof_reason or record.reason or None,
    )


def _m36_execution_recipe(
    prepared: PreparedCandidateEquivalence,
    config: FormalExplorationConfig,
    mode: EquivalenceMode,
    toolchain: FormalToolchainContext,
) -> dict[str, object]:
    return {
        "schema": "zlang-candidate-m36-execution-v2",
        "property": prepared.property_identity,
        "candidate": prepared.property.implementation_root,
        "backend": "direct_systemverilog",
        "reference_artifact": prepared.reference_artifact_hash,
        "implementation_artifact": prepared.implementation_artifact_hash,
        "harness": prepared.harness_hash,
        "assumptions": prepared.assumptions_identity,
        "backend_identity": prepared.backend_identity,
        "formal_tool_versions": [list(item) for item in toolchain.versions],
        "formal_tools_missing": list(toolchain.missing),
        "mode": mode.value,
        "depth": config.bmc_depth,
        "timeout_seconds": config.timeout_seconds,
        "engine": config.engine,
        "solver": config.solver,
    }


def _execute_m36(
    prepared: PreparedCandidateEquivalence,
    config: FormalExplorationConfig,
    mode: EquivalenceMode,
    provider: FormalArtifactProvider,
    toolchain: FormalToolchainContext,
) -> EquivalenceResult:
    recipe = _m36_execution_recipe(prepared, config, mode, toolchain)
    return provider.get_or_prepare(
        FormalArtifactNamespace.M36,
        "candidate-equivalence-result-v2",
        recipe,
        lambda: run_equivalence_formal(
            prepared.property,
            prepared.source,
            top=prepared.top,
            backend="direct_systemverilog",
            mode=mode,
            depth=config.bmc_depth,
            solver=config.solver,
            reference_hash=prepared.reference_artifact_hash,
            implementation_hash=prepared.implementation_artifact_hash,
            timeout_seconds=config.timeout_seconds,
            work_directory=config.work_directory,
            trace_metadata=prepared.trace_metadata,
        ),
        encode=equivalence_result_to_data,
        decode=equivalence_result_from_data,
        cacheable=decisive_formal_cacheable,
    )


def _provider_and_config(
    compilation: object | None,
    config: FormalExplorationConfig,
) -> tuple[FormalArtifactProvider, FormalExplorationConfig]:
    provider = getattr(compilation, "formal_artifact_provider", None)
    if not isinstance(provider, FormalArtifactProvider):
        provider = getattr(config, "artifact_provider", None)
    if not isinstance(provider, FormalArtifactProvider):
        provider = FormalArtifactProvider(config.cache_directory)
    return provider, replace(config, artifact_provider=provider)


def _candidate_job_work_directory(
    config: FormalExplorationConfig,
    *,
    recipe: dict[str, object],
) -> Path | None:
    if config.work_directory is None:
        return None
    identity = FormalArtifactRecipe(
        FormalArtifactNamespace.M36, "candidate-equivalence-result-v2", recipe
    ).digest
    return (
        Path(config.work_directory).resolve(strict=False)
        / "candidate-equivalence"
        / identity
    )


def prepare_selected_candidate_equivalence(
    compilation: object,
    compiler_plan: CompilerFormalExecutionPlan,
    config: FormalExplorationConfig,
) -> tuple[CompilerFormalExecutionPlan, tuple[PreparedCandidateEquivalenceSite, ...]]:
    """Prepare direct-SV M36 plans without running a solver."""

    if config.policy is FormalPolicy.OFF:
        return compiler_plan, ()
    if compiler_plan.formal_policy is not config.policy:
        raise FormalOrchestrationError(
            "candidate equivalence policy differs from the compiler formal plan"
        )
    provider, config = _provider_and_config(compilation, config)
    selected_sites = selected_candidate_sites(
        getattr(compilation, "ir"), getattr(compilation, "exploration_results", ())
    )
    ledger_sites = {
        item.identity: item for item in compiler_plan.candidate_site_ledger.sites
    }
    prepared_sites = []
    plans = []
    for selected in selected_sites:
        if ledger_sites.get(selected.site.identity) != selected.site:
            raise FormalOrchestrationError(
                f"selected candidate site '{selected.site.identity}' differs from its ledger"
            )
        domain, limitation = candidate_owner_formal_domain(
            getattr(compilation, "ir"), selected.site.owner_identity
        )
        verifier = M36DirectSystemVerilogCandidateVerifier(
            selected.reference_expression,
            candidate_class=selected.candidate_class,
            artifact_provider=provider,
            clock_domain_contract=domain,
            unavailable_reason=limitation,
        )
        try:
            prepared = verifier.prepare(selected.candidate, config)
        except FormalCandidateUnavailable:
            continue
        property_ = prepared.property
        if not isinstance(property_, EquivalenceProperty):
            raise FormalOrchestrationError(
                "candidate preparation did not return typed M36 property IR"
            )
        plan = CandidateEquivalencePlanReference(
            selected.site.identity,
            selected.site.selected_candidate_identity,
            _m36_plan(selected, property_, prepared),
            property_.implementation_output,
        )
        plans.append(plan)
        prepared_sites.append(PreparedCandidateEquivalenceSite(
            selected, plan, property_, prepared
        ))
    return (
        replace(compiler_plan, candidate_equivalence_plans=tuple(plans)),
        tuple(prepared_sites),
    )


def _execute_candidate_equivalence(
    compilation: object | None,
    compiler_plan: CompilerFormalExecutionPlan,
    prepared_sites: tuple[PreparedCandidateEquivalenceSite | FrozenCandidateEquivalenceSite, ...],
    config: FormalExplorationConfig,
    *,
    jobs: int = 1,
) -> tuple[CandidateEquivalenceExecutionReport, ...]:
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise FormalOrchestrationError("candidate equivalence jobs must be positive")
    if config.policy is FormalPolicy.OFF:
        if prepared_sites:
            raise FormalOrchestrationError(
                "formal-policy off cannot execute prepared candidate evidence"
            )
        return ()
    provider, config = _provider_and_config(compilation, config)
    if tuple(item.plan for item in prepared_sites) != compiler_plan.candidate_equivalence_plans:
        raise FormalOrchestrationError(
            "prepared candidate equivalence plans differ from compiler plan"
        )
    context: FormalToolchainContext | None = None
    context_lock = Lock()

    def toolchain() -> FormalToolchainContext:
        nonlocal context
        with context_lock:
            if context is None:
                resolve = getattr(config.tool_resolver, "formal_context", None)
                context = (
                    resolve(engine=config.engine, solver=config.solver)
                    if callable(resolve)
                    else FormalToolchainContext.discover(
                        engine=config.engine, solver=config.solver
                    )
                )
            return context

    def execute_site(
        item: PreparedCandidateEquivalenceSite | FrozenCandidateEquivalenceSite,
    ) -> CandidateEquivalenceExecutionReport:
        prepared = item.direct_systemverilog
        selected = item.selected if isinstance(item, PreparedCandidateEquivalenceSite) else None
        work_directories: dict[str, str] = {}

        def execute(mode: EquivalenceMode) -> EquivalenceResult:
            if compilation is not None and selected is not None:
                retained = _selected_m39_record(compilation, selected, mode)
                if _m39_recipe_matches(
                    retained, prepared, selected, config, mode, provider,
                    _selected_owner_domain_for_reuse(compilation, selected, prepared),
                ):
                    assert retained is not None
                    if retained.work_directory is not None:
                        work_directories[f"m36:{mode.value}"] = retained.work_directory
                    return _equivalence_from_m39(retained, prepared)
            active = toolchain()
            recipe = _m36_execution_recipe(prepared, config, mode, active)
            directory = _candidate_job_work_directory(config, recipe=recipe)
            job_config = replace(config, work_directory=directory)
            with use_formal_toolchain(active):
                result = _execute_m36(prepared, job_config, mode, provider, active)
            if directory is not None and directory.is_dir():
                work_directories[f"m36:{mode.value}"] = str(directory)
            return result

        bounded = execute(EquivalenceMode.BMC)
        if (
            config.policy is FormalPolicy.REQUIRED_PROVEN
            and bounded.status is EquivalenceStatus.BOUNDED_PASS
        ):
            final = execute(EquivalenceMode.PROVE)
            return CandidateEquivalenceExecutionReport(
                item.plan,
                final,
                bounded,
                work_directories=tuple(work_directories.items()),
            )
        return CandidateEquivalenceExecutionReport(
            item.plan,
            bounded,
            work_directories=tuple(work_directories.items()),
        )

    if jobs == 1 or len(prepared_sites) < 2:
        reports = tuple(execute_site(item) for item in prepared_sites)
    else:
        with ThreadPoolExecutor(
            max_workers=min(jobs, len(prepared_sites)),
            thread_name_prefix="zlang-candidate-formal",
        ) as executor:
            reports = tuple(executor.map(execute_site, prepared_sites))
    versions = () if context is None else context.versions
    return tuple(replace(item, tool_versions=versions) for item in reports)


def execute_prepared_candidate_equivalence(
    compilation: object,
    compiler_plan: CompilerFormalExecutionPlan,
    prepared_sites: tuple[PreparedCandidateEquivalenceSite, ...],
    config: FormalExplorationConfig,
    *,
    jobs: int = 1,
) -> tuple[CandidateEquivalenceExecutionReport, ...]:
    return _execute_candidate_equivalence(
        compilation, compiler_plan, prepared_sites, config, jobs=jobs
    )


def execute_frozen_candidate_equivalence(
    compiler_plan: CompilerFormalExecutionPlan,
    frozen_sites: tuple[FrozenCandidateEquivalenceSite, ...],
    config: FormalExplorationConfig,
    *,
    jobs: int = 1,
) -> tuple[CandidateEquivalenceExecutionReport, ...]:
    return _execute_candidate_equivalence(
        None, compiler_plan, frozen_sites, config, jobs=jobs
    )


def execute_selected_candidate_equivalence(
    compilation: object,
    compiler_plan: CompilerFormalExecutionPlan,
    config: FormalExplorationConfig,
    *,
    jobs: int = 1,
) -> tuple[CompilerFormalExecutionPlan, tuple[CandidateEquivalenceExecutionReport, ...]]:
    enriched, prepared = prepare_selected_candidate_equivalence(
        compilation, compiler_plan, config
    )
    return enriched, execute_prepared_candidate_equivalence(
        compilation, enriched, prepared, config, jobs=jobs
    )


__all__ = [
    "FrozenCandidateEquivalenceSite",
    "PreparedCandidateEquivalenceSite",
    "execute_frozen_candidate_equivalence",
    "execute_prepared_candidate_equivalence",
    "execute_selected_candidate_equivalence",
    "prepare_selected_candidate_equivalence",
]
