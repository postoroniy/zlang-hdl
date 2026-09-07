"""Aggregate wire ports on compile-time instance arrays."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.simulate import simulate
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


SOURCE = r"""
struct Pair { left:u8 right:u8 }
module PairLane {
    in x:u8
    out y:Pair
    y=Pair { left=x right=x }
}
module PairLaneArray {
    in x:vec<2,u8>
    out y:vec<2,Pair>
    inst lane[2]:PairLane
    generate(i in 0..2) { lane[i].x=x[i] }
    y=generate(i in 0..2) lane[i].y
}
"""


def _module():
    return compile_source(SOURCE, top="PairLaneArray", include_clash=False).ir


def test_simulator_and_artifacts_preserve_sequence_and_struct_members() -> None:
    module = _module()
    assert simulate(module, x=(0x12, 0x34)) == {
        "y": [
            {"left": 0x12, "right": 0x12},
            {"left": 0x34, "right": 0x34},
        ]
    }
    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert BackendArtifact.from_json(first.to_json()).to_json() == first.to_json()
    assert first.text.count("module PairLane_s") == 1
    assert first.text.count("PairLane_s") == 3
    assert "input wire logic [7:0] x [0:1]" in first.text
    assert "output logic [7:0] y_left [0:1]" in first.text
    assert emit_clash(module) == emit_clash(module)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_lints_and_simulates_aggregate_leaves(tmp_path: Path) -> None:
    rtl = tmp_path / "PairLaneArray.sv"
    rtl.write_text(emit_sv_artifact(_module()).text)
    bench = tmp_path / "tb.sv"
    bench.write_text(r"""
module tb;
  logic [7:0] x [0:1];
  logic [7:0] y_left [0:1];
  logic [7:0] y_right [0:1];
  PairLaneArray dut(.*);
  initial begin
    x[0]=8'h12; x[1]=8'h34; #1;
    if (y_left[0]!=8'h12 || y_right[0]!=8'h12 ||
        y_left[1]!=8'h34 || y_right[1]!=8'h34) $fatal(1,"aggregate order");
    $finish;
  end
endmodule
""")
    object_dir = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL",
            str(rtl), str(bench), "--Mdir", str(object_dir),
        ),
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / "Vtb"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash or Verilator unavailable",
)
def test_real_clash_generates_lint_clean_aggregate_array_rtl(tmp_path: Path) -> None:
    module = _module()
    files = generate_verilog(
        emit_clash(module),
        module.name,
        tmp_path / "clash",
        find_clash_executable(),
    )
    lint_with_verilator(files, module.name)
