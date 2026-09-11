from __future__ import annotations

import json

import pytest

from zlang.candidate_sites import (
    CandidateRankRecord,
    CandidateRewriteKind,
    CandidateSiteKind,
    CandidateSiteLedger,
    CandidateSiteRecord,
)
from zlang.compiler_verification_report import (
    CompilerVerificationReport,
    CompilerVerificationReportError,
)
from zlang.formal_exploration import FormalPolicy
from zlang.formal_orchestration import (
    CandidateEquivalenceExecutionReport,
    CandidateEquivalencePlanReference,
    CompilerFormalExecutionPlan,
)
from zlang.ir import ComparisonWindow
from zlang.ir.equivalence import (
    EquivalenceCounterexample,
    EquivalenceMode,
    EquivalenceRelation,
    EquivalenceResult,
    EquivalenceStatus,
)
from zlang.ir.formal_planning import (
    FormalBackendArtifactRef,
    FormalExecutableRoute,
    FormalExecutionPlan,
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalRouteKind,
    FormalSkipCode,
    FormalSkipReason,
)
from zlang.verification_bundle import (
    VerificationJobResult,
    VerificationRunConfig,
    VerificationRunReport,
)


MODULE = "selected:" + "1" * 64
CANDIDATE = "selected:" + "2" * 64
REFERENCE = "3" * 64
DIRECT = "5" * 64
PROPERTY = "m36.report"


def _report(*, failed: bool = False) -> CompilerVerificationReport:
    rank = CandidateRankRecord(CANDIDATE, "semantic:candidate", 1, (0,))
    site = CandidateSiteRecord(
        CandidateSiteKind.SOURCE_EXPLORE, "module:report", "y",
        "semantic:source", CANDIDATE, (rank,),
        CandidateRewriteKind.OUTPUT_ASSIGNMENT,
    )
    artifact = FormalBackendArtifactRef(
        "direct_systemverilog", DIRECT, "bindings:" + "7" * 64
    )
    goal = FormalGoalPlan(
        "goal:m36:direct", PROPERTY, FormalPlanGoalKind.M36_EQUIVALENCE,
        None, None, (), ("port:a", "port:y"), CANDIDATE,
        ComparisonWindow.same_cycle(), 1,
        route=FormalExecutableRoute(
            FormalRouteKind.SEMANTIC_EQUIVALENCE, (artifact,),
            reference_identity=REFERENCE,
        ),
    )
    plan_ref = CandidateEquivalencePlanReference(
        site.identity, CANDIDATE, goal, "port:y"
    )
    status = EquivalenceStatus.FAILED if failed else EquivalenceStatus.BOUNDED_PASS
    result = EquivalenceResult(
        PROPERTY, status, EquivalenceMode.BMC, "sby", "z3", 8,
        EquivalenceRelation.SAME_CYCLE_VALUE, 0, "direct_systemverilog",
        REFERENCE, DIRECT, 10, CANDIDATE,
        counterexample=(
            EquivalenceCounterexample(PROPERTY, 1, values=(("port:y", "1'b1"),))
            if failed else None
        ),
        reason="counterexample found" if failed else None,
    )
    candidate = CandidateEquivalenceExecutionReport(plan_ref, result)
    safety = FormalGoalPlan(
        "goal:m35:safety", "m35.safety", FormalPlanGoalKind.SAFETY,
        "clk", "rst", (), ("port:y",), MODULE,
        ComparisonWindow.same_cycle(), 1,
        skip_reason=FormalSkipReason(FormalSkipCode.BACKEND_UNAVAILABLE, "fixture"),
    )
    verification_plan = FormalExecutionPlan(
        MODULE, "verification:" + "9" * 64, (safety,)
    )
    compiler_plan = CompilerFormalExecutionPlan(
        MODULE, verification_plan, CandidateSiteLedger((site,)),
        FormalPolicy.AVAILABLE, candidate_equivalence_plans=(plan_ref,),
    )
    versions = (("sby", "1"), ("z3", "1"))
    verification = VerificationRunReport(
        "verification-bundle:" + "a" * 64, "ReportTop",
        VerificationRunConfig(depth=8),
        (VerificationJobResult(
            "m35.safety", "safety", "bounded_pass", "bmc", "sby", "z3", 8,
            tool_versions=versions,
        ),),
        versions,
    )
    return CompilerVerificationReport(verification, compiler_plan, (candidate,))


def test_direct_only_combined_report_round_trips_and_renders() -> None:
    report = _report()
    restored = CompilerVerificationReport.from_json(report.to_json())
    assert restored == report
    assert restored.outcome == "passed"
    assert restored.exit_code == 0
    text = restored.to_text()
    assert "backend=direct_systemverilog" in text
    assert "formal applicability executable=0 skipped=1 total=1" in text


def test_direct_candidate_failure_controls_exit_status() -> None:
    report = _report(failed=True)
    assert report.outcome == "failed"
    assert report.exit_code == 1


def test_combined_report_rejects_corrupted_candidate_link() -> None:
    report = _report()
    malformed = json.loads(report.to_json())
    malformed["candidate_equivalence"][0]["plan"]["candidate_identity"] = (
        "selected:" + "f" * 64
    )
    with pytest.raises(CompilerVerificationReportError):
        CompilerVerificationReport.from_data(malformed)
