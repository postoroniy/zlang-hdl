"""Direct-SV physical validation for the frozen scalar FFT reduction slice."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_target, emit_target_artifact
from zlang.compiler import compile_source
from zlang.costs import CandidateCost
from zlang.fixed_point import quantize_rational
from zlang.equivalence import formal_tools_available
from zlang.formal_candidate import (
    M36DirectSystemVerilogCandidateVerifier,
    PhysicalTargetFormalCandidate,
)
from zlang.formal_exploration import FormalExplorationConfig, FormalPolicy
from zlang.ir.formal import ProofMode
from zlang.ir.formal import FormalStatus
from zlang.ir import expressions as expr
from zlang.ir.types import SIntType
from zlang.pipeline_scheduling import erase_pipeline_timing
from zlang.targets import (
    load_architecture_templates,
    load_target,
    map_auto_signed_product_configuration,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/fft/complex_multiply_pipeline_auto.zhl").read_text()
TARGET = "xc7z030ffg676-1"
EXACT_SOURCE = """
module ExactSignedProductPipeline {
    clock clk
    reset rst
    in a, b, c, d : fixed<8,4>
    out y : fixed<8,4>
    y = pipeline(3) {
        quantize<fixed<8,4>>(a * b + c * d) {
            round nearest_even
            overflow saturate
        }
    }
}
"""
SMALL_FORMAL_SOURCE = """
module SmallExactSignedProductPipeline {
    clock clk
    reset rst
    in a, b, c, d : fixed<4,2>
    out y : fixed<4,2>
    y = pipeline(3) {
        quantize<fixed<4,2>>(a * b + c * d) {
            round nearest_even
            overflow saturate
        }
    }
}
"""
CONFIGURATIONS = (
    ("unregistered", 1),
    ("multiply_registered", 2),
    ("multiply_output_registered", 3),
    ("fully_pipelined", 4),
)


class _BoundPhysicalVerifier:
    formal_route = "M36_direct_systemverilog"

    def __init__(self) -> None:
        self.calls: list[PhysicalTargetFormalCandidate] = []

    @staticmethod
    def _identity(candidate: PhysicalTargetFormalCandidate) -> dict[str, str]:
        return {
            "property_identity": f"m36.physical.{candidate.implementation_identity}",
            "reference_artifact_hash": "a" * 64,
            "implementation_artifact_hash": "b" * 64,
            "artifact_hash": "b" * 64,
            "harness_hash": "c" * 64,
            "backend_identity": "d" * 64,
            "assumptions_identity": "e" * 64,
        }

    def cache_identity(self, candidate, _config):
        return self._identity(candidate)

    def __call__(self, candidate, config):
        self.calls.append(candidate)
        return {
            "status": FormalStatus.BOUNDED_PASS,
            "mode": ProofMode.BMC,
            "depth": config.bmc_depth,
            "backend": "direct_systemverilog",
            **self._identity(candidate),
        }


def _physical_graph(configuration_name: str, top: str):
    typed = compile_source(SOURCE, top=top)
    target, family, resources = load_target(TARGET)
    template = next(
        item for item in load_architecture_templates(operation="signed_product_reduction")
        if item.name == "Xilinx7SignedProductCascade"
    )
    resource = next(item for item in resources if item.name == "DSP48E1")
    configuration = resource.pipeline_configuration(configuration_name)
    return typed, map_auto_signed_product_configuration(
        typed.ir, target, family, resources, template, configuration,
    )


def _literal(value: int, width: int) -> str:
    return f"-{width}'sd{-value}" if value < 0 else f"{width}'sd{value}"


def test_target_planner_publishes_both_signed_fft_physical_candidate_families() -> None:
    real = compile_source(SOURCE, top="FFTComplexMultiplyRealAuto", target=TARGET)
    imag = compile_source(SOURCE, top="FFTComplexMultiplyImagAuto", target=TARGET)
    for result in (real, imag):
        physical = tuple(
            item for item in result.target_planning_result.generated_candidates
            if not item.graph.is_generic and "SignedProduct" in item.name
        )
        assert {item.name.rsplit("/", 1)[-1] for item in physical} == {
            item[0] for item in CONFIGURATIONS
        }
        assert all(len(item.graph.resources) == 2 for item in physical)
        assert all(
            dict(node.configuration)["accumulator_mode"]
            in {"accumulator_plus_product", "accumulator_minus_product"}
            for item in physical for node in item.graph.resources
        )
    _, real_graph = _physical_graph("unregistered", "FFTComplexMultiplyRealAuto")
    _, imag_graph = _physical_graph("unregistered", "FFTComplexMultiplyImagAuto")
    assert real_graph.resources[1].configuration != imag_graph.resources[1].configuration
    assert real.ir.pipeline_explorations and imag.ir.pipeline_explorations
    artifact = emit_target_artifact(real.ir, real_graph)
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.implementation is not None
    assert len(restored.implementation.resources) == 2
    assert restored.implementation.pipeline_configuration_identity.endswith("unregistered")
    assert restored.implementation.physical_binding_identities


def test_packaged_catalog_selects_fft_dsp_without_explicit_evidence_path() -> None:
    for top in ("FFTComplexMultiplyRealAuto", "FFTComplexMultiplyImagAuto"):
        result = compile_source(SOURCE, top=top, target=TARGET)
        selected = result.target_planning_result.selected_candidate
        assert selected.graph.pipeline_configuration_identity.endswith(
            "multiply_registered"
        )
        assert selected.evidence is not None
        assert selected.cost.fmax_est.source.value == "routed_measurement"


def test_targeted_implement_m39_gates_one_complete_physical_candidate() -> None:
    verifier = _BoundPhysicalVerifier()
    result = compile_source(
        SOURCE,
        top="FFTComplexMultiplyRealAuto",
        target=TARGET,
        formal_policy="required_bmc",
        formal_depth=8,
        formal_verifier=verifier,
    )

    assert len(verifier.calls) == 1
    candidate = verifier.calls[0]
    assert not candidate.implementation_graph.is_generic
    assert candidate.implementation_graph.scheduled_value_graph is not None
    assert len(result.physical_formal_records) == 1
    assert result.physical_formal_records[0].candidate_identity == (
        candidate.implementation_identity
    )
    assert result.target_planning_result.formal_records == (
        result.physical_formal_records
    )
    assert all(
        record.status is None and record.cache_state == "not-run"
        for record in result.exploration_results[0].formal_records
    )


def test_exact_pipeline_is_target_planned_and_emits_real_dsp_boundaries(
    tmp_path: Path,
) -> None:
    result = compile_source(
        EXACT_SOURCE,
        top="ExactSignedProductPipeline",
        target=TARGET,
    )
    assert result.ir.pipeline_explorations == ()
    assert result.implementation_graph.latency == 3
    assert result.implementation_graph.pipeline_configuration_identity.endswith(
        "multiply_output_registered"
    )
    assert len(result.implementation_graph.resources) == 2
    scheduled_graph = result.implementation_graph.scheduled_value_graph
    assert scheduled_graph is not None
    assert scheduled_graph.exact_latency == 3
    # Two product nodes and their ordered accumulation join are covered by
    # the two DSPs; the final quantization remains one explicit fabric node.
    assert len(scheduled_graph.resource_bindings) == 3
    assert len({
        item.resource_instance_identity
        for item in scheduled_graph.resource_bindings
    }) == 2
    assert "requirements: latency == 3, ii == 1" in result.target_planner_report
    rtl = tmp_path / "exact_signed_product_pipeline.sv"
    rtl.write_text(
        emit_target(result.ir, result.implementation_graph, simulation_model=True)
    )
    text = rtl.read_text()
    assert "zlang_sp_dsp0_primitive" in text
    assert "zlang_sp_dsp1_primitive" in text
    assert "assign y = __target_result_q0;" in text
    artifact = emit_target_artifact(
        result.ir,
        result.implementation_graph,
        simulation_model=True,
    )
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.implementation is not None
    assert restored.implementation.scheduled_value_graph_identity == scheduled_graph.identity
    verilator = shutil.which("verilator")
    if verilator is None:
        pytest.skip("Verilator unavailable")
    subprocess.run(
        (
            verilator,
            "--lint-only",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSEDPARAM",
            "-Wno-UNUSEDSIGNAL",
            "--top-module",
            "ExactSignedProductPipeline",
            str(rtl),
        ),
        check=True,
        capture_output=True,
        text=True,
    )


def test_exact_pipeline_physical_graph_is_the_m36_implementation_artifact() -> None:
    result = compile_source(
        EXACT_SOURCE,
        top="ExactSignedProductPipeline",
        target=TARGET,
    )
    implementation = result.ir.assignments[0].expression
    reference = erase_pipeline_timing(implementation)
    graph = result.implementation_graph
    candidate = PhysicalTargetFormalCandidate(
        expression=implementation,
        module=result.ir,
        implementation_graph=graph,
        semantic_identity=graph.semantic_region_identity,
        implementation_identity=graph.identity,
        cost=CandidateCost.estimate(
            lut=0,
            ff=0,
            dsp=len(graph.resources),
            latency=graph.latency,
            ii=graph.initiation_interval,
        ),
    )
    verifier = M36DirectSystemVerilogCandidateVerifier(
        reference,
        candidate_class="pipeline",
        clock_domain_contract=result.ir.clock_domains[0],
    )
    prepared = verifier.prepare(
        candidate,
        FormalExplorationConfig(FormalPolicy.AVAILABLE, bmc_depth=8),
    )

    assert "DSP48E1" in prepared.implementation_artifact.text
    assert prepared.property.latency_delta == 3
    manifest = prepared.implementation_artifact.implementation
    assert manifest is not None
    assert manifest.scheduled_value_graph_identity == (
        graph.scheduled_value_graph.identity
    )


@pytest.mark.skipif(
    len(formal_tools_available()) != 3 or shutil.which("z3") is None,
    reason="Yosys/SymbiYosys/Z3 route unavailable",
)
def test_planning_phase_m39_checks_complete_dsp_graph_and_detects_mutations() -> None:
    result = compile_source(
        SMALL_FORMAL_SOURCE,
        top="SmallExactSignedProductPipeline",
        target=TARGET,
        formal_policy="required_bmc",
        formal_depth=8,
        formal_timeout=30,
    )
    assert len(result.physical_formal_records) == 1
    record = result.physical_formal_records[0]
    assert record.status is FormalStatus.BOUNDED_PASS
    assert record.backend == "direct_systemverilog"
    assert result.target_planning_result.formal_records == (record,)

    implementation = result.ir.assignments[0].expression
    reference = erase_pipeline_timing(implementation)
    graph = result.implementation_graph
    verifier = M36DirectSystemVerilogCandidateVerifier(
        reference,
        candidate_class="pipeline",
        clock_domain_contract=result.ir.clock_domains[0],
    )
    config = FormalExplorationConfig(
        FormalPolicy.REQUIRED_BMC,
        bmc_depth=8,
        timeout_seconds=30,
    )

    for field, replacement in (
        ("accumulator_mode", "accumulator_minus_product"),
        ("terminal_preg", 0),
    ):
        resources = list(graph.resources)
        configuration = dict(resources[-1].configuration)
        configuration[field] = replacement
        resources[-1] = replace(
            resources[-1], configuration=tuple(configuration.items())
        )
        mutated_graph = replace(graph, resources=tuple(resources))
        candidate = PhysicalTargetFormalCandidate(
            expression=implementation,
            module=result.ir,
            implementation_graph=mutated_graph,
            semantic_identity=mutated_graph.semantic_region_identity,
            implementation_identity=mutated_graph.identity,
            cost=CandidateCost.estimate(
                lut=0,
                ff=0,
                dsp=len(mutated_graph.resources),
                latency=mutated_graph.latency,
                ii=mutated_graph.initiation_interval,
            ),
        )
        evidence = verifier(candidate, config)
        assert evidence["status"] is FormalStatus.FAILED
        assert evidence["counterexample"] is not None


@pytest.mark.skipif(
    len(formal_tools_available()) != 3 or shutil.which("z3") is None,
    reason="Yosys/SymbiYosys/Z3 route unavailable",
)
def test_planning_phase_m39_checks_the_selected_egraph_value_schedule() -> None:
    result = compile_source(
        "module ShiftedFormal { clock clk reset rst in a:u4 out y:u8 "
        "y=pipeline(2){a*8} }",
        top="ShiftedFormal",
        target="generic",
        formal_policy="required_bmc",
        formal_depth=6,
        formal_timeout=30,
    )
    plan = result.ir.assignments[0].expression.pipeline_plan
    assert plan is not None
    assert plan.selected_value_identity != plan.source_expression_identity
    assert any(
        item.startswith("rewrite=multiply_power_of_two")
        for item in plan.rewrite_certificate
    )
    assert len(result.physical_formal_records) == 1
    record = result.physical_formal_records[0]
    assert record.status is FormalStatus.BOUNDED_PASS
    assert record.candidate_identity == result.implementation_graph.identity


@pytest.mark.skipif(
    len(formal_tools_available()) != 3 or shutil.which("z3") is None,
    reason="Yosys/SymbiYosys/Z3 route unavailable",
)
def test_physical_m36_detects_unsigned_dsp_sign_extension_mutation() -> None:
    result = compile_source(
        "module UnsignedDSP { clock clk reset rst in a,b:u3 out y:u6 "
        "y=pipeline(3){a*b} }",
        top="UnsignedDSP",
        target=TARGET,
        formal_policy="required_bmc",
        formal_depth=7,
        formal_timeout=30,
    )
    assert result.physical_formal_records[0].status is FormalStatus.BOUNDED_PASS
    implementation = result.ir.assignments[0].expression
    graph = result.implementation_graph
    node = graph.resources[0]
    mappings = tuple(
        replace(
            item,
            expression=expr.InputRef(item.expression.name, SIntType(3)),
        )
        if item.resource_port == "a" and isinstance(item.expression, expr.InputRef)
        else item
        for item in node.semantic_mappings
    )
    mutated_graph = replace(
        graph,
        resources=(replace(node, semantic_mappings=mappings),),
    )
    candidate = PhysicalTargetFormalCandidate(
        expression=implementation,
        module=result.ir,
        implementation_graph=mutated_graph,
        semantic_identity=mutated_graph.semantic_region_identity,
        implementation_identity=mutated_graph.identity,
        cost=CandidateCost.estimate(
            lut=0,
            ff=0,
            dsp=1,
            latency=mutated_graph.latency,
            ii=mutated_graph.initiation_interval,
        ),
    )
    verifier = M36DirectSystemVerilogCandidateVerifier(
        erase_pipeline_timing(implementation),
        candidate_class="pipeline",
        clock_domain_contract=result.ir.clock_domains[0],
    )
    evidence = verifier(
        candidate,
        FormalExplorationConfig(
            FormalPolicy.REQUIRED_BMC,
            bmc_depth=7,
            timeout_seconds=30,
        ),
    )
    assert evidence["status"] is FormalStatus.FAILED
    assert evidence["counterexample"] is not None


@pytest.mark.parametrize(
    ("output", "mode", "alumode", "carryin", "expected"),
    (
        ("plus", "accumulator_plus_product", "4'b0000", "1'b0", -8),
        (
            "accumulator_minus_product",
            "accumulator_minus_product",
            "4'b0011",
            "1'b0",
            16,
        ),
        (
            "product_minus_accumulator",
            "product_minus_accumulator",
            "4'b0001",
            "1'b1",
            -16,
        ),
    ),
)
def test_exact_multiply_add_modes_use_one_dsp48_and_preserve_subtraction_order(
    output: str,
    mode: str,
    alumode: str,
    carryin: str,
    expected: int,
    tmp_path: Path,
) -> None:
    # Keep each root single-output: exact target planning deliberately accepts
    # one independently schedulable scalar region at a time.
    expressions = {
        "plus": "a * b + c",
        "accumulator_minus_product": "c - a * b",
        "product_minus_accumulator": "a * b - c",
    }
    source = f"""
module ExactMultiplyAdd {{
    clock clk
    reset rst
    in a, b : fixed<8,4>
    in c : fixed<16,8>
    out selected : fixed<8,4>
    selected = pipeline(3) {{
        quantize<fixed<8,4>>({expressions[output]}) {{
            round nearest_even
            overflow saturate
        }}
    }}
}}
"""
    result = compile_source(
        source,
        top="ExactMultiplyAdd",
        target=TARGET,
    )
    graph = result.implementation_graph
    assert len(graph.resources) == 1
    assert dict(graph.resources[0].configuration)["accumulator_mode"] == mode
    rtl = tmp_path / f"multiply_add_{output}.sv"
    rtl.write_text(emit_target(result.ir, graph, simulation_model=True))
    text = rtl.read_text()
    assert f".ALUMODE({alumode})" in text
    assert f".CARRYIN({carryin})" in text
    verilator = shutil.which("verilator")
    if verilator is None:
        pytest.skip("Verilator unavailable")
    bench = tmp_path / f"tb_multiply_add_{output}.sv"
    bench.write_text(
        "module tb; logic clk=0,rst=1; "
        "logic signed [7:0] a=0,b=0; logic signed [15:0] c=0; "
        "wire signed [7:0] selected; ExactMultiplyAdd dut(.clk,.rst,.a,.b,.c,.selected); "
        "task tick; begin #1 clk=1; #1; clk=0; #1; end endtask "
        "initial begin tick; rst=0; a=8'sd24; b=-8'sd8; c=16'sd64; "
        "tick; tick; tick; "
        f"if ($signed(selected) !== {_literal(expected, 8)}) $fatal(1,\"mismatch\"); "
        "$finish; end endmodule\n"
    )
    obj = tmp_path / f"obj_multiply_add_{output}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            verilator,
            "--binary",
            "--timing",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSEDPARAM",
            "-Wno-UNUSEDSIGNAL",
            "--top-module",
            "tb",
            str(rtl),
            str(bench),
            "-Mdir",
            str(obj),
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    completed = subprocess.run(
        (str(obj / "Vtb"),), check=True, capture_output=True, text=True
    )
    assert "$finish" in completed.stdout


@pytest.mark.parametrize(
    (
        "expression",
        "input_type",
        "addend_type",
        "result_type",
        "a",
        "b",
        "c",
        "expected",
    ),
    (
        ("a * b + c", "s8", "s16", "s17", -7, 9, 12, -51),
        ("a * b + c", "u8", "u16", "u17", 250, 200, 60000, 110000),
        ("a * b", "s8", "s8", "s16", -7, 9, 0, -63),
        ("a * b", "u8", "u8", "u16", 250, 200, 0, 50000),
        ("(a + b) * c", "s8", "s8", "s17", -7, 9, 12, 24),
        ("(a + b) * c", "u8", "u8", "u17", 250, 200, 200, 90000),
    ),
)
def test_integer_multiply_add_uses_exact_signed_or_zero_extended_dsp_inputs(
    expression: str,
    input_type: str,
    addend_type: str,
    result_type: str,
    a: int,
    b: int,
    c: int,
    expected: int,
    tmp_path: Path,
) -> None:
    source = f"""
module IntegerMultiplyAdd {{
    clock clk reset rst
    in a, b : {input_type}
    in c : {addend_type}
    out y : {result_type}
    y = pipeline(3) {{ {expression} }}
}}
"""
    result = compile_source(
        source,
        top="IntegerMultiplyAdd",
        target=TARGET,
    )
    assert result.target_planning_result.selected_candidate.name == (
        "Xilinx7MultiplyAdd/multiply_output_registered"
    )
    assert len(result.implementation_graph.resources) == 1
    mappings = {
        item.resource_port for item in result.implementation_graph.resources[0].semantic_mappings
    }
    assert ("d" in mappings) == expression.startswith("(")
    rtl = tmp_path / f"integer_madd_{input_type}.sv"
    rtl.write_text(
        emit_target(result.ir, result.implementation_graph, simulation_model=True)
    )
    text = rtl.read_text()
    expected_acc_width = int(result_type[1:]) + int(result_type.startswith("u"))
    assert f".ZLANG_ACC_WIDTH({expected_acc_width})" in text
    verilator = shutil.which("verilator")
    if verilator is None:
        pytest.skip("Verilator unavailable")
    signed = input_type.startswith("s")
    qualifier = " signed" if signed else ""
    addend_width = int(addend_type[1:])
    result_width = int(result_type[1:])
    bench = tmp_path / f"tb_integer_madd_{input_type}.sv"
    bench.write_text(
        "module tb; logic clk=0,rst=1; "
        f"logic{qualifier} [7:0] a=0,b=0; "
        f"logic{qualifier} [{addend_width - 1}:0] c=0; "
        f"wire{qualifier} [{result_width - 1}:0] y; "
        "IntegerMultiplyAdd dut(.clk,.rst,.a,.b,.c,.y); "
        "task tick; begin #1 clk=1; #1; clk=0; #1; end endtask "
        f"initial begin tick; rst=0; a={a}; b={b}; c={c}; "
        "tick; tick; tick; "
        f"if ({'$signed(y)' if signed else 'y'} !== {expected}) "
        "$fatal(1,\"integer DSP mismatch\"); $finish; end endmodule\n"
    )
    obj = tmp_path / f"obj_integer_madd_{input_type}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            verilator,
            "--binary",
            "--timing",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSEDPARAM",
            "-Wno-UNUSEDSIGNAL",
            "--top-module",
            "tb",
            str(rtl),
            str(bench),
            "-Mdir",
            str(obj),
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    completed = subprocess.run(
        (str(obj / "Vtb"),), check=True, capture_output=True, text=True
    )
    assert "$finish" in completed.stdout


@pytest.mark.parametrize(
    ("numeric_type", "result_type", "values", "expected"),
    (
        ("s4", "s9", (-7, 6, 5, -3), -57),
        ("u4", "u9", (15, 14, 13, 12), 366),
    ),
)
def test_integer_two_product_accumulation_uses_exact_dsp_cascade(
    numeric_type: str,
    result_type: str,
    values: tuple[int, int, int, int],
    expected: int,
    tmp_path: Path,
) -> None:
    source = f"""
module IntegerProductSum {{
    clock clk reset rst
    in a, b, c, d : {numeric_type}
    out y : {result_type}
    y = pipeline(3) {{ a * b + c * d }}
}}
"""
    result = compile_source(
        source,
        top="IntegerProductSum",
        target=TARGET,
    )
    assert len(result.implementation_graph.resources) == 2
    assert all(
        dict(item.configuration)["accumulator_mode"]
        == "accumulator_plus_product"
        for item in result.implementation_graph.resources
    )
    rtl = tmp_path / f"integer_product_sum_{numeric_type}.sv"
    rtl.write_text(
        emit_target(result.ir, result.implementation_graph, simulation_model=True)
    )
    verilator = shutil.which("verilator")
    if verilator is None:
        pytest.skip("Verilator unavailable")
    signed = numeric_type.startswith("s")
    qualifier = " signed" if signed else ""
    width = int(numeric_type[1:])
    result_width = int(result_type[1:])
    assignments = "; ".join(
        f"{name}={value}" for name, value in zip("abcd", values, strict=True)
    )
    bench = tmp_path / f"tb_integer_product_sum_{numeric_type}.sv"
    bench.write_text(
        "module tb; logic clk=0,rst=1; "
        f"logic{qualifier} [{width - 1}:0] a=0,b=0,c=0,d=0; "
        f"wire{qualifier} [{result_width - 1}:0] y; "
        "IntegerProductSum dut(.clk,.rst,.a,.b,.c,.d,.y); "
        "task tick; begin #1 clk=1; #1; clk=0; #1; end endtask "
        f"initial begin tick; rst=0; {assignments}; tick; tick; tick; "
        f"if ({'$signed(y)' if signed else 'y'} !== {expected}) "
        "$fatal(1,\"integer cascade mismatch\"); $finish; end endmodule\n"
    )
    obj = tmp_path / f"obj_integer_product_sum_{numeric_type}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            verilator,
            "--binary",
            "--timing",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSEDPARAM",
            "-Wno-UNUSEDSIGNAL",
            "--top-module",
            "tb",
            str(rtl),
            str(bench),
            "-Mdir",
            str(obj),
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    completed = subprocess.run(
        (str(obj / "Vtb"),), check=True, capture_output=True, text=True
    )
    assert "$finish" in completed.stdout


@pytest.mark.parametrize("mode", ("real", "imag"))
def test_measured_required_uses_routed_signed_product_evidence(mode: str) -> None:
    top = "FFTComplexMultiplyRealAuto" if mode == "real" else "FFTComplexMultiplyImagAuto"
    result = compile_source(
        SOURCE,
        top=top,
        target=TARGET,
        target_evidence_policy="measured_required",
        target_evidence_path=ROOT / "zlang/data/xc7z030_signed_product_qor.json",
    )
    selected = result.target_planning_result.selected_candidate
    assert not selected.graph.is_generic
    assert selected.graph.pipeline_configuration_identity.endswith("multiply_registered")
    assert selected.cost.fmax_est.source.value == "routed_measurement"
    rejected = {
        item.name: item.rejection_reasons
        for item in result.target_planning_result.rejected_candidates
    }
    unregistered = next(
        item for item in result.target_planning_result.generated_candidates
        if item.name == "Xilinx7SignedProductCascade/unregistered"
    )
    assert unregistered.evidence is not None
    assert any(
        str(unregistered.evidence.fmax_mhz) in reason
        for reason in rejected["Xilinx7SignedProductCascade/unregistered"]
    )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_all_signed_fft_physical_variants_are_bit_exact_in_verilator(tmp_path: Path) -> None:
    sr, si, tr, ti = 10000, -7000, 1200, 3000
    expected = {
        "real": quantize_rational(
            sr * tr - si * ti, 1 << 30, fraction=16, width=18, signed=True,
            rounding="nearest_even", overflow="saturate",
        ),
        "imag": quantize_rational(
            sr * ti + si * tr, 1 << 30, fraction=16, width=18, signed=True,
            rounding="nearest_even", overflow="saturate",
        ),
    }
    expected_model = {
        "real": quantize_rational(
            sr * tr - si * ti, 1 << 30, fraction=16, width=18, signed=True,
            rounding="nearest_even", overflow="saturate",
        ),
        "imag": quantize_rational(
            sr * ti + si * tr, 1 << 30, fraction=16, width=18, signed=True,
            rounding="nearest_even", overflow="saturate",
        ),
    }
    for mode in ("real", "imag"):
        module_top = (
            "FFTComplexMultiplyRealAuto"
            if mode == "real" else "FFTComplexMultiplyImagAuto"
        )
        for configuration, latency in CONFIGURATIONS:
            typed, graph = _physical_graph(configuration, module_top)
            rtl = tmp_path / f"{mode}_{configuration}.sv"
            rtl.write_text(emit_target(typed.ir, graph, simulation_model=True))
            name = f"dut_{mode}_{configuration}"
            bench = tmp_path / f"tb_{mode}_{configuration}.sv"
            bench.write_text(
                "module tb; logic clk=0,rst=1; "
                f"logic signed [17:0] sample_re=0,sample_im=0; "
                f"logic signed [15:0] twiddle_re=0,twiddle_im=0; "
                f"wire signed [17:0] result; {module_top} {name}(.clk,.rst,"
                ".sample_re,.sample_im,.twiddle_re,.twiddle_im,.result); "
                "task tick; begin #1 clk=1; #1; clk=0; #1; end endtask "
                "initial begin tick; rst=0; "
                f"sample_re={_literal(sr, 18)};sample_im={_literal(si, 18)}; "
                f"twiddle_re={_literal(tr, 16)};twiddle_im={_literal(ti, 16)}; "
                + "tick;" * latency
                + f"if ($signed(result) !== {_literal(expected_model[mode], 18)}) "
                f"$fatal(1,\"{mode}/{configuration} mismatch\"); "
                "$finish; end endmodule\n"
            )
            obj = tmp_path / f"obj_{mode}_{configuration}"
            environment = os.environ.copy()
            environment["CCACHE_DISABLE"] = "1"
            subprocess.run(
                ("verilator", "--binary", "--timing", "-Wno-fatal", "--top-module", "tb",
                 str(rtl), str(bench), "-Mdir", str(obj)),
                check=True, capture_output=True, text=True, env=environment,
            )
            completed = subprocess.run(
                (str(obj / "Vtb"),), check=True, capture_output=True, text=True,
            )
            assert "$finish" in completed.stdout
