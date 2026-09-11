"""Compile-time arrays of children with scalar and ready/valid ports."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles
from zlang.toolchain import lint_with_verilator


SOURCE = r"""
module MixedLane {
    clock clk reset rst
    in bias:u8
    in input:rv<u8>
    out output:rv<u8>
    input.ready=output.ready
    output.valid=input.valid
    output.payload=truncate<8>(input.payload + bias)
}
module MixedLaneArray {
    clock clk reset rst
    in input0:rv<u8> in input1:rv<u8>
    out output0:rv<u8> out output1:rv<u8>
    inst lane[2]:MixedLane
    generate(i in 0..2) { lane[i].bias=1 }
    connect input0 -> lane[0].input
    connect lane[0].output -> output0
    connect input1 -> lane[1].input
    connect lane[1].output -> output1
}
"""


def _module():
    return compile_source(SOURCE, top="MixedLaneArray").ir




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_mixed_array_is_strict_lint_clean(tmp_path: Path) -> None:
    rtl = tmp_path / "MixedLaneArray.sv"
    rtl.write_text(emit_sv_artifact(_module()).text)
    lint_with_verilator((rtl,), "MixedLaneArray")
