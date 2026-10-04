# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Neutral construction primitives for deterministic compiler evidence records."""

from __future__ import annotations

from zlang.build_manifest import EvidenceRecord
from zlang.common import stable_digest, stable_json
from zlang.formal_counterexample_codec import counterexample_to_data
from zlang.ir.equivalence import EquivalenceCounterexample
from zlang.ir.formal import Counterexample
from zlang.source import SourceOrigin


EVIDENCE_REPORT_SCHEMA = "zlang-evidence-report-v2"


class EvidenceReportError(ValueError):
    """A value cannot be represented as truthful structured evidence."""


def evidence_details(**values: object) -> tuple[tuple[str, str], ...]:
    """Render typed evidence metadata into its stable manifest representation."""

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


def build_evidence_record(
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
    counterexample: Counterexample | EquivalenceCounterexample | None,
    details: tuple[tuple[str, str], ...],
) -> EvidenceRecord:
    """Build one content-addressed evidence record from typed compiler facts."""

    if depth is not None and depth < 1:
        raise EvidenceReportError("evidence depth must be positive")
    try:
        counterexample_data = counterexample_to_data(counterexample)
    except TypeError as error:
        raise EvidenceReportError(
            "counterexample evidence must use an safety verification or "
            "semantic-reference equivalence typed counterexample"
        ) from error
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


__all__ = [
    "EVIDENCE_REPORT_SCHEMA",
    "EvidenceReportError",
    "build_evidence_record",
    "evidence_details",
]
