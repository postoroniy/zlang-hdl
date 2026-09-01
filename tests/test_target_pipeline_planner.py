from dataclasses import replace
from pathlib import Path

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_target_artifact
from zlang.compiler import compile_source
from zlang.costs import MetricSource
from zlang.ir import expressions as expr
from zlang.ir.pipelines import PipelineMetric, PipelineRelation
from zlang.ir.types import UIntType
from zlang.target_planner import MeasurementKey, QoREvidence
from zlang.target_timing import alignment_delays
from zlang.targets import TargetArchitectureError


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "examples/symmetric_fixed_fir_auto.zl").read_text()
TARGET = "xc7z030ffg676-1"


def _compile(top="SymmetricFixedFIRAuto", **kwargs):
    return compile_source(SOURCE, top=top, target=TARGET, **kwargs)


def _target_candidate(result, configuration="multiply_output_registered"):
    return next(
        item for item in result.target_planning_result.generated_candidates
        if item.graph.pipeline_configuration_identity
        and item.graph.pipeline_configuration_identity.endswith(configuration)
    )


def _evidence(graph, *, stage=MetricSource.ROUTED_MEASUREMENT, tool="Vivado"):
    return QoREvidence(
        MeasurementKey(
            graph.target_identity, graph.target_part,
            graph.architecture_template_identity, graph.identity,
            graph.pipeline_configuration_identity, "direct_systemverilog",
            tool, "2024.2", 10.0,
        ),
        stage, 233, 16, 4, 0, 108.08, 0.748, "test fixture",
    )


def test_ii_alias_normalizes_to_existing_throughput_constraint_and_exact_latency():
    result = _compile(top="SymmetricFixedFIRAutoExact8")
    constraints = result.ir.pipeline_explorations[0].constraints
    assert constraints[0].metric is PipelineMetric.LATENCY
    assert constraints[0].relation is PipelineRelation.EXACT
    assert constraints[1].metric is PipelineMetric.THROUGHPUT
    assert constraints[1].value == 1


def test_measured_bounded_contract_selects_lower_latency_100mhz_configuration():
    result = _compile(target_evidence_policy="measured_required")
    graph = result.implementation_graph
    assert graph.pipeline_configuration_identity.endswith("multiply_output_registered")
    assert graph.active_pipeline_sites == ("multiply", "accumulate_output")
    assert graph.latency == 3
    assert graph.initiation_interval == 1
    assert len(graph.resources) == 4
    assert len(graph.dedicated_edges) == 3
    assert not graph.timing_dag.compensation_delays
    report = result.target_planner_report
    assert "72.1 < 100" in report
    assert "80.2 < 100" in report
    assert "108.08 MHz (routed_measurement)" in report


def test_candidate_set_is_generic_plus_four_resource_configurations():
    result = _compile()
    candidates = result.target_planning_result.generated_candidates
    assert len(candidates) == 5
    assert candidates[0].graph.is_generic
    assert [item.graph.latency for item in candidates[1:]] == [1, 2, 3, 4]
    assert all(len(item.graph.dedicated_edges) == 3 for item in candidates[1:])


def test_exact_latency_adds_only_explicit_compensation():
    result = _compile(
        top="SymmetricFixedFIRAutoExact8",
        target_evidence_policy="measured_required",
    )
    selected = result.implementation_graph
    assert selected.latency == 8
    assert selected.active_pipeline_sites == ("multiply", "accumulate_output")
    assert sum(item.cycles for item in selected.timing_dag.compensation_delays) == 5
    assert sum(item.ff_cost for item in selected.timing_dag.compensation_delays) == 80
    assert result.target_planning_result.selected_candidate.cost.fmax_est.value == pytest.approx(106.1233)


def test_bounded_latency_does_not_add_compensation():
    result = _compile(target_evidence_policy="measured_required")
    assert result.implementation_graph.latency == 3
    assert result.implementation_graph.timing_dag.compensation_delays == ()


def test_timing_dag_keeps_dedicated_edges_and_resource_local_cuts():
    graph = _compile(target_evidence_policy="measured_required").implementation_graph
    dedicated = tuple(item for item in graph.timing_dag.edges if item.kind == "dedicated")
    assert tuple(item.dedicated_edge_identity for item in dedicated) == tuple(
        item.identity for item in graph.dedicated_edges
    )
    assert {item.pipeline_site_identity.rsplit(".", 1)[-1] for item in graph.timing_dag.cuts} == {
        "multiply", "accumulate_output",
    }


def test_m30_alignment_becomes_explicit_deterministic_ff_cost():
    a = expr.InputRef("a", UIntType(8))
    b = expr.Pipeline(2, expr.InputRef("b", UIntType(8)), 0, UIntType(8))
    first = alignment_delays((a, b), ("path-a", "path-b"), width=8,
                             destination_node="join")
    second = alignment_delays((a, b), ("path-a", "path-b"), width=8,
                              destination_node="join")
    assert first == second
    assert len(first) == 1
    assert first[0].cycles == 2
    assert first[0].ff_cost == 16
    assert first[0].kind == "alignment"


def test_routed_required_rejects_synthesis_only_and_incompatible_evidence():
    discovery = _compile()
    graph = _target_candidate(discovery).graph
    synthesis = _evidence(graph, stage=MetricSource.SYNTHESIS_MEASUREMENT)
    with pytest.raises(TargetArchitectureError, match="no legal candidate"):
        _compile(target_evidence_policy="measured_required", target_evidence=(synthesis,))
    incompatible = _evidence(graph, tool="OtherTool")
    with pytest.raises(TargetArchitectureError, match="no legal candidate"):
        _compile(target_evidence_policy="measured_required", target_evidence=(incompatible,))
    routed = _compile(
        target_evidence_policy="measured_required",
        target_evidence=(_evidence(graph),),
    )
    assert not routed.implementation_graph.is_generic


def test_independent_coefficients_do_not_match_symmetric_template():
    source = SOURCE.replace("vec<4,SF2.10>", "vec<8,SF2.10>").replace(
        "samples[4] * coefficients[3]", "samples[4] * coefficients[4]"
    ).replace(
        "samples[5] * coefficients[2]", "samples[5] * coefficients[5]"
    ).replace(
        "samples[6] * coefficients[1]", "samples[6] * coefficients[6]"
    ).replace(
        "samples[7] * coefficients[0]", "samples[7] * coefficients[7]"
    )
    result = compile_source(source, top="SymmetricFixedFIRAuto", target=TARGET)
    assert result.implementation_graph.is_generic
    assert any("four semantically reused coefficients" in reason
               for item in result.target_planning_result.rejected_candidates
               for reason in item.rejection_reasons)


def test_illegal_exact_width_rejects_target_and_keeps_generic():
    source = SOURCE.replace("vec<8,SF2.10>", "vec<8,SF20.10>")
    result = compile_source(source, top="SymmetricFixedFIRAuto", target=TARGET)
    assert result.implementation_graph.is_generic
    assert any("preadder width exceeded" in reason
               for item in result.target_planning_result.rejected_candidates
               for reason in item.rejection_reasons)
    with pytest.raises(TargetArchitectureError, match="required target architecture"):
        compile_source(
            source, top="SymmetricFixedFIRAuto", target=TARGET,
            architecture="Xilinx7SymmetricDSPCascade",
            architecture_mode="required",
        )


def test_ranking_is_independent_of_evidence_order():
    discovery = _compile()
    records = tuple(
        _evidence(item.graph)
        for item in discovery.target_planning_result.generated_candidates[1:]
    )
    # Make the first two fail the frequency requirement exactly as production evidence does.
    records = (
        replace(records[0], fmax_mhz=72.1),
        replace(records[1], fmax_mhz=80.2),
        records[2], records[3],
    )
    forward = _compile(target_evidence_policy="measured_required", target_evidence=records)
    reverse = _compile(target_evidence_policy="measured_required", target_evidence=reversed(records))
    assert forward.implementation_graph.identity == reverse.implementation_graph.identity
    assert forward.implementation_graph.latency == 3


def test_manifest_v7_retains_policy_timing_cost_evidence_and_backend():
    result = _compile(target_evidence_policy="measured_required")
    artifact = emit_target_artifact(result.ir, result.implementation_graph, simulation_model=True)
    restored = BackendArtifact.from_json(artifact.to_json())
    implementation = restored.implementation
    assert restored.manifest_version == 7
    assert implementation.realization_backend == "direct_systemverilog"
    assert implementation.latency_knowledge == "known"
    assert implementation.policy_requirements == (
        ("latency", "<=", 8), ("ii", "==", 1), ("fmax", ">=", 100),
    )
    assert implementation.objective == "lut"
    assert implementation.timing_dag_identity
    assert len(implementation.timing_nodes) == 7
    assert len(tuple(item for item in implementation.timing_edges if item[1] == "dedicated")) == 3
    assert implementation.evidence_identity
