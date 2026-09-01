from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

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
    FormalOrchestrationError,
)
from zlang.compiler import compile_source
from zlang.ir import (
    ComparisonWindow,
    CrossBackendCounterexample,
    CrossBackendMode,
    CrossBackendProperty,
    CrossBackendRelation,
    CrossBackendResult,
    CrossBackendStatus,
    EquivalenceMode,
    EquivalenceRelation,
    EquivalenceResult,
    EquivalenceStatus,
    FormalBackendArtifactRef,
    FormalExecutableRoute,
    FormalExecutionPlan,
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalRouteKind,
    FormalSkipCode,
    FormalSkipReason,
)
from zlang.triangular_evidence import M36EvidenceLeg, M38EvidenceReport
from zlang.verification_bundle import (
    VerificationJobResult,
    VerificationRunConfig,
    VerificationRunReport,
    load_verification_bundle,
)
from zlang.verification_publication import publish_compilation_verification_bundle


MODULE = "selected:" + "1" * 64
CANDIDATE = "selected:" + "2" * 64
REFERENCE = "3" * 64
CLASH = "4" * 64
DIRECT = "5" * 64
PROPERTY = "m36.report"
DEPTH = 8


def _artifact(backend: str, identity: str) -> FormalBackendArtifactRef:
    return FormalBackendArtifactRef(
        backend,
        identity,
        "bindings:" + ("6" if backend == "clash" else "7") * 64,
    )


def _m36_plan(backend: str, artifact: str) -> FormalGoalPlan:
    return FormalGoalPlan(
        f"goal:m36:{backend}",
        PROPERTY,
        FormalPlanGoalKind.M36_EQUIVALENCE,
        None,
        None,
        (),
        ("port:a", "port:y"),
        CANDIDATE,
        ComparisonWindow.same_cycle(),
        1,
        route=FormalExecutableRoute(
            FormalRouteKind.SEMANTIC_EQUIVALENCE,
            (_artifact(backend, artifact),),
            reference_identity=REFERENCE,
        ),
    )


def _m38_plan() -> FormalGoalPlan:
    return FormalGoalPlan(
        "goal:m38:backends",
        "m38.report",
        FormalPlanGoalKind.M38_EQUIVALENCE,
        None,
        None,
        (),
        ("port:y",),
        CANDIDATE,
        ComparisonWindow.same_cycle(),
        1,
        route=FormalExecutableRoute(
            FormalRouteKind.CROSS_BACKEND_EQUIVALENCE,
            (_artifact("clash", CLASH), _artifact("direct_systemverilog", DIRECT)),
        ),
    )


def _m36_result(backend: str, artifact: str) -> EquivalenceResult:
    return EquivalenceResult(
        PROPERTY,
        EquivalenceStatus.BOUNDED_PASS,
        EquivalenceMode.BMC,
        "sby",
        "z3",
        DEPTH,
        EquivalenceRelation.SAME_CYCLE_VALUE,
        0,
        backend,
        REFERENCE,
        artifact,
        10,
        CANDIDATE,
    )


def _candidate_report(
    site_identity: str, *, failed: bool = False
) -> CandidateEquivalenceExecutionReport:
    clash_plan = _m36_plan("clash", CLASH)
    direct_plan = _m36_plan("direct_systemverilog", DIRECT)
    m38_plan = _m38_plan()
    counterexample = None
    status = CrossBackendStatus.FAILED if failed else CrossBackendStatus.BOUNDED_PASS
    if failed:
        counterexample = CrossBackendCounterexample(
            "m38.report",
            "port:y",
            3,
            3,
            "clash",
            "direct_systemverilog",
            CLASH,
            DIRECT,
            (("left:port:y", "1"), ("right:port:y", "0")),
            "trace",
        )
    m38_result = CrossBackendResult(
        "m38.report",
        status,
        CrossBackendMode.BMC,
        "sby",
        "z3",
        DEPTH,
        CrossBackendRelation.SAME_CYCLE_VALUE,
        0,
        CANDIDATE,
        "clash",
        "direct_systemverilog",
        CLASH,
        DIRECT,
        10,
        observable_signal_id="port:y",
        counterexample=counterexample,
    )
    plan = CandidateEquivalencePlanReference(
        site_identity,
        CANDIDATE,
        clash_plan,
        direct_plan,
        m38_plan,
        "port:y",
    )
    triangle = M38EvidenceReport.from_results(
        m38_plan,
        m38_result,
        M36EvidenceLeg.from_result(
            clash_plan, _m36_result("clash", CLASH)
        ),
        M36EvidenceLeg.from_result(
            direct_plan, _m36_result("direct_systemverilog", DIRECT)
        ),
    )
    return CandidateEquivalenceExecutionReport(plan, triangle)


def _candidate_site() -> CandidateSiteRecord:
    rank = CandidateRankRecord(CANDIDATE, "semantic:candidate", 1, (0,))
    return CandidateSiteRecord(
        CandidateSiteKind.SOURCE_EXPLORE,
        "module:report",
        "y",
        "semantic:source",
        CANDIDATE,
        (rank,),
        CandidateRewriteKind.OUTPUT_ASSIGNMENT,
    )


def _compiler_plan(
    report: CandidateEquivalenceExecutionReport,
    site: CandidateSiteRecord,
) -> CompilerFormalExecutionPlan:
    assert report.plan.site_identity == site.identity
    verification_goal = FormalGoalPlan(
        "goal:m35:safety",
        "m35.safety",
        FormalPlanGoalKind.SAFETY,
        "clk",
        "rst",
        (),
        ("port:y",),
        MODULE,
        ComparisonWindow.same_cycle(),
        1,
        skip_reason=FormalSkipReason(
            FormalSkipCode.BACKEND_UNAVAILABLE,
            "fixture",
        ),
    )
    verification = FormalExecutionPlan(
        MODULE,
        "verification:" + "9" * 64,
        (verification_goal,),
    )
    return CompilerFormalExecutionPlan(
        MODULE,
        verification,
        CandidateSiteLedger((site,)),
        FormalPolicy.AVAILABLE,
        candidate_equivalence_plans=(report.plan,),
    )


def _verification_report() -> VerificationRunReport:
    versions = (("sby", "1"), ("z3", "1"))
    result = VerificationJobResult(
        "m35.safety",
        "safety",
        "bounded_pass",
        "bmc",
        "sby",
        "z3",
        DEPTH,
        tool_versions=versions,
    )
    return VerificationRunReport(
        "verification-bundle:" + "a" * 64,
        "ReportTop",
        VerificationRunConfig(depth=DEPTH),
        (result,),
        versions,
    )


def _combined(*, failed: bool = False) -> CompilerVerificationReport:
    site = _candidate_site()
    candidate = _candidate_report(site.identity, failed=failed)
    plan = _compiler_plan(candidate, site)
    return CompilerVerificationReport(_verification_report(), plan, (candidate,))


def test_combined_report_round_trips_and_renders_candidate_evidence() -> None:
    report = _combined()

    restored = CompilerVerificationReport.from_json(report.to_json())
    assert restored == report
    assert restored.outcome == "passed"
    assert restored.exit_code == 0
    assert "candidate equivalence site=" in restored.to_text()
    assert "formal applicability executable=0 skipped=1 total=1" in restored.to_text()
    assert "skipped reason=backend_unavailable count=1" in restored.to_text()
    assert "m36.selected_architecture_equivalence" in restored.to_text()
    assert "m38.cross_backend_equivalence" in restored.to_text()


def test_combined_report_identity_excludes_relocatable_work_roots() -> None:
    report = _combined()
    left_candidate = replace(
        report.candidate_equivalence[0],
        work_directories=(("m36:clash:bmc", "/build/left/m36"),),
    )
    right_candidate = replace(
        report.candidate_equivalence[0],
        work_directories=(("m36:clash:bmc", "/other/root/m36"),),
    )
    left = CompilerVerificationReport(
        report.verification,
        report.formal_execution_plan,
        (left_candidate,),
    )
    right = CompilerVerificationReport(
        report.verification,
        report.formal_execution_plan,
        (right_candidate,),
    )

    assert left.report_identity == right.report_identity
    assert left.to_json() != right.to_json()
    assert "/build/left/m36" in left.to_json()
    assert "/other/root/m36" in right.to_json()
    assert CompilerVerificationReport.from_json(left.to_json()) == left
    assert CompilerVerificationReport.from_json(right.to_json()) == right


def test_executed_m38_failure_controls_combined_exit_status() -> None:
    report = _combined(failed=True)

    assert report.outcome == "failed"
    assert report.exit_code == 1
    restored = CompilerVerificationReport.from_json(report.to_json())
    assert restored.exit_code == 1


def test_combined_report_rejects_missing_or_corrupted_candidate_links() -> None:
    report = _combined()
    with pytest.raises(
        CompilerVerificationReportError,
        match="differ from the compiler plan",
    ):
        CompilerVerificationReport(
            report.verification,
            report.formal_execution_plan,
            (),
        )

    malformed = json.loads(report.to_json())
    malformed["candidate_equivalence"][0]["plan"]["candidate_identity"] = (
        "selected:" + "f" * 64
    )
    with pytest.raises(CompilerVerificationReportError):
        CompilerVerificationReport.from_data(malformed)


def test_immutable_bundle_rejects_a_candidate_plan_without_prepared_inputs(
    tmp_path: Path,
) -> None:
    source = """
module CandidateBundlePlan {
    clock clk reset rst
    in a : u8
    out y : u8
    y = explore { a ^ 0 minimize lut }
    assert follows @ clk { y == a }
}
"""

    class Verifier:
        formal_route = "M36_clash"

        @staticmethod
        def cache_identity(candidate, _config=None):
            return {
                "property_identity": "m36.bundle." + candidate.implementation_identity,
                "reference_artifact_hash": "a" * 64,
                "implementation_artifact_hash": "b" * 64,
                "artifact_hash": "b" * 64,
                "harness_hash": "c" * 64,
                "backend_identity": "d" * 64,
                "assumptions_identity": "e" * 64,
            }

        def __call__(self, candidate, config):
            from zlang.ir.formal import FormalStatus, ProofMode

            return {
                "status": FormalStatus.BOUNDED_PASS,
                "mode": ProofMode.BMC,
                "depth": config.bmc_depth,
                "backend": "clash",
                "engine": "sby",
                "solver": "z3",
                **self.cache_identity(candidate),
            }

    compilation = compile_source(
        source,
        include_clash=False,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=Verifier(),
    )
    publish_compilation_verification_bundle(compilation, tmp_path / "base")
    base_payload = load_verification_bundle(tmp_path / "base").verification_ir[
        "payload"
    ]
    assert isinstance(base_payload, dict)
    base = CompilerFormalExecutionPlan.from_data(
        base_payload["compiler_execution_plan"]
    )
    site = base.candidate_site_ledger.sites[0]
    skipped = FormalSkipReason(
        FormalSkipCode.BACKEND_UNAVAILABLE,
        "prepared route fixture",
    )

    def goal(kind: FormalPlanGoalKind, name: str) -> FormalGoalPlan:
        observations = (
            ("port:a", "port:y")
            if kind is FormalPlanGoalKind.M36_EQUIVALENCE
            else ("port:y",)
        )
        return FormalGoalPlan(
            f"goal:{name}",
            "m36.bundle.property" if kind is FormalPlanGoalKind.M36_EQUIVALENCE
            else "m38.bundle.property",
            kind,
            None,
            None,
            (),
            observations,
            site.selected_candidate_identity,
            ComparisonWindow.same_cycle(),
            1,
            skip_reason=skipped,
        )

    candidate = CandidateEquivalencePlanReference(
        site.identity,
        site.selected_candidate_identity,
        goal(FormalPlanGoalKind.M36_EQUIVALENCE, "clash"),
        goal(FormalPlanGoalKind.M36_EQUIVALENCE, "direct"),
        goal(FormalPlanGoalKind.M38_EQUIVALENCE, "m38"),
        "port:y",
    )
    enriched = replace(base, candidate_equivalence_plans=(candidate,))

    with pytest.raises(
        FormalOrchestrationError,
        match="candidate replay inputs differ",
    ):
        publish_compilation_verification_bundle(
            compilation,
            tmp_path / "enriched",
            compiler_execution_plan=enriched,
        )
