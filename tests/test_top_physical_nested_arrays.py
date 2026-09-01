"""Focused evidence for nested native-array public top boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang import compile_source
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.clash.public_wrapper import (
    ClashPublicTopWrapper,
    bind_artifact_to_public_wrapper,
)
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.toolchain import generate_verilog, lint_with_verilator


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


def test_both_wrappers_publish_nested_arrays_with_identical_order() -> None:
    module = compile_source(NESTED_SOURCE).ir
    direct = emit_sv_artifact(module).text
    clash = ClashPublicTopWrapper.build(module).text

    declarations = (
        (
            "input wire logic [3:0] x_matrix [0:1] [0:2]",
            "input wire [3:0] x_matrix [0:1] [0:2]",
        ),
        (
            "input wire logic [7:0] x_lanes_data [0:1]",
            "input wire [7:0] x_lanes_data [0:1]",
        ),
        (
            "output logic [3:0] y_matrix [0:1] [0:2]",
            "output wire [3:0] y_matrix [0:1] [0:2]",
        ),
    )
    for direct_declaration, clash_declaration in declarations:
        assert direct_declaration in direct
        assert clash_declaration in clash
    gather = (
        "x_matrix[0][0], x_matrix[0][1], x_matrix[0][2], "
        "x_matrix[1][0], x_matrix[1][1], x_matrix[1][2]"
    )
    assert gather in direct
    assert gather in clash
    assert "assign y_matrix[0][0] = zlang_top_core_y[41:38];" in direct
    assert "assign y_matrix[0][0] = zlang_top_core_y_matrix[23:20];" in clash


def test_protocol_array_leaves_and_artifact_json_are_backend_identical() -> None:
    for source, expected in (
        (
            RV_SOURCE,
            {
                "port:x.payload.data": ("x_payload_data", "vec<2,u8>"),
                "port:x.payload.valid": ("x_payload_valid", "vec<2,bit>"),
                "port:y.payload.data": ("y_payload_data", "vec<2,u8>"),
            },
        ),
        (
            RR_SOURCE,
            {
                "port:mem.request.payload.data": (
                    "mem_request_payload_data", "vec<2,vec<3,u4>>"
                ),
                "port:mem.response.payload.lanes.data": (
                    "mem_response_payload_lanes_data", "vec<2,u8>"
                ),
                "port:mem.response.payload.lanes.valid": (
                    "mem_response_payload_lanes_valid", "vec<2,bit>"
                ),
            },
        ),
    ):
        module = compile_source(source).ir
        direct = _bindings(emit_sv_artifact(module))
        clash = _bindings(bind_artifact_to_public_wrapper(
            emit_clash_artifact(module), ClashPublicTopWrapper.build(module)
        ))
        for semantic, (path, canonical_type) in expected.items():
            for bindings in (direct, clash):
                binding = bindings[semantic]
                assert binding.rtl_path == path
                assert binding.physical_available
                assert binding.canonical_type == canonical_type


def test_vector_reserved_field_names_and_private_names_do_not_collide() -> None:
    module = compile_source(RESERVED_VECTOR_FIELDS).ir
    direct = emit_sv_artifact(module).text
    clash = ClashPublicTopWrapper.build(module).text
    for name in ("x_module", "x_zlang_module", "y_module", "y_zlang_module"):
        assert name in direct
        assert name in clash
    assert direct == emit_sv_artifact(module).text
    assert clash == ClashPublicTopWrapper.build(module).text


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


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize("backend", ("direct", "clash"))
def test_nested_array_boundary_is_bit_exact_in_real_rtl(
    tmp_path: Path,
    backend: str,
) -> None:
    compilation = compile_source(NESTED_SOURCE)
    if backend == "direct":
        rtl = tmp_path / "NestedArrayTop.sv"
        rtl.write_text(emit_sv_artifact(compilation.ir).text)
        paths = (rtl,)
    else:
        paths = generate_verilog(
            compilation.clash,
            "NestedArrayTop",
            tmp_path / "clash",
            CLASH_EXECUTABLE,
            public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
        )
    lint_with_verilator(tuple(paths), "NestedArrayTop")
    bench = tmp_path / f"tb_{backend}.sv"
    bench.write_text(NESTED_BENCH)
    object_dir = tmp_path / f"obj_{backend}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "--Mdir", str(object_dir), *(str(path) for path in paths), str(bench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    ran = subprocess.run(
        (str(object_dir / "Vtb"),), cwd=tmp_path, capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stderr or ran.stdout


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(
    ("source", "top"),
    (
        (RV_SOURCE, "RvArrayTop"),
        (RR_SOURCE, "RequestResponseArrayTop"),
    ),
)
@pytest.mark.parametrize("backend", ("direct", "clash"))
def test_protocol_array_boundaries_are_strict_lint_clean(
    tmp_path: Path,
    source: str,
    top: str,
    backend: str,
) -> None:
    compilation = compile_source(source)
    if backend == "direct":
        rtl = tmp_path / f"{top}.sv"
        rtl.write_text(emit_sv_artifact(compilation.ir).text)
        paths = (rtl,)
    else:
        paths = generate_verilog(
            compilation.clash,
            top,
            tmp_path / f"{top}_clash",
            CLASH_EXECUTABLE,
            public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
        )
    lint_with_verilator(tuple(paths), top)
