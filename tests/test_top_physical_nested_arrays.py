"""Focused evidence for nested native-array public top boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang import compile_source
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.toolchain import lint_with_verilator


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


def test_nested_vector_and_vec_struct_layout_is_msb_first() -> None:
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
        ((0, 0), 41, 38), ((0, 1), 37, 34), ((0, 2), 33, 30),
        ((1, 0), 29, 26), ((1, 1), 25, 22), ((1, 2), 21, 18),
    )
    lane_data = leaves["port:x.lanes.data"]
    lane_valid = leaves["port:x.lanes.valid"]
    assert lane_data.array_dimensions == (2,)
    assert tuple(
        (item.indices, item.msb, item.lsb)
        for item in lane_data.packed_element_slices
    ) == (((0,), 17, 10), ((1,), 8, 1))
    assert tuple(
        (item.indices, item.msb, item.lsb)
        for item in lane_valid.packed_element_slices
    ) == (((0,), 9, 9), ((1,), 0, 0))








NESTED_BENCH = r"""
module tb;
  logic [3:0] x_matrix [0:1] [0:2];
  logic [7:0] x_lanes_data [0:1];
  logic x_lanes_valid [0:1];
  wire [3:0] y_matrix [0:1] [0:2];
  wire [7:0] y_lanes_data [0:1];
  wire y_lanes_valid [0:1];
  NestedArrayTop dut(.*);
  initial begin
    x_matrix[0][0]=1;x_matrix[0][1]=2;x_matrix[0][2]=3;
    x_matrix[1][0]=4;x_matrix[1][1]=5;x_matrix[1][2]=6;
    x_lanes_data[0]=8'h12;x_lanes_valid[0]=1;
    x_lanes_data[1]=8'ha5;x_lanes_valid[1]=0;
    #1;
    if (y_matrix[0][0]!==1 || y_matrix[0][1]!==2 || y_matrix[0][2]!==3 ||
        y_matrix[1][0]!==4 || y_matrix[1][1]!==5 || y_matrix[1][2]!==6)
      $fatal(1,"nested vector ordering");
    if (y_lanes_data[0]!==8'h12 || y_lanes_valid[0]!==1 ||
        y_lanes_data[1]!==8'ha5 || y_lanes_valid[1]!==0)
      $fatal(1,"vector-of-struct ordering");
    $finish;
  end
endmodule
"""
