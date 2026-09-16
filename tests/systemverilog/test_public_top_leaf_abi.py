from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import SystemVerilogEmissionError, emit_artifact
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]


USER_STRUCT = """
struct UserPayload {
    address : u8
    lanes : vec<2,u4>
}

module UserStructTop {
    in request : UserPayload
    out response : UserPayload
    response = request
}
"""


MANGLED_NAME_COLLISION = """
module MangledNameCollision {
    in module : u8
    out zlang_module : u8
    zlang_module = module
}
"""


WRAPPER_TEMPORARY_COLLISION = """
struct Result {
    value : u8
}

module WrapperTemporaryCollision {
    in zlang_top_core_y : u8
    out y : Result
    y = Result { value = zlang_top_core_y }
}
"""


RESERVED_READY_VALID_ROOTS = """
module ReservedReadyValidRoots {
    in input : rv<u8>
    out output : rv<u8>
    output.payload = input.payload
    output.valid = input.valid
    input.ready = output.ready
}
"""


WRAPPER_INSTANCE_COLLISION = """
struct WrappedResult {
    value : u8
}

module WrapperInstanceCollision {
    in zlang_top_core : u8
    out y : WrappedResult
    y = WrappedResult { value = zlang_top_core }
}
"""


PACKED_ALIAS_COLLISION = """
struct AliasResult { value : u8 }
module PackedAliasCollision {
    in zlang_packed_y : u8
    out y : AliasResult
    y = AliasResult { value = zlang_packed_y }
}
"""


def _strict_lint(text: str, top: str) -> None:
    if shutil.which("verilator") is None:
        pytest.skip("Verilator is unavailable")
    with tempfile.TemporaryDirectory() as temporary:
        rtl = Path(temporary) / f"{top}.sv"
        rtl.write_text(text)
        lint_with_verilator((rtl,), top)


def test_user_struct_top_is_always_leaf_and_vectors_are_packed_arrays() -> None:
    module = compile_source(USER_STRUCT).ir
    artifact = emit_artifact(module)

    assert "module UserStructTop_zlang_core (" not in artifact.text
    assert "module UserStructTop (" in artifact.text
    public = artifact.text.split("module UserStructTop (", 1)[1]
    assert "input wire logic [7:0] request_address" in public
    assert "input wire logic [1:0][3:0] request_lanes" in public
    assert "output logic [7:0] response_address" in public
    assert "output logic [1:0][3:0] response_lanes" in public
    assert "logic [15:0] zlang_packed_request;" in public
    assert "logic [15:0] zlang_packed_response;" in public
    assert "assign zlang_packed_request = {request_address, request_lanes};" in public
    assert "assign zlang_packed_response = zlang_packed_request;" in public
    assert "assign response_address = zlang_packed_response[15:8];" in public
    assert "assign response_lanes = zlang_packed_response[7:0];" in public
    assert "zlang_top_core" not in public

    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    leaf_names = {
        item.rtl_path
        for item in restored.bindings
        if item.semantic_signal_id.startswith("port:request.")
    }
    assert leaf_names == {"request_address", "request_lanes"}
    _strict_lint(artifact.text, "UserStructTop")


@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys is unavailable")
def test_dynamic_placement_packed_array_top_synthesizes_with_yosys() -> None:
    module = compile_source(
        (ROOT / "examples" / "all_syntax.zhl").read_text(),
        top="DynamicPlacement",
    ).ir
    artifact = emit_artifact(module)

    assert "`ifdef" not in artifact.text
    assert "`ifndef" not in artifact.text
    assert "output logic [255:0][7:0] contents" in artifact.text
    assert "logic [2047:0] zlang_packed_contents;" in artifact.text
    assert "assign contents = zlang_packed_contents;" in artifact.text
    assert "zlang_top_core" not in artifact.text
    assert "_zlang_core" not in artifact.text

    with tempfile.TemporaryDirectory() as temporary:
        rtl = Path(temporary) / "DynamicPlacement.sv"
        rtl.write_text(artifact.text)
        result = subprocess.run(
            (
                "yosys", "-Q", "-p",
                f"read_verilog -sv {rtl}; hierarchy -top DynamicPlacement; "
                "synth -top DynamicPlacement -run coarse; check",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    _strict_lint(artifact.text, "DynamicPlacement")


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_dynamic_placement_inline_boundary_preserves_runtime_indices(
    tmp_path: Path,
) -> None:
    module = compile_source(
        (ROOT / "examples" / "all_syntax.zhl").read_text(),
        top="DynamicPlacement",
    ).ir
    rtl = tmp_path / "DynamicPlacement.sv"
    rtl.write_text(emit_artifact(module).text)
    bench = tmp_path / "tb.sv"
    bench.write_text(r"""
module tb;
  logic clk = 0;
  logic rst = 1;
  logic write_enable = 0;
  logic [7:0] value = 0;
  logic [7:0] index = 0;
  wire [255:0][7:0] contents;
  always #5 clk = ~clk;
  DynamicPlacement dut(.*);
  task automatic write_and_check(
      input logic [7:0] logical_index,
      input logic [7:0] expected);
    @(negedge clk);
    index = logical_index;
    value = expected;
    write_enable = 1;
    @(posedge clk);
    #1;
        if (contents[logical_index] !== expected)
      $fatal(1, "packed-array index mismatch");
  endtask
  initial begin
    repeat (2) @(posedge clk);
    @(negedge clk);
    rst = 0;
    write_and_check(8'd0, 8'h12);
    write_and_check(8'd255, 8'ha5);
    write_and_check(8'd37, 8'h5c);
    write_enable = 0;
    if (contents[0] !== 8'h12 || contents[255] !== 8'ha5 ||
        contents[37] !== 8'h5c)
      $fatal(1, "packed-array boundary did not retain values");
    $finish;
  end
endmodule
""")
    obj = tmp_path / "obj"
    completed = subprocess.run(
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
    assert completed.returncode == 0, completed.stdout + completed.stderr
    executed = subprocess.run(
        (str(obj / "Vtb"),), capture_output=True, text=True, check=False,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr


def test_axi_lite_top_exposes_struct_payload_fields_not_packed_payloads() -> None:
    module = compile_source(
        (ROOT / "examples" / "axi_csr_top.zhl").read_text(),
        top="AxiCsrTop",
    ).ir
    artifact = emit_artifact(module)
    public = artifact.text.split("module AxiCsrTop (", 1)[1]

    assert "input wire logic [31:0] axi_aw_payload_addr" in public
    assert "input wire logic [31:0] axi_w_payload_data" in public
    assert "input wire logic [3:0] axi_w_payload_strb" in public
    assert "output logic [31:0] axi_r_payload_data" in public
    assert "output logic [1:0] axi_r_payload_resp" in public
    assert "input wire logic [35:0] axi__w_payload" not in public
    assert (
        "assign zlang_packed_axi__w_payload = "
        "{axi_w_payload_data, axi_w_payload_strb};"
        in public
    )
    assert ".axi__w_payload(zlang_packed_axi__w_payload)" in public

    bindings = {item.semantic_signal_id: item for item in artifact.bindings}
    assert bindings["aggregate:AxiCsrTop.axi.w.payload.data"].rtl_path == (
        "axi_w_payload_data"
    )
    assert bindings["aggregate:AxiCsrTop.axi.w.payload.strb"].rtl_path == (
        "axi_w_payload_strb"
    )
    _strict_lint(artifact.text, "AxiCsrTop")


def test_public_names_colliding_after_sv_mangling_are_rejected() -> None:
    module = compile_source(MANGLED_NAME_COLLISION).ir

    with pytest.raises(
        SystemVerilogEmissionError,
        match=(
            "public top identifier collision after mangling: 'module' .* "
            "and 'zlang_module' .* both map to 'zlang_module'"
        ),
    ):
        emit_artifact(module)


def test_inline_packed_alias_is_allocated_away_from_public_leaf_names() -> None:
    module = compile_source(WRAPPER_TEMPORARY_COLLISION).ir
    artifact = emit_artifact(module)
    repeated = emit_artifact(module)
    public = artifact.text.split("module WrapperTemporaryCollision (", 1)[1]

    assert "input wire logic [7:0] zlang_top_core_y" in public
    assert "output logic [7:0] y_value" in public
    assert "logic [7:0] zlang_packed_y;" in public
    assert "assign zlang_packed_y = {zlang_top_core_y};" in public
    assert "assign y_value = zlang_packed_y;" in public
    assert "_zlang_core" not in public
    assert repeated.text == artifact.text
    assert repeated.artifact_hash == artifact.artifact_hash
    _strict_lint(artifact.text, "WrapperTemporaryCollision")


def test_reserved_rv_roots_use_unmangled_public_flattened_names() -> None:
    module = compile_source(RESERVED_READY_VALID_ROOTS).ir
    artifact = emit_artifact(module)
    public = artifact.text.split("module ReservedReadyValidRoots (", 1)[1]

    assert "input wire logic [7:0] input_payload" in public
    assert "input wire logic input_valid" in public
    assert "output logic input_ready" in public
    assert "output logic [7:0] output_payload" in public
    assert "output logic output_valid" in public
    assert "input wire logic output_ready" in public
    assert "assign zlang_packed_input_payload = input_payload;" in public
    assert "assign zlang_packed_input_valid = input_valid;" in public
    assert "assign zlang_packed_output_ready = output_ready;" in public
    assert "assign input_ready = zlang_packed_input_ready;" in public
    assert "assign output_payload = zlang_packed_output_payload;" in public
    assert "assign output_valid = zlang_packed_output_valid;" in public
    assert "zlang_top_core" not in public

    bindings = {item.semantic_signal_id: item for item in artifact.bindings}
    assert bindings["port:input.payload"].rtl_path == "input_payload"
    assert bindings["port:input.valid"].rtl_path == "input_valid"
    assert bindings["port:input.ready"].rtl_path == "input_ready"
    assert bindings["port:output.payload"].rtl_path == "output_payload"
    assert bindings["port:output.valid"].rtl_path == "output_valid"
    assert bindings["port:output.ready"].rtl_path == "output_ready"
    _strict_lint(artifact.text, "ReservedReadyValidRoots")


def test_removed_wrapper_needs_no_instance_name_beside_colliding_port() -> None:
    module = compile_source(WRAPPER_INSTANCE_COLLISION).ir
    artifact = emit_artifact(module)
    repeated = emit_artifact(module)
    public = artifact.text.split("module WrapperInstanceCollision (", 1)[1]

    assert "input wire logic [7:0] zlang_top_core" in public
    assert "logic [7:0] zlang_packed_y;" in public
    assert "assign zlang_packed_y = {zlang_top_core};" in public
    assert "assign y_value = zlang_packed_y;" in public
    assert "_zlang_core" not in public
    assert repeated.text == artifact.text
    assert repeated.artifact_hash == artifact.artifact_hash
    _strict_lint(artifact.text, "WrapperInstanceCollision")


def test_inline_packed_alias_avoids_source_owned_collision_deterministically() -> None:
    module = compile_source(PACKED_ALIAS_COLLISION).ir
    artifact = emit_artifact(module)
    repeated = emit_artifact(module)
    match = re.search(r"logic \[7:0\] (zlang_packed_y_[0-9a-f]+);", artifact.text)

    assert match is not None
    alias = match.group(1)
    assert f"assign {alias} = {{zlang_packed_y}};" in artifact.text
    assert f"assign y_value = {alias};" in artifact.text
    assert artifact.text == repeated.text
    assert artifact.artifact_hash == repeated.artifact_hash
    _strict_lint(artifact.text, "PackedAliasCollision")
