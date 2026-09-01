from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang import compile_source
from zlang.backend.clash import emit_artifact
from zlang.backend.clash.public_wrapper import (
    ClashPublicTopWrapper,
    bind_artifact_to_public_wrapper,
)
from zlang.backend.manifest import BackendArtifact
from zlang.toolchain import generate_verilog, lint_with_verilator


COMBINATIONAL_SOURCE = """
struct Pair { left:u8 right:u8 }

module ScalarOutputs {
    in x:u8
    in lanes:vec<2,u8>
    out sum:u9
    out echoed:vec<2,u8>
    out pair:Pair
    sum=x+x
    echoed=lanes
    pair=Pair { left=x right=lanes[1] }
}
"""


SEQUENTIAL_SOURCE = """
struct Snapshot { held:u8 current:u8 }

module StatefulOutputs {
    clock clk
    reset rst
    in x:u8
    in enable:bit
    out held:u8
    out accepted:u8
    out snapshot:Snapshot
    reg q:u8=0
    when enable {
        q <- x
        accepted <- x
    }
    held=q
    snapshot=Snapshot { held=q current=x }
}
"""


@pytest.mark.parametrize(
    ("source", "expected_type", "expected_annotation"),
    (
        (
            "module EmptyScalarTop { in x:u8 }",
            "topEntity :: Unsigned 8 -> ()",
            't_output = PortProduct "" []',
        ),
        (
            "module OneScalarTop { in x:u8 out y:u8 y=x }",
            "topEntity :: Unsigned 8 -> Unsigned 8",
            't_output = PortName "y"',
        ),
        (
            "module TwoScalarTop { in x:u8 out y:u8 out z:u9 y=x z=x+x }",
            "topEntity :: Unsigned 8 -> (Unsigned 8, Unsigned 9)",
            't_output = PortProduct "" [PortName "y", PortName "z"]',
        ),
    ),
)
def test_scalar_top_result_arity_is_deterministic(
    source: str,
    expected_type: str,
    expected_annotation: str,
) -> None:
    compilation = compile_source(source)
    assert expected_type in compilation.clash
    assert expected_annotation in compilation.clash
    assert compilation.clash == compile_source(source).clash


def test_multiple_outputs_preserve_physical_leaf_bindings() -> None:
    compilation = compile_source(COMBINATIONAL_SOURCE)
    wrapper = ClashPublicTopWrapper.build(compilation.ir)
    artifact = bind_artifact_to_public_wrapper(
        emit_artifact(compilation.ir), wrapper
    )
    restored = BackendArtifact.from_json(artifact.to_json())
    bindings = {
        binding.semantic_signal_id: binding
        for binding in restored.bindings
    }

    assert (
        't_output = PortProduct "" [PortName "sum", PortName "echoed", '
        'PortProduct "pair" [PortName "left", PortName "right"]]'
    ) in artifact.text
    for semantic, path in (
        ("port:sum", "sum"),
        ("port:echoed", "echoed"),
        ("port:pair.left", "pair_left"),
        ("port:pair.right", "pair_right"),
    ):
        binding = bindings[semantic]
        assert binding.rtl_path == path
        assert binding.physical_available


def test_combinational_scalar_hierarchy_uses_the_same_top_product_abi() -> None:
    source = """
module Child { in x:u8 out y:u8 y=x }
module HierarchyOutputs {
    in x:u8
    out a:u8
    out b:u9
    inst child:Child { x }
    a=child.y
    b=child.y+1
}
"""
    clash = compile_source(source, top="HierarchyOutputs").clash
    assert "child x = x" in clash
    assert "topEntity :: Unsigned 8 -> (Unsigned 8, Unsigned 9)" in clash
    assert "topEntity x = (child_y," in clash
    assert (
        't_output = PortProduct "" [PortName "a", PortName "b"]'
        in clash
    )


def test_sequential_multiple_outputs_share_one_state_and_rule_schedule() -> None:
    clash = compile_source(SEQUENTIAL_SOURCE).clash
    assert (
        "circuit x enable = (held, accepted, snapshot)" in clash
    )
    assert "q = register (0 :: Unsigned 8) (q_next)" in clash
    assert "accepted =" in clash
    assert (
        't_output = PortProduct "" [PortName "held", PortName "accepted", '
        'PortProduct "snapshot" [PortName "held", PortName "current"]]'
    ) in clash


def _run_verilator(
    tmp_path: Path,
    rtl: tuple[Path, ...],
    bench_text: str,
) -> None:
    bench = tmp_path / "tb.sv"
    bench.write_text(bench_text)
    object_dir = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "--Mdir", str(object_dir), *(str(path) for path in rtl), str(bench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    ran = subprocess.run(
        (str(object_dir / "Vtb"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stderr or ran.stdout


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_multiple_combinational_outputs_and_public_wrapper(
    tmp_path: Path,
) -> None:
    compilation = compile_source(COMBINATIONAL_SOURCE)
    rtl = generate_verilog(
        compilation.clash,
        compilation.ir.name,
        tmp_path / "rtl",
        CLASH_EXECUTABLE,
        public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
    )
    lint_with_verilator(rtl, compilation.ir.name)
    _run_verilator(
        tmp_path,
        rtl,
        """
`timescale 1ns/1ps
module tb;
  logic [7:0] x;
  logic [7:0] lanes [0:1];
  wire [8:0] sum;
  wire [7:0] echoed [0:1];
  wire [7:0] pair_left;
  wire [7:0] pair_right;
  ScalarOutputs dut(.*);
  initial begin
    x=8'h81; lanes[0]=8'h12; lanes[1]=8'ha5; #1;
    if (sum !== 9'h102) $fatal(1, "sum");
    if (echoed[0] !== 8'h12 || echoed[1] !== 8'ha5)
      $fatal(1, "vector output order");
    if (pair_left !== 8'h81 || pair_right !== 8'ha5)
      $fatal(1, "struct output leaves");
    $finish;
  end
endmodule
""",
    )


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_real_clash_register_and_rule_outputs_are_cycle_accurate(
    tmp_path: Path,
) -> None:
    compilation = compile_source(SEQUENTIAL_SOURCE)
    rtl = generate_verilog(
        compilation.clash,
        compilation.ir.name,
        tmp_path / "rtl",
        CLASH_EXECUTABLE,
        public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
    )
    lint_with_verilator(rtl, compilation.ir.name)
    _run_verilator(
        tmp_path,
        rtl,
        """
`timescale 1ns/1ps
module tb;
  logic clk=0;
  logic rst=1;
  logic [7:0] x=0;
  logic enable=0;
  wire [7:0] held;
  wire [7:0] accepted;
  wire [7:0] snapshot_held;
  wire [7:0] snapshot_current;
  StatefulOutputs dut(.*);
  always #5 clk=~clk;
  initial begin
    repeat (2) @(posedge clk);
    #1;
    if (held !== 0 || accepted !== 0 || snapshot_held !== 0)
      $fatal(1, "reset");
    rst=0; x=8'h35; enable=1;
    @(posedge clk); #1;
    if (held !== 8'h35 || accepted !== 8'h35)
      $fatal(1, "accepted rule update");
    if (snapshot_held !== 8'h35 || snapshot_current !== 8'h35)
      $fatal(1, "snapshot");
    x=8'h9a; enable=0;
    @(posedge clk); #1;
    if (held !== 8'h35 || accepted !== 0)
      $fatal(1, "hold and rule-output default");
    if (snapshot_held !== 8'h35 || snapshot_current !== 8'h9a)
      $fatal(1, "mixed state/current output");
    rst=1;
    @(posedge clk); #1;
    if (held !== 0 || accepted !== 0 || snapshot_held !== 0)
      $fatal(1, "mid-stream reset");
    $finish;
  end
endmodule
""",
    )


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(
    "source",
    (
        "module EmptyScalarTop { in x:u8 }",
        "module EmptyStatefulTop { clock clk reset rst in x:u8 reg q:u8=0 q<-x }",
    ),
)
def test_real_clash_zero_output_unit_abi_lints(
    tmp_path: Path,
    source: str,
) -> None:
    compilation = compile_source(source)
    rtl = generate_verilog(
        compilation.clash,
        compilation.ir.name,
        tmp_path / compilation.ir.name,
        CLASH_EXECUTABLE,
        public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
    )
    lint_with_verilator(rtl, compilation.ir.name)
