"""Compiler-owned view over the production M35, M36, and M39 products.

This module deliberately does not merge the independently versioned M35, M36,
or M39 result types.  It records how one compilation relates the
per-goal verification plan to the selection-owned candidate ledger and to the
M39 evidence records that already exist on selected IR.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping

from zlang.build_manifest import EvidenceRecord
from zlang.candidate_sites import (
    CandidateSiteLedger,
    CandidateSiteRecord,
    candidate_formal_record_sites,
)
from zlang.common import stable_digest, stable_json
from zlang.equivalence_result_codec import (
    equivalence_result_from_data,
    equivalence_result_to_data,
)
from zlang.evidence_report import (
    evidence_from_equivalence_result,
    evidence_from_formal_exploration_record,
)
from zlang.formal_exploration import FormalExplorationRecord, FormalPolicy
from zlang.ir.formal_planning import FormalExecutionPlan, FormalPlanningError
from zlang.ir.formal_planning import FormalGoalPlan, FormalPlanGoalKind
from zlang.ir.equivalence import EquivalenceResult, EquivalenceStatus


COMPILER_FORMAL_EXECUTION_PLAN_SCHEMA = 2


class FormalOrchestrationError(ValueError):
    """Compiler formal products are missing or cross-linked inconsistently."""


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise FormalOrchestrationError(f"{label} must be a JSON object")
    return value  # type: ignore[return-value]


def _exact_keys(data: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(data) != expected:
        raise FormalOrchestrationError(
            f"{label} fields differ: expected {sorted(expected)}, got {sorted(data)}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FormalOrchestrationError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FormalOrchestrationError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class M39AttemptReference:
    """Stable reference from the compiler plan to one existing M39 record."""

    site_identity: str
    candidate_identity: str
    rank: int
    evidence_id: str
    policy: FormalPolicy
    status: str
    mode: str | None
    depth: int | None
    property_identity: str | None
    route: str

    def __post_init__(self) -> None:
        _string(self.site_identity, "M39 candidate-site identity")
        _string(self.candidate_identity, "M39 candidate identity")
        _integer(self.rank, "M39 candidate rank")
        _string(self.evidence_id, "M39 evidence identity")
        try:
            object.__setattr__(self, "policy", FormalPolicy(self.policy))
        except ValueError as error:
            raise FormalOrchestrationError(str(error)) from error
        if self.status not in {
            "not_run", "bounded_pass", "proven", "failed", "unknown", "skipped",
        }:
            raise FormalOrchestrationError(
                f"unsupported M39 attempt status '{self.status}'"
            )
        if self.mode is not None and self.mode not in {"bmc", "prove"}:
            raise FormalOrchestrationError(
                f"unsupported M39 attempt mode '{self.mode}'"
            )
        if self.depth is not None:
            _integer(self.depth, "M39 attempt depth")
        if self.status == "not_run" and (
            self.mode is not None or self.depth is not None
        ):
            raise FormalOrchestrationError(
                "an unexecuted M39 attempt cannot carry mode or depth"
            )
        if self.status == "bounded_pass" and (
            self.mode != "bmc" or self.depth is None
        ):
            raise FormalOrchestrationError(
                "M39 bounded_pass requires BMC mode and a positive depth"
            )
        if self.status == "failed" and (
            self.mode not in {"bmc", "prove"} or self.depth is None
        ):
            raise FormalOrchestrationError(
                "M39 failed requires BMC or prove mode and a positive depth"
            )
        if self.status == "proven" and self.mode != "prove":
            raise FormalOrchestrationError("M39 proven requires prove mode")
        _optional_string(self.property_identity, "M39 property identity")
        _string(self.route, "M39 formal route")

    @classmethod
    def from_evidence(
        cls,
        *,
        site_identity: str,
        rank: int,
        evidence: EvidenceRecord,
    ) -> "M39AttemptReference":
        if evidence.claim != "m39.formal_candidate_eligibility":
            raise FormalOrchestrationError(
                "M39 attempt reference requires M39 candidate evidence"
            )
        if evidence.candidate_identity is None or evidence.route is None:
            raise FormalOrchestrationError(
                "M39 candidate evidence is missing candidate or route identity"
            )
        details = dict(evidence.details)
        try:
            policy = FormalPolicy(details["policy"])
        except (KeyError, ValueError) as error:
            raise FormalOrchestrationError(
                "M39 candidate evidence has no valid formal policy"
            ) from error
        try:
            evidence_rank = int(details["rank"])
        except (KeyError, ValueError) as error:
            raise FormalOrchestrationError(
                "M39 candidate evidence has no valid rank"
            ) from error
        if evidence_rank != rank:
            raise FormalOrchestrationError(
                "M39 candidate evidence rank differs from its candidate ledger"
            )
        return cls(
            site_identity,
            evidence.candidate_identity,
            rank,
            evidence.evidence_id,
            policy,
            evidence.status,
            evidence.mode,
            evidence.depth,
            evidence.property_id,
            evidence.route,
        )

    def identity_data(self) -> dict[str, object]:
        return {
            "site_identity": self.site_identity,
            "candidate_identity": self.candidate_identity,
            "rank": self.rank,
            "evidence_id": self.evidence_id,
            "policy": self.policy.value,
            "status": self.status,
            "mode": self.mode,
            "depth": self.depth,
            "property_identity": self.property_identity,
            "route": self.route,
        }

    def to_data(self) -> dict[str, object]:
        return self.identity_data()

    @classmethod
    def from_data(cls, value: object) -> "M39AttemptReference":
        data = _mapping(value, "M39 attempt reference")
        _exact_keys(
            data,
            {
                "site_identity", "candidate_identity", "rank", "evidence_id",
                "policy", "status", "mode", "depth", "property_identity", "route",
            },
            "M39 attempt reference",
        )
        depth = data["depth"]
        if depth is not None:
            depth = _integer(depth, "M39 attempt depth")
        try:
            policy = FormalPolicy(_string(data["policy"], "M39 formal policy"))
        except ValueError as error:
            raise FormalOrchestrationError(str(error)) from error
        return cls(
            _string(data["site_identity"], "M39 candidate-site identity"),
            _string(data["candidate_identity"], "M39 candidate identity"),
            _integer(data["rank"], "M39 candidate rank"),
            _string(data["evidence_id"], "M39 evidence identity"),
            policy,
            _string(data["status"], "M39 attempt status"),
            _optional_string(data["mode"], "M39 attempt mode"),
            depth,
            _optional_string(data["property_identity"], "M39 property identity"),
            _string(data["route"], "M39 formal route"),
        )


@dataclass(frozen=True)
class CandidateEquivalencePlanReference:
    """Typed direct-SystemVerilog M36 plan for one selected candidate site."""

    site_identity: str
    candidate_identity: str
    direct_systemverilog_m36: FormalGoalPlan
    implementation_observable_identity: str

    def __post_init__(self) -> None:
        _string(self.site_identity, "candidate equivalence site identity")
        _string(self.candidate_identity, "candidate equivalence identity")
        _string(
            self.implementation_observable_identity,
            "candidate implementation observable identity",
        )
        plan = self.direct_systemverilog_m36
        if not isinstance(plan, FormalGoalPlan):
            raise FormalOrchestrationError("direct-SystemVerilog M36 plan must be typed")
        if plan.kind is not FormalPlanGoalKind.M36_EQUIVALENCE:
            raise FormalOrchestrationError("direct-SystemVerilog M36 plan has the wrong kind")
        if plan.selected_ir_identity != self.candidate_identity:
            raise FormalOrchestrationError(
                "direct-SystemVerilog M36 plan references a different candidate"
            )
        if self.implementation_observable_identity not in plan.required_observations:
            raise FormalOrchestrationError(
                "direct-SystemVerilog M36 plan does not publish the candidate "
                "implementation observable"
            )

    @property
    def plan_identity(self) -> str:
        return "candidate-equivalence-plan:" + stable_digest(self.identity_data())

    def identity_data(self) -> dict[str, object]:
        return {
            "site_identity": self.site_identity,
            "candidate_identity": self.candidate_identity,
            "direct_systemverilog_m36": self.direct_systemverilog_m36.plan_identity,
            "implementation_observable_identity": self.implementation_observable_identity,
        }

    def to_data(self) -> dict[str, object]:
        return {
            "site_identity": self.site_identity,
            "candidate_identity": self.candidate_identity,
            "direct_systemverilog_m36": self.direct_systemverilog_m36.to_data(),
            "implementation_observable_identity": self.implementation_observable_identity,
            "plan_identity": self.plan_identity,
        }

    @classmethod
    def from_data(cls, value: object) -> "CandidateEquivalencePlanReference":
        data = _mapping(value, "candidate equivalence plan")
        _exact_keys(data, {
            "site_identity", "candidate_identity", "direct_systemverilog_m36",
            "implementation_observable_identity", "plan_identity",
        }, "candidate equivalence plan")
        try:
            restored = cls(
                _string(data["site_identity"], "candidate equivalence site"),
                _string(data["candidate_identity"], "candidate identity"),
                FormalGoalPlan.from_data(data["direct_systemverilog_m36"]),
                _string(data["implementation_observable_identity"],
                        "candidate implementation observable identity"),
            )
        except FormalPlanningError as error:
            raise FormalOrchestrationError(str(error)) from error
        if data["plan_identity"] != restored.plan_identity:
            raise FormalOrchestrationError(
                "candidate equivalence plan identity does not match its contents"
            )
        return restored


@dataclass(frozen=True)
class CandidateEquivalenceExecutionReport:
    """Typed direct-SystemVerilog M36 result linked to its exact plan."""

    plan: CandidateEquivalencePlanReference
    direct_systemverilog_m36: EquivalenceResult
    bounded_prerequisite: EquivalenceResult | None = None
    tool_versions: tuple[tuple[str, str], ...] = ()
    work_directories: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.plan, CandidateEquivalencePlanReference):
            raise FormalOrchestrationError(
                "candidate equivalence execution requires a typed plan"
            )
        for label, result in (
            ("direct-SystemVerilog M36", self.direct_systemverilog_m36),
            ("bounded prerequisite", self.bounded_prerequisite),
        ):
            if result is not None and not isinstance(result, EquivalenceResult):
                raise FormalOrchestrationError(f"{label} must be typed M36 evidence")
            if result is not None and result.property_id != (
                self.plan.direct_systemverilog_m36.property_identity
            ):
                raise FormalOrchestrationError(f"{label} differs from its M36 plan")
        if (
            self.bounded_prerequisite is not None
            and self.bounded_prerequisite.mode.value != "bmc"
        ):
            raise FormalOrchestrationError(
                "candidate bounded prerequisite contains non-BMC evidence"
            )
        for label, values in (
            ("tool versions", self.tool_versions),
            ("work directories", self.work_directories),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(item, tuple) or len(item) != 2
                or not all(isinstance(value, str) and value for value in item)
                for item in values
            ):
                raise FormalOrchestrationError(
                    f"candidate {label} require non-empty string pairs"
                )
            ordered = tuple(sorted(values))
            if len({name for name, _ in ordered}) != len(ordered):
                raise FormalOrchestrationError(f"candidate {label} contain duplicates")
            object.__setattr__(self, label.replace(" ", "_"), ordered)

    @property
    def verification_failure(self) -> bool:
        return any(
            item is not None and item.status is EquivalenceStatus.FAILED
            for item in (self.bounded_prerequisite, self.direct_systemverilog_m36)
        )

    @property
    def evidence_records(self) -> tuple[EvidenceRecord, ...]:
        unique: dict[str, EvidenceRecord] = {}
        for result in (self.bounded_prerequisite, self.direct_systemverilog_m36):
            if result is not None:
                evidence = evidence_from_equivalence_result(result)
                unique.setdefault(evidence.evidence_id, evidence)
        return tuple(unique.values())

    def to_data(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_data(),
            "direct_systemverilog_m36": equivalence_result_to_data(
                self.direct_systemverilog_m36
            ),
            "bounded_prerequisite": (
                None if self.bounded_prerequisite is None
                else equivalence_result_to_data(self.bounded_prerequisite)
            ),
            "tool_versions": [list(item) for item in self.tool_versions],
            "work_directories": [list(item) for item in self.work_directories],
        }

    @classmethod
    def from_data(cls, value: object) -> "CandidateEquivalenceExecutionReport":
        data = _mapping(value, "candidate equivalence execution report")
        _exact_keys(data, {
            "plan", "direct_systemverilog_m36", "bounded_prerequisite",
            "tool_versions", "work_directories",
        }, "candidate equivalence execution report")
        def pairs(value: object, label: str) -> tuple[tuple[str, str], ...]:
            if not isinstance(value, list):
                raise FormalOrchestrationError(f"candidate {label} must be an array")
            result = []
            for item in value:
                if not isinstance(item, list) or len(item) != 2 or not all(
                    isinstance(part, str) and part for part in item
                ):
                    raise FormalOrchestrationError(
                        f"candidate {label} entries must be non-empty string pairs"
                    )
                result.append((item[0], item[1]))
            return tuple(result)
        return cls(
            CandidateEquivalencePlanReference.from_data(data["plan"]),
            equivalence_result_from_data(data["direct_systemverilog_m36"]),
            None if data["bounded_prerequisite"] is None else
                equivalence_result_from_data(data["bounded_prerequisite"]),
            pairs(data["tool_versions"], "tool versions"),
            pairs(data["work_directories"], "work directories"),
        )

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "CandidateEquivalenceExecutionReport":
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise FormalOrchestrationError(
                "candidate equivalence execution report is not valid JSON"
            ) from error
        return cls.from_data(value)

def _compilation_formal_policy(compilation: object) -> FormalPolicy:
    request = getattr(compilation, "implementation_request", None)
    try:
        return FormalPolicy(getattr(request, "formal_policy"))
    except (TypeError, ValueError) as error:
        raise FormalOrchestrationError(
            "compilation has no normalized formal policy"
        ) from error


def _m39_site_evidence(
    compilation: object,
) -> tuple[tuple[CandidateSiteRecord, EvidenceRecord], ...]:
    """Join retained records to their exact semantic sites.

    Candidate implementation identities are deliberately not used as a global
    key: two independent source sites may select the same implementation.
    """

    if _compilation_formal_policy(compilation) is FormalPolicy.OFF:
        # Selection retains useful ``not_run`` metadata under the OFF policy,
        # but it is not an M39 attempt and must not leak into evidence.
        return ()

    module = getattr(compilation, "ir", None)
    explorations = getattr(compilation, "exploration_results", ())
    if module is None:
        raise FormalOrchestrationError("compilation has no selected semantic IR")
    ledger = getattr(compilation, "candidate_site_ledger", None)
    if not isinstance(ledger, CandidateSiteLedger):
        raise FormalOrchestrationError(
            "compilation has no selection-owned CandidateSiteLedger"
        )
    ledger_sites = {site.identity: site for site in ledger.sites}
    typed: list[tuple[CandidateSiteRecord, EvidenceRecord]] = []
    by_identity: dict[str, tuple[CandidateSiteRecord, EvidenceRecord]] = {}
    for derived_site, record in candidate_formal_record_sites(module, explorations):
        if not isinstance(record, FormalExplorationRecord):
            raise FormalOrchestrationError(
                "candidate-site formal metadata must use FormalExplorationRecord"
            )
        site = ledger_sites.get(derived_site.identity)
        if site is None:
            raise FormalOrchestrationError(
                f"M39 record references unknown candidate site '{derived_site.identity}'"
            )
        matches = tuple(
            candidate
            for candidate in site.candidates
            if candidate.candidate_identity == record.candidate_identity
        )
        if len(matches) != 1 or matches[0].rank != record.rank:
            raise FormalOrchestrationError(
                "M39 record candidate/rank differs from its exact candidate site"
            )
        evidence = evidence_from_formal_exploration_record(
            record,
            site_identity=site.identity,
        )
        previous = by_identity.get(evidence.evidence_id)
        if previous is not None:
            if previous != (site, evidence):
                raise FormalOrchestrationError(
                    f"M39 evidence '{evidence.evidence_id}' is retained inconsistently"
                )
            continue
        by_identity[evidence.evidence_id] = (site, evidence)
        typed.append((site, evidence))
    return tuple(sorted(typed, key=lambda item: item[1].evidence_id))


def collect_m39_evidence(compilation: object) -> tuple[EvidenceRecord, ...]:
    """Collect every retained M39 record across all frozen candidate entry points."""

    return tuple(evidence for _, evidence in _m39_site_evidence(compilation))


@dataclass(frozen=True)
class CompilerFormalExecutionPlan:
    """One compiler-owned plan view without conflating formal result families."""

    selected_ir_identity: str
    verification_plan: FormalExecutionPlan
    candidate_site_ledger: CandidateSiteLedger
    formal_policy: FormalPolicy
    m39_attempts: tuple[M39AttemptReference, ...] = ()
    candidate_equivalence_plans: tuple[CandidateEquivalencePlanReference, ...] = ()
    schema_version: int = COMPILER_FORMAL_EXECUTION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        _string(self.selected_ir_identity, "compiler formal selected-IR identity")
        if self.verification_plan.compilation_identity != self.selected_ir_identity:
            raise FormalOrchestrationError(
                "compiler formal plan and verification plan selected-IR identities differ"
            )
        mismatched_goal = next((
            goal
            for goal in self.verification_plan.goals
            if goal.selected_ir_identity != self.selected_ir_identity
        ), None)
        if mismatched_goal is not None:
            raise FormalOrchestrationError(
                "compiler formal goal "
                f"'{mismatched_goal.goal_identity}' references a different selected IR"
            )
        if not isinstance(self.candidate_site_ledger, CandidateSiteLedger):
            raise FormalOrchestrationError(
                "compiler formal plan requires a CandidateSiteLedger"
            )
        try:
            object.__setattr__(self, "formal_policy", FormalPolicy(self.formal_policy))
        except ValueError as error:
            raise FormalOrchestrationError(str(error)) from error
        if self.schema_version != COMPILER_FORMAL_EXECUTION_PLAN_SCHEMA:
            raise FormalOrchestrationError(
                "unsupported compiler formal execution-plan schema"
            )
        ordered = tuple(sorted(
            self.m39_attempts,
            key=lambda item: (item.site_identity, item.rank, item.evidence_id),
        ))
        object.__setattr__(self, "m39_attempts", ordered)
        evidence_ids = tuple(item.evidence_id for item in ordered)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise FormalOrchestrationError(
                "compiler formal plan contains duplicate M39 evidence references"
            )
        sites = {item.identity: item for item in self.candidate_site_ledger.sites}
        for attempt in ordered:
            site = sites.get(attempt.site_identity)
            if site is None:
                raise FormalOrchestrationError(
                    f"M39 attempt references unknown candidate site '{attempt.site_identity}'"
                )
            matches = tuple(
                item for item in site.candidates
                if item.candidate_identity == attempt.candidate_identity
            )
            if len(matches) != 1 or matches[0].rank != attempt.rank:
                raise FormalOrchestrationError(
                    "M39 attempt candidate/rank differs from its candidate ledger"
                )
            if attempt.policy is not self.formal_policy:
                raise FormalOrchestrationError(
                    "M39 attempt policy differs from the compiler formal policy"
                )
        if self.formal_policy is FormalPolicy.OFF and ordered:
            raise FormalOrchestrationError(
                "formal policy 'off' cannot contain executed M39 attempt records"
            )
        candidate_plans = tuple(sorted(
            self.candidate_equivalence_plans,
            key=lambda item: item.site_identity,
        ))
        object.__setattr__(self, "candidate_equivalence_plans", candidate_plans)
        if any(
            not isinstance(item, CandidateEquivalencePlanReference)
            for item in candidate_plans
        ):
            raise FormalOrchestrationError(
                "compiler formal candidate-equivalence plans must be typed"
            )
        candidate_sites = tuple(item.site_identity for item in candidate_plans)
        if len(candidate_sites) != len(set(candidate_sites)):
            raise FormalOrchestrationError(
                "compiler formal plan contains duplicate candidate-equivalence sites"
            )
        if self.formal_policy is FormalPolicy.OFF and candidate_plans:
            raise FormalOrchestrationError(
                "formal policy 'off' cannot contain candidate-equivalence plans"
            )
        for candidate_plan in candidate_plans:
            site = sites.get(candidate_plan.site_identity)
            if site is None:
                raise FormalOrchestrationError(
                    "candidate equivalence references unknown candidate site "
                    f"'{candidate_plan.site_identity}'"
                )
            if site.selected_candidate_identity != candidate_plan.candidate_identity:
                raise FormalOrchestrationError(
                    "candidate equivalence plan differs from its ledger selection"
                )

    @property
    def plan_identity(self) -> str:
        return "compiler-formal-plan:" + stable_digest(self.identity_data())

    @property
    def m39_evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.m39_attempts)

    def identity_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "selected_ir_identity": self.selected_ir_identity,
            "verification_plan": self.verification_plan.identity_data(),
            "candidate_site_ledger": self.candidate_site_ledger.to_identity_data(),
            "formal_policy": self.formal_policy.value,
            "m39_attempts": [item.identity_data() for item in self.m39_attempts],
            "candidate_equivalence_plans": [
                item.identity_data() for item in self.candidate_equivalence_plans
            ],
        }

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "selected_ir_identity": self.selected_ir_identity,
            "verification_plan": self.verification_plan.to_data(),
            "candidate_site_ledger": self.candidate_site_ledger.to_data(),
            "formal_policy": self.formal_policy.value,
            "m39_attempts": [item.to_data() for item in self.m39_attempts],
            "candidate_equivalence_plans": [
                item.to_data() for item in self.candidate_equivalence_plans
            ],
            "plan_identity": self.plan_identity,
        }

    def to_json(self) -> str:
        return stable_json(self.to_data(), indent=2) + "\n"

    @classmethod
    def from_data(cls, value: object) -> "CompilerFormalExecutionPlan":
        data = _mapping(value, "compiler formal execution plan")
        _exact_keys(
            data,
            {
                "schema_version", "selected_ir_identity", "verification_plan",
                "candidate_site_ledger", "formal_policy", "m39_attempts",
                "candidate_equivalence_plans", "plan_identity",
            },
            "compiler formal execution plan",
        )
        attempts = data["m39_attempts"]
        if not isinstance(attempts, list):
            raise FormalOrchestrationError("M39 attempts must be an array")
        candidate_plans = data["candidate_equivalence_plans"]
        if not isinstance(candidate_plans, list):
            raise FormalOrchestrationError(
                "candidate-equivalence plans must be an array"
            )
        try:
            verification = FormalExecutionPlan.from_data(data["verification_plan"])
            ledger = CandidateSiteLedger.from_data(data["candidate_site_ledger"])
            policy = FormalPolicy(_string(data["formal_policy"], "formal policy"))
        except (FormalPlanningError, ValueError) as error:
            raise FormalOrchestrationError(str(error)) from error
        schema = data["schema_version"]
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise FormalOrchestrationError(
                "compiler formal execution-plan schema must be an integer"
            )
        restored = cls(
            _string(data["selected_ir_identity"], "selected-IR identity"),
            verification,
            ledger,
            policy,
            tuple(M39AttemptReference.from_data(item) for item in attempts),
            tuple(
                CandidateEquivalencePlanReference.from_data(item)
                for item in candidate_plans
            ),
            schema,
        )
        if data["plan_identity"] != restored.plan_identity:
            raise FormalOrchestrationError(
                "compiler formal execution-plan identity does not match its contents"
            )
        return restored

    @classmethod
    def from_json(cls, text: str) -> "CompilerFormalExecutionPlan":
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            raise FormalOrchestrationError(
                "compiler formal execution plan is not valid JSON"
            ) from error
        return cls.from_data(value)


def build_compiler_formal_execution_plan(
    compilation: object,
    verification_plan: FormalExecutionPlan,
) -> tuple[CompilerFormalExecutionPlan, tuple[EvidenceRecord, ...]]:
    """Join existing planning products after selection, without executing tools."""

    ledger = getattr(compilation, "candidate_site_ledger", None)
    if not isinstance(ledger, CandidateSiteLedger):
        raise FormalOrchestrationError(
            "compilation has no selection-owned CandidateSiteLedger"
        )
    selected = getattr(compilation, "selected_ir_identity", None)
    if not isinstance(selected, str) or not selected:
        raise FormalOrchestrationError("compilation has no selected-IR identity")
    policy = _compilation_formal_policy(compilation)
    site_evidence = _m39_site_evidence(compilation)
    evidence = tuple(item for _, item in site_evidence)
    attempts: list[M39AttemptReference] = []
    for site, record in site_evidence:
        assert record.candidate_identity is not None
        matches = tuple(
            candidate
            for candidate in site.candidates
            if candidate.candidate_identity == record.candidate_identity
        )
        assert len(matches) == 1
        attempts.append(M39AttemptReference.from_evidence(
            site_identity=site.identity,
            rank=matches[0].rank,
            evidence=record,
        ))
    return (
        CompilerFormalExecutionPlan(
            selected,
            verification_plan,
            ledger,
            policy,
            tuple(attempts),
        ),
        evidence,
    )


def validate_legacy_formal_view(compilation: object, design: object) -> None:
    """Reject legacy combined output when it would hide goals or change routes."""

    connected = getattr(design, "connected_artifact_hash", None) is not None
    unavailable = (
        tuple(
            item for item in getattr(design, "properties", ())
            if getattr(item, "predicate", None) is None
            or getattr(item, "non_executable_reason", None) is not None
        )
        if connected else ()
    )
    covers = tuple(getattr(design, "covers", ()))
    recursive = getattr(compilation, "recursive_formal_design", None)
    root_name = getattr(getattr(compilation, "ir", None), "name", None)
    descendants = tuple(
        item for item in getattr(recursive, "properties", ())
        if tuple(getattr(item, "physical_instance_path", ())) != (root_name,)
    )
    if not unavailable and not covers and not descendants:
        return
    if unavailable:
        detail = f"goal '{unavailable[0].id}' is not executable on the selected route"
    elif covers:
        detail = f"cover '{covers[0].id}' requires an independent cover job"
    else:
        detail = (
            f"descendant goal '{descendants[0].concrete_property_id}' requires "
            "per-instance routing"
        )
    raise FormalOrchestrationError(
        "legacy combined formal output would be incomplete: "
        f"{detail}. Use --verification-bundle for per-goal backend routing"
    )


__all__ = [
    "COMPILER_FORMAL_EXECUTION_PLAN_SCHEMA",
    "CandidateEquivalenceExecutionReport",
    "CandidateEquivalencePlanReference",
    "CompilerFormalExecutionPlan",
    "FormalOrchestrationError",
    "M39AttemptReference",
    "build_compiler_formal_execution_plan",
    "collect_m39_evidence",
    "validate_legacy_formal_view",
]
