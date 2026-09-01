"""Typed adapters and deterministic rendering for whole-build evidence.

This module is deliberately a reporting boundary.  It accepts the structured
results produced by M35, M36, M38, and M39; it never infers proof from log text,
property registration, or harness generation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING, Iterable, Mapping

from zlang.build_manifest import EvidenceRecord, ReportRecord
from zlang.common import stable_digest, stable_json
from zlang.formal_exploration import FormalExplorationRecord
from zlang.ir.cross_backend import (
    CrossBackendCounterexample,
    CrossBackendResult,
)
from zlang.ir.equivalence import EquivalenceCounterexample, EquivalenceResult
from zlang.ir.formal import Counterexample, FormalProperty, FormalResult
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module, PortDirection
from zlang.ir.timing import ModuleTimingContract, TimingKnowledge
from zlang.source import SourceOrigin
from zlang.verification_bundle import VerificationJobResult, VerificationRunReport


if TYPE_CHECKING:
    from zlang.formal_orchestration import (
        CandidateEquivalenceExecutionReport,
        CompilerFormalExecutionPlan,
    )


EVIDENCE_REPORT_SCHEMA = "zlang-evidence-report-v2"


class EvidenceReportError(ValueError):
    """A value cannot be represented as truthful structured evidence."""


@dataclass(frozen=True)
class RenderedEvidenceReport:
    """A report manifest record together with its deterministic companion text."""

    record: ReportRecord
    content: str


@dataclass(frozen=True)
class EvidenceReportPayload:
    """Strict common evidence view for verification and M39 selection records."""

    evidence: tuple[EvidenceRecord, ...]
    formal_execution_plan: "CompilerFormalExecutionPlan | None" = None
    candidate_equivalence: tuple["CandidateEquivalenceExecutionReport", ...] = ()
    schema: str = EVIDENCE_REPORT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EVIDENCE_REPORT_SCHEMA:
            raise EvidenceReportError("unsupported evidence report schema")
        ordered = _ordered(self.evidence)
        object.__setattr__(self, "evidence", ordered)
        plan = self.formal_execution_plan
        from zlang.formal_orchestration import CandidateEquivalenceExecutionReport

        reports = tuple(sorted(
            self.candidate_equivalence,
            key=lambda item: item.plan.site_identity,
        ))
        object.__setattr__(self, "candidate_equivalence", reports)
        if any(not isinstance(item, CandidateEquivalenceExecutionReport) for item in reports):
            raise EvidenceReportError(
                "candidate-equivalence evidence must use typed execution reports"
            )
        if plan is None:
            if reports:
                raise EvidenceReportError(
                    "candidate-equivalence evidence requires a compiler formal plan"
                )
            return
        from zlang.formal_orchestration import CompilerFormalExecutionPlan

        if not isinstance(plan, CompilerFormalExecutionPlan):
            raise EvidenceReportError(
                "evidence report formal plan must be compiler-owned typed data"
            )
        expected_candidate_plans = {
            item.plan_identity for item in plan.candidate_equivalence_plans
        }
        actual_candidate_plans = {item.plan.plan_identity for item in reports}
        if expected_candidate_plans != actual_candidate_plans:
            detail = sorted(expected_candidate_plans ^ actual_candidate_plans)[0]
            raise EvidenceReportError(
                "candidate-equivalence reports differ from compiler plan at "
                f"'{detail}'"
            )
        report_evidence = {
            item.evidence_id
            for report in reports
            for item in report.evidence_records
        }
        common_equivalence = {
            item.evidence_id for item in ordered
            if item.claim in {
                "m36.selected_architecture_equivalence",
                "m38.cross_backend_equivalence",
            }
        }
        if report_evidence != common_equivalence:
            detail = sorted(report_evidence ^ common_equivalence)[0]
            raise EvidenceReportError(
                "candidate-equivalence typed and common evidence differ at "
                f"'{detail}'"
            )
        by_id = {item.evidence_id: item for item in ordered}
        actual_m39 = {
            item.evidence_id for item in ordered
            if item.claim == "m39.formal_candidate_eligibility"
        }
        expected_m39 = set(plan.m39_evidence_ids)
        if actual_m39 != expected_m39:
            missing = sorted(expected_m39 - actual_m39)
            extra = sorted(actual_m39 - expected_m39)
            detail = missing[0] if missing else extra[0]
            raise EvidenceReportError(
                f"evidence report M39 records differ from compiler plan: '{detail}'"
            )
        for attempt in plan.m39_attempts:
            item = by_id[attempt.evidence_id]
            details = dict(item.details)
            if (
                item.candidate_identity != attempt.candidate_identity
                or item.status != attempt.status
                or item.mode != attempt.mode
                or item.depth != attempt.depth
                or item.property_id != attempt.property_identity
                or item.route != attempt.route
                or details.get("site_identity") != attempt.site_identity
                or details.get("rank") != str(attempt.rank)
                or details.get("policy") != attempt.policy.value
            ):
                raise EvidenceReportError(
                    f"M39 evidence '{attempt.evidence_id}' differs from compiler plan"
                )
        verification = tuple(
            item for item in ordered
            if item.claim in {
                "verification.safety_goal",
                "verification.cover_goal",
                "verification.unsupported_goal",
            }
        )
        if verification:
            expected_goals = {
                item.property_identity for item in plan.verification_plan.goals
            }
            actual_goals = {item.property_id for item in verification}
            if actual_goals != expected_goals:
                detail = sorted(expected_goals ^ actual_goals)[0]
                raise EvidenceReportError(
                    "verification evidence differs from compiler plan at "
                    f"property '{detail}'"
                )

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "formal_execution_plan": (
                None
                if self.formal_execution_plan is None
                else self.formal_execution_plan.to_data()
            ),
            "candidate_equivalence": [
                item.to_data() for item in self.candidate_equivalence
            ],
            "evidence": [item.to_data() for item in self.evidence],
        }

    @classmethod
    def from_data(cls, value: object) -> "EvidenceReportPayload":
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str) for key in value
        ):
            raise EvidenceReportError("evidence report must be a JSON object")
        expected = {
            "schema", "formal_execution_plan", "candidate_equivalence", "evidence",
        }
        if set(value) != expected:
            raise EvidenceReportError(
                "evidence report fields differ from the current schema"
            )
        records = value["evidence"]
        if not isinstance(records, list):
            raise EvidenceReportError("evidence report evidence must be an array")
        candidate_data = value["candidate_equivalence"]
        if not isinstance(candidate_data, list):
            raise EvidenceReportError(
                "candidate-equivalence reports must be an array"
            )
        plan_data = value["formal_execution_plan"]
        plan = None
        if plan_data is not None:
            from zlang.formal_orchestration import (
                CompilerFormalExecutionPlan,
                FormalOrchestrationError,
            )

            try:
                plan = CompilerFormalExecutionPlan.from_data(plan_data)
            except FormalOrchestrationError as error:
                raise EvidenceReportError(
                    f"invalid compiler formal execution plan: {error}"
                ) from error
        try:
            typed_records = tuple(EvidenceRecord.from_data(item) for item in records)
        except ValueError as error:
            raise EvidenceReportError(f"invalid evidence record: {error}") from error
        from zlang.formal_orchestration import CandidateEquivalenceExecutionReport

        try:
            candidate_reports = tuple(
                CandidateEquivalenceExecutionReport.from_data(item)
                for item in candidate_data
            )
        except ValueError as error:
            raise EvidenceReportError(
                f"invalid candidate-equivalence report: {error}"
            ) from error
        return cls(typed_records, plan, candidate_reports, str(value["schema"]))

    @classmethod
    def from_json(cls, text: str) -> "EvidenceReportPayload":
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise EvidenceReportError("evidence report is not valid JSON") from error
        return cls.from_data(value)


def _counterexample_data(
    counterexample: Counterexample | EquivalenceCounterexample | CrossBackendCounterexample | None,
) -> dict[str, object] | None:
    if counterexample is None:
        return None
    if isinstance(counterexample, Counterexample):
        return {
            "kind": "m35",
            "property_id": counterexample.property_id,
            "cycle": counterexample.cycle,
            "values": [list(item) for item in counterexample.values],
            "raw_trace": counterexample.raw_trace,
        }
    if isinstance(counterexample, EquivalenceCounterexample):
        return {
            "kind": "m36",
            "property_id": counterexample.property_id,
            "failure_cycle": counterexample.failure_cycle,
            "sample_cycle": counterexample.sample_cycle,
            "values": [list(item) for item in counterexample.values],
            "raw_trace": counterexample.raw_trace,
        }
    if isinstance(counterexample, CrossBackendCounterexample):
        return {
            "kind": "m38",
            "property_id": counterexample.property_id,
            "semantic_signal_id": counterexample.semantic_signal_id,
            "cycle": counterexample.cycle,
            "sample_cycle": counterexample.sample_cycle,
            "left_backend": counterexample.left_backend,
            "right_backend": counterexample.right_backend,
            "left_artifact_hash": counterexample.left_artifact_hash,
            "right_artifact_hash": counterexample.right_artifact_hash,
            "left_rtl_path": counterexample.left_rtl_path,
            "right_rtl_path": counterexample.right_rtl_path,
            "values": [list(item) for item in counterexample.values],
            "raw_trace": counterexample.raw_trace,
        }
    raise EvidenceReportError(
        "counterexample evidence must use an M35, M36, or M38 typed counterexample"
    )


def _details(**values: object) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, str):
            rendered = value
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = stable_json(value)
        result.append((key, rendered))
    return tuple(sorted(result))


def _record(
    family: str,
    *,
    claim: str,
    status: str,
    mode: str | None,
    depth: int | None,
    property_id: str | None,
    candidate_identity: str | None,
    backend: str | None,
    artifact_hash: str | None,
    reference_hash: str | None,
    source_origin: SourceOrigin | None,
    engine: str | None,
    solver: str | None,
    relation: str | None,
    route: str | None,
    counterexample: Counterexample | EquivalenceCounterexample | CrossBackendCounterexample | None,
    details: tuple[tuple[str, str], ...],
) -> EvidenceRecord:
    if depth is not None and depth < 1:
        raise EvidenceReportError("evidence depth must be positive")
    counterexample_data = _counterexample_data(counterexample)
    counterexample_digest = (
        None if counterexample_data is None else stable_digest(counterexample_data)
    )
    if counterexample_data is not None:
        concise_counterexample = {
            key: value
            for key, value in counterexample_data.items()
            if key not in {"raw_trace", "source_origin"}
        }
        details = tuple(sorted((
            *details,
            ("counterexample_metadata", stable_json(concise_counterexample)),
        )))
    identity_data = {
        "schema": EVIDENCE_REPORT_SCHEMA,
        "family": family,
        "claim": claim,
        "status": status,
        "mode": mode,
        "depth": depth,
        "property_id": property_id,
        "candidate_identity": candidate_identity,
        "backend": backend,
        "artifact_hash": artifact_hash,
        "reference_hash": reference_hash,
        "engine": engine,
        "solver": solver,
        "relation": relation,
        "route": route,
        "counterexample_digest": counterexample_digest,
        # Cache hit/executed is useful report metadata, but it describes how an
        # already-identical result was obtained and cannot change evidence or
        # whole-build identity.
        "details": [
            list(item) for item in details if item[0] != "cache_state"
        ],
    }
    return EvidenceRecord(
        evidence_id=f"{family}.{stable_digest(identity_data)}",
        claim=claim,
        status=status,
        mode=mode,
        depth=depth,
        property_id=property_id,
        candidate_identity=candidate_identity,
        backend=backend,
        artifact_hash=artifact_hash,
        reference_hash=reference_hash,
        source_origin=source_origin,
        engine=engine,
        solver=solver,
        relation=relation,
        route=route,
        counterexample_digest=counterexample_digest,
        details=details,
    )


def evidence_from_formal_result(
    result: FormalResult,
    *,
    backend: str | None = None,
    artifact_hash: str | None = None,
) -> EvidenceRecord:
    """Adapt one executed or explicitly skipped M35 property result."""

    if not isinstance(result, FormalResult):
        raise TypeError("M35 evidence requires FormalResult")
    return _record(
        "m35",
        claim="m35.safety_property",
        status=result.status.value,
        mode=result.mode.value,
        depth=result.depth,
        property_id=result.property_id,
        candidate_identity=None,
        backend=backend,
        artifact_hash=artifact_hash,
        reference_hash=None,
        source_origin=result.source_origin,
        engine=result.engine,
        solver=result.solver,
        relation=None,
        route=None,
        counterexample=result.counterexample,
        details=_details(
            reason=result.reason,
            tool_versions=[list(item) for item in result.tool_versions],
        ),
    )


def evidence_from_verification_report(
    report: VerificationRunReport,
) -> tuple[EvidenceRecord, ...]:
    """Adapt one executed first-class verification run without changing routes.

    The verification executor already owns the distinction between same-cycle
    safety and bounded reachability.  This adapter merely publishes those typed
    results through the common evidence facade; it never invokes M36, M38, or
    M39 and it deliberately excludes host-local work-directory paths.
    """

    if not isinstance(report, VerificationRunReport):
        raise TypeError("verification evidence requires VerificationRunReport")
    return tuple(
        evidence_from_verification_job_result(item, report=report)
        for item in sorted(report.results, key=lambda result: result.property_id)
    )


def evidence_from_verification_job_result(
    result: VerificationJobResult,
    *,
    report: VerificationRunReport,
) -> EvidenceRecord:
    """Adapt one safety or cover job from an executed verification report."""

    if not isinstance(result, VerificationJobResult):
        raise TypeError("verification job evidence requires VerificationJobResult")
    if not isinstance(report, VerificationRunReport):
        raise TypeError("verification job evidence requires VerificationRunReport")
    if result not in report.results:
        raise EvidenceReportError(
            "verification job result does not belong to the supplied run report"
        )

    witness_details: dict[str, object] = {}
    if result.witness is not None:
        witness_data = {
            "property_id": result.witness.property_id,
            "cycle": result.witness.cycle,
            "values": [list(item) for item in result.witness.values],
            "raw_trace": result.witness.raw_trace,
        }
        witness_details = {
            "witness_cycle": result.witness.cycle,
            "witness_values": [list(item) for item in result.witness.values],
            "witness_digest": stable_digest(witness_data),
        }

    counterexample_details: dict[str, object] = {}
    if result.counterexample_metadata is not None:
        counterexample_details = {
            "counterexample_sample_cycle": (
                result.counterexample_metadata.sample_cycle
            ),
            "counterexample_reset_state": (
                result.counterexample_metadata.reset_state
            ),
            "counterexample_comparison_valid_state": (
                result.counterexample_metadata.comparison_valid_state
            ),
        }

    if result.kind == "safety":
        claim = "verification.safety_goal"
        counterexample = result.counterexample
    elif result.kind == "cover":
        claim = "verification.cover_goal"
        counterexample = None
    else:
        # Unknown future job kinds are already fail-closed as skipped by the
        # executor.  Preserve that fact without inventing formal semantics.
        claim = "verification.unsupported_goal"
        counterexample = None

    return _record(
        "verification",
        claim=claim,
        status=result.status,
        mode=result.mode,
        depth=result.depth,
        property_id=result.property_id,
        candidate_identity=None,
        backend=None,
        artifact_hash=None,
        reference_hash=None,
        source_origin=result.source_origin,
        engine=result.engine,
        solver=result.solver,
        relation=None,
        route="verification_bundle",
        counterexample=counterexample,
        details=_details(
            bundle_identity=report.bundle_identity,
            run_identity=report.run_identity,
            top=report.top,
            job_kind=result.kind,
            reason=result.reason,
            timeout_seconds=report.config.timeout_seconds,
            tool_versions=[list(item) for item in result.tool_versions],
            **counterexample_details,
            **witness_details,
        ),
    )


def evidence_from_equivalence_result(result: EquivalenceResult) -> EvidenceRecord:
    """Adapt one M36 semantic-reference equivalence result."""

    if not isinstance(result, EquivalenceResult):
        raise TypeError("M36 evidence requires EquivalenceResult")
    return _record(
        "m36",
        claim="m36.selected_architecture_equivalence",
        status=result.status.value,
        mode=result.mode.value,
        depth=result.depth,
        property_id=result.property_id,
        candidate_identity=result.candidate_identity,
        backend=result.backend,
        artifact_hash=result.implementation_hash,
        reference_hash=result.reference_hash,
        source_origin=result.source_origin,
        engine=result.engine,
        solver=result.solver,
        relation=result.relation_kind.value,
        route="semantic_reference",
        counterexample=result.counterexample,
        details=_details(
            binding_map_version=result.binding_map_version,
            latency_delta=result.latency_delta,
            reason=result.reason,
        ),
    )


def evidence_from_cross_backend_result(result: CrossBackendResult) -> EvidenceRecord:
    """Adapt one M38 backend-pair equivalence result."""

    if not isinstance(result, CrossBackendResult):
        raise TypeError("M38 evidence requires CrossBackendResult")
    return _record(
        "m38",
        claim="m38.cross_backend_equivalence",
        status=result.status.value,
        mode=result.mode.value,
        depth=result.depth,
        property_id=result.property_id,
        candidate_identity=result.selected_ir_identity,
        backend=f"{result.left_backend}<->{result.right_backend}",
        artifact_hash=result.right_artifact_hash,
        reference_hash=result.left_artifact_hash,
        source_origin=(
            result.source_origin
            if result.source_origin is not None
            else (
                None
                if result.counterexample is None
                else result.counterexample.source_origin
            )
        ),
        engine=result.engine,
        solver=result.solver,
        relation=result.relation.value,
        route="cross_backend",
        counterexample=result.counterexample,
        details=_details(
            selected_ir_identity=result.selected_ir_identity,
            left_backend=result.left_backend,
            right_backend=result.right_backend,
            left_artifact_hash=result.left_artifact_hash,
            right_artifact_hash=result.right_artifact_hash,
            manifest_version=result.manifest_version,
            observable_signal_id=result.observable_signal_id,
            latency_delta=result.latency_delta,
            reason=result.reason,
        ),
    )


def evidence_from_formal_exploration_record(
    result: FormalExplorationRecord,
    *,
    site_identity: str | None = None,
) -> EvidenceRecord:
    """Adapt one M39 candidate record without promoting an unexecuted route."""

    if not isinstance(result, FormalExplorationRecord):
        raise TypeError("M39 evidence requires FormalExplorationRecord")
    if not result.candidate_identity:
        raise EvidenceReportError("M39 evidence requires a candidate identity")
    if result.rank < 1:
        raise EvidenceReportError("M39 evidence rank must be positive")
    if site_identity is not None and not site_identity:
        raise EvidenceReportError("M39 candidate-site identity must be non-empty")
    if result.status is None:
        if result.mode is not None or result.depth is not None:
            raise EvidenceReportError(
                "unexecuted M39 evidence cannot carry proof mode or depth"
            )
        status = "not_run"
        mode = None
        depth = None
    else:
        status = result.status.value
        if result.mode is None:
            raise EvidenceReportError("executed M39 evidence requires a proof mode")
        mode = result.mode.value
        depth = result.depth
        if status in {"bounded_pass", "proven", "failed"}:
            if not result.backend or not result.artifact_hash:
                raise EvidenceReportError(
                    "decisive M39 evidence requires a connected backend artifact"
                )
            for label, value in (
                ("property identity", result.property_identity),
                ("harness hash", result.harness_hash),
                ("assumptions identity", result.assumptions_identity),
                ("backend identity", result.backend_identity),
                ("reference artifact hash", result.reference_artifact_hash),
                (
                    "implementation artifact hash",
                    result.implementation_artifact_hash,
                ),
            ):
                if not value:
                    raise EvidenceReportError(
                        f"decisive M39 evidence requires {label}"
                    )
    counterexample = result.counterexample
    if counterexample is not None and not isinstance(
        counterexample,
        (Counterexample, EquivalenceCounterexample, CrossBackendCounterexample),
    ):
        raise EvidenceReportError("M39 counterexample metadata must use typed formal IR")
    if status == "failed" and counterexample is None:
        raise EvidenceReportError(
            "failed M39 evidence requires counterexample metadata"
        )
    return _record(
        "m39",
        claim="m39.formal_candidate_eligibility",
        status=status,
        mode=mode,
        depth=depth,
        property_id=result.property_identity,
        candidate_identity=result.candidate_identity,
        backend=result.backend,
        artifact_hash=(
            result.implementation_artifact_hash or result.artifact_hash
        ),
        reference_hash=result.reference_artifact_hash,
        source_origin=result.source_origin,
        engine=result.engine,
        solver=result.solver,
        relation=None,
        route=result.formal_route,
        counterexample=counterexample,
        details=_details(
            site_identity=site_identity,
            rank=result.rank,
            semantic_legality=result.semantic_legality,
            policy=result.policy.value,
            cache_state=result.cache_state,
            eligible=result.eligible,
            reason=result.reason,
            proof_reason=result.proof_reason or None,
            work_directory=result.work_directory,
            execution_recipe_identity=result.execution_recipe_identity,
            harness_hash=result.harness_hash,
            assumptions_identity=result.assumptions_identity,
            backend_identity=result.backend_identity,
            selected_origin=(
                None
                if result.selected_origin is None
                else result.selected_origin.to_data()
            ),
        ),
    )


def evidence_for_unexecuted_property(
    property_: FormalProperty,
    *,
    reason: str = "property generated but no proof result was executed",
) -> EvidenceRecord:
    """Report property generation truthfully as ``not_run`` evidence."""

    if not isinstance(property_, FormalProperty):
        raise TypeError("unexecuted property evidence requires FormalProperty")
    return _record(
        "m35",
        claim="m35.safety_property",
        status="not_run",
        mode=None,
        depth=None,
        property_id=property_.id,
        candidate_identity=None,
        backend=None,
        artifact_hash=None,
        reference_hash=None,
        source_origin=property_.source_origin,
        engine=None,
        solver=None,
        relation=None,
        route=None,
        counterexample=None,
        details=_details(
            generated_from=property_.generated_from,
            ownership=property_.ownership.value,
            temporal_form=property_.temporal_form.value,
            reason=reason,
        ),
    )


def evidence_for_typed_module(
    module: Module,
    *,
    high_level_ir_identity: str,
    source_origin: SourceOrigin | None = None,
) -> EvidenceRecord:
    """Record successful typed lowering from an actual semantic ``Module``."""

    if not isinstance(module, Module):
        raise TypeError("typed legality evidence requires a semantic Module")
    if not high_level_ir_identity:
        raise EvidenceReportError("typed legality evidence requires high-level IR identity")
    return _record(
        "semantic",
        claim="semantic.typed_legality",
        status="typed_legal",
        mode=None,
        depth=None,
        property_id=None,
        candidate_identity=high_level_ir_identity,
        backend=None,
        artifact_hash=None,
        reference_hash=None,
        source_origin=source_origin,
        engine=None,
        solver=None,
        relation=None,
        route="typed_semantic_ir",
        counterexample=None,
        details=_details(
            module=module.name,
            high_level_ir_identity=high_level_ir_identity,
        ),
    )


def evidence_for_validated_timing(
    module: Module,
    *,
    selected_ir_identity: str,
) -> EvidenceRecord:
    """Record an exact timing contract only after typed output validation.

    A declaration alone is not timing evidence.  Every public scalar wire output
    must carry an exact, derived latency equal to the module contract.
    """

    if not isinstance(module, Module):
        raise TypeError("timing evidence requires a semantic Module")
    contract = module.timing_contract
    if not isinstance(contract, ModuleTimingContract):
        raise EvidenceReportError("timing evidence requires an exact module contract")
    if contract.initiation_interval != 1:
        raise EvidenceReportError("the validated timing slice supports only II=1")
    if not selected_ir_identity:
        raise EvidenceReportError("timing evidence requires selected-IR identity")
    scalar_outputs = tuple(
        port.name
        for port in module.ports
        if port.direction is PortDirection.OUTPUT
        and port.protocol is InterfaceProtocol.WIRE
    )
    if not scalar_outputs:
        raise EvidenceReportError("timing evidence requires a public scalar wire output")
    timing_ports = tuple(item.port for item in module.output_timings)
    if len(timing_ports) != len(set(timing_ports)):
        raise EvidenceReportError("derived output timing records must be unique")
    timings = {item.port: item.timing for item in module.output_timings}
    if set(timings) != set(scalar_outputs):
        raise EvidenceReportError(
            "timing evidence requires exact derived timing for every scalar output"
        )
    for output in scalar_outputs:
        timing = timings[output]
        if (
            timing.knowledge is TimingKnowledge.UNKNOWN
            or (
                timing.knowledge is TimingKnowledge.KNOWN
                and timing.latency != contract.latency
            )
        ):
            raise EvidenceReportError(
                f"output '{output}' does not validate exact latency {contract.latency}"
            )
    if contract.latency > 0 and (
        contract.clock_domain is None or contract.reset_domain is None
    ):
        raise EvidenceReportError(
            "positive-latency timing evidence requires clock/reset domains"
        )
    return _record(
        "timing",
        claim="timing.exact_module_contract",
        status="timing_validated",
        mode=None,
        depth=None,
        property_id=None,
        candidate_identity=selected_ir_identity,
        backend=None,
        artifact_hash=None,
        reference_hash=None,
        source_origin=contract.source_origin,
        engine=None,
        solver=None,
        relation="exact_scalar_output_latency",
        route="typed_timing_analysis",
        counterexample=None,
        details=_details(
            module=module.name,
            selected_ir_identity=selected_ir_identity,
            latency=contract.latency,
            ii=contract.initiation_interval,
            clock_domain=contract.clock_domain,
            reset_domain=contract.reset_domain,
            outputs=list(sorted(scalar_outputs)),
        ),
    )


def evidence_from_result(result: object) -> EvidenceRecord:
    """Strict typed dispatcher; strings and log-shaped dictionaries are rejected."""

    if isinstance(result, FormalResult):
        return evidence_from_formal_result(result)
    if isinstance(result, EquivalenceResult):
        return evidence_from_equivalence_result(result)
    if isinstance(result, CrossBackendResult):
        return evidence_from_cross_backend_result(result)
    if isinstance(result, FormalExplorationRecord):
        return evidence_from_formal_exploration_record(result)
    raise TypeError(
        "evidence adapters accept only FormalResult, EquivalenceResult, "
        "CrossBackendResult, or FormalExplorationRecord"
    )


def _ordered(records: Iterable[EvidenceRecord]) -> tuple[EvidenceRecord, ...]:
    materialized = tuple(records)
    if not all(isinstance(item, EvidenceRecord) for item in materialized):
        raise TypeError("evidence reports require EvidenceRecord values")
    identities = tuple(item.evidence_id for item in materialized)
    if len(identities) != len(set(identities)):
        duplicate = next(item for item in identities if identities.count(item) > 1)
        raise EvidenceReportError(f"duplicate evidence identity: {duplicate}")
    return tuple(sorted(materialized, key=lambda item: item.evidence_id))


def render_evidence_json(
    records: Iterable[EvidenceRecord],
    *,
    formal_execution_plan: "CompilerFormalExecutionPlan | None" = None,
    candidate_equivalence: tuple["CandidateEquivalenceExecutionReport", ...] = (),
) -> str:
    payload = EvidenceReportPayload(
        tuple(records),
        formal_execution_plan,
        candidate_equivalence,
    )
    return stable_json(payload.to_data(), indent=2) + "\n"


def render_evidence_text(
    records: Iterable[EvidenceRecord],
    *,
    formal_execution_plan: "CompilerFormalExecutionPlan | None" = None,
    candidate_equivalence: tuple["CandidateEquivalenceExecutionReport", ...] = (),
) -> str:
    payload = EvidenceReportPayload(
        tuple(records), formal_execution_plan, candidate_equivalence
    )
    lines = [f"evidence schema={EVIDENCE_REPORT_SCHEMA}"]
    if payload.formal_execution_plan is not None:
        plan = payload.formal_execution_plan
        lines.append(
            f"formal_plan={plan.plan_identity} "
            f"verification_plan={plan.verification_plan.plan_identity} "
            f"candidate_ledger={plan.candidate_site_ledger.identity} "
            f"formal_policy={plan.formal_policy.value}"
        )
    for report in payload.candidate_equivalence:
        lines.append(
            f"candidate_equivalence={report.plan.plan_identity} "
            f"site={report.plan.site_identity} "
            f"candidate={report.plan.candidate_identity} "
            f"classification={report.triangle.classification.value} "
            f"reason={report.triangle.reason.value}"
        )
        if report.tool_versions:
            lines.append(
                "candidate_tools="
                + ",".join(
                    f"{name}:{version}" for name, version in report.tool_versions
                )
            )
        for route, path in report.work_directories:
            lines.append(f"candidate_work={route} path={path}")
    for item in payload.evidence:
        status = item.status
        if status == "bounded_pass":
            status = f"bounded_pass depth={item.depth}"
        elif status == "witnessed":
            cycle = dict(item.details).get("witness_cycle")
            status = f"witnessed cycle={cycle} depth={item.depth}"
        elif item.depth is not None:
            status = f"{status} depth={item.depth}"
        fields = [
            item.evidence_id,
            f"claim={item.claim}",
            f"status={status}",
        ]
        if item.mode is not None:
            fields.append(f"mode={item.mode}")
        if item.property_id is not None:
            fields.append(f"property={item.property_id}")
        if item.backend is not None:
            fields.append(f"backend={item.backend}")
        if item.engine is not None:
            fields.append(f"engine={item.engine}")
        if item.solver is not None:
            fields.append(f"solver={item.solver}")
        if item.route is not None:
            fields.append(f"route={item.route}")
        if item.counterexample_digest is not None:
            fields.append(f"counterexample={item.counterexample_digest}")
        lines.append(" ".join(fields))
    return "\n".join(lines) + "\n"


def build_evidence_report(
    report_id: str,
    records: Iterable[EvidenceRecord],
    *,
    format: str,
    logical_path: str | None = None,
    formal_execution_plan: "CompilerFormalExecutionPlan | None" = None,
    candidate_equivalence: tuple["CandidateEquivalenceExecutionReport", ...] = (),
) -> RenderedEvidenceReport:
    """Render evidence and create the corresponding whole-build report record."""

    ordered = _ordered(records)
    if format == "json":
        content = render_evidence_json(
            ordered,
            formal_execution_plan=formal_execution_plan,
            candidate_equivalence=candidate_equivalence,
        )
    elif format == "text":
        content = render_evidence_text(
            ordered,
            formal_execution_plan=formal_execution_plan,
            candidate_equivalence=candidate_equivalence,
        )
    else:
        raise EvidenceReportError("evidence report format must be 'text' or 'json'")
    record = ReportRecord(
        report_id=report_id,
        kind="evidence",
        format=format,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        logical_path=logical_path,
        evidence_ids=tuple(item.evidence_id for item in ordered),
    )
    return RenderedEvidenceReport(record, content)


__all__ = [
    "EVIDENCE_REPORT_SCHEMA",
    "EvidenceReportError",
    "EvidenceReportPayload",
    "RenderedEvidenceReport",
    "build_evidence_report",
    "evidence_for_typed_module",
    "evidence_for_unexecuted_property",
    "evidence_for_validated_timing",
    "evidence_from_cross_backend_result",
    "evidence_from_equivalence_result",
    "evidence_from_formal_exploration_record",
    "evidence_from_formal_result",
    "evidence_from_verification_job_result",
    "evidence_from_verification_report",
    "evidence_from_result",
    "render_evidence_json",
    "render_evidence_text",
]
