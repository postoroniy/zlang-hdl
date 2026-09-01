from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.clash.public_wrapper import (
    ClashPublicTopWrapper,
    bind_artifact_to_public_wrapper,
)
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.parser import parse
from zlang.simulate import simulate
from zlang.toolchain import generate_verilog, lint_with_verilator


COMPLEX_SUM_SOURCE = """
import std.math.complex

module ComplexSumRTL {
    in x : vec<4,Complex<s8>>
    out y : Complex<s10>
    y = sum(x)
}
"""


GENERATED_GENERIC_SOURCE = """
fn helper<K>() -> u4 { K + 1 }

module GeneratedGenericRTL {
    out y : vec<4,u4>
    y = generate(k in 0..4) helper<K=k>()
}
"""


COMPLEX_SUM_BENCH = r"""
module tb;
  logic signed [7:0] x_re [0:3];
  logic signed [7:0] x_im [0:3];
  wire signed [9:0] y_re;
  wire signed [9:0] y_im;
  ComplexSumRTL dut(.x_re, .x_im, .y_re, .y_im);

  initial begin
    x_re = '{8'sd1, 8'sd3, -8'sd5, 8'sd7};
    x_im = '{8'sd2, 8'sd4, 8'sd6, -8'sd8};
    #1;
    if ($signed(y_re) !== 10'sd6) $fatal(1, "real sum");
    if ($signed(y_im) !== 10'sd4) $fatal(1, "imaginary sum");
    $finish;
  end
endmodule
"""


GENERATED_GENERIC_BENCH = r"""
module tb;
  wire [3:0] y [0:3];
  GeneratedGenericRTL dut(.y);
  initial begin
    #1;
    if (y[0] !== 4'h1 || y[1] !== 4'h2 ||
        y[2] !== 4'h3 || y[3] !== 4'h4)
      $fatal(1, "generated specialization constants");
    $finish;
  end
endmodule
"""


ROOT = Path(__file__).resolve().parents[2]
IFFT_COMPLEX_REDUCTION_SOURCE = (
    ROOT / "docs/reproducers/ifft_complex_reduce.zl"
).read_text()
IFFT64_SCALABILITY_SOURCE = (
    ROOT / "docs/reproducers/ifft64_whole_vector_elaboration.zl"
).read_text()


def _verilate_and_run(
    tmp_path: Path,
    rtl: tuple[Path, ...],
    bench_text: str,
    suffix: str,
) -> None:
    bench = tmp_path / f"tb_{suffix}.sv"
    object_dir = tmp_path / f"obj_{suffix}"
    bench.write_text(bench_text)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator",
            "--binary",
            "--top-module",
            "tb",
            "-Wno-fatal",
            "--Mdir",
            str(object_dir),
            *(str(path) for path in rtl),
            str(bench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / "Vtb"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_small_complex_nominal_sum_is_bit_exact_in_semantic_simulation() -> None:
    module = compile_source(COMPLEX_SUM_SOURCE, include_clash=False).ir
    result = simulate(
        module,
        x=(
            {"re": 1, "im": 2},
            {"re": 3, "im": 4},
            {"re": -5, "im": 6},
            {"re": 7, "im": -8},
        ),
    )
    assert result == {"y": {"re": 6, "im": 4}}


def test_ifft64_scalability_reproducer_keeps_the_nested_source_concise() -> None:
    module = parse(IFFT64_SCALABILITY_SOURCE)
    assert module.name == "IFFT64WholeVectorElaboration"
    assert len(IFFT64_SCALABILITY_SOURCE.splitlines()) < 80
    assert "generate(n in 0..64)" in IFFT64_SCALABILITY_SOURCE
    assert "sum(generate(k in 0..N)" in IFFT64_SCALABILITY_SOURCE


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize(
    ("source", "top", "bench", "suffix"),
    (
        (COMPLEX_SUM_SOURCE, "ComplexSumRTL", COMPLEX_SUM_BENCH, "complex_sum_sv"),
        (
            GENERATED_GENERIC_SOURCE,
            "GeneratedGenericRTL",
            GENERATED_GENERIC_BENCH,
            "generated_generic_sv",
        ),
    ),
)
def test_direct_sv_nominal_sum_and_generated_generic_are_strict_and_bit_exact(
    tmp_path: Path,
    source: str,
    top: str,
    bench: str,
    suffix: str,
) -> None:
    artifact = emit_artifact(compile_source(source, include_clash=False).ir)
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), top)
    _verilate_and_run(tmp_path, (rtl,), bench, suffix)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_ifft_complex_reduction_handles_negative_twiddles_and_reserved_ports(
    tmp_path: Path,
) -> None:
    module = compile_source(
        IFFT_COMPLEX_REDUCTION_SOURCE, include_clash=False
    ).ir
    artifact = emit_artifact(module)
    assert "16'sd-" not in artifact.text
    assert "assign output =" not in artifact.text
    bindings = {
        item.semantic_signal_id: item.rtl_path for item in artifact.bindings
    }
    assert bindings["port:input"] == ""
    assert bindings["port:output"] == ""
    assert bindings["port:input.re"] == "input_re"
    assert bindings["port:input.im"] == "input_im"
    assert bindings["port:output.re"] == "output_re"
    assert bindings["port:output.im"] == "output_im"
    rtl = tmp_path / "IFFTComplexReductionBlocker.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "IFFTComplexReductionBlocker")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(
    ("source", "top", "bench", "suffix"),
    (
        (
            COMPLEX_SUM_SOURCE,
            "ComplexSumRTL",
            COMPLEX_SUM_BENCH,
            "complex_sum_clash",
        ),
        (
            GENERATED_GENERIC_SOURCE,
            "GeneratedGenericRTL",
            GENERATED_GENERIC_BENCH,
            "generated_generic_clash",
        ),
    ),
)
def test_real_clash_nominal_sum_and_generated_generic_are_strict_and_bit_exact(
    tmp_path: Path,
    source: str,
    top: str,
    bench: str,
    suffix: str,
) -> None:
    compilation = compile_source(source)
    rtl = generate_verilog(
        compilation.clash,
        top,
        tmp_path / f"{suffix}_rtl",
        CLASH_EXECUTABLE,
        public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
    )
    lint_with_verilator(rtl, top)
    _verilate_and_run(tmp_path, tuple(rtl), bench, suffix)


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_ifft_complex_reduction_mangles_reserved_rtl_ports(
    tmp_path: Path,
) -> None:
    compilation = compile_source(IFFT_COMPLEX_REDUCTION_SOURCE)
    wrapper = ClashPublicTopWrapper.build(compilation.ir)
    artifact = bind_artifact_to_public_wrapper(
        emit_clash_artifact(compilation.ir), wrapper
    )
    bindings = {
        item.semantic_signal_id: item.rtl_path for item in artifact.bindings
    }
    assert bindings["port:input"] == ""
    assert bindings["port:output"] == ""
    assert bindings["port:input.re"] == "input_re"
    assert bindings["port:input.im"] == "input_im"
    assert bindings["port:output.re"] == "output_re"
    assert bindings["port:output.im"] == "output_im"
    rtl = generate_verilog(
        compilation.clash,
        "IFFTComplexReductionBlocker",
        tmp_path / "ifft_complex_reduction_clash_rtl",
        CLASH_EXECUTABLE,
        public_wrapper=wrapper,
    )
    lint_with_verilator(rtl, "IFFTComplexReductionBlocker")
