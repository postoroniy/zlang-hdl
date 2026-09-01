"""Typed callable-body materialization across direct SV and Clash."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file, compile_source
from zlang.toolchain import generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
MAPPER = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "mapper.zl"
)


FIXED_SOURCE = """
fn quantized(
    a : fixed<24,16>,
    b : fixed<24,16>,
    c : fixed<24,16>,
    d : fixed<24,16>,
    e : fixed<24,16>
) -> fixed_sat<16,8> {
    quantize<fixed_sat<16,8>>((a + b) + (c + d) + e) {
        round nearest_even
        overflow saturate
    }
}

module CallableFixedMaterialization {
    in a : fixed<24,16>
    in b : fixed<24,16>
    in c : fixed<24,16>
    in d : fixed<24,16>
    in e : fixed<24,16>
    out y : fixed_sat<16,8>
    y = quantized(a,b,c,d,e)
}
"""


def _sv_function(text: str, name: str) -> str:
    start = text.index("function automatic", text.index(name) - 80)
    return text[start : text.index("endfunction", start)]


def _clash_function(text: str, name: str, next_name: str) -> str:
    start = text.index(f"{name} ::")
    return text[start : text.index(f"\n{next_name} ::", start)]


def test_fixed_convert_input_is_named_once_inside_callable_body() -> None:
    compilation = compile_source(FIXED_SOURCE)
    sv = emit_sv_artifact(compilation.ir).text
    sv_helper = _sv_function(sv, "quantized")
    assert sv_helper.count("logic signed [26:0] zlang_fn_expr_0;") == 1
    assert sv_helper.count("zlang_fn_expr_0 =") == 1
    assert "quantized =" in sv_helper
    assert sv_helper.count("zlang_fn_expr_0") > 2

    clash_helper = _clash_function(compilation.clash, "quantized", "topEntity")
    assert clash_helper.count("zlang_fn_expr_0 :: Signed 27") == 1
    assert clash_helper.count("zlang_fn_expr_0 =") == 1
    assert "quantized a b c d e =" in clash_helper


def test_mapper_frame_call_is_materialized_once_in_each_callable_helper(
    tmp_path: Path,
) -> None:
    compilation = compile_file(MAPPER, top="IeeeMapperFrame64")

    sv_artifact = emit_sv_artifact(compilation.ir)
    assert sv_artifact.text == emit_sv_artifact(compilation.ir).text
    sv_helper = _sv_function(sv_artifact.text, "ieee_mapper_frame")
    assert sv_helper.count("mapper_frame48(") == 1
    assert sv_helper.count("logic [2047:0] zlang_fn_expr_0;") == 1
    assert sv_helper.count("zlang_fn_expr_0 =") == 1
    direct_rtl = tmp_path / "IeeeMapperFrame64.sv"
    direct_rtl.write_text(sv_artifact.text)
    lint_with_verilator((direct_rtl,), "IeeeMapperFrame64")

    clash_helper = _clash_function(
        compilation.clash,
        "ieee_mapper_frame",
        "topEntity",
    )
    assert clash_helper.count("mapper_frame48") == 1
    assert clash_helper.count(
        "zlang_fn_expr_0 :: Vec 64 (ComplexRaw16)"
    ) == 1
    assert clash_helper.count("zlang_fn_expr_0 =") == 1


@pytest.mark.skipif(CLASH_EXECUTABLE is None, reason="real Clash 1.11 is unavailable")
def test_mapper_materialized_callable_compiles_with_real_clash(
    tmp_path: Path,
) -> None:
    compilation = compile_file(MAPPER, top="IeeeMapperFrame64")
    rtl = tuple(
        generate_verilog(
            compilation.clash,
            "IeeeMapperFrame64",
            tmp_path / "clash_rtl",
            CLASH_EXECUTABLE,
        )
    )
    assert rtl
    lint_with_verilator(rtl, "IeeeMapperFrame64")
