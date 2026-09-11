"""Aggregate wire ports on compile-time instance arrays."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.simulate import simulate
from zlang.toolchain import lint_with_verilator


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
    return compile_source(SOURCE, top="PairLaneArray").ir




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
