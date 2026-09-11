"""Typed callable-body materialization in production direct SystemVerilog."""

from __future__ import annotations

from pathlib import Path

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file, compile_source
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
MAPPER = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "mapper.zhl"
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
