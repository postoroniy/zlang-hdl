"""Truthful reporting for existing M36/M38 triangular evidence.

This module only orchestrates typed results that already exist.  It does not
run solvers, create a new equivalence relation, or participate in M39 candidate
eligibility.  M38 without both compatible M36 semantic-reference legs remains
explicitly raw advisory evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Mapping

from zlang.build_manifest import BuildManifestError, EvidenceRecord
from zlang.common import stable_digest, stable_json
from zlang.evidence_report import (
    evidence_from_cross_backend_result,
    evidence_from_equivalence_result,
)
from zlang.ir.cross_backend import CrossBackendResult
from zlang.ir.equivalence import EquivalenceResult
from zlang.ir.formal_planning import (
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalRouteKind,
)


TRIANGULAR_EVIDENCE_SCHEMA = 1


class TriangularEvidenceError(ValueError):
    """Supplied M36/M38 plans or evidence cannot describe one triangle."""


class M38EvidenceClassification(str, Enum):
    RAW_ADVISORY = "raw_advisory"
    TRIANGULAR = "triangular"


class M38EvidenceReason(str, Enum):
    COMPLETE_TRIANGLE = "complete_triangle"
    M38_NOT_EXECUTED = "m38_not_executed"
    M38_UNAVAILABLE = "m38_unavailable"
    M36_LEGS_MISSING = "m36_legs_missing"
    M36_LEG_NOT_EXECUTED = "m36_leg_not_executed"
    M36_LEG_UNAVAILABLE = "m36_leg_unavailable"


_DECISIVE = {"bounded_pass", "proven", "failed"}


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TriangularEvidenceError(f"{description} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TriangularEvidenceError(f"{description} keys must be strings")
    return value  # type: ignore[return-value]


def _exact_keys(
    data: Mapping[str, object], expected: set[str], description: str
) -> None:
    actual = set(data)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("unexpected " + ", ".join(extra))
    raise TriangularEvidenceError(f"{description} has " + "; ".join(details))


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise TriangularEvidenceError(f"{description} must be a non-empty string")
    return value


def _detail(record: EvidenceRecord, name: str) -> str:
    values = dict(record.details)
    value = values.get(name)
    if value is None:
        raise TriangularEvidenceError(
            f"{record.claim} evidence is missing detail '{name}'"
        )
    return value


def _integer_detail(record: EvidenceRecord, name: str) -> int:
    value = _detail(record, name)
    try:
        return int(value)
    except ValueError as error:
        raise TriangularEvidenceError(
            f"{record.claim} detail '{name}' must be an integer"
        ) from error


def _record_from_data(value: object) -> EvidenceRecord:
    try:
        return EvidenceRecord.from_data(value)
    except (BuildManifestError, TypeError, ValueError) as error:
        raise TriangularEvidenceError(str(error)) from error


def _validate_failed_record(record: EvidenceRecord) -> None:
    if record.status == "failed" and record.counterexample_digest is None:
        raise TriangularEvidenceError(
            f"failed {record.claim} evidence requires counterexample metadata"
        )


def _require_m36_plan(plan: FormalGoalPlan) -> None:
    if not isinstance(plan, FormalGoalPlan):
        raise TriangularEvidenceError("an M36 leg requires a typed FormalGoalPlan")
    if plan.kind is not FormalPlanGoalKind.M36_EQUIVALENCE:
        raise TriangularEvidenceError("an M36 leg requires an M36 goal plan")
    if plan.route is not None and plan.route.kind is not FormalRouteKind.SEMANTIC_EQUIVALENCE:
        raise TriangularEvidenceError("an M36 leg has an incompatible route")


def _require_m38_plan(plan: FormalGoalPlan) -> None:
    if not isinstance(plan, FormalGoalPlan):
        raise TriangularEvidenceError("M38 evidence requires a typed FormalGoalPlan")
    if plan.kind is not FormalPlanGoalKind.M38_EQUIVALENCE:
        raise TriangularEvidenceError("M38 evidence requires an M38 goal plan")
    if plan.route is not None and plan.route.kind is not FormalRouteKind.CROSS_BACKEND_EQUIVALENCE:
        raise TriangularEvidenceError("M38 evidence has an incompatible route")


def _validate_m36_evidence(plan: FormalGoalPlan, evidence: EvidenceRecord) -> None:
    if plan.route is None:
        raise TriangularEvidenceError("a skipped M36 plan cannot carry executed evidence")
    if evidence.claim != "m36.selected_architecture_equivalence":
        raise TriangularEvidenceError("an M36 leg requires M36 evidence")
    if evidence.property_id != plan.property_identity:
        raise TriangularEvidenceError("M36 property identity does not match its plan")
    if evidence.candidate_identity != plan.selected_ir_identity:
        raise TriangularEvidenceError("M36 selected-IR identity does not match its plan")
    artifact = plan.route.artifacts[0]
    if (evidence.backend, evidence.artifact_hash) != (
        artifact.backend, artifact.artifact_identity,
    ):
        raise TriangularEvidenceError("M36 backend artifact does not match its plan")
    if evidence.reference_hash != plan.route.reference_identity:
        raise TriangularEvidenceError("M36 reference identity does not match its plan")
    if evidence.status in _DECISIVE:
        if evidence.depth is None:
            raise TriangularEvidenceError("decisive M36 evidence requires a depth")
        if evidence.depth < plan.minimum_bmc_depth:
            raise TriangularEvidenceError(
                "M36 evidence depth does not reach its comparison window"
            )
    _validate_failed_record(evidence)


def _validate_m38_evidence(plan: FormalGoalPlan, evidence: EvidenceRecord) -> None:
    if plan.route is None:
        raise TriangularEvidenceError("a skipped M38 plan cannot carry executed evidence")
    if evidence.claim != "m38.cross_backend_equivalence":
        raise TriangularEvidenceError("M38 reporting requires M38 evidence")
    if evidence.property_id != plan.property_identity:
        raise TriangularEvidenceError("M38 property identity does not match its plan")
    if evidence.candidate_identity != plan.selected_ir_identity:
        raise TriangularEvidenceError("M38 selected-IR identity does not match its plan")
    expected = tuple(
        (item.backend, item.artifact_identity) for item in plan.route.artifacts
    )
    actual = (
        (_detail(evidence, "left_backend"), _detail(evidence, "left_artifact_hash")),
        (_detail(evidence, "right_backend"), _detail(evidence, "right_artifact_hash")),
    )
    if actual != expected:
        raise TriangularEvidenceError("M38 backend artifacts do not match its plan")
    if evidence.status in _DECISIVE:
        if evidence.depth is None:
            raise TriangularEvidenceError("decisive M38 evidence requires a depth")
        if evidence.depth < plan.minimum_bmc_depth:
            raise TriangularEvidenceError(
                "M38 evidence depth does not reach its comparison window"
            )
    _validate_failed_record(evidence)


def _validate_partial_m36_leg(
    m38_plan: FormalGoalPlan,
    leg: "M36EvidenceLeg",
    *,
    side: str,
    artifact_index: int,
) -> None:
    """Bind one supplied M36 leg to its exact side of the M38 plan.

    Partial reports are intentionally useful when only one semantic-reference
    backend route executed.  They must nevertheless not accept a leg from a
    different candidate, timing window, or physical artifact merely because
    the opposite leg is absent.
    """

    if leg.plan.selected_ir_identity != m38_plan.selected_ir_identity:
        raise TriangularEvidenceError(
            f"{side} M36/M38 selected-IR identities do not match"
        )
    if leg.plan.comparison_window != m38_plan.comparison_window:
        raise TriangularEvidenceError(
            f"{side} M36 comparison window does not match the M38 plan"
        )
    if m38_plan.route is None:
        # A non-executable M38 plan has no ordered artifact pair.  The exact
        # M36 plan/evidence remains independently validated by M36EvidenceLeg.
        return
    if leg.plan.route is None:
        raise TriangularEvidenceError(
            f"{side} M36 plan has no artifact for the executable M38 route"
        )
    expected = m38_plan.route.artifacts[artifact_index]
    actual = leg.plan.route.artifacts[0]
    if (
        actual.backend,
        actual.artifact_identity,
    ) != (
        expected.backend,
        expected.artifact_identity,
    ):
        raise TriangularEvidenceError(
            f"{side} M36 artifact does not match the ordered M38 route"
        )


@dataclass(frozen=True)
class M36EvidenceLeg:
    """One M36 route and its normalized common evidence record."""

    plan: FormalGoalPlan
    evidence: EvidenceRecord | None = None

    def __post_init__(self) -> None:
        _require_m36_plan(self.plan)
        if self.evidence is not None:
            if not isinstance(self.evidence, EvidenceRecord):
                raise TriangularEvidenceError(
                    "M36 leg evidence must use a typed EvidenceRecord"
                )
            _validate_m36_evidence(self.plan, self.evidence)

    @classmethod
    def from_result(
        cls, plan: FormalGoalPlan, result: EquivalenceResult | None
    ) -> "M36EvidenceLeg":
        if result is not None and not isinstance(result, EquivalenceResult):
            raise TriangularEvidenceError("M36 result must use typed M36 IR")
        return cls(
            plan,
            None if result is None else evidence_from_equivalence_result(result),
        )

    @property
    def is_decisive(self) -> bool:
        return self.evidence is not None and self.evidence.status in _DECISIVE

    def to_data(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_data(),
            "evidence": None if self.evidence is None else self.evidence.to_data(),
        }

    def identity_data(self) -> dict[str, object]:
        return {
            "plan_identity": self.plan.plan_identity,
            "evidence": (
                None if self.evidence is None else self.evidence.identity_data()
            ),
        }

    @classmethod
    def from_data(cls, value: object) -> "M36EvidenceLeg":
        data = _mapping(value, "M36 evidence leg")
        _exact_keys(data, {"plan", "evidence"}, "M36 evidence leg")
        try:
            plan = FormalGoalPlan.from_data(data["plan"])
        except ValueError as error:
            raise TriangularEvidenceError(str(error)) from error
        return cls(
            plan,
            None if data["evidence"] is None else _record_from_data(data["evidence"]),
        )


def _validate_triangle(
    m38_plan: FormalGoalPlan,
    m38_evidence: EvidenceRecord | None,
    left: M36EvidenceLeg,
    right: M36EvidenceLeg,
) -> None:
    if left.plan.property_identity != right.plan.property_identity:
        raise TriangularEvidenceError("M36 legs use different canonical properties")
    if not (
        left.plan.selected_ir_identity
        == right.plan.selected_ir_identity
        == m38_plan.selected_ir_identity
    ):
        raise TriangularEvidenceError("triangle selected-IR identities do not match")
    if not (
        left.plan.comparison_window
        == right.plan.comparison_window
        == m38_plan.comparison_window
    ):
        raise TriangularEvidenceError("triangle comparison windows do not match")

    if left.plan.route is not None and right.plan.route is not None:
        if left.plan.route.reference_identity != right.plan.route.reference_identity:
            raise TriangularEvidenceError("M36 legs use different references")
        if m38_plan.route is not None:
            m36_artifacts = (
                (
                    left.plan.route.artifacts[0].backend,
                    left.plan.route.artifacts[0].artifact_identity,
                ),
                (
                    right.plan.route.artifacts[0].backend,
                    right.plan.route.artifacts[0].artifact_identity,
                ),
            )
            m38_artifacts = tuple(
                (item.backend, item.artifact_identity)
                for item in m38_plan.route.artifacts
            )
            if m36_artifacts != m38_artifacts:
                raise TriangularEvidenceError(
                    "M36 plan artifacts do not match the ordered M38 route"
                )

    if left.evidence is None or right.evidence is None:
        return
    if left.evidence.mode != right.evidence.mode:
        raise TriangularEvidenceError("triangle proof modes do not match")
    if left.evidence.depth != right.evidence.depth:
        raise TriangularEvidenceError("triangle proof depths do not match")
    if left.evidence.relation != right.evidence.relation:
        raise TriangularEvidenceError("triangle equivalence relations do not match")
    if _integer_detail(left.evidence, "latency_delta") != _integer_detail(
        right.evidence, "latency_delta"
    ):
        raise TriangularEvidenceError("triangle latency relations do not match")
    if left.evidence.reference_hash != right.evidence.reference_hash:
        raise TriangularEvidenceError("M36 legs use different references")
    if m38_evidence is None:
        return
    if left.evidence.mode != m38_evidence.mode:
        raise TriangularEvidenceError("triangle proof modes do not match")
    if left.evidence.depth != m38_evidence.depth:
        raise TriangularEvidenceError("triangle proof depths do not match")
    if left.evidence.relation != m38_evidence.relation:
        raise TriangularEvidenceError("triangle equivalence relations do not match")
    left_delta = _integer_detail(left.evidence, "latency_delta")
    right_delta = _integer_detail(right.evidence, "latency_delta")
    cross_backend_delta = _integer_detail(m38_evidence, "latency_delta")
    # Each M36 delta is measured from the shared semantic reference to one
    # backend. M38 is measured from the left backend to the right backend, so
    # its exact relation is the difference of those two M36 deltas. Comparing
    # it directly with either M36 leg incorrectly rejects every non-zero but
    # equally pipelined candidate triangle.
    if cross_backend_delta != right_delta - left_delta:
        raise TriangularEvidenceError("triangle latency relations do not match")


@dataclass(frozen=True)
class M38EvidenceReport:
    """A raw advisory M38 record or a complete M36/M38 triangle."""

    m38_plan: FormalGoalPlan
    m38_evidence: EvidenceRecord | None = None
    left_m36: M36EvidenceLeg | None = None
    right_m36: M36EvidenceLeg | None = None
    schema_version: int = TRIANGULAR_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ):
            raise TriangularEvidenceError(
                "triangular-evidence schema must be an integer"
            )
        if self.schema_version != TRIANGULAR_EVIDENCE_SCHEMA:
            raise TriangularEvidenceError(
                f"unsupported triangular-evidence schema {self.schema_version}"
            )
        _require_m38_plan(self.m38_plan)
        if self.m38_evidence is not None:
            if not isinstance(self.m38_evidence, EvidenceRecord):
                raise TriangularEvidenceError(
                    "M38 evidence must use a typed EvidenceRecord"
                )
            _validate_m38_evidence(self.m38_plan, self.m38_evidence)
        if self.left_m36 is not None and not isinstance(self.left_m36, M36EvidenceLeg):
            raise TriangularEvidenceError("left M36 evidence must use a typed leg")
        if self.right_m36 is not None and not isinstance(self.right_m36, M36EvidenceLeg):
            raise TriangularEvidenceError("right M36 evidence must use a typed leg")
        if self.left_m36 is not None:
            _validate_partial_m36_leg(
                self.m38_plan,
                self.left_m36,
                side="left",
                artifact_index=0,
            )
        if self.right_m36 is not None:
            _validate_partial_m36_leg(
                self.m38_plan,
                self.right_m36,
                side="right",
                artifact_index=1,
            )
        if self.left_m36 is not None and self.right_m36 is not None:
            _validate_triangle(
                self.m38_plan, self.m38_evidence, self.left_m36, self.right_m36
            )

    @classmethod
    def from_results(
        cls,
        m38_plan: FormalGoalPlan,
        m38_result: CrossBackendResult | None,
        left_m36: M36EvidenceLeg | None = None,
        right_m36: M36EvidenceLeg | None = None,
    ) -> "M38EvidenceReport":
        if m38_result is not None and not isinstance(m38_result, CrossBackendResult):
            raise TriangularEvidenceError("M38 result must use typed M38 IR")
        return cls(
            m38_plan,
            None if m38_result is None else evidence_from_cross_backend_result(m38_result),
            left_m36,
            right_m36,
        )

    @property
    def classification(self) -> M38EvidenceClassification:
        if (
            self.m38_evidence is not None
            and self.m38_evidence.status in _DECISIVE
            and self.left_m36 is not None
            and self.right_m36 is not None
            and self.left_m36.is_decisive
            and self.right_m36.is_decisive
        ):
            return M38EvidenceClassification.TRIANGULAR
        return M38EvidenceClassification.RAW_ADVISORY

    @property
    def reason(self) -> M38EvidenceReason:
        if self.classification is M38EvidenceClassification.TRIANGULAR:
            return M38EvidenceReason.COMPLETE_TRIANGLE
        if self.m38_plan.route is None:
            return M38EvidenceReason.M38_UNAVAILABLE
        if self.m38_evidence is None:
            return M38EvidenceReason.M38_NOT_EXECUTED
        if self.m38_evidence.status not in _DECISIVE:
            return M38EvidenceReason.M38_UNAVAILABLE
        if self.left_m36 is None or self.right_m36 is None:
            return M38EvidenceReason.M36_LEGS_MISSING
        if self.left_m36.plan.route is None or self.right_m36.plan.route is None:
            return M38EvidenceReason.M36_LEG_UNAVAILABLE
        if self.left_m36.evidence is None or self.right_m36.evidence is None:
            return M38EvidenceReason.M36_LEG_NOT_EXECUTED
        return M38EvidenceReason.M36_LEG_UNAVAILABLE

    @property
    def verification_failure(self) -> bool:
        records = (
            self.m38_evidence,
            None if self.left_m36 is None else self.left_m36.evidence,
            None if self.right_m36 is None else self.right_m36.evidence,
        )
        return any(item is not None and item.status == "failed" for item in records)

    @property
    def affects_m39_eligibility(self) -> bool:
        """M38 is advisory and cannot influence M39 selection."""

        return False

    def identity_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "m38_plan_identity": self.m38_plan.plan_identity,
            "m38_evidence": (
                None
                if self.m38_evidence is None
                else self.m38_evidence.identity_data()
            ),
            "left_m36": (
                None if self.left_m36 is None else self.left_m36.identity_data()
            ),
            "right_m36": (
                None if self.right_m36 is None else self.right_m36.identity_data()
            ),
        }

    @property
    def evidence_identity(self) -> str:
        return "m38-evidence:" + stable_digest(self.identity_data())

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "m38_plan": self.m38_plan.to_data(),
            "m38_plan_identity": self.m38_plan.plan_identity,
            "m38_evidence": (
                None if self.m38_evidence is None else self.m38_evidence.to_data()
            ),
            "left_m36": None if self.left_m36 is None else self.left_m36.to_data(),
            "right_m36": None if self.right_m36 is None else self.right_m36.to_data(),
            "evidence_identity": self.evidence_identity,
            "classification": self.classification.value,
            "reason": self.reason.value,
            "verification_failure": self.verification_failure,
            "affects_m39_eligibility": self.affects_m39_eligibility,
        }

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_data(cls, value: object) -> "M38EvidenceReport":
        data = _mapping(value, "M38 evidence report")
        _exact_keys(
            data,
            {
                "schema_version", "m38_plan", "m38_plan_identity", "m38_evidence",
                "left_m36", "right_m36", "evidence_identity", "classification",
                "reason", "verification_failure", "affects_m39_eligibility",
            },
            "M38 evidence report",
        )
        schema = data["schema_version"]
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise TriangularEvidenceError(
                "triangular-evidence schema must be an integer"
            )
        try:
            plan = FormalGoalPlan.from_data(data["m38_plan"])
        except ValueError as error:
            raise TriangularEvidenceError(str(error)) from error
        restored = cls(
            plan,
            (
                None
                if data["m38_evidence"] is None
                else _record_from_data(data["m38_evidence"])
            ),
            (
                None
                if data["left_m36"] is None
                else M36EvidenceLeg.from_data(data["left_m36"])
            ),
            (
                None
                if data["right_m36"] is None
                else M36EvidenceLeg.from_data(data["right_m36"])
            ),
            schema,
        )
        if _string(data["m38_plan_identity"], "M38 plan identity") != plan.plan_identity:
            raise TriangularEvidenceError("M38 plan identity does not match its contents")
        claimed_identity = _string(
            data["evidence_identity"], "M38 evidence identity"
        )
        if claimed_identity != restored.evidence_identity:
            raise TriangularEvidenceError("M38 evidence identity does not match its contents")
        for field, expected in (
            ("classification", restored.classification.value),
            ("reason", restored.reason.value),
        ):
            if _string(data[field], f"M38 {field}") != expected:
                raise TriangularEvidenceError(
                    f"M38 evidence {field} does not match its contents"
                )
        for field, expected in (
            ("verification_failure", restored.verification_failure),
            ("affects_m39_eligibility", restored.affects_m39_eligibility),
        ):
            actual = data[field]
            if not isinstance(actual, bool) or actual is not expected:
                raise TriangularEvidenceError(
                    f"M38 evidence {field} does not match its contents"
                )
        return restored

    @classmethod
    def from_json(cls, text: str) -> "M38EvidenceReport":
        try:
            data = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise TriangularEvidenceError("M38 evidence report is not valid JSON") from error
        return cls.from_data(data)


__all__ = [
    "M36EvidenceLeg",
    "M38EvidenceClassification",
    "M38EvidenceReason",
    "M38EvidenceReport",
    "TRIANGULAR_EVIDENCE_SCHEMA",
    "TriangularEvidenceError",
]
