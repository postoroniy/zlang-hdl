from __future__ import annotations

from pathlib import Path
import shutil
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


def _strict_lint(text: str, top: str) -> None:
    if shutil.which("verilator") is None:
        pytest.skip("Verilator is unavailable")
    with tempfile.TemporaryDirectory() as temporary:
        rtl = Path(temporary) / f"{top}.sv"
        rtl.write_text(text)
        lint_with_verilator((rtl,), top)


def test_user_struct_top_is_always_leaf_and_vectors_are_native_arrays() -> None:
    module = compile_source(USER_STRUCT).ir
    artifact = emit_artifact(module)

    assert "module UserStructTop_zlang_core (" in artifact.text
    assert "module UserStructTop (" in artifact.text
    public = artifact.text.split("module UserStructTop (", 1)[1]
    assert "input wire logic [7:0] request_address" in public
    assert "input wire logic [3:0] request_lanes [0:1]" in public
    assert "output logic [7:0] response_address" in public
    assert "output logic [3:0] response_lanes [0:1]" in public
    assert ".request({request_address, request_lanes[0], request_lanes[1]})" in public
    assert "assign response_address = zlang_top_core_response[15:8];" in public
    assert "assign response_lanes[0] = zlang_top_core_response[7:4];" in public
    assert "assign response_lanes[1] = zlang_top_core_response[3:0];" in public

    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    leaf_names = {
        item.rtl_path
        for item in restored.bindings
        if item.semantic_signal_id.startswith("port:request.")
    }
    assert leaf_names == {"request_address", "request_lanes"}
    _strict_lint(artifact.text, "UserStructTop")


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
    assert ".axi__w_payload({axi_w_payload_data, axi_w_payload_strb})" in public

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


def test_wrapper_temporary_is_allocated_away_from_public_leaf_names() -> None:
    module = compile_source(WRAPPER_TEMPORARY_COLLISION).ir
    artifact = emit_artifact(module)
    repeated = emit_artifact(module)
    public = artifact.text.split("module WrapperTemporaryCollision (", 1)[1]

    assert "input wire logic [7:0] zlang_top_core_y" in public
    assert "output logic [7:0] y_value" in public
    temporary = next(
        line.strip().removeprefix("logic [7:0] ").removesuffix(";")
        for line in public.splitlines()
        if line.strip().startswith("logic [7:0] zlang_top_core_y_")
    )
    assert temporary != "zlang_top_core_y"
    assert "__" not in temporary
    assert f".y({temporary})" in public
    assert f"assign y_value = {temporary}[7:0];" in public
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
    assert ".zlang_input_payload(input_payload)" in public
    assert ".zlang_input_valid(input_valid)" in public
    assert ".zlang_input_ready(zlang_top_core_zlang_input_ready)" in public
    assert ".zlang_output_payload(zlang_top_core_zlang_output_payload)" in public
    assert ".zlang_output_valid(zlang_top_core_zlang_output_valid)" in public
    assert ".zlang_output_ready(output_ready)" in public
    assert "assign input_ready = zlang_top_core_zlang_input_ready;" in public
    assert (
        "assign output_payload = zlang_top_core_zlang_output_payload[7:0];"
        in public
    )
    assert "assign output_valid = zlang_top_core_zlang_output_valid;" in public

    bindings = {item.semantic_signal_id: item for item in artifact.bindings}
    assert bindings["port:input.payload"].rtl_path == "input_payload"
    assert bindings["port:input.valid"].rtl_path == "input_valid"
    assert bindings["port:input.ready"].rtl_path == "input_ready"
    assert bindings["port:output.payload"].rtl_path == "output_payload"
    assert bindings["port:output.valid"].rtl_path == "output_valid"
    assert bindings["port:output.ready"].rtl_path == "output_ready"
    _strict_lint(artifact.text, "ReservedReadyValidRoots")


def test_wrapper_instance_is_allocated_away_from_public_leaf_names() -> None:
    module = compile_source(WRAPPER_INSTANCE_COLLISION).ir
    artifact = emit_artifact(module)
    repeated = emit_artifact(module)
    public = artifact.text.split("module WrapperInstanceCollision (", 1)[1]

    assert "input wire logic [7:0] zlang_top_core" in public
    instance_line = next(
        line.strip()
        for line in public.splitlines()
        if line.strip().startswith("WrapperInstanceCollision_zlang_core ")
    )
    instance_name = instance_line.split()[1]
    assert instance_name.startswith("zlang_top_core_")
    assert "__" not in instance_name
    assert instance_name != "zlang_top_core"
    assert repeated.text == artifact.text
    assert repeated.artifact_hash == artifact.artifact_hash
    _strict_lint(artifact.text, "WrapperInstanceCollision")
