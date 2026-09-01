"""Compiler-owned execution of the existing candidate M36/M38 routes.

This module is intentionally a thin orchestration layer.  It does not define
new equivalence semantics, participate in M39 eligibility, or widen the frozen
M36/M38 feature subset.  It prepares the final candidate selected at an exact
``CandidateSiteLedger`` site, reuses compatible M39 Clash evidence, executes
the missing semantic-reference leg, and only then attempts advisory M38.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import json
from pathlib import Path
from threading import Lock

from zlang.backend.manifest import BackendArtifact, MANIFEST_VERSION
from zlang.candidate_sites import (
    CandidateSiteError,
    SelectedCandidateSite,
    candidate_owner_formal_domain,
    candidate_formal_record_sites,
    selected_candidate_sites,
)
from zlang.common import stable_digest
from zlang.cross_backend import run_cross_backend_formal
from zlang.equivalence import run_equivalence_formal
from zlang.equivalence_result_codec import (
    cross_backend_result_from_data,
    cross_backend_result_to_data,
    equivalence_result_from_data,
    equivalence_result_to_data,
)
from zlang.formal import FormalToolchainContext, use_formal_toolchain
from zlang.formal_artifact_provider import (
    FormalArtifactNamespace,
    FormalArtifactProvider,
    FormalArtifactRecipe,
    decisive_formal_cacheable,
)
from zlang.formal_candidate import (
    FormalCandidateUnavailable,
    M36ClashCandidateVerifier,
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
from zlang.ir.cross_backend import (
    CrossBackendMode,
    CrossBackendProperty,
    CrossBackendRelation,
    CrossBackendResult,
    CrossBackendStatus,
)
from zlang.ir.cdc import ClockDomain, clock_domain_data, clock_domain_from_data
from zlang.ir.equivalence import (
    EquivalenceMode,
    EquivalenceProperty,
    EquivalenceResult,
    EquivalenceStatus,
)
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.ir.formal_planning import (
    FormalBackendArtifactRef,
    FormalExecutableRoute,
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalRouteKind,
    FormalSkipCode,
    FormalSkipReason,
)
from zlang.triangular_evidence import M36EvidenceLeg, M38EvidenceReport


_DECISIVE_M36 = frozenset({
    EquivalenceStatus.BOUNDED_PASS,
    EquivalenceStatus.PROVEN,
    EquivalenceStatus.FAILED,
})


@dataclass(frozen=True)
class PreparedCandidateEquivalenceSite:
    """Backend-prepared, solver-unexecuted equivalence plan for one site."""

    selected: SelectedCandidateSite
    plan: CandidateEquivalencePlanReference
    property: EquivalenceProperty
    m38_property: CrossBackendProperty
    clash: PreparedCandidateEquivalence | None
    direct_systemverilog: PreparedCandidateEquivalence | None

    def __post_init__(self) -> None:
        if self.plan.site_identity != self.selected.site.identity:
            raise FormalOrchestrationError(
                "prepared candidate plan references a different semantic site"
            )
        if self.plan.candidate_identity != self.selected.site.selected_candidate_identity:
            raise FormalOrchestrationError(
                "prepared candidate plan references a different selected candidate"
            )
        if self.property.id != self.plan.clash_m36.property_identity:
            raise FormalOrchestrationError(
                "prepared candidate property differs from its typed M36 plan"
            )
        if (
            self.property.implementation_output
            != self.plan.implementation_observable_identity
        ):
            raise FormalOrchestrationError(
                "prepared candidate output differs from its typed observable"
            )
        if self.m38_property.id != self.plan.m38.property_identity:
            raise FormalOrchestrationError(
                "prepared M38 property differs from its typed plan"
            )
        if (
            self.m38_property.clock_domain_contract
            != self.property.clock_domain_contract
        ):
            raise FormalOrchestrationError(
                "prepared M38 physical domain differs from its M36 property"
            )

    def freeze(self) -> "FrozenCandidateEquivalenceSite":
        """Drop selection-only expressions while retaining exact proof inputs."""

        return FrozenCandidateEquivalenceSite(
            self.plan,
            self.property,
            self.m38_property,
            self.clash,
            self.direct_systemverilog,
        )


def _cross_backend_property_data(
    property_: CrossBackendProperty,
) -> dict[str, object]:
    return {
        "id": property_.id,
        "relation": property_.relation.value,
        "selected_ir_identity": property_.selected_ir_identity,
        "observable_ids": list(property_.observable_ids),
        "clock_domain": property_.clock_domain,
        "reset_domain": property_.reset_domain,
        "left_latency": property_.left_latency,
        "right_latency": property_.right_latency,
        "left_ii": property_.left_ii,
        "right_ii": property_.right_ii,
        "comparison_window": property_.comparison_window.to_data(),
        "clock_domain_contract": clock_domain_data(
            property_.clock_domain_contract
        ),
        "source_origin": (
            None
            if property_.source_origin is None
            else property_.source_origin.to_data()
        ),
    }


def _cross_backend_property_from_data(value: object) -> CrossBackendProperty:
    from zlang.ir.comparison_window import ComparisonWindow, ComparisonWindowKind
    from zlang.source import SourceOrigin

    if not isinstance(value, dict) or set(value) != {
        "id", "relation", "selected_ir_identity", "observable_ids",
        "clock_domain", "reset_domain", "left_latency", "right_latency",
        "left_ii", "right_ii", "comparison_window", "source_origin",
        "clock_domain_contract",
    }:
        raise FormalOrchestrationError(
            "frozen M38 property fields differ from the current schema"
        )
    observables = value["observable_ids"]
    window = value["comparison_window"]
    if not isinstance(observables, list) or not all(
        isinstance(item, str) and item for item in observables
    ):
        raise FormalOrchestrationError("frozen M38 observables must be strings")
    if not isinstance(window, dict) or set(window) != {
        "kind", "fill_cycles", "reset_release_cycles",
        "first_comparison_cycle", "minimum_bmc_depth",
    }:
        raise FormalOrchestrationError("frozen M38 comparison window is invalid")
    try:
        typed_window = ComparisonWindow(
            ComparisonWindowKind(window["kind"]),
            window["fill_cycles"],
            window["reset_release_cycles"],
        )
    except (TypeError, ValueError) as error:
        raise FormalOrchestrationError(str(error)) from error
    if window != typed_window.to_data():
        raise FormalOrchestrationError(
            "frozen M38 comparison-window metadata is inconsistent"
        )
    origin_value = value["source_origin"]
    if origin_value is not None and not isinstance(origin_value, dict):
        raise FormalOrchestrationError("frozen M38 source origin must be an object")
    try:
        return CrossBackendProperty(
            value["id"],
            CrossBackendRelation(value["relation"]),
            value["selected_ir_identity"],
            tuple(observables),
            value["clock_domain"],
            value["reset_domain"],
            value["left_latency"],
            value["right_latency"],
            value["left_ii"],
            value["right_ii"],
            typed_window,
            None if origin_value is None else SourceOrigin.from_data(origin_value),
            clock_domain_from_data(value["clock_domain_contract"]),
        )
    except (TypeError, ValueError) as error:
        raise FormalOrchestrationError(str(error)) from error


@dataclass(frozen=True)
class FrozenCandidateEquivalenceSite:
    """Immutable M36/M38 inputs for replay without source or reselection."""

    plan: CandidateEquivalencePlanReference
    property: EquivalenceProperty
    m38_property: CrossBackendProperty
    clash: PreparedCandidateEquivalence | None
    direct_systemverilog: PreparedCandidateEquivalence | None

    def __post_init__(self) -> None:
        if self.property.id != self.plan.clash_m36.property_identity:
            raise FormalOrchestrationError(
                "frozen M36 property differs from its compiler plan"
            )
        if self.property.implementation_root != self.plan.candidate_identity:
            raise FormalOrchestrationError(
                "frozen M36 property references a different selected candidate"
            )
        if (
            self.property.implementation_output
            != self.plan.implementation_observable_identity
        ):
            raise FormalOrchestrationError(
                "frozen M36 output differs from its compiler plan"
            )
        if self.m38_property.id != self.plan.m38.property_identity:
            raise FormalOrchestrationError(
                "frozen M38 property differs from its compiler plan"
            )
        if (
            self.m38_property.selected_ir_identity
            != self.plan.candidate_identity
            or self.m38_property.observable_ids
            != (self.plan.implementation_observable_identity,)
            or self.m38_property.comparison_window
            != self.property.comparison_window
            or self.m38_property.clock_domain_contract
            != self.property.clock_domain_contract
        ):
            raise FormalOrchestrationError(
                "frozen M38 relation differs from the selected M36 relation"
            )
        self._validate_leg(
            "clash", self.plan.clash_m36, self.property, self.clash
        )
        self._validate_leg(
            "direct_systemverilog",
            self.plan.direct_systemverilog_m36,
            self.property,
            self.direct_systemverilog,
        )
        if self.plan.m38.route is None:
            if self.clash is not None and self.direct_systemverilog is not None:
                raise FormalOrchestrationError(
                    "frozen candidate has both backend artifacts but no M38 route"
                )
        else:
            if self.clash is None or self.direct_systemverilog is None:
                raise FormalOrchestrationError(
                    "frozen M38 route is missing one prepared backend leg"
                )
            actual = tuple(
                _artifact_ref(item.implementation_artifact)
                for item in (self.clash, self.direct_systemverilog)
            )
            if self.plan.m38.route.artifacts != actual:
                raise FormalOrchestrationError(
                    "frozen M38 artifact/binding hashes differ from its plan"
                )
            if self.clash.input_semantic_ids != self.direct_systemverilog.input_semantic_ids:
                raise FormalOrchestrationError(
                    "frozen M38 backend inputs differ"
                )

    @staticmethod
    def _validate_leg(
        backend: str,
        plan: FormalGoalPlan,
        property_: EquivalenceProperty,
        prepared: PreparedCandidateEquivalence | None,
    ) -> None:
        if prepared is None:
            if plan.route is not None:
                raise FormalOrchestrationError(
                    f"frozen {backend} M36 route has no prepared proof input"
                )
            return
        if prepared.property != property_:
            raise FormalOrchestrationError(
                f"frozen {backend} M36 property differs from its plan"
            )
        try:
            validate_prepared_equivalence_domains(
                property_,
                prepared.reference_artifact,
                prepared.implementation_artifact,
            )
        except ValueError as error:
            raise FormalOrchestrationError(
                f"frozen {backend} M36 physical domain is invalid: {error}"
            ) from error
        if prepared.backend != backend:
            raise FormalOrchestrationError(
                f"frozen {backend} M36 leg has backend '{prepared.backend}'"
            )
        if prepared.implementation_artifact is None or plan.route is None:
            raise FormalOrchestrationError(
                f"frozen {backend} M36 route is incomplete"
            )
        if plan.route.reference_identity != prepared.reference_artifact_hash:
            raise FormalOrchestrationError(
                f"frozen {backend} M36 reference hash differs from its plan"
            )
        if plan.route.artifacts != (_artifact_ref(prepared.implementation_artifact),):
            raise FormalOrchestrationError(
                f"frozen {backend} M36 artifact/binding hashes differ from its plan"
            )

    @property
    def replay_identity(self) -> str:
        return "candidate-equivalence-replay:" + stable_digest(
            self._payload_data()
        )

    def _payload_data(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_data(),
            "m38_property": _cross_backend_property_data(self.m38_property),
            "clash": (
                None
                if self.clash is None
                else prepared_candidate_equivalence_to_data(self.clash)
            ),
            "direct_systemverilog": (
                None
                if self.direct_systemverilog is None
                else prepared_candidate_equivalence_to_data(
                    self.direct_systemverilog
                )
            ),
        }

    def to_data(self) -> dict[str, object]:
        return {
            "schema": "zlang-frozen-candidate-equivalence-site-v2",
            **self._payload_data(),
            "replay_identity": self.replay_identity,
        }

    @classmethod
    def from_data(cls, value: object) -> "FrozenCandidateEquivalenceSite":
        if not isinstance(value, dict) or set(value) != {
            "schema", "plan", "m38_property", "clash",
            "direct_systemverilog", "replay_identity",
        }:
            raise FormalOrchestrationError(
                "frozen candidate equivalence fields differ from the current schema"
            )
        if value["schema"] != "zlang-frozen-candidate-equivalence-site-v2":
            raise FormalOrchestrationError(
                "unsupported frozen candidate equivalence schema"
            )
        try:
            plan = CandidateEquivalencePlanReference.from_data(value["plan"])
            clash = (
                None
                if value["clash"] is None
                else prepared_candidate_equivalence_from_data(value["clash"])
            )
            direct = (
                None
                if value["direct_systemverilog"] is None
                else prepared_candidate_equivalence_from_data(
                    value["direct_systemverilog"]
                )
            )
        except (TypeError, ValueError) as error:
            raise FormalOrchestrationError(str(error)) from error
        common = clash or direct
        if common is None:
            raise FormalOrchestrationError(
                "frozen candidate equivalence has no prepared M36 leg"
            )
        restored = cls(
            plan,
            common.property,
            _cross_backend_property_from_data(value["m38_property"]),
            clash,
            direct,
        )
        if value["replay_identity"] != restored.replay_identity:
            raise FormalOrchestrationError(
                "frozen candidate replay identity does not match its contents"
            )
        return restored


def _binding_identity(artifact: BackendArtifact) -> str:
    manifest = json.loads(artifact.to_json())
    return "bindings:" + stable_digest({
        "manifest_version": artifact.manifest_version,
        "artifact_hash": artifact.artifact_hash,
        "bindings": manifest["bindings"],
    })


def _artifact_ref(artifact: BackendArtifact) -> FormalBackendArtifactRef:
    return FormalBackendArtifactRef(
        artifact.backend,
        artifact.artifact_hash,
        _binding_identity(artifact),
    )


def _m36_plan(
    site: SelectedCandidateSite,
    property_: EquivalenceProperty,
    *,
    backend: str,
    prepared: PreparedCandidateEquivalence | None,
    unavailable_reason: str | None = None,
) -> FormalGoalPlan:
    route = None
    skip = None
    if prepared is None:
        skip = FormalSkipReason(
            FormalSkipCode.BACKEND_UNAVAILABLE,
            unavailable_reason or f"{backend} M36 route is unavailable",
            (site.site.identity, site.site.selected_candidate_identity),
            backend,
        )
    else:
        if prepared.implementation_artifact is None:
            raise FormalOrchestrationError(
                f"prepared {backend} M36 route has no implementation artifact"
            )
        route = FormalExecutableRoute(
            FormalRouteKind.SEMANTIC_EQUIVALENCE,
            (_artifact_ref(prepared.implementation_artifact),),
            reference_identity=prepared.reference_artifact_hash,
        )
    observations = tuple(dict.fromkeys((
        *property_.inputs,
        property_.implementation_output,
    )))
    return FormalGoalPlan(
        "goal:m36:" + stable_digest({
            "site": site.site.identity,
            "candidate": site.site.selected_candidate_identity,
            "backend": backend,
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
        skip_reason=skip,
        source_origin=property_.source_origin,
    )


def _m38_property(
    site: SelectedCandidateSite,
    property_: EquivalenceProperty,
) -> CrossBackendProperty:
    relation = (
        CrossBackendRelation.SAME_CYCLE_VALUE
        if property_.relation_kind.value == "same_cycle_value"
        else CrossBackendRelation.FIXED_LATENCY_VALUE
    )
    latency = property_.implementation_latency
    return CrossBackendProperty(
        "m38.equiv." + stable_digest({
            "site": site.site.identity,
            "candidate": site.site.selected_candidate_identity,
            "m36_property": property_.id,
        })[:16],
        relation,
        site.site.selected_candidate_identity,
        (property_.implementation_output,),
        property_.implementation_clock,
        property_.implementation_reset,
        latency,
        latency,
        comparison_window=property_.comparison_window,
        source_origin=property_.source_origin,
        clock_domain_contract=property_.clock_domain_contract,
    )


def _m38_plan(
    site: SelectedCandidateSite,
    property_: CrossBackendProperty,
    clash: PreparedCandidateEquivalence | None,
    direct: PreparedCandidateEquivalence | None,
    *,
    unavailable_reasons: tuple[str, ...],
) -> FormalGoalPlan:
    route = None
    skip = None
    if (
        clash is not None
        and direct is not None
        and clash.implementation_artifact is not None
        and direct.implementation_artifact is not None
    ):
        route = FormalExecutableRoute(
            FormalRouteKind.CROSS_BACKEND_EQUIVALENCE,
            (
                _artifact_ref(clash.implementation_artifact),
                _artifact_ref(direct.implementation_artifact),
            ),
        )
    else:
        skip = FormalSkipReason(
            FormalSkipCode.ARTIFACT_UNAVAILABLE,
            "; ".join(unavailable_reasons) or "M38 implementation artifacts unavailable",
            (site.site.identity, site.site.selected_candidate_identity),
        )
    return FormalGoalPlan(
        "goal:m38:" + stable_digest({
            "site": site.site.identity,
            "candidate": site.site.selected_candidate_identity,
            "property": property_.id,
        }),
        property_.id,
        FormalPlanGoalKind.M38_EQUIVALENCE,
        property_.clock_domain,
        property_.reset_domain,
        (),
        property_.observable_ids,
        site.site.selected_candidate_identity,
        property_.comparison_window,
        property_.comparison_window.minimum_bmc_depth,
        route=route,
        skip_reason=skip,
        source_origin=property_.source_origin,
    )


def _selected_m39_record(
    compilation: object,
    site: SelectedCandidateSite,
    mode: EquivalenceMode,
) -> FormalExplorationRecord | None:
    expected_mode = ProofMode.BMC if mode is EquivalenceMode.BMC else ProofMode.PROVE
    matches = tuple(
        record
        for derived_site, record in candidate_formal_record_sites(
            getattr(compilation, "ir"),
            getattr(compilation, "exploration_results", ()),
        )
        if derived_site.identity == site.site.identity
        and isinstance(record, FormalExplorationRecord)
        and record.candidate_identity == site.site.selected_candidate_identity
        and record.mode is expected_mode
    )
    if len(matches) > 1:
        # A selected candidate is executed at most once per mode at one exact
        # site.  ``required_proven`` intentionally retains both BMC and PROVE
        # records, so counting across modes would reject valid staged evidence.
        raise FormalOrchestrationError(
            f"candidate site '{site.site.identity}' retains duplicate selected "
            f"M39 {expected_mode.value} records"
        )
    return matches[0] if matches else None


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
    expected_proof_mode = (
        ProofMode.BMC if mode is EquivalenceMode.BMC else ProofMode.PROVE
    )
    connected_identity = (
        record.formal_route == "M36_clash"
        and record.candidate_identity == prepared.property.implementation_root
        and record.policy is config.policy
        and record.mode is expected_proof_mode
        and record.depth == config.bmc_depth
        and record.engine == config.engine
        and record.solver == config.solver
        and record.backend == "clash"
        and record.property_identity == prepared.property_identity
        and record.reference_artifact_hash == prepared.reference_artifact_hash
        and record.implementation_artifact_hash == prepared.implementation_artifact_hash
        and record.artifact_hash == prepared.implementation_artifact_hash
        and record.harness_hash == prepared.harness_hash
        and record.assumptions_identity == prepared.assumptions_identity
        and record.backend_identity == prepared.backend_identity
    )
    if not connected_identity:
        return False
    stage_policy = record.policy
    if (
        record.policy is FormalPolicy.REQUIRED_PROVEN
        and mode is EquivalenceMode.BMC
    ):
        stage_policy = FormalPolicy.REQUIRED_BMC
    stage_config = replace(config, policy=stage_policy)
    verifier = M36ClashCandidateVerifier(
        selected.reference_expression,
        candidate_class=selected.candidate_class,
        artifact_provider=provider,
        # M39 preparation binds the candidate owner's physical contract even
        # for a same-cycle relation.  The M36 property itself intentionally has
        # no sampled domain in that case, so recover the owner contract from
        # the compiler plan rather than losing recipe identity on replay.
        clock_domain_contract=clock_domain_contract,
    )
    expected_recipe = formal_execution_recipe_identity(
        selected.candidate,
        stage_config,
        verifier,
        {
            "property_identity": prepared.property_identity,
            "artifact_hash": prepared.implementation_artifact_hash,
            "reference_artifact_hash": prepared.reference_artifact_hash,
            "implementation_artifact_hash": (
                prepared.implementation_artifact_hash
            ),
            "harness_hash": prepared.harness_hash,
            "assumptions_identity": prepared.assumptions_identity,
            "backend_identity": prepared.backend_identity,
        },
    )
    return record.execution_recipe_identity == expected_recipe


def _selected_owner_domain_for_reuse(
    compilation: object,
    selected: SelectedCandidateSite,
    prepared: PreparedCandidateEquivalence,
) -> ClockDomain | None:
    """Recover M39's owner-domain recipe without breaking frozen unit fixtures."""

    # Timed M36 properties retain the exact sampled domain themselves.
    if prepared.property.clock_domain_contract is not None:
        return prepared.property.clock_domain_contract
    module = getattr(compilation, "ir", None)
    try:
        domain, limitation = candidate_owner_formal_domain(
            module, selected.site.owner_identity
        )
    except (AttributeError, CandidateSiteError, TypeError):
        # Older orchestration unit fixtures intentionally supply only a frozen
        # property and a placeholder compilation object.  Their legacy
        # same-cycle recipe had no physical-domain input.
        return None
    return None if limitation is not None else domain


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
        "clash",
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
        "schema": "zlang-candidate-m36-execution-v1",
        "property": prepared.property_identity,
        "candidate": prepared.property.implementation_root,
        "backend": prepared.backend,
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
        "candidate-equivalence-result-v1",
        recipe,
        lambda: run_equivalence_formal(
            prepared.property,
            prepared.source,
            top=prepared.top,
            backend=prepared.backend,
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


def _m38_execution_recipe(
    property_: CrossBackendProperty,
    clash: PreparedCandidateEquivalence,
    direct: PreparedCandidateEquivalence,
    config: FormalExplorationConfig,
    mode: CrossBackendMode,
    toolchain: FormalToolchainContext,
) -> dict[str, object]:
    assert clash.implementation_artifact is not None
    assert direct.implementation_artifact is not None
    return {
        "schema": "zlang-candidate-m38-execution-v1",
        "property": property_.id,
        "candidate": property_.selected_ir_identity,
        "left_artifact": clash.implementation_artifact.artifact_hash,
        "right_artifact": direct.implementation_artifact.artifact_hash,
        "inputs": list(clash.input_semantic_ids),
        "left_backend_identity": clash.backend_identity,
        "right_backend_identity": direct.backend_identity,
        "formal_tool_versions": [list(item) for item in toolchain.versions],
        "formal_tools_missing": list(toolchain.missing),
        "mode": mode.value,
        "depth": config.bmc_depth,
        "timeout_seconds": config.timeout_seconds,
        "solver": config.solver,
    }


def _execute_m38(
    property_: CrossBackendProperty,
    clash: PreparedCandidateEquivalence,
    direct: PreparedCandidateEquivalence,
    config: FormalExplorationConfig,
    mode: CrossBackendMode,
    provider: FormalArtifactProvider,
    toolchain: FormalToolchainContext,
) -> CrossBackendResult:
    recipe = _m38_execution_recipe(
        property_, clash, direct, config, mode, toolchain
    )
    return provider.get_or_prepare(
        FormalArtifactNamespace.M38,
        "candidate-cross-backend-result-v1",
        recipe,
        lambda: run_cross_backend_formal(
            property_,
            clash.implementation_artifact,
            direct.implementation_artifact,
            inputs=clash.input_semantic_ids,
            mode=mode,
            depth=config.bmc_depth,
            solver=config.solver,
            timeout_seconds=config.timeout_seconds,
            work_directory=config.work_directory,
        ),
        encode=cross_backend_result_to_data,
        decode=cross_backend_result_from_data,
        cacheable=decisive_formal_cacheable,
    )


def _provider_and_config(
    compilation: object,
    config: FormalExplorationConfig,
) -> tuple[FormalArtifactProvider, FormalExplorationConfig]:
    provider = getattr(compilation, "formal_artifact_provider", None)
    if not isinstance(provider, FormalArtifactProvider):
        provider = FormalArtifactProvider(config.cache_directory)
    resolver = config.tool_resolver or getattr(
        compilation, "formal_tool_resolver", None
    )
    return provider, replace(
        config,
        artifact_provider=provider,
        tool_resolver=resolver,
    )


def _candidate_job_work_directory(
    config: FormalExplorationConfig,
    *,
    namespace: FormalArtifactNamespace,
    kind: str,
    recipe: dict[str, object],
) -> Path | None:
    """Return one collision-free operational workspace for a proof job.

    Work paths are addressed by the exact provider recipe.  Concurrent sites
    requesting identical evidence therefore share the one workspace owned by
    the provider's coalesced producer, and every waiter can retain the same
    complete operational attribution.  Paths remain outside semantic,
    property, evidence, artifact, and proof-cache identities.
    """

    if config.work_directory is None:
        return None
    identity = FormalArtifactRecipe(namespace, kind, recipe).digest
    return (
        Path(config.work_directory).resolve(strict=False)
        / "candidate-equivalence"
        / identity
    )


def prepare_selected_candidate_equivalence(
    compilation: object,
    compiler_plan: CompilerFormalExecutionPlan,
    config: FormalExplorationConfig,
) -> tuple[
    CompilerFormalExecutionPlan,
    tuple[PreparedCandidateEquivalenceSite, ...],
]:
    """Prepare exact M36/M38 plans and artifacts without running a solver.

    The API is intentionally separate from execution so a joint verification
    bundle can retain the immutable candidate plans before any proof job runs.
    Callers must still enforce the public joint-trigger rule; the defensive OFF
    check keeps ordinary and ``--verify``-only compilation probe-free.
    """

    if config.policy is FormalPolicy.OFF:
        return compiler_plan, ()
    if compiler_plan.formal_policy is not config.policy:
        raise FormalOrchestrationError(
            "candidate equivalence policy differs from the compiler formal plan"
        )
    provider, config = _provider_and_config(compilation, config)
    selected_sites = selected_candidate_sites(
        getattr(compilation, "ir"),
        getattr(compilation, "exploration_results", ()),
    )
    if not selected_sites:
        return compiler_plan, ()
    ledger_sites = {
        item.identity: item for item in compiler_plan.candidate_site_ledger.sites
    }
    prepared_sites: list[PreparedCandidateEquivalenceSite] = []
    plans: list[CandidateEquivalencePlanReference] = []
    for selected in selected_sites:
        ledger_site = ledger_sites.get(selected.site.identity)
        if ledger_site != selected.site:
            raise FormalOrchestrationError(
                f"selected candidate site '{selected.site.identity}' differs "
                "from its ledger"
            )
        domain, domain_limitation = candidate_owner_formal_domain(
            getattr(compilation, "ir"), selected.site.owner_identity
        )
        verifier = M36ClashCandidateVerifier(
            selected.reference_expression,
            candidate_class=selected.candidate_class,
            artifact_provider=provider,
            clock_domain_contract=domain,
            unavailable_reason=domain_limitation,
        )
        prepared: dict[str, PreparedCandidateEquivalence | None] = {
            "clash": None,
            "direct_systemverilog": None,
        }
        reasons: dict[str, str] = {}
        # Direct SV supplies the typed semantic property even when the
        # external Clash compiler is unavailable.
        for backend in ("direct_systemverilog", "clash"):
            try:
                prepared[backend] = verifier.prepare(
                    selected.candidate,
                    config,
                    backend=backend,
                )
            except FormalCandidateUnavailable as error:
                reasons[backend] = str(error)
        common = prepared["clash"] or prepared["direct_systemverilog"]
        if common is None:
            # The candidate lies outside the existing frozen M36 subset.  No
            # executable property may be fabricated from backend names.
            continue
        property_ = common.property
        if not isinstance(property_, EquivalenceProperty):
            raise FormalOrchestrationError(
                "candidate preparation did not return typed M36 property IR"
            )
        for item in prepared.values():
            if item is not None and item.property != property_:
                raise FormalOrchestrationError(
                    "candidate backends prepared different M36 properties"
                )
        clash_plan = _m36_plan(
            selected,
            property_,
            backend="clash",
            prepared=prepared["clash"],
            unavailable_reason=reasons.get("clash"),
        )
        direct_plan = _m36_plan(
            selected,
            property_,
            backend="direct_systemverilog",
            prepared=prepared["direct_systemverilog"],
            unavailable_reason=reasons.get("direct_systemverilog"),
        )
        m38_property = _m38_property(selected, property_)
        m38_plan = _m38_plan(
            selected,
            m38_property,
            prepared["clash"],
            prepared["direct_systemverilog"],
            unavailable_reasons=tuple(reasons[key] for key in sorted(reasons)),
        )
        plan = CandidateEquivalencePlanReference(
            selected.site.identity,
            selected.site.selected_candidate_identity,
            clash_plan,
            direct_plan,
            m38_plan,
            property_.implementation_output,
        )
        plans.append(plan)
        prepared_sites.append(PreparedCandidateEquivalenceSite(
            selected,
            plan,
            property_,
            m38_property,
            prepared["clash"],
            prepared["direct_systemverilog"],
        ))
    enriched = replace(
        compiler_plan,
        candidate_equivalence_plans=tuple(plans),
    )
    return enriched, tuple(prepared_sites)


def _execute_candidate_equivalence(
    compilation: object | None,
    compiler_plan: CompilerFormalExecutionPlan,
    prepared_sites: tuple[
        PreparedCandidateEquivalenceSite | FrozenCandidateEquivalenceSite, ...
    ],
    config: FormalExplorationConfig,
    *,
    jobs: int = 1,
) -> tuple[CandidateEquivalenceExecutionReport, ...]:
    """Execute staged M36 legs and optional M38 for prepared exact sites."""

    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise FormalOrchestrationError(
            "candidate equivalence jobs must be a positive integer"
        )
    if config.policy is FormalPolicy.OFF:
        if prepared_sites:
            raise FormalOrchestrationError(
                "formal-policy off cannot execute prepared candidate evidence"
            )
        return ()
    provider, config = _provider_and_config(compilation, config)
    if tuple(item.plan for item in prepared_sites) != (
        compiler_plan.candidate_equivalence_plans
    ):
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

    def execute_m36(
        item: PreparedCandidateEquivalence,
        selected: SelectedCandidateSite | None,
        mode: EquivalenceMode,
        work_directories: dict[str, str],
        *,
        allow_m39_reuse: bool,
    ) -> EquivalenceResult:
        if allow_m39_reuse and compilation is not None and selected is not None:
            retained = _selected_m39_record(compilation, selected, mode)
            if _m39_recipe_matches(
                retained,
                item,
                selected,
                config,
                mode,
                provider,
                _selected_owner_domain_for_reuse(
                    compilation, selected, item
                ),
            ):
                assert retained is not None
                if retained.work_directory is not None:
                    work_directories[
                        f"m36:{item.backend}:{mode.value}"
                    ] = retained.work_directory
                return _equivalence_from_m39(retained, item)
        label = f"m36:{item.backend}:{mode.value}"
        active = toolchain()
        recipe = _m36_execution_recipe(item, config, mode, active)
        work_directory = _candidate_job_work_directory(
            config,
            namespace=FormalArtifactNamespace.M36,
            kind="candidate-equivalence-result-v1",
            recipe=recipe,
        )
        job_config = replace(config, work_directory=work_directory)
        with use_formal_toolchain(active):
            result = _execute_m36(item, job_config, mode, provider, active)
        if work_directory is not None and work_directory.is_dir():
            work_directories[label] = str(work_directory)
        return result

    def execute_m38(
        item: PreparedCandidateEquivalenceSite | FrozenCandidateEquivalenceSite,
        mode: CrossBackendMode,
        work_directories: dict[str, str],
    ) -> CrossBackendResult:
        assert item.clash is not None and item.direct_systemverilog is not None
        label = f"m38:{mode.value}"
        active = toolchain()
        recipe = _m38_execution_recipe(
            item.m38_property,
            item.clash,
            item.direct_systemverilog,
            config,
            mode,
            active,
        )
        work_directory = _candidate_job_work_directory(
            config,
            namespace=FormalArtifactNamespace.M38,
            kind="candidate-cross-backend-result-v1",
            recipe=recipe,
        )
        job_config = replace(config, work_directory=work_directory)
        with use_formal_toolchain(active):
            result = _execute_m38(
                item.m38_property,
                item.clash,
                item.direct_systemverilog,
                job_config,
                mode,
                provider,
                active,
            )
        if work_directory is not None and work_directory.is_dir():
            work_directories[label] = str(work_directory)
        return result

    def execute_site(
        item: PreparedCandidateEquivalenceSite | FrozenCandidateEquivalenceSite,
    ) -> CandidateEquivalenceExecutionReport:
        site_work_directories: dict[str, str] = {}
        selected = (
            item.selected
            if isinstance(item, PreparedCandidateEquivalenceSite)
            else None
        )
        clash_bmc = (
            None
            if item.clash is None
            else execute_m36(
                item.clash,
                selected,
                EquivalenceMode.BMC,
                site_work_directories,
                allow_m39_reuse=True,
            )
        )
        direct_bmc = (
            None
            if item.direct_systemverilog is None
            else execute_m36(
                item.direct_systemverilog,
                selected,
                EquivalenceMode.BMC,
                site_work_directories,
                allow_m39_reuse=False,
            )
        )
        m38_bmc = None
        if (
            item.plan.m38.route is not None
            and clash_bmc is not None
            and direct_bmc is not None
            and clash_bmc.status in _DECISIVE_M36
            and direct_bmc.status in _DECISIVE_M36
        ):
            m38_bmc = execute_m38(
                item, CrossBackendMode.BMC, site_work_directories
            )
        bounded_triangle = M38EvidenceReport.from_results(
            item.plan.m38,
            m38_bmc,
            M36EvidenceLeg.from_result(item.plan.clash_m36, clash_bmc),
            M36EvidenceLeg.from_result(
                item.plan.direct_systemverilog_m36, direct_bmc
            ),
        )
        if config.policy is not FormalPolicy.REQUIRED_PROVEN:
            return CandidateEquivalenceExecutionReport(
                item.plan,
                bounded_triangle,
                work_directories=tuple(site_work_directories.items()),
            )

        clash_prove = None
        if (
            item.clash is not None
            and clash_bmc is not None
            and clash_bmc.status is EquivalenceStatus.BOUNDED_PASS
        ):
            clash_prove = execute_m36(
                item.clash,
                selected,
                EquivalenceMode.PROVE,
                site_work_directories,
                allow_m39_reuse=True,
            )
        direct_prove = None
        if (
            item.direct_systemverilog is not None
            and direct_bmc is not None
            and direct_bmc.status is EquivalenceStatus.BOUNDED_PASS
        ):
            direct_prove = execute_m36(
                item.direct_systemverilog,
                selected,
                EquivalenceMode.PROVE,
                site_work_directories,
                allow_m39_reuse=False,
            )
        if clash_prove is None and direct_prove is None:
            return CandidateEquivalenceExecutionReport(
                item.plan,
                bounded_triangle,
                work_directories=tuple(site_work_directories.items()),
            )
        m38_prove = None
        if (
            m38_bmc is not None
            and m38_bmc.status is CrossBackendStatus.BOUNDED_PASS
            and clash_prove is not None
            and direct_prove is not None
            and clash_prove.status in _DECISIVE_M36
            and direct_prove.status in _DECISIVE_M36
        ):
            m38_prove = execute_m38(
                item, CrossBackendMode.PROVE, site_work_directories
            )
        prove_triangle = M38EvidenceReport.from_results(
            item.plan.m38,
            m38_prove,
            M36EvidenceLeg.from_result(item.plan.clash_m36, clash_prove),
            M36EvidenceLeg.from_result(
                item.plan.direct_systemverilog_m36, direct_prove
            ),
        )
        return CandidateEquivalenceExecutionReport(
            item.plan,
            prove_triangle,
            bounded_triangle,
            (),
            tuple(site_work_directories.items()),
        )

    if jobs == 1 or len(prepared_sites) < 2:
        reports = tuple(execute_site(item) for item in prepared_sites)
    else:
        with ThreadPoolExecutor(
            max_workers=min(jobs, len(prepared_sites)),
            thread_name_prefix="zlang-candidate-formal",
        ) as executor:
            # ``map`` preserves the deterministic typed-plan order even when
            # independent sites finish in a different physical order.
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
    """Execute prepared evidence, retaining exact compatible M39 reuse."""

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
    """Replay frozen M36/M38 evidence without source or M39 selection."""

    return _execute_candidate_equivalence(
        None, compiler_plan, frozen_sites, config, jobs=jobs
    )


def execute_selected_candidate_equivalence(
    compilation: object,
    compiler_plan: CompilerFormalExecutionPlan,
    config: FormalExplorationConfig,
    *,
    jobs: int = 1,
) -> tuple[
    CompilerFormalExecutionPlan,
    tuple[CandidateEquivalenceExecutionReport, ...],
]:
    """Prepare then execute optional evidence for selected candidate sites."""

    enriched, prepared = prepare_selected_candidate_equivalence(
        compilation, compiler_plan, config
    )
    reports = execute_prepared_candidate_equivalence(
        compilation, enriched, prepared, config, jobs=jobs
    )
    return enriched, reports


__all__ = [
    "FrozenCandidateEquivalenceSite",
    "PreparedCandidateEquivalenceSite",
    "execute_frozen_candidate_equivalence",
    "execute_prepared_candidate_equivalence",
    "execute_selected_candidate_equivalence",
    "prepare_selected_candidate_equivalence",
]
