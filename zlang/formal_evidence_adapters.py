# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Evidence adapters for formal selection and semantic equivalence products."""

from __future__ import annotations

from zlang.build_manifest import EvidenceRecord
from zlang.evidence_records import (
    EvidenceReportError,
    build_evidence_record,
    evidence_details,
)
from zlang.formal_exploration import FormalExplorationRecord
from zlang.ir.equivalence import EquivalenceCounterexample, EquivalenceResult
from zlang.ir.formal import Counterexample


def evidence_from_equivalence_result(result: EquivalenceResult) -> EvidenceRecord:
    """Adapt one semantic-reference equivalence result."""

    if not isinstance(result, EquivalenceResult):
        raise TypeError("semantic-reference equivalence evidence requires EquivalenceResult")
    return build_evidence_record(
        "semantic_equivalence",
        claim="semantic_equivalence.selected_architecture_equivalence",
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
        details=evidence_details(
            binding_map_version=result.binding_map_version,
            latency_delta=result.latency_delta,
            reason=result.reason,
            unbounded=(result.mode.value != "bmc"),
        ),
    )


def evidence_from_formal_exploration_record(
    result: FormalExplorationRecord,
    *,
    site_identity: str | None = None,
) -> EvidenceRecord:
    """Adapt formal-aware selection without promoting an unexecuted route."""

    if not isinstance(result, FormalExplorationRecord):
        raise TypeError("formal-aware selection evidence requires FormalExplorationRecord")
    if not result.candidate_identity:
        raise EvidenceReportError(
            "formal-aware selection evidence requires a candidate identity"
        )
    if result.rank < 1:
        raise EvidenceReportError(
            "formal-aware selection evidence rank must be positive"
        )
    if site_identity is not None and not site_identity:
        raise EvidenceReportError(
            "formal-aware selection candidate-site identity must be non-empty"
        )
    if result.status is None:
        if result.mode is not None or result.depth is not None:
            raise EvidenceReportError(
                "unexecuted formal-aware selection evidence cannot carry proof mode or depth"
            )
        status = "not_run"
        mode = None
        depth = None
    else:
        status = result.status.value
        if result.mode is None:
            raise EvidenceReportError(
                "executed formal-aware selection evidence requires a proof mode"
            )
        mode = result.mode.value
        depth = result.depth
        if status in {"bounded_pass", "proven", "failed"}:
            if not result.backend or not result.artifact_hash:
                raise EvidenceReportError(
                    "decisive formal-aware selection evidence requires a connected "
                    "backend artifact"
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
                        "decisive formal-aware selection evidence requires " + label
                    )
    counterexample = result.counterexample
    if counterexample is not None and not isinstance(
        counterexample,
        (Counterexample, EquivalenceCounterexample),
    ):
        raise EvidenceReportError(
            "formal-aware selection counterexample metadata must use typed formal IR"
        )
    if status == "failed" and counterexample is None:
        raise EvidenceReportError(
            "failed formal-aware selection evidence requires counterexample metadata"
        )
    context: dict[str, object] = {
        "tool_versions": [list(item) for item in result.tool_versions],
        "unbounded": False if mode == "bmc" else status == "proven",
    }
    metadata = result.evidence_metadata
    if metadata is not None:
        context.update(metadata.details())
    return build_evidence_record(
        "formal_selection",
        claim="formal_selection.formal_candidate_eligibility",
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
        relation=(None if metadata is None else metadata.relation),
        route=result.formal_route,
        counterexample=counterexample,
        details=evidence_details(
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
            **context,
            selected_origin=(
                None
                if result.selected_origin is None
                else result.selected_origin.to_data()
            ),
        ),
    )


__all__ = [
    "evidence_from_equivalence_result",
    "evidence_from_formal_exploration_record",
]
