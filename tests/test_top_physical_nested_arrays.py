"""Focused evidence for nested packed-array public top boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang import compile_source
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact


NESTED_SOURCE = """
struct Lane { data:u8 valid:bit }
struct Container {
    matrix:vec<2,vec<3,u4>>
    lanes:vec<2,Lane>
}
module NestedArrayTop {
    in x:Container
    out y:Container
    y=x
}
"""


RV_SOURCE = """
struct Lane { data:u8 valid:bit }
module RvArrayTop {
    in x:rv<vec<2,Lane>>
    out y:rv<vec<2,Lane>>
    y.payload=x.payload
    y.valid=x.valid
    x.ready=y.ready
}
"""


RR_SOURCE = """
struct Lane { data:u8 valid:bit }
struct Request { id:u2 data:vec<2,vec<3,u4>> }
struct Response { id:u2 lanes:vec<2,Lane> }
module RequestResponseArrayTop {
    clock clk reset rst
    interface mem:request_response<Request,Response> {
        max_outstanding 2
        ordering out_of_order
        match_by id
    }
    in request:Request
    in issue:bit
    in accept:bit
    out response:Response
    mem.request.payload=request
    mem.request.valid=issue
    mem.response.ready=accept
    response=mem.response.payload
}
"""


RESERVED_VECTOR_FIELDS = """
struct ReservedLane { module:u8 zlang_module:u8 }
module ReservedVectorFieldsTop {
    in x:vec<2,ReservedLane>
    out y:vec<2,ReservedLane>
    y=x
}
"""


def _bindings(artifact: BackendArtifact) -> dict[str, object]:
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.to_json() == artifact.to_json()
    return {
        binding.semantic_signal_id: binding for binding in restored.bindings
    }


def test_nested_vectors_are_lsb_first_while_struct_fields_remain_msb_first() -> None:
    module = compile_source(NESTED_SOURCE).ir
    leaves = {
        leaf.leaf_semantic_id: leaf for leaf in module.top_physical_abi.leaves
    }

    matrix = leaves["port:x.matrix"]
    assert matrix.external_name == "x_matrix"
    assert matrix.array_dimensions == (2, 3)
    assert tuple(
        (item.indices, item.msb, item.lsb)
        for item in matrix.packed_element_slices
    ) == (
        ((0, 0), 21, 18), ((0, 1), 25, 22), ((0, 2), 29, 26),
        ((1, 0), 33, 30), ((1, 1), 37, 34), ((1, 2), 41, 38),
    )
    lane_data = leaves["port:x.lanes.data"]
    lane_valid = leaves["port:x.lanes.valid"]
    assert lane_data.array_dimensions == (2,)
    assert tuple(
        (item.indices, item.msb, item.lsb)
        for item in lane_data.packed_element_slices
    ) == (((0,), 8, 1), ((1,), 17, 10))
    assert tuple(
        (item.indices, item.msb, item.lsb)
        for item in lane_valid.packed_element_slices
    ) == (((0,), 0, 0), ((1,), 9, 9))








NESTED_BENCH = r"""
module tb;
  logic [1:0][2:0][3:0] x_matrix;
  logic [1:0][7:0] x_lanes_data;
  logic [1:0] x_lanes_valid;
  wire [1:0][2:0][3:0] y_matrix;
  wire [1:0][7:0] y_lanes_data;
  wire [1:0] y_lanes_valid;
  NestedArrayTop dut(.*);
  initial begin
    x_matrix[1][2]=1;x_matrix[1][1]=2;x_matrix[1][0]=3;
    x_matrix[0][2]=4;x_matrix[0][1]=5;x_matrix[0][0]=6;
    x_lanes_data[1]=8'h12;x_lanes_valid[1]=1;
    x_lanes_data[0]=8'ha5;x_lanes_valid[0]=0;
    #1;
    if (y_matrix[1][2]!==1 || y_matrix[1][1]!==2 || y_matrix[1][0]!==3 ||
        y_matrix[0][2]!==4 || y_matrix[0][1]!==5 || y_matrix[0][0]!==6)
      $fatal(1,"nested vector ordering");
    if (y_lanes_data[1]!==8'h12 || y_lanes_valid[1]!==1 ||
        y_lanes_data[0]!==8'ha5 || y_lanes_valid[0]!==0)
      $fatal(1,"vector-of-struct ordering");
    $finish;
  end
endmodule
"""


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_nested_vec_struct_inline_boundary_preserves_exact_slices(
    tmp_path: Path,
) -> None:
    module = compile_source(NESTED_SOURCE).ir
    artifact = emit_artifact(module)
    assert artifact.text.count("module NestedArrayTop (") == 1
    assert "NestedArrayTop_zlang_core" not in artifact.text
    assert "assign zlang_packed_x[17:10] = x_lanes_data[1];" in artifact.text
    assert "assign zlang_packed_x[9] = x_lanes_valid[1];" in artifact.text
    assert "assign zlang_packed_x[8:1] = x_lanes_data[0];" in artifact.text
    assert "assign zlang_packed_x[0] = x_lanes_valid[0];" in artifact.text

    rtl = tmp_path / "NestedArrayTop.sv"
    bench = tmp_path / "tb.sv"
    rtl.write_text(artifact.text)
    bench.write_text(NESTED_BENCH)
    obj = tmp_path / "obj"
    compiled = subprocess.run(
        (
            "verilator", "--binary", "--timing", "-Wno-fatal",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--top-module", "tb", "--Mdir", str(obj), str(rtl), str(bench),
        ),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CCACHE_DISABLE": "1"},
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    executed = subprocess.run(
        (str(obj / "Vtb"),), capture_output=True, text=True, check=False,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
