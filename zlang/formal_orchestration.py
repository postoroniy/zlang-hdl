"""Compiler-owned view over the existing M35--M39 planning products.

This module deliberately does not merge the independently versioned M35, M36,
M38, or M39 result types.  It records how one compilation relates the
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
from zlang.evidence_report import evidence_from_formal_exploration_record
from zlang.formal_exploration import FormalExplorationRecord, FormalPolicy
from zlang.ir.formal_planning import FormalExecutionPlan, FormalPlanningError
from zlang.ir.formal_planning import FormalGoalPlan, FormalPlanGoalKind
from zlang.triangular_evidence import M38EvidenceReport


COMPILER_FORMAL_EXECUTION_PLAN_SCHEMA = 1


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
    """Typed M36/M38 plans for one exact selected candidate site.

    Candidate goals deliberately do not live in the module-scoped M35
    :class:`FormalExecutionPlan`: their selected-IR identity is the exact M28
    candidate identity, not the whole selected module identity.  The compiler
    wrapper cross-links both plan families through the selection-owned ledger.
    """

    site_identity: str
    candidate_identity: str
    clash_m36: FormalGoalPlan
    direct_systemverilog_m36: FormalGoalPlan
    m38: FormalGoalPlan
    implementation_observable_identity: str

    def __post_init__(self) -> None:
        _string(self.site_identity, "candidate equivalence site identity")
        _string(self.candidate_identity, "candidate equivalence identity")
        _string(
            self.implementation_observable_identity,
            "candidate implementation observable identity",
        )
        for label, plan in (
            ("Clash M36", self.clash_m36),
            ("direct-SystemVerilog M36", self.direct_systemverilog_m36),
        ):
            if not isinstance(plan, FormalGoalPlan):
                raise FormalOrchestrationError(f"{label} plan must be typed")
            if plan.kind is not FormalPlanGoalKind.M36_EQUIVALENCE:
                raise FormalOrchestrationError(f"{label} plan has the wrong kind")
            if plan.selected_ir_identity != self.candidate_identity:
                raise FormalOrchestrationError(
                    f"{label} plan references a different candidate"
                )
            if self.implementation_observable_identity not in (
                plan.required_observations
            ):
                raise FormalOrchestrationError(
                    f"{label} plan does not publish the candidate implementation "
                    "observable"
                )
        if not isinstance(self.m38, FormalGoalPlan):
            raise FormalOrchestrationError("M38 candidate plan must be typed")
        if self.m38.kind is not FormalPlanGoalKind.M38_EQUIVALENCE:
            raise FormalOrchestrationError("M38 candidate plan has the wrong kind")
        if self.m38.selected_ir_identity != self.candidate_identity:
            raise FormalOrchestrationError(
                "M38 candidate plan references a different candidate"
            )
        if self.m38.required_observations != (
            self.implementation_observable_identity,
        ):
            raise FormalOrchestrationError(
                "candidate M38 observations differ from the M36 implementation "
                "observable"
            )
        if self.clash_m36.property_identity != self.direct_systemverilog_m36.property_identity:
            raise FormalOrchestrationError(
                "candidate M36 plans use different semantic-reference properties"
            )
        if not (
            self.clash_m36.comparison_window
            == self.direct_systemverilog_m36.comparison_window
            == self.m38.comparison_window
        ):
            raise FormalOrchestrationError(
                "candidate M36/M38 plans use different comparison windows"
            )

    @property
    def plan_identity(self) -> str:
        return "candidate-equivalence-plan:" + stable_digest(self.identity_data())

    def identity_data(self) -> dict[str, object]:
        return {
            "site_identity": self.site_identity,
            "candidate_identity": self.candidate_identity,
            "clash_m36": self.clash_m36.plan_identity,
            "direct_systemverilog_m36": self.direct_systemverilog_m36.plan_identity,
            "m38": self.m38.plan_identity,
            "implementation_observable_identity": (
                self.implementation_observable_identity
            ),
        }

    def to_data(self) -> dict[str, object]:
        return {
            "site_identity": self.site_identity,
            "candidate_identity": self.candidate_identity,
            "clash_m36": self.clash_m36.to_data(),
            "direct_systemverilog_m36": self.direct_systemverilog_m36.to_data(),
            "m38": self.m38.to_data(),
            "implementation_observable_identity": (
                self.implementation_observable_identity
            ),
            "plan_identity": self.plan_identity,
        }

    @classmethod
    def from_data(cls, value: object) -> "CandidateEquivalencePlanReference":
        data = _mapping(value, "candidate equivalence plan")
        _exact_keys(
            data,
            {
                "site_identity", "candidate_identity", "clash_m36",
                "direct_systemverilog_m36", "m38", "plan_identity",
                "implementation_observable_identity",
            },
            "candidate equivalence plan",
        )
        try:
            restored = cls(
                _string(data["site_identity"], "candidate equivalence site"),
                _string(data["candidate_identity"], "candidate identity"),
                FormalGoalPlan.from_data(data["clash_m36"]),
                FormalGoalPlan.from_data(data["direct_systemverilog_m36"]),
                FormalGoalPlan.from_data(data["m38"]),
                _string(
                    data["implementation_observable_identity"],
                    "candidate implementation observable identity",
                ),
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
    """Typed result cross-linked to one candidate equivalence plan."""

    plan: CandidateEquivalencePlanReference
    triangle: M38EvidenceReport
    bounded_prerequisite: M38EvidenceReport | None = None
    # Operational execution metadata is deliberately outside every semantic,
    # property, evidence, and proof-cache identity.
    tool_versions: tuple[tuple[str, str], ...] = ()
    work_directories: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.plan, CandidateEquivalencePlanReference):
            raise FormalOrchestrationError(
                "candidate equivalence execution requires a typed plan"
            )
        if not isinstance(self.triangle, M38EvidenceReport):
            raise FormalOrchestrationError(
                "candidate equivalence execution requires typed triangle evidence"
            )
        if not isinstance(self.tool_versions, tuple):
            raise FormalOrchestrationError(
                "candidate tool versions must be a tuple"
            )
        raw_versions = self.tool_versions
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or not isinstance(item[1], str)
            or not item[1]
            for item in raw_versions
        ):
            raise FormalOrchestrationError(
                "candidate tool versions require non-empty name/value pairs"
            )
        versions = tuple(sorted(raw_versions))
        if len({name for name, _ in versions}) != len(versions):
            raise FormalOrchestrationError(
                "candidate tool versions contain duplicate tool names"
            )
        object.__setattr__(self, "tool_versions", versions)
        if not isinstance(self.work_directories, tuple):
            raise FormalOrchestrationError(
                "candidate work directories must be a tuple"
            )
        raw_directories = self.work_directories
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or not isinstance(item[1], str)
            or not item[1]
            for item in raw_directories
        ):
            raise FormalOrchestrationError(
                "candidate work directories require non-empty route/path pairs"
            )
        directories = tuple(sorted(raw_directories))
        if len({route for route, _ in directories}) != len(directories):
            raise FormalOrchestrationError(
                "candidate work directories contain duplicate route labels"
            )
        object.__setattr__(self, "work_directories", directories)
        if self.triangle.m38_plan != self.plan.m38:
            raise FormalOrchestrationError(
                "candidate equivalence M38 evidence differs from its plan"
            )
        left = self.triangle.left_m36
        right = self.triangle.right_m36
        if left is None or right is None:
            raise FormalOrchestrationError(
                "candidate equivalence execution must retain both typed M36 legs"
            )
        if left.plan != self.plan.clash_m36 or right.plan != self.plan.direct_systemverilog_m36:
            raise FormalOrchestrationError(
                "candidate equivalence M36 evidence differs from its plan"
            )
        prerequisite = self.bounded_prerequisite
        if prerequisite is not None:
            if not isinstance(prerequisite, M38EvidenceReport):
                raise FormalOrchestrationError(
                    "candidate bounded prerequisite must use typed triangle evidence"
                )
            if prerequisite.m38_plan != self.plan.m38:
                raise FormalOrchestrationError(
                    "candidate bounded prerequisite differs from its M38 plan"
                )
            if prerequisite.left_m36 is None or prerequisite.right_m36 is None:
                raise FormalOrchestrationError(
                    "candidate bounded prerequisite must retain both M36 legs"
                )
            if (
                prerequisite.left_m36.plan != self.plan.clash_m36
                or prerequisite.right_m36.plan
                != self.plan.direct_systemverilog_m36
            ):
                raise FormalOrchestrationError(
                    "candidate bounded prerequisite differs from its M36 plans"
                )
            bounded = tuple(
                item
                for item in (
                    prerequisite.left_m36.evidence,
                    prerequisite.right_m36.evidence,
                    prerequisite.m38_evidence,
                )
                if item is not None
            )
            final = tuple(
                item
                for item in (
                    left.evidence,
                    right.evidence,
                    self.triangle.m38_evidence,
                )
                if item is not None
            )
            if any(item.mode != "bmc" for item in bounded):
                raise FormalOrchestrationError(
                    "candidate bounded prerequisite contains non-BMC evidence"
                )
            if any(item.mode != "prove" for item in final):
                raise FormalOrchestrationError(
                    "candidate final triangle contains non-PROVE evidence"
                )

    @property
    def verification_failure(self) -> bool:
        return self.triangle.verification_failure or (
            self.bounded_prerequisite is not None
            and self.bounded_prerequisite.verification_failure
        )

    @property
    def evidence_records(self) -> tuple[EvidenceRecord, ...]:
        prerequisite_values = () if self.bounded_prerequisite is None else (
            self.bounded_prerequisite.left_m36.evidence,
            self.bounded_prerequisite.right_m36.evidence,
            self.bounded_prerequisite.m38_evidence,
        )
        values = (*prerequisite_values,
            self.triangle.left_m36.evidence,
            self.triangle.right_m36.evidence,
            self.triangle.m38_evidence,
        )
        unique: dict[str, EvidenceRecord] = {}
        for item in values:
            if item is not None:
                unique.setdefault(item.evidence_id, item)
        return tuple(unique.values())

    def to_data(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_data(),
            "triangle": self.triangle.to_data(),
            "bounded_prerequisite": (
                None
                if self.bounded_prerequisite is None
                else self.bounded_prerequisite.to_data()
            ),
            "tool_versions": [list(item) for item in self.tool_versions],
            "work_directories": [list(item) for item in self.work_directories],
        }

    @classmethod
    def from_data(cls, value: object) -> "CandidateEquivalenceExecutionReport":
        data = _mapping(value, "candidate equivalence execution report")
        _exact_keys(
            data,
            {
                "plan", "triangle", "bounded_prerequisite", "tool_versions",
                "work_directories",
            },
            "candidate equivalence execution report",
        )
        tool_versions = data["tool_versions"]
        work_directories = data["work_directories"]
        if not isinstance(tool_versions, list) or not isinstance(
            work_directories, list
        ):
            raise FormalOrchestrationError(
                "candidate execution metadata must use JSON arrays"
            )

        def pairs(values: list[object], label: str) -> tuple[tuple[str, str], ...]:
            restored: list[tuple[str, str]] = []
            for item in values:
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or not all(isinstance(value, str) for value in item)
                ):
                    raise FormalOrchestrationError(
                        f"candidate {label} entries must be string pairs"
                    )
                restored.append((item[0], item[1]))
            return tuple(restored)
        try:
            return cls(
                CandidateEquivalencePlanReference.from_data(data["plan"]),
                M38EvidenceReport.from_data(data["triangle"]),
                (
                    None
                    if data["bounded_prerequisite"] is None
                    else M38EvidenceReport.from_data(
                        data["bounded_prerequisite"]
                    )
                ),
                pairs(tool_versions, "tool version"),
                pairs(work_directories, "work directory"),
            )
        except ValueError as error:
            raise FormalOrchestrationError(str(error)) from error

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
