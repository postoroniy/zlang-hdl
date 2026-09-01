from __future__ import annotations

from dataclasses import replace
import json

import pytest

from zlang.build_manifest import EvidenceRecord
from zlang.compiler import compile_source
from zlang.evidence_report import (
    EvidenceReportError,
    build_evidence_report,
    evidence_for_typed_module,
    evidence_for_unexecuted_property,
    evidence_for_validated_timing,
    evidence_from_cross_backend_result,
    evidence_from_equivalence_result,
    evidence_from_formal_exploration_record,
    evidence_from_formal_result,
    evidence_from_result,
    evidence_from_verification_report,
    render_evidence_json,
    render_evidence_text,
)
from zlang.formal_exploration import (
    FormalExplorationRecord,
    FormalPolicy,
)
from zlang.ir.cross_backend import (
    CrossBackendCounterexample,
    CrossBackendError,
    CrossBackendMode,
    CrossBackendRelation,
    CrossBackendResult,
    CrossBackendStatus,
)
from zlang.ir.equivalence import (
    EquivalenceCounterexample,
    EquivalenceError,
    EquivalenceMode,
    EquivalenceRelation,
    EquivalenceResult,
    EquivalenceStatus,
)
from zlang.ir.formal import (
    Counterexample,
    CoverWitness,
    FormalProperty,
    FormalResult,
    FormalStatus,
    Ownership,
    ProofMode,
    PropertyKind,
    TemporalForm,
)
from zlang.source import SourceOrigin, SourceSpan
from zlang.verification_bundle import (
    VerificationCounterexampleMetadata,
    VerificationJobResult,
    VerificationRunConfig,
    VerificationRunReport,
)


ORIGIN = SourceOrigin(
    SourceSpan(4, 3, 4, 12),
    "guarantee safe",
    "examples/safe.zl",
    "a" * 64,
)

M39_BOUND = {
    "property_identity": "m36.test.candidate",
    "harness_hash": "2" * 64,
    "assumptions_identity": "3" * 64,
    "backend_identity": "4" * 64,
    "reference_artifact_hash": "5" * 64,
    "implementation_artifact_hash": "1" * 64,
    "source_origin": ORIGIN,
    "selected_origin": SourceOrigin(
        SourceSpan(8, 3, 8, 12),
        "selected candidate",
        "examples/safe.zl",
        "a" * 64,
    ),
}


def _formal_result() -> FormalResult:
    return FormalResult(
        property_id="m35.fifo.bounds",
        status=FormalStatus.FAILED,
        mode=ProofMode.BMC,
        engine="sby",
        solver="z3",
        depth=8,
        counterexample=Counterexample(
            "m35.fifo.bounds", 3, (("fifo.count", "3"),), "trace"
        ),
        source_origin=ORIGIN,
        tool_versions=(("yosys", "0.68"), ("z3", "4.8.12")),
        reason="counterexample found",
    )


def _equivalence_result() -> EquivalenceResult:
    return EquivalenceResult(
        property_id="m36.eq.dot",
        status=EquivalenceStatus.BOUNDED_PASS,
        mode=EquivalenceMode.BMC,
        engine="sby",
        solver="z3",
        depth=12,
        relation_kind=EquivalenceRelation.FIXED_LATENCY_VALUE,
        latency_delta=3,
        backend="clash",
        reference_hash="b" * 64,
        implementation_hash="c" * 64,
        binding_map_version=2,
        candidate_identity="candidate.dot.pipeline3",
        source_origin=ORIGIN,
    )


def _cross_backend_result() -> CrossBackendResult:
    return CrossBackendResult(
        property_id="m38.eq.backends",
        status=CrossBackendStatus.FAILED,
        mode=CrossBackendMode.BMC,
        engine="sby",
        solver="z3",
        depth=10,
        relation=CrossBackendRelation.SAME_CYCLE_VALUE,
        latency_delta=0,
        selected_ir_identity="selected.dot",
        left_backend="clash",
        right_backend="direct_systemverilog",
        left_artifact_hash="d" * 64,
        right_artifact_hash="e" * 64,
        manifest_version=9,
        observable_signal_id="port:y",
        source_origin=ORIGIN,
        counterexample=CrossBackendCounterexample(
            property_id="m38.eq.backends",
            semantic_signal_id="port:y",
            cycle=2,
            sample_cycle=2,
            left_backend="clash",
            right_backend="direct_systemverilog",
            left_artifact_hash="d" * 64,
            right_artifact_hash="e" * 64,
            values=(("left.y", "1"), ("right.y", "0")),
            raw_trace="backend trace",
            source_origin=ORIGIN,
        ),
    )


def test_typed_adapters_preserve_formal_equivalence_and_artifact_metadata() -> None:
    m35 = evidence_from_formal_result(
        _formal_result(), backend="direct_systemverilog", artifact_hash="f" * 64
    )
    m36 = evidence_from_equivalence_result(_equivalence_result())
    m38 = evidence_from_cross_backend_result(_cross_backend_result())

    assert (m35.status, m35.mode, m35.depth) == ("failed", "bmc", 8)
    assert m35.property_id == "m35.fifo.bounds"
    assert m35.backend == "direct_systemverilog"
    assert m35.artifact_hash == "f" * 64
    assert m35.source_origin == ORIGIN
    assert m35.counterexample_digest is not None
    m35_details = dict(m35.details)
    assert json.loads(m35_details["counterexample_metadata"])["cycle"] == 3
    assert json.loads(m35_details["tool_versions"]) == [
        ["yosys", "0.68"], ["z3", "4.8.12"]
    ]

    assert (m36.status, m36.mode, m36.depth) == ("bounded_pass", "bmc", 12)
    assert m36.reference_hash == "b" * 64
    assert m36.artifact_hash == "c" * 64
    assert m36.relation == "fixed_latency_value"
    assert m36.candidate_identity == "candidate.dot.pipeline3"

    assert m38.backend == "clash<->direct_systemverilog"
    assert m38.reference_hash == "d" * 64
    assert m38.artifact_hash == "e" * 64
    assert m38.counterexample_digest is not None
    trace = json.loads(dict(m38.details)["counterexample_metadata"])
    assert trace["semantic_signal_id"] == "port:y"
    assert "raw_trace" not in trace

    # Attribution never participates in the evidence identity.
    moved = replace(
        _formal_result(),
        source_origin=SourceOrigin(
            SourceSpan(40, 1, 40, 2), "moved guarantee", "other.zl", "9" * 64
        ),
    )
    assert evidence_from_formal_result(moved).evidence_id == (
        evidence_from_formal_result(_formal_result()).evidence_id
    )


def test_m39_not_run_and_executed_candidate_evidence_are_distinct() -> None:
    not_run = evidence_from_formal_exploration_record(
        FormalExplorationRecord(
            "candidate.a", 1, "valid", "M36_clash", FormalPolicy.OFF,
            None, None, None, "not-run", True, "formal disabled",
        )
    )
    assert not_run.status == "not_run"
    assert not_run.mode is None and not_run.depth is None
    assert not_run.candidate_identity == "candidate.a"

    bounded = evidence_from_formal_exploration_record(
        FormalExplorationRecord(
            "candidate.b", 2, "valid", "M36_clash",
            FormalPolicy.REQUIRED_BMC, ProofMode.BMC, 16,
            FormalStatus.BOUNDED_PASS, "executed", True,
            "bounded proof satisfied", "clash", "1" * 64,
            **M39_BOUND,
        )
    )
    assert (bounded.status, bounded.mode, bounded.depth) == (
        "bounded_pass", "bmc", 16
    )
    assert bounded.route == "M36_clash"
    assert bounded.property_id == "m36.test.candidate"
    assert bounded.reference_hash == "5" * 64
    assert bounded.source_origin == ORIGIN
    assert "selected_origin" in dict(bounded.details)


def test_required_proven_bmc_and_prove_stages_remain_distinct_evidence() -> None:
    common = dict(
        candidate_identity="candidate.staged",
        rank=1,
        semantic_legality="valid",
        formal_route="M36_clash",
        policy=FormalPolicy.REQUIRED_PROVEN,
        depth=12,
        cache_state="executed",
        backend="clash",
        artifact_hash="1" * 64,
        engine="sby",
        solver="z3",
        **M39_BOUND,
    )
    bounded = evidence_from_formal_exploration_record(FormalExplorationRecord(
        mode=ProofMode.BMC,
        status=FormalStatus.BOUNDED_PASS,
        eligible=False,
        reason="bounded precheck passed; unbounded proof still required",
        **common,
    ))
    proven = evidence_from_formal_exploration_record(FormalExplorationRecord(
        mode=ProofMode.PROVE,
        status=FormalStatus.PROVEN,
        eligible=True,
        reason="unbounded proof satisfied",
        **common,
    ))
    assert (bounded.status, bounded.mode) == ("bounded_pass", "bmc")
    assert (proven.status, proven.mode) == ("proven", "prove")
    assert bounded.evidence_id != proven.evidence_id
    report = build_evidence_report(
        "m39.staged", (bounded, proven), format="json",
    )
    assert set(report.record.evidence_ids) == {
        bounded.evidence_id, proven.evidence_id,
    }


def test_m39_cache_hit_state_does_not_change_evidence_identity() -> None:
    common = dict(
        candidate_identity="candidate.cached",
        rank=1,
        semantic_legality="valid",
        formal_route="M36_clash",
        policy=FormalPolicy.REQUIRED_BMC,
        mode=ProofMode.BMC,
        depth=8,
        status=FormalStatus.BOUNDED_PASS,
        eligible=True,
        reason="bounded proof satisfied",
        backend="clash",
        artifact_hash="7" * 64,
        engine="sby",
        solver="z3",
        proof_reason="solver completed",
        **M39_BOUND,
    )
    executed = evidence_from_formal_exploration_record(
        FormalExplorationRecord(cache_state="executed", **common)
    )
    cached = evidence_from_formal_exploration_record(
        FormalExplorationRecord(cache_state="hit", **common)
    )
    assert executed.evidence_id == cached.evidence_id
    assert executed.identity_data() == cached.identity_data()
    assert executed.to_data() != cached.to_data()
    assert (executed.engine, executed.solver) == ("sby", "z3")
    assert ("proof_reason", "solver completed") in executed.details


def test_property_or_harness_generation_is_not_a_proof() -> None:
    property_ = FormalProperty(
        "m35.generated", PropertyKind.ASSERTION, "clk", "rst", "x == x",
        TemporalForm.SAME_CYCLE, Ownership.IMPLEMENTATION,
        source_origin=ORIGIN,
    )
    evidence = evidence_for_unexecuted_property(property_)
    assert evidence.status == "not_run"
    assert evidence.mode is None and evidence.depth is None
    assert "generated" in dict(evidence.details)["reason"]
    with pytest.raises(TypeError, match="accept only"):
        evidence_from_result(property_)


def test_static_legality_and_timing_evidence_require_typed_validated_ir() -> None:
    result = compile_source(
        "module Timed { clock clk reset rst in x:u8 out y:u8 "
        "y=delay<2>(x) timing { latency 2 ii 1 } }",
        include_clash=False,
    )
    legality = evidence_for_typed_module(
        result.ir, high_level_ir_identity=result.high_level_ir_identity,
    )
    timing = evidence_for_validated_timing(
        result.ir, selected_ir_identity=result.selected_ir_identity,
    )
    assert legality.status == "typed_legal"
    assert legality.candidate_identity == result.high_level_ir_identity
    assert timing.status == "timing_validated"
    assert timing.candidate_identity == result.selected_ir_identity
    assert dict(timing.details)["latency"] == "2"

    untimed = compile_source(
        "module Untimed { in x:u8 out y:u8 y=x }", include_clash=False,
    )
    with pytest.raises(EvidenceReportError, match="exact module contract"):
        evidence_for_validated_timing(
            untimed.ir, selected_ir_identity=untimed.selected_ir_identity,
        )


def test_untyped_logs_and_malformed_candidate_records_are_rejected() -> None:
    for value in ("Status: PASSED", {"status": "proven"}, object()):
        with pytest.raises(TypeError, match="accept only"):
            evidence_from_result(value)

    with pytest.raises(EvidenceReportError, match="cannot carry proof mode"):
        evidence_from_formal_exploration_record(
            FormalExplorationRecord(
                "candidate", 1, "valid", "none", FormalPolicy.OFF,
                ProofMode.BMC, None, None, "not-run", True, "not run",
            )
        )

    with pytest.raises(EvidenceReportError, match="typed formal IR"):
        evidence_from_formal_exploration_record(
            FormalExplorationRecord(
                "candidate", 1, "valid", "M36_clash", FormalPolicy.AVAILABLE,
                ProofMode.BMC, 4, FormalStatus.FAILED, "executed", False,
                "failed", backend="clash", artifact_hash="1" * 64,
                counterexample={"cycle": 1}, **M39_BOUND,
            )
        )

    with pytest.raises(EvidenceReportError, match="connected backend artifact"):
        evidence_from_formal_exploration_record(
            FormalExplorationRecord(
                "candidate", 1, "valid", "M36_clash",
                FormalPolicy.REQUIRED_BMC, ProofMode.BMC, 4,
                FormalStatus.BOUNDED_PASS, "executed", True, "passed",
            )
        )


def test_m36_and_m38_never_accept_bmc_as_unbounded_proof() -> None:
    with pytest.raises(EquivalenceError, match="proven is valid only for prove"):
        EquivalenceResult(
            "m36.bad", EquivalenceStatus.PROVEN, EquivalenceMode.BMC,
            "sby", "z3", 4, EquivalenceRelation.SAME_CYCLE_VALUE, 0,
            "clash", "a", "b", 2, "candidate",
        )
    with pytest.raises(CrossBackendError, match="proven is valid only for prove"):
        CrossBackendResult(
            "m38.bad", CrossBackendStatus.PROVEN, CrossBackendMode.BMC,
            "sby", "z3", 4, CrossBackendRelation.SAME_CYCLE_VALUE, 0,
            "selected", "clash", "direct_systemverilog", "a", "b", 9,
        )


def test_json_text_and_report_records_are_deterministic_and_truthful() -> None:
    records = (
        evidence_from_cross_backend_result(_cross_backend_result()),
        evidence_from_equivalence_result(_equivalence_result()),
        evidence_from_formal_result(_formal_result()),
        EvidenceRecord(
            "semantic.typed", "semantic.typecheck", "typed_legal",
            details=(("selected_ir_identity", "selected.dot"),),
        ),
        EvidenceRecord(
            "timing.validated", "timing.contract", "timing_validated",
            details=(("latency", "4"), ("ii", "1")),
        ),
    )
    first = render_evidence_json(records)
    second = render_evidence_json(tuple(reversed(records)))
    assert first == second
    assert "backend trace" not in first
    data = json.loads(first)
    assert [item["evidence_id"] for item in data["evidence"]] == sorted(
        item.evidence_id for item in records
    )
    assert {item["status"] for item in data["evidence"]} >= {
        "typed_legal", "timing_validated", "bounded_pass", "failed"
    }

    text = render_evidence_text(records)
    assert "status=bounded_pass depth=12" in text
    assert "status=typed_legal" in text
    assert "status=timing_validated" in text
    assert "status=proven" not in text

    report = build_evidence_report(
        "build.evidence.json", records, format="json",
        logical_path="reports/evidence.json",
    )
    assert report.content == first
    assert report.record.content_hash
    assert report.record.evidence_ids == tuple(
        sorted(item.evidence_id for item in records)
    )
    assert report.record.to_data() == report.record.from_data(
        report.record.to_data()
    ).to_data()


def test_first_class_verification_report_adapts_safety_and_cover_truthfully() -> None:
    tool_versions = (("yosys", "0.68"), ("z3", "4.8.12"))
    report = VerificationRunReport(
        "verification-bundle:" + "a" * 64,
        "VerifiedTop",
        VerificationRunConfig(
            mode=ProofMode.BMC,
            engine="sby",
            solver="z3",
            depth=9,
            timeout_seconds=37,
        ),
        (
            VerificationJobResult(
                "verification.assert.safe",
                "safety",
                "bounded_pass",
                "bmc",
                "sby",
                "z3",
                9,
                source_origin=ORIGIN,
                tool_versions=tool_versions,
                work_directory="/host-a/safety",
            ),
            VerificationJobResult(
                "verification.assert.failed",
                "safety",
                "failed",
                "bmc",
                "sby",
                "z3",
                9,
                counterexample=Counterexample(
                    "verification.assert.failed",
                    6,
                    (("port:y", "0"),),
                    "solver trace",
                ),
                source_origin=ORIGIN,
                tool_versions=tool_versions,
                work_directory="/host-a/failed",
                counterexample_metadata=VerificationCounterexampleMetadata(
                    sample_cycle=6,
                    reset_state="0",
                    comparison_valid_state=None,
                ),
            ),
            VerificationJobResult(
                "verification.cover.done",
                "cover",
                "witnessed",
                "cover",
                "sby",
                "z3",
                9,
                witness=CoverWitness(
                    "verification.cover.done",
                    4,
                    (("port:done", "0b1"),),
                    "solver trace",
                ),
                source_origin=ORIGIN,
                tool_versions=tool_versions,
                work_directory="/host-a/cover",
            ),
            VerificationJobResult(
                "verification.cover.impossible",
                "cover",
                "bounded_unreached",
                "cover",
                "sby",
                "z3",
                9,
                source_origin=ORIGIN,
                tool_versions=tool_versions,
                work_directory="/host-a/unreached",
            ),
        ),
        tool_versions,
    )
    records = evidence_from_verification_report(report)
    by_property = {item.property_id: item for item in records}

    safety = by_property["verification.assert.safe"]
    assert (safety.claim, safety.status, safety.mode, safety.depth) == (
        "verification.safety_goal", "bounded_pass", "bmc", 9,
    )
    failed_details = dict(
        by_property["verification.assert.failed"].details
    )
    assert failed_details["counterexample_sample_cycle"] == "6"
    assert failed_details["counterexample_reset_state"] == "0"
    assert "counterexample_comparison_valid_state" not in failed_details
    witnessed = by_property["verification.cover.done"]
    assert (witnessed.claim, witnessed.status, witnessed.mode) == (
        "verification.cover_goal", "witnessed", "cover",
    )
    witness_details = dict(witnessed.details)
    assert witness_details["witness_cycle"] == "4"
    assert witness_details["witness_digest"]
    assert "/host-a" not in render_evidence_json(records)
    assert witnessed.source_origin == ORIGIN
    assert "status=witnessed cycle=4 depth=9" in render_evidence_text(records)
    assert by_property["verification.cover.impossible"].status == "bounded_unreached"

    relocated = replace(
        report,
        results=tuple(
            replace(item, work_directory=f"/host-b/{index}")
            for index, item in enumerate(report.results)
        ),
    )
    assert tuple(item.evidence_id for item in records) == tuple(
        item.evidence_id for item in evidence_from_verification_report(relocated)
    )


def test_duplicate_evidence_identity_is_rejected() -> None:
    record = EvidenceRecord("same", "semantic.typecheck", "typed_legal")
    with pytest.raises(EvidenceReportError, match="duplicate evidence identity"):
        render_evidence_json((record, record))


def test_renderer_keeps_every_frozen_status_semantically_distinct() -> None:
    records = (
        EvidenceRecord("typed", "semantic", "typed_legal"),
        EvidenceRecord("timed", "timing", "timing_validated"),
        EvidenceRecord("bounded", "formal", "bounded_pass", "bmc", 7),
        EvidenceRecord("proven", "formal", "proven", "prove"),
        EvidenceRecord(
            "failed", "formal", "failed", "bmc", 7,
            counterexample_digest="0" * 64,
        ),
        EvidenceRecord("witnessed", "cover", "witnessed", "cover", 7),
        EvidenceRecord(
            "bounded-unreached", "cover", "bounded_unreached", "cover", 7,
        ),
        EvidenceRecord("unknown", "formal", "unknown", "bmc", 7),
        EvidenceRecord("skipped", "formal", "skipped", "bmc", 7),
        EvidenceRecord("not-run", "formal candidate", "not_run"),
    )
    text = render_evidence_text(records)
    payload = render_evidence_json(records)
    for status in (
        "typed_legal", "timing_validated", "bounded_pass", "proven",
        "failed", "witnessed", "bounded_unreached", "unknown", "skipped",
        "not_run",
    ):
        assert f"status={status}" in text
        assert f'"status": "{status}"' in payload
