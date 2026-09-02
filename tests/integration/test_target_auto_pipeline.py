import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_target
from zlang.compiler import compile_source
from zlang.fixed_point import quantize_rational
from zlang.ir import expressions as expr
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/symmetric_fixed_fir_auto.zhl").read_text()
TARGET = "xc7z030ffg676-1"


def _oracle(samples, coefficients):
    expanded = (*coefficients, *reversed(coefficients))
    return quantize_rational(
        sum(a * b for a, b in zip(samples, expanded, strict=True)),
        1 << 6, fraction=0, width=16, signed=True,
        rounding=expr.FixedRounding.NEAREST_EVEN,
        overflow=expr.FixedOverflow.SATURATE,
    )


def _compile(top):
    return compile_source(
        SOURCE, top=top, target=TARGET,
        target_evidence_policy="measured_required",
    )


def _drive(name, values):
    return "".join(
        f"{name}[{index}]=12'h{value & 0xfff:03x};"
        for index, value in enumerate(values)
    )


def _bench(top, latency):
    vectors = (
        ((0,) * 8, (0,) * 4),
        ((1024,) * 8, (1024,) * 4),
        ((-1024, 512, -256, 128, 64, -32, 16, -8), (256, -128, 64, -32)),
        ((2047,) * 8, (2047,) * 4),
    )
    expected = tuple(_oracle(*item) for item in vectors)
    checks = []
    padded = (*vectors, *((((0,) * 8), ((0,) * 4)) for _ in range(latency - 1)))
    for cycle, (samples, coefficients) in enumerate(padded):
        checks.append(
            f"{_drive('samples', samples)}"
            f"{_drive('coefficients', coefficients)}tick;"
        )
        index = cycle - latency + 1
        if 0 <= index < len(expected):
            value = expected[index]
            literal = f"-16'sd{-value}" if value < 0 else f"16'sd{value}"
            checks.append(
                f"if ($signed(result) !== {literal}) $fatal(1,\"vector {index}: %0d\",$signed(result));"
            )
    return (
        "module tb; logic clk=0,rst=1; logic signed [11:0] samples[0:7]; "
        "logic signed [11:0] coefficients[0:3]; wire signed [15:0] result; "
        f"{top} dut(.clk,.rst,.samples,.coefficients,.result); "
        "task tick; begin #1 clk=1; #1; clk=0; #1; end endtask "
        f"initial begin tick; rst=0; {''.join(checks)} $finish; end endmodule\n"
    )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize(
    "top,latency", (("SymmetricFixedFIRAuto", 3), ("SymmetricFixedFIRAutoExact8", 8)),
)
def test_selected_target_pipeline_is_bit_exact_in_verilator(tmp_path, top, latency):
    result = _compile(top)
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(emit_target(result.ir, result.implementation_graph, simulation_model=True))
    bench = tmp_path / "tb.sv"
    bench.write_text(_bench(top, latency))
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    obj = tmp_path / "obj"
    subprocess.run(
        ("verilator", "--binary", "--timing", "-Wno-fatal", "--top-module", "tb",
         str(rtl), str(bench), "-Mdir", str(obj)),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run((str(obj / "Vtb"),), check=True, capture_output=True, text=True)


@pytest.mark.skipif(find_clash_executable() is None, reason="Clash unavailable")
def test_targetless_generic_auto_pipeline_still_generates_clash(tmp_path):
    result = compile_source(SOURCE, top="SymmetricFixedFIRAuto")
    assert result.implementation_graph.is_generic
    assert generate_verilog(result.clash, "SymmetricFixedFIRAuto", tmp_path / "rtl")
