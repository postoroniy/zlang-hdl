"""Compile-time arrays of children with scalar and ready/valid ports."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


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
    return compile_source(SOURCE, top="MixedLaneArray", include_clash=False).ir


def test_mixed_port_array_simulates_and_round_trips_artifacts() -> None:
    module = _module()
    cycles = (
        {
            "input0":{"payload":10,"valid":1},
            "input1":{"payload":20,"valid":1},
            "output0":{"ready":0},
            "output1":{"ready":1},
        },
        {
            "input0":{"payload":11,"valid":1},
            "input1":{"payload":21,"valid":1},
            "output0":{"ready":1},
            "output1":{"ready":1},
        },
    )
    trace = simulate_cycles(module, cycles, reset=(False, False))
    assert trace[0]["output0"] == {"payload":11, "valid":1, "transfer":0}
    assert trace[0]["output1"] == {"payload":21, "valid":1, "transfer":1}
    assert trace[1]["output0"] == {"payload":12, "valid":1, "transfer":1}
    assert trace[1]["output1"] == {"payload":22, "valid":1, "transfer":1}

    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert BackendArtifact.from_json(first.to_json()).to_json() == first.to_json()
    assert first.text.count("module MixedLane__") == 1
    assert first.text.count("MixedLane__") == 3
    clash = emit_clash(module)
    assert clash == emit_clash(module)
    assert clash.count("protocol_mixedLane ::") == 1


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_mixed_array_is_strict_lint_clean(tmp_path: Path) -> None:
    rtl = tmp_path / "MixedLaneArray.sv"
    rtl.write_text(emit_sv_artifact(_module()).text)
    lint_with_verilator((rtl,), "MixedLaneArray")


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash or Verilator unavailable",
)
def test_real_clash_mixed_array_is_lint_clean(tmp_path: Path) -> None:
    module = _module()
    files = generate_verilog(
        emit_clash(module),
        module.name,
        tmp_path / "clash",
        find_clash_executable(),
    )
    lint_with_verilator(files, module.name)
