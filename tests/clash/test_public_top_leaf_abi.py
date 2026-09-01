from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang import compile_source
from zlang.backend.clash import emit_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.toolchain import generate_verilog, lint_with_verilator


STRUCT_SOURCE = """
struct Inner { tag:u3 data:u5 }
struct Envelope { flag:bit body:Inner }

module StructLeafTop {
    in request:Envelope
    out response:Envelope
    response=request
}
"""


RV_SOURCE = """
struct Payload { address:u8 data:u16 }

module ReadyValidLeafTop {
    in source:rv<Payload>
    out sink:rv<Payload>
    sink.payload=source.payload
    sink.valid=source.valid
    source.ready=sink.ready
}
"""


RESERVED_FIELD_SOURCE = """
struct ReservedFields { module:u8 zlang_module:u8 }

module ReservedFieldTop {
    in request:ReservedFields
    out response:ReservedFields
    response=request
}
"""


SINGLE_FIELD_NESTED_SOURCE = """
struct Meta { tag:u2 }
struct FrameBeat { data:u8 meta:Meta first:bit last:bit }

module SingleFieldNestedTop {
    in source:rv<FrameBeat>
    out sink:rv<FrameBeat>
    sink.payload=source.payload
    sink.valid=source.valid
    source.ready=sink.ready
}
"""


def test_plain_and_nested_struct_top_annotations_are_recursive() -> None:
    clash = compile_source(STRUCT_SOURCE).clash
    expected_input = (
        't_inputs = [PortProduct "request" '
        '[PortName "flag", PortProduct "body" '
        '[PortName "tag", PortName "data"]]]'
    )
    expected_output = (
        't_output = PortProduct "response" '
        '[PortName "flag", PortProduct "body" '
        '[PortName "tag", PortName "data"]]'
    )
    assert expected_input in clash
    assert expected_output in clash


def test_ready_valid_struct_payload_annotation_is_recursive() -> None:
    clash = compile_source(RV_SOURCE).clash
    assert (
        'PortProduct "source" [PortProduct "payload" '
        '[PortName "address", PortName "data"], PortName "valid"]'
    ) in clash
    assert (
        'PortProduct "sink" [PortProduct "payload" '
        '[PortName "address", PortName "data"], PortName "valid"]'
    ) in clash


def test_nested_single_field_struct_uses_the_scalar_clash_physical_shape() -> None:
    clash = compile_source(SINGLE_FIELD_NESTED_SOURCE).clash
    assert 'PortName "meta_tag"' in clash
    assert 'PortProduct "meta" [PortName "tag"]' not in clash


def test_request_response_struct_payload_annotation_is_recursive() -> None:
    source = Path("examples/request_client.zl").read_text()
    clash = compile_source(source).clash
    assert (
        'PortProduct "mem_response" [PortProduct "payload" '
        '[PortName "id", PortName "data"], PortName "valid"]'
    ) in clash
    assert (
        'PortProduct "mem_request" [PortProduct "payload" '
        '[PortName "id", PortName "data"], PortName "valid"]'
    ) in clash


@pytest.mark.parametrize(
    ("source", "semantic_paths"),
    (
        (
            STRUCT_SOURCE,
            {
                "port:request.flag": "request_flag",
                "port:request.body.tag": "request_body_tag",
                "port:request.body.data": "request_body_data",
                "port:response.flag": "response_flag",
                "port:response.body.tag": "response_body_tag",
                "port:response.body.data": "response_body_data",
            },
        ),
        (
            RV_SOURCE,
            {
                "port:source.payload.address": "source_payload_address",
                "port:source.payload.data": "source_payload_data",
                "port:source.valid": "source_valid",
                "port:source.ready": "source_ready",
                "port:sink.payload.address": "sink_payload_address",
                "port:sink.payload.data": "sink_payload_data",
                "port:sink.valid": "sink_valid",
                "port:sink.ready": "sink_ready",
            },
        ),
    ),
)
def test_struct_annotation_leaves_survive_artifact_json_as_physical(
    source: str,
    semantic_paths: dict[str, str],
) -> None:
    artifact = BackendArtifact.from_json(
        emit_artifact(compile_source(source).ir).to_json()
    )
    bindings = {
        binding.semantic_signal_id: binding for binding in artifact.bindings
    }
    for semantic, path in semantic_paths.items():
        binding = bindings[semantic]
        assert binding.rtl_path == path
        assert binding.physical_available
    # A structured attestation only promotes concrete annotation leaves.  The
    # packed semantic root hidden by recursive leaves is still unavailable.
    hidden_root = "port:request" if source == STRUCT_SOURCE else "port:source.payload"
    assert not bindings[hidden_root].physical_available
    assert bindings[hidden_root].rtl_path == ""


def test_request_response_annotation_leaves_survive_artifact_json_as_physical() -> None:
    compilation = compile_source(Path("examples/request_client.zl").read_text())
    artifact = BackendArtifact.from_json(emit_artifact(compilation.ir).to_json())
    bindings = {
        binding.semantic_signal_id: binding for binding in artifact.bindings
    }
    for channel in ("request", "response"):
        for field in ("id", "data"):
            semantic = f"port:mem.{channel}.payload.{field}"
            binding = bindings[semantic]
            assert binding.rtl_path == f"mem_{channel}_payload_{field}"
            assert binding.physical_available


def test_nested_reserved_field_labels_are_not_mangled_independently() -> None:
    compilation = compile_source(RESERVED_FIELD_SOURCE)
    clash = compilation.clash
    assert (
        'PortProduct "request" '
        '[PortName "module", PortName "zlang_module"]'
    ) in clash
    assert (
        'PortProduct "response" '
        '[PortName "module", PortName "zlang_module"]'
    ) in clash
    artifact = BackendArtifact.from_json(emit_artifact(compilation.ir).to_json())
    bindings = {
        binding.semantic_signal_id: binding for binding in artifact.bindings
    }
    assert bindings["port:request.module"].rtl_path == "request_module"
    assert bindings["port:request.zlang_module"].rtl_path == "request_zlang_module"
    assert bindings["port:response.module"].rtl_path == "response_module"
    assert bindings["port:response.zlang_module"].rtl_path == "response_zlang_module"
    assert all(
        bindings[semantic].physical_available
        for semantic in (
            "port:request.module", "port:request.zlang_module",
            "port:response.module", "port:response.zlang_module",
        )
    )


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(
    ("source", "top", "physical_ports"),
    (
        (
            STRUCT_SOURCE,
            "StructLeafTop",
            (
                "request_flag", "request_body_tag", "request_body_data",
                "response_flag", "response_body_tag", "response_body_data",
            ),
        ),
        (
            RV_SOURCE,
            "ReadyValidLeafTop",
            (
                "source_payload_address", "source_payload_data", "source_valid",
                "source_ready", "sink_payload_address", "sink_payload_data",
                "sink_valid", "sink_ready",
            ),
        ),
        (
            RESERVED_FIELD_SOURCE,
            "ReservedFieldTop",
            (
                "request_module", "request_zlang_module",
                "response_module", "response_zlang_module",
            ),
        ),
        (
            SINGLE_FIELD_NESTED_SOURCE,
            "SingleFieldNestedTop",
            (
                "source_payload_data", "source_payload_meta_tag",
                "source_payload_first", "source_payload_last", "source_valid",
                "source_ready", "sink_payload_data", "sink_payload_meta_tag",
                "sink_payload_first", "sink_payload_last", "sink_valid",
                "sink_ready",
            ),
        ),
    ),
)
def test_real_clash_public_struct_leaves_and_lint(
    tmp_path: Path,
    source: str,
    top: str,
    physical_ports: tuple[str, ...],
) -> None:
    compilation = compile_source(source)
    rtl = generate_verilog(
        compilation.clash,
        top,
        tmp_path / "rtl",
        CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, top)
    generated = "\n".join(path.read_text() for path in rtl)
    for physical_port in physical_ports:
        assert physical_port in generated
