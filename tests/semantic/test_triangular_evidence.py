from __future__ import annotations

from dataclasses import replace
import json

import pytest

from zlang.ir import (
    ComparisonWindow,
    CrossBackendCounterexample,
    CrossBackendMode,
    CrossBackendRelation,
    CrossBackendResult,
    CrossBackendStatus,
    EquivalenceCounterexample,
    EquivalenceMode,
    EquivalenceRelation,
    EquivalenceResult,
    EquivalenceStatus,
    FormalBackendArtifactRef,
    FormalExecutableRoute,
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalRouteKind,
    FormalSkipCode,
    FormalSkipReason,
)
from zlang.source import SourceOrigin, SourceSpan
from zlang.triangular_evidence import (
    M36EvidenceLeg,
    M38EvidenceClassification,
    M38EvidenceReason,
    M38EvidenceReport,
    TriangularEvidenceError,
)


REFERENCE = "a" * 64
CLASH_ARTIFACT = "b" * 64
SV_ARTIFACT = "c" * 64
PROPERTY = "m36.eq.selected"
SELECTED = "selected:example"
DEPTH = 8


def _artifact(backend: str, identity: str) -> FormalBackendArtifactRef:
    return FormalBackendArtifactRef(backend, identity, f"bindings:{backend}")


def _m36_plan(backend: str, artifact: str) -> FormalGoalPlan:
    return FormalGoalPlan(
        f"goal:m36:{backend}",
        PROPERTY,
        FormalPlanGoalKind.M36_EQUIVALENCE,
        "clk",
        "rst",
        (),
        ("port:a", "port:y"),
        SELECTED,
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
        "m38.eq.backends",
        FormalPlanGoalKind.M38_EQUIVALENCE,
        "clk",
        "rst",
        (),
        ("port:a", "port:y"),
        SELECTED,
        ComparisonWindow.same_cycle(),
        1,
        route=FormalExecutableRoute(
            FormalRouteKind.CROSS_BACKEND_EQUIVALENCE,
            (
                _artifact("clash", CLASH_ARTIFACT),
                _artifact("direct_systemverilog", SV_ARTIFACT),
            ),
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
        2,
        SELECTED,
    )


def _m38_result(
    status: CrossBackendStatus = CrossBackendStatus.BOUNDED_PASS,
) -> CrossBackendResult:
    counterexample = None
    if status is CrossBackendStatus.FAILED:
        counterexample = CrossBackendCounterexample(
            "m38.eq.backends",
            "port:y",
            3,
            3,
            "clash",
            "direct_systemverilog",
            CLASH_ARTIFACT,
            SV_ARTIFACT,
            values=(("clash.y", "1"), ("direct.y", "0")),
            raw_trace="trace",
        )
    return CrossBackendResult(
        "m38.eq.backends",
        status,
        CrossBackendMode.BMC,
        "sby",
        "z3",
        DEPTH,
        CrossBackendRelation.SAME_CYCLE_VALUE,
        0,
        SELECTED,
        "clash",
        "direct_systemverilog",
        CLASH_ARTIFACT,
        SV_ARTIFACT,
        10,
        observable_signal_id="port:y",
        counterexample=counterexample,
    )


def _left() -> M36EvidenceLeg:
    return M36EvidenceLeg.from_result(
        _m36_plan("clash", CLASH_ARTIFACT),
        _m36_result("clash", CLASH_ARTIFACT),
    )


def _right() -> M36EvidenceLeg:
    return M36EvidenceLeg.from_result(
        _m36_plan("direct_systemverilog", SV_ARTIFACT),
        _m36_result("direct_systemverilog", SV_ARTIFACT),
    )


def _triangle(
    status: CrossBackendStatus = CrossBackendStatus.BOUNDED_PASS,
) -> M38EvidenceReport:
    return M38EvidenceReport.from_results(
        _m38_plan(), _m38_result(status), _left(), _right()
    )


def test_complete_compatible_evidence_is_triangular_and_round_trips() -> None:
    report = _triangle()

    assert report.classification is M38EvidenceClassification.TRIANGULAR
    assert report.reason is M38EvidenceReason.COMPLETE_TRIANGLE
    assert not report.verification_failure
    assert not report.affects_m39_eligibility
    assert report.evidence_identity.startswith("m38-evidence:")
    assert M38EvidenceReport.from_data(report.to_data()) == report
    assert M38EvidenceReport.from_json(report.to_json()) == report


def test_fixed_latency_triangle_compares_backend_delta_between_m36_legs() -> None:
    window = ComparisonWindow.reset_fill(3)
    left_plan = replace(
        _m36_plan("clash", CLASH_ARTIFACT),
        comparison_window=window,
        minimum_bmc_depth=window.minimum_bmc_depth,
    )
    right_plan = replace(
        _m36_plan("direct_systemverilog", SV_ARTIFACT),
        comparison_window=window,
        minimum_bmc_depth=window.minimum_bmc_depth,
    )
    m38_plan = replace(
        _m38_plan(),
        comparison_window=window,
        minimum_bmc_depth=window.minimum_bmc_depth,
    )
    left_result = replace(
        _m36_result("clash", CLASH_ARTIFACT),
        relation_kind=EquivalenceRelation.FIXED_LATENCY_VALUE,
        latency_delta=3,
    )
    right_result = replace(
        _m36_result("direct_systemverilog", SV_ARTIFACT),
        relation_kind=EquivalenceRelation.FIXED_LATENCY_VALUE,
        latency_delta=3,
    )
    m38_result = replace(
        _m38_result(),
        relation=CrossBackendRelation.FIXED_LATENCY_VALUE,
        latency_delta=0,
    )
    report = M38EvidenceReport.from_results(
        m38_plan,
        m38_result,
        M36EvidenceLeg.from_result(left_plan, left_result),
        M36EvidenceLeg.from_result(right_plan, right_result),
    )
    assert report.classification is M38EvidenceClassification.TRIANGULAR

    with pytest.raises(TriangularEvidenceError, match="latency relations"):
        M38EvidenceReport.from_results(
            m38_plan,
            replace(m38_result, latency_delta=1),
            M36EvidenceLeg.from_result(left_plan, left_result),
            M36EvidenceLeg.from_result(right_plan, right_result),
        )


def test_raw_m38_is_advisory_and_never_claims_a_triangle() -> None:
    raw = M38EvidenceReport.from_results(_m38_plan(), _m38_result())
    left_only = M38EvidenceReport.from_results(
        _m38_plan(), _m38_result(), _left()
    )

    for report in (raw, left_only):
        assert report.classification is M38EvidenceClassification.RAW_ADVISORY
        assert report.reason is M38EvidenceReason.M36_LEGS_MISSING
        assert not report.verification_failure
        assert not report.affects_m39_eligibility


@pytest.mark.parametrize(
    ("side", "mismatch", "message"),
    (
        ("left", "selected", "left M36/M38 selected-IR identities"),
        ("left", "artifact", "left M36 artifact"),
        ("right", "window", "right M36 comparison window"),
        ("right", "artifact", "right M36 artifact"),
    ),
)
def test_partial_m36_leg_must_match_its_exact_m38_side(
    side: str,
    mismatch: str,
    message: str,
) -> None:
    backend = "clash" if side == "left" else "direct_systemverilog"
    artifact = CLASH_ARTIFACT if side == "left" else SV_ARTIFACT
    plan = _m36_plan(backend, artifact)
    result = _m36_result(backend, artifact)
    if mismatch == "selected":
        plan = replace(plan, selected_ir_identity="selected:other")
        result = replace(result, candidate_identity="selected:other")
    elif mismatch == "window":
        window = ComparisonWindow.reset_fill(1)
        plan = replace(
            plan,
            comparison_window=window,
            minimum_bmc_depth=window.minimum_bmc_depth,
        )
    else:
        wrong_artifact = "d" * 64
        plan = replace(
            plan,
            route=FormalExecutableRoute(
                FormalRouteKind.SEMANTIC_EQUIVALENCE,
                (_artifact(backend, wrong_artifact),),
                reference_identity=REFERENCE,
            ),
        )
        result = replace(result, implementation_hash=wrong_artifact)
    leg = M36EvidenceLeg.from_result(plan, result)

    with pytest.raises(TriangularEvidenceError, match=message):
        M38EvidenceReport.from_results(
            _m38_plan(),
            None,
            leg if side == "left" else None,
            leg if side == "right" else None,
        )


@pytest.mark.parametrize("side", ("left", "right"))
def test_partial_failed_m36_leg_retains_failure_attribution(side: str) -> None:
    backend = "clash" if side == "left" else "direct_systemverilog"
    artifact = CLASH_ARTIFACT if side == "left" else SV_ARTIFACT
    failed = replace(
        _m36_result(backend, artifact),
        status=EquivalenceStatus.FAILED,
        counterexample=EquivalenceCounterexample(
            PROPERTY,
            4,
            4,
            (("reference.y", "0"), (f"{backend}.y", "1")),
            "trace",
        ),
    )
    leg = M36EvidenceLeg.from_result(_m36_plan(backend, artifact), failed)
    report = M38EvidenceReport.from_results(
        _m38_plan(),
        None,
        leg if side == "left" else None,
        leg if side == "right" else None,
    )

    assert report.classification is M38EvidenceClassification.RAW_ADVISORY
    assert report.verification_failure
    assert (
        report.left_m36 if side == "left" else report.right_m36
    ).evidence.counterexample_digest is not None


def test_unavailable_advisory_routes_are_not_verification_failures() -> None:
    not_run = M38EvidenceReport(_m38_plan())
    unavailable_result = replace(
        _m38_result(),
        status=CrossBackendStatus.SKIPPED,
        reason="direct-SV formal artifact unavailable",
    )
    unavailable = M38EvidenceReport.from_results(
        _m38_plan(), unavailable_result, _left(), _right()
    )
    missing_leg_result = M38EvidenceReport.from_results(
        _m38_plan(),
        _m38_result(),
        M36EvidenceLeg.from_result(_left().plan, None),
        _right(),
    )
    unknown_leg = M38EvidenceReport.from_results(
        _m38_plan(),
        _m38_result(),
        M36EvidenceLeg.from_result(
            _left().plan,
            replace(
                _m36_result("clash", CLASH_ARTIFACT),
                status=EquivalenceStatus.UNKNOWN,
                reason="solver timeout",
            ),
        ),
        _right(),
    )
    unavailable_plan = replace(
        _m38_plan(),
        route=None,
        skip_reason=FormalSkipReason(
            FormalSkipCode.ARTIFACT_UNAVAILABLE,
            "direct-SV formal artifact is unavailable",
            backend="direct_systemverilog",
        ),
    )
    unavailable_planned_route = M38EvidenceReport(unavailable_plan)

    assert not_run.reason is M38EvidenceReason.M38_NOT_EXECUTED
    assert unavailable.reason is M38EvidenceReason.M38_UNAVAILABLE
    assert unavailable_planned_route.reason is M38EvidenceReason.M38_UNAVAILABLE
    assert missing_leg_result.reason is M38EvidenceReason.M36_LEG_NOT_EXECUTED
    assert unknown_leg.reason is M38EvidenceReason.M36_LEG_UNAVAILABLE
    assert all(
        not report.verification_failure
        for report in (
            not_run, unavailable, unavailable_planned_route,
            missing_leg_result, unknown_leg,
        )
    )


def test_executed_m38_failure_is_a_verification_failure_with_counterexample() -> None:
    report = _triangle(CrossBackendStatus.FAILED)

    assert report.classification is M38EvidenceClassification.TRIANGULAR
    assert report.verification_failure
    assert report.m38_evidence is not None
    assert report.m38_evidence.counterexample_digest is not None
    assert M38EvidenceReport.from_json(report.to_json()) == report

    corrupted = report.to_data()
    corrupted["m38_evidence"]["counterexample_digest"] = None  # type: ignore[index]
    with pytest.raises(
        TriangularEvidenceError,
        match="requires exactly one counterexample digest",
    ):
        M38EvidenceReport.from_data(corrupted)


@pytest.mark.parametrize(
    ("left", "right", "m38", "message"),
    (
        (
            _left(),
            M36EvidenceLeg.from_result(
                replace(_right().plan, property_identity="m36.other"),
                replace(
                    _m36_result("direct_systemverilog", SV_ARTIFACT),
                    property_id="m36.other",
                ),
            ),
            _m38_result(),
            "different canonical properties",
        ),
        (
            _left(),
            M36EvidenceLeg.from_result(
                replace(_right().plan, selected_ir_identity="selected:other"),
                replace(
                    _m36_result("direct_systemverilog", SV_ARTIFACT),
                    candidate_identity="selected:other",
                ),
            ),
            _m38_result(),
            "selected-IR identities",
        ),
        (
            _left(),
            M36EvidenceLeg.from_result(
                _right().plan,
                replace(
                    _m36_result("direct_systemverilog", SV_ARTIFACT),
                    depth=DEPTH + 1,
                ),
            ),
            _m38_result(),
            "proof depths",
        ),
        (
            _left(),
            M36EvidenceLeg.from_result(
                _right().plan,
                replace(
                    _m36_result("direct_systemverilog", SV_ARTIFACT),
                    mode=EquivalenceMode.PROVE,
                    status=EquivalenceStatus.PROVEN,
                ),
            ),
            _m38_result(),
            "proof modes",
        ),
    ),
)
def test_supplied_incompatible_triangle_evidence_is_rejected(
    left: M36EvidenceLeg,
    right: M36EvidenceLeg,
    m38: CrossBackendResult,
    message: str,
) -> None:
    with pytest.raises(TriangularEvidenceError, match=message):
        M38EvidenceReport.from_results(_m38_plan(), m38, left, right)


def test_ordered_backend_artifacts_must_match_all_three_legs() -> None:
    with pytest.raises(TriangularEvidenceError, match="ordered M38 route"):
        M38EvidenceReport.from_results(
            _m38_plan(), _m38_result(), _right(), _left()
        )

    wrong_plan = replace(
        _m38_plan(),
        route=FormalExecutableRoute(
            FormalRouteKind.CROSS_BACKEND_EQUIVALENCE,
            (
                _artifact("clash", "wrong"),
                _artifact("direct_systemverilog", SV_ARTIFACT),
            ),
        ),
    )
    with pytest.raises(TriangularEvidenceError, match="do not match its plan"):
        M38EvidenceReport.from_results(
            wrong_plan, _m38_result(), _left(), _right()
        )


def test_strict_report_codec_rejects_corruption() -> None:
    report = _triangle()

    extra = report.to_data()
    extra["unknown"] = True
    with pytest.raises(TriangularEvidenceError, match="unexpected unknown"):
        M38EvidenceReport.from_data(extra)

    wrong_classification = report.to_data()
    wrong_classification["classification"] = "raw_advisory"
    with pytest.raises(TriangularEvidenceError, match="classification does not match"):
        M38EvidenceReport.from_data(wrong_classification)

    wrong_failure = report.to_data()
    wrong_failure["verification_failure"] = True
    with pytest.raises(TriangularEvidenceError, match="verification_failure"):
        M38EvidenceReport.from_data(wrong_failure)

    wrong_identity = report.to_data()
    wrong_identity["evidence_identity"] = "m38-evidence:wrong"
    with pytest.raises(TriangularEvidenceError, match="identity does not match"):
        M38EvidenceReport.from_data(wrong_identity)

    with pytest.raises(TriangularEvidenceError, match="not valid JSON"):
        M38EvidenceReport.from_json("{")
    with pytest.raises(TriangularEvidenceError, match="must be an object"):
        M38EvidenceReport.from_json(json.dumps([]))


def test_failed_m36_leg_is_also_visible_as_verification_failure() -> None:
    failed = replace(
        _m36_result("clash", CLASH_ARTIFACT),
        status=EquivalenceStatus.FAILED,
        counterexample=EquivalenceCounterexample(
            PROPERTY, 4, 4, (("reference.y", "0"), ("clash.y", "1")), "trace"
        ),
    )
    report = M38EvidenceReport.from_results(
        _m38_plan(), _m38_result(),
        M36EvidenceLeg.from_result(_left().plan, failed), _right()
    )

    assert report.classification is M38EvidenceClassification.TRIANGULAR
    assert report.verification_failure


def test_evidence_identity_excludes_source_relocation() -> None:
    report = _triangle()
    origin = SourceOrigin(
        SourceSpan(4, 2, 4, 20),
        "equivalence result",
        "examples/first.zl",
        "d" * 64,
    )
    relocated = SourceOrigin(
        SourceSpan(40, 3, 40, 21),
        "equivalence result",
        "examples/relocated.zl",
        "e" * 64,
    )
    first = replace(
        report,
        m38_evidence=replace(report.m38_evidence, source_origin=origin),
        left_m36=replace(
            report.left_m36,
            evidence=replace(report.left_m36.evidence, source_origin=origin),
        ),
    )
    second = replace(
        report,
        m38_evidence=replace(report.m38_evidence, source_origin=relocated),
        left_m36=replace(
            report.left_m36,
            evidence=replace(report.left_m36.evidence, source_origin=relocated),
        ),
    )

    assert first.evidence_identity == second.evidence_identity
    assert first.to_data() != second.to_data()
