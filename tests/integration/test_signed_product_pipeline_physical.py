"""Direct-SV physical validation for the frozen scalar FFT reduction slice."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_target, emit_target_artifact
from zlang.compiler import compile_source
from zlang.fixed_point import quantize_rational
from zlang.targets import (
    load_architecture_templates,
    load_target,
    map_auto_signed_product_configuration,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/fft/complex_multiply_pipeline_auto.zhl").read_text()
TARGET = "xc7z030ffg676-1"
CONFIGURATIONS = (
    ("unregistered", 1),
    ("multiply_registered", 2),
    ("multiply_output_registered", 3),
    ("fully_pipelined", 4),
)


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
