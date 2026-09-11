import shutil
import subprocess
import tempfile
import os
from pathlib import Path

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.simulate import simulate
from zlang.timing import timing_info


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/complex_fft_butterfly.zhl").read_text()
PIPELINE_SOURCE = (
    ROOT / "examples/fft/complex_multiply_pipeline_auto.zhl"
).read_text()


def test_complex_fft_butterfly_is_bit_exact_for_cardinal_twiddles() -> None:
    module = compile_source(SOURCE, top="ComplexFFTButterfly").ir
    one = 1 << 16
    half = 1 << 15
    twiddle_one = 1 << 14
    vectors = (
        (
            {"re": one, "im": 0}, {"re": half, "im": 0},
            {"re": twiddle_one, "im": 0},
            {"sum": {"re": one + half, "im": 0},
             "difference": {"re": half, "im": 0}},
        ),
        (
            {"re": half, "im": -half}, {"re": half, "im": half},
            {"re": 0, "im": -twiddle_one},
            {"sum": {"re": one, "im": -one},
             "difference": {"re": 0, "im": 0}},
        ),
    )
    for a, b, twiddle, expected in vectors:
        assert simulate(module, a=a, b=b, twiddle=twiddle)["result"] == expected


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_complex_butterfly_direct_sv_is_lint_clean_and_materialized() -> None:
    module = compile_source(SOURCE, top="ComplexFFTButterfly").ir
    text = emit_experimental(module)
    assert len(text) < 20_000
    assert "function automatic" in text
    assert "zlang_spec_" in text
    with tempfile.TemporaryDirectory() as temporary:
        rtl = Path(temporary) / "ComplexFFTButterfly.sv"
        rtl.write_text(text)
        subprocess.run(
            ("verilator", "--lint-only", "-Wall", "-Wno-fatal", str(rtl)),
            check=True, capture_output=True, text=True,
        )




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_signed_complex_real_pipeline_direct_sv_is_lint_clean() -> None:
    module = compile_source(
        PIPELINE_SOURCE, top="FFTComplexMultiplyRealAuto"
    ).ir
    text = emit_experimental(module)
    assert " - " in text
    with tempfile.TemporaryDirectory() as temporary:
        rtl = Path(temporary) / "FFTComplexMultiplyRealAuto.sv"
        rtl.write_text(text)
        subprocess.run(
            ("verilator", "--lint-only", "-Wall", "-Wno-fatal", str(rtl)),
            check=True, capture_output=True, text=True,
        )




def _signed_pipeline_bench(latency: int) -> str:
    wait = "tick; " * latency
    return rf"""
module tb;
  logic clk=0, rst=1;
  logic signed [17:0] sample_re=0, sample_im=0;
  logic signed [15:0] twiddle_re=0, twiddle_im=0;
  wire signed [17:0] result;
  FFTComplexMultiplyRealAuto dut(
    .clk,.rst,.sample_re,.sample_im,.twiddle_re,.twiddle_im,.result
  );
  task tick; begin #1 clk=1; #1; clk=0; #1; end endtask
  initial begin
    tick; rst=0;
    sample_re=18'h10000; sample_im=18'h00000;
    twiddle_re=16'h4000; twiddle_im=16'h0000;
    {wait}if ($signed(result) !== 18'sd65536) $fatal(1,"unit twiddle");
    sample_re=18'h08000; sample_im=18'h04000;
    twiddle_re=16'h2000; twiddle_im=16'h1000;
    {wait}if ($signed(result) !== 18'sd12288) $fatal(1,"signed products");
    $finish;
  end
endmodule
"""


def _simulate_rtl(files: list[Path], root: Path, *, latency: int) -> None:
    bench = root / "tb.sv"
    bench.write_text(_signed_pipeline_bench(latency))
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        ("verilator", "--binary", "--top-module", "tb", "-Wno-fatal",
         *map(str, files), str(bench), "-Mdir", str(root / "obj")),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run(
        (str(root / "obj" / "Vtb"),), check=True,
        capture_output=True, text=True,
    )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_signed_complex_real_pipeline_direct_sv_simulates_bit_exact() -> None:
    module = compile_source(
        PIPELINE_SOURCE, top="FFTComplexMultiplyRealAuto"
    ).ir
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / "direct.sv"
        rtl.write_text(emit_experimental(module))
        assignment = next(item for item in module.assignments if item.target.name == "result")
        _simulate_rtl([rtl], root, latency=timing_info(assignment.expression).latency)
