from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

from zlang import sim
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import SystemVerilogEmissionError, emit_artifact
from zlang.compiler import compile_file, compile_source
from zlang.simulation_compare import compare_program
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


AXI_STREAM_FIFO = """
import std.bus.axi_stream

module AxiStreamFifo {
    clock clk reset rst
    interface s_axis:AXIStream<32>.sink @clk
    interface m_axis:AXIStream<32>.source @clk
    fifo words:fifo<AXIStreamBeat<32>,2>
    words.data=s_axis.t.payload
    words.push=s_axis.t.transfer
    words.pop=m_axis.t.transfer
    s_axis.t.ready=words.ready
    m_axis.t.valid=words.valid
    m_axis.t.payload=words.front
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
    assert "logic [2047:0] buffer;" in artifact.text
    assert "buffer[(32'(index) << 3) +: 8] <= value;" in artifact.text
    assert "assign contents = buffer;" in artifact.text
    assert "zlang_packed_contents" not in artifact.text
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


def test_axi_stream_fifo_preserves_ready_valid_and_public_payload(tmp_path: Path) -> None:
    source = tmp_path / "axi_stream_fifo.zhl"
    source.write_text(AXI_STREAM_FIFO)
    empty = dict(data=0, keep=0, strb=0, last=0)
    a = dict(data=0x1234ABCD, keep=0xF, strb=0x3, last=1)
    b = dict(data=0x87654321, keep=0x7, strb=0x5, last=0)
    c = dict(data=0x00FF00FF, keep=0x9, strb=0x1, last=1)
    # Input reset, offered beat and consumer ready followed by the
    # expected producer ready and visible beat after the clock edge.
    cycles = (
        (True, None, 0, 0, None),
        (False, None, 0, 1, None),
        (False, a, 0, 1, a),
        (False, b, 0, 0, a),
        (False, c, 0, 0, a),  # stall at capacity
        (False, c, 1, 1, b),  # full pop and push on the same edge
        (False, None, 1, 1, c),
        (True, None, 0, 0, None),  # discard a live pending beat
        (False, b, 0, 1, b),
    )
    events, expected = [], []
    for reset, offered, accept, ready, visible in cycles:
        events.append({
            "reset": {"rst": reset},
            "set": {
                "s_axis__t": {"payload": offered or empty, "valid": int(offered is not None)},
                "m_axis__t": {"ready": accept},
            },
            "edges": ["clk"],
        })
        sample = {"s_axis_t_ready": ready, "m_axis_t_valid": int(visible is not None)}
        sample.update({f"m_axis_t_payload_{field}": value
                       for field, value in (visible or empty).items()})
        expected.append(sample)
    comparison = compare_program(
        sim.compile(source), events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30,
    )
    assert comparison.native_trace == tuple(expected)
    assert comparison.rtl_trace == tuple(expected)


def test_axi_stream_fifo_rejects_endpoint_without_a_rendered_port() -> None:
    module = compile_source(AXI_STREAM_FIFO).ir
    phantom = replace(module.protocol_endpoints[0], name="phantom")
    phantom_bundle = replace(
        module.aggregate_protocol_endpoints[0], name="phantom_bundle", members=(),
    )
    with_phantom = replace(module, protocol_endpoints=(*module.protocol_endpoints, phantom))
    with_phantom_bundle = replace(
        module,
        aggregate_protocol_endpoints=(*module.aggregate_protocol_endpoints, phantom_bundle),
    )
    unrendered = (
        (with_phantom, r"unclaimed=.*\.phantom:ready_valid"),
        (with_phantom_bundle, r"unclaimed=.*aggregate_endpoint:phantom_bundle:"),
    )

    for altered, diagnostic in unrendered:
        with pytest.raises(SystemVerilogEmissionError, match=diagnostic) as caught:
            emit_artifact(altered)
        assert caught.value.code == "ZL-BACKEND-SYSTEMVERILOG-FEATURE-ACCOUNTING"


@pytest.mark.parametrize("storage", ("ram", "rom"))
def test_axi_stream_with_registered_storage(tmp_path: Path, storage: str) -> None:
    declaration = (
        """
    memory table:mem<u8,2> { read_latency 1 collision read_first }
    table.write_enable=rx.t.transfer
    table.write_address=address
    table.write_data=truncate<8>(rx.t.payload.data)
""" if storage == "ram" else
        "    rom table:rom<u8,2> {read_latency 1 init [57,166]}\n"
    )
    source = tmp_path / f"axi_stream_{storage}.zhl"
    source.write_text("""import std.bus.axi_stream
module AxiStorage {
    clock clk reset rst
    interface rx:AXIStream<32>.sink @clk
    interface tx:AXIStream<32>.source @clk
    out value:u8
    address=bitcast<u1>(rx.t.payload.data[0])
""" + declaration + """
    table.read_address=address
    value=table.read_data
    rx.t.ready=tx.t.ready
    tx.t.valid=rx.t.valid
    tx.t.payload=rx.t.payload
}
""")
    even = dict(data=0x48EC1252, keep=0xF, strb=0x2, last=0)
    odd = dict(data=0x9300AC35, keep=0x7, strb=0x5, last=1)
    # Reset and edge controls, offered beat, valid/ready, then the independent
    # post-event read data for read-first RAM and registered ROM.
    frames = (
        (True,  True,  even, 0, 0, 0,    0),
        (False, False, even, 1, 0, 0,    0),
        (False, True,  even, 1, 0, 0,   57),
        (False, True,  even, 1, 1, 0,   57),
        (False, False, odd,  1, 1, 0,   57),
        (False, True,  odd,  1, 1, 0,  166),
        (False, True,  even, 0, 1, 0x52,57),
        (False, False, odd,  1, 0, 0x52,57),
        (False, True,  odd,  1, 0, 0x35,166),
        (True,  True,  odd,  1, 1, 0,    0),
        (False, True,  even, 0, 0, 0,   57),
    )
    events, expected = [], []
    for reset, edge, beat, valid, ready, ram, rom in frames:
        events.append({
            "reset": {"rst": reset},
            "set": {"rx__t": {"payload": beat, "valid": valid}, "tx__t": {"ready": ready}},
            "edges": ["clk"] if edge else [],
        })
        sample = {"rx_t_ready": ready, "tx_t_valid": valid,
                  "value": ram if storage == "ram" else rom}
        sample.update({f"tx_t_payload_{field}": value for field, value in beat.items()})
        expected.append(sample)

    program = sim.compile(source)
    comparison_dir = tmp_path / "comparison"
    comparison = compare_program(program, events, simulator="iverilog",
                                 artifact_directory=comparison_dir, timeout=30)
    assert comparison.native_trace == tuple(expected)
    assert comparison.rtl_trace == tuple(expected)

    if storage == "rom":
        images = list(comparison_dir.glob("*.mem"))
        assert len(images) == 1
        assert images[0].read_text().splitlines() == ["00111001", "10100110"]

    module = program.module
    phantom = replace(module.protocol_endpoints[0], name="phantom")
    altered = replace(module, protocol_endpoints=(*module.protocol_endpoints, phantom))
    with pytest.raises(SystemVerilogEmissionError, match=r"unclaimed=.*\.phantom:ready_valid") as caught:
        emit_artifact(altered)
    assert caught.value.code == "ZL-BACKEND-SYSTEMVERILOG-FEATURE-ACCOUNTING"
    if storage == "ram":
        _check_protocol_storage_admission(tmp_path)


def _check_protocol_storage_admission(tmp_path: Path) -> None:
    from zlang.semantic import SemanticError

    events = [
        {"reset": {"rst": rst}, "set": {"wr": wr}, "edges": ["clk"]}
        for rst, wr in ((True, 0), (False, 1), (False, 1), (False, 0))
    ]
    storage = (
        ("memory m:mem<u13,4> {read_latency 1 collision write_first}",
         "m.read_address=0 m.write_address=0 m.write_enable=wr m.write_data=35 q=m.read_data", 35),
        ("fifo m:fifo<u13,4>", "m.push=wr & m.ready m.pop=0 m.data=35 q=mux(m.valid,m.front,0)", 35),
        ("rom m:rom<u13,2> {read_latency 1 init [9,13]}", "m.read_address=0 q=m.read_data", 9),
    )
    protocols = (
        ("packet", "in a:packet<u13> in b:packet<u13> out tx:packet<u13>",
         "arbiter [a,b] -> tx {policy fixed_priority grant packet}"),
        ("vc", "out tx:vc_credit<u13,4,2>", "tx.payload=0 tx.vc=0 tx.send=0"),
        ("credit", "out tx:credit<u13,2>", "tx.payload=0 tx.send=0"),
        ("rv", "out tx:rv<u13>", "tx.payload=0 tx.valid=0"),
    )
    for declaration, behavior, value in storage:
        for name, ports, logic in protocols:
            kind = declaration.split()[0]
            case = tmp_path / f"{name}-{kind}"
            case.mkdir()
            path = case / "dut.zhl"
            source = f"""
module Top {{
    clock clk reset rst in wr:bit out q:u13
    {ports}
    {logic}
    {declaration}
    {behavior}
}}
"""
            path.write_text(source)
            if name == "credit":
                with pytest.raises(SemanticError, match="storage resources cannot yet be mixed"):
                    compile_file(path)
                continue
            ir = compile_file(path).ir
            if name in ("packet", "vc"):
                with pytest.raises(SystemVerilogEmissionError) as caught:
                    emit_artifact(ir)
                error = caught.value
                assert error.code == "ZL-BACKEND-SYSTEMVERILOG-PROTOCOL-STORAGE"
                assert error.semantic_path == ("Top", "m")
                assert error.primary is not None and error.primary.source_unit == path.name
                assert declaration in source.splitlines()[error.primary.span.start_line - 1]
            expected = [0, value, value, value]
            if name == "vc":
                with sim.compile(path).create() as runtime:
                    assert [row["q"] for row in runtime.run_events(events)] == expected
            if name == "rv":
                compared = compare_program(
                    sim.compile(path), events, simulator="iverilog",
                    artifact_directory=case / "comparison", timeout=30, build_timeout=30,
                )
                assert [row["q"] for row in compared.native_trace] == expected
                assert [row["q"] for row in compared.rtl_trace] == expected

def test_axi_stream_with_csr_preserves_sidebands_and_register_access(tmp_path: Path) -> None:
    source = tmp_path / "axi_stream_csr.zhl"
    source.write_text("""import std.bus.axi_stream
module AxiCsr {
    clock clk reset rst
    interface rx:AXIStream<32>.sink @clk
    interface tx:AXIStream<32>.source @clk
    csr bank @0 { CONTROL @0 { value u8 @7:0 rw=0x5a } }
    rx.t.ready=tx.t.ready
    tx.t.valid=rx.t.valid
    tx.t.payload=rx.t.payload
}
""")
    a = dict(data=0x0123ABCD, keep=0xF, strb=0x9, last=0)
    b = dict(data=0xFEDCBA98, keep=0x5, strb=0x1, last=1)
    # Reset, clock edge, stream beat, valid, ready, CSR address/read/write,
    # write data and the reference readback after the event.
    rows = (
        (1, 1, a, 0, 0, 0, 1, 1, 0xFF, 0x5A),
        (0, 0, a, 1, 0, 0, 1, 1, 0x26, 0x5A),
        (0, 1, a, 1, 0, 0, 1, 1, 0x26, 0x26),
        (0, 1, a, 1, 1, 4, 1, 1, 0xFF, 0),
        (0, 0, b, 1, 0, 0, 1, 0, 0, 0x26),
        (0, 1, b, 1, 1, 0, 0, 0, 0, 0),
        (1, 1, b, 0, 0, 0, 1, 0, 0, 0x5A),
    )
    events, expected = [], []
    for reset, edge, beat, valid, ready, addr, read, write, wdata, rdata in rows:
        events.append({
            "reset": {"rst": bool(reset)},
            "set": {"rx__t": {"payload": beat, "valid": valid}, "tx__t": {"ready": ready},
                    "addr": addr, "read": read, "write": write, "wdata": wdata},
            "edges": ["clk"] if edge else [],
        })
        expected.append({
            "rx_t_ready": ready, "tx_t_valid": valid, "rdata": rdata,
            "ready": int(addr == 0 and bool(read or write)),
            **{f"tx_t_payload_{field}": value for field, value in beat.items()},
        })
    program = sim.compile(source)
    comparison = compare_program(
        program, events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30,
    )
    assert comparison.native_trace == tuple(expected)
    assert comparison.rtl_trace == tuple(expected)

    module = program.module
    artifact = emit_artifact(module)
    assert "csr_field_0_0_0_state" not in artifact.text
    assert not any(
        binding.semantic_signal_id.startswith("port:csr_field_")
        for binding in artifact.bindings
    )
    phantom = replace(module.protocol_endpoints[0], name="phantom")
    phantom_bundle = replace(module.aggregate_protocol_endpoints[0], name="phantom_bundle")
    invalid = (
        (replace(module, protocol_endpoints=(*module.protocol_endpoints, phantom)),
         r"unclaimed=.*\.phantom:ready_valid"),
        (replace(module, aggregate_protocol_endpoints=(*module.aggregate_protocol_endpoints, phantom_bundle)),
         r"unclaimed=.*aggregate_endpoint:phantom_bundle:"),
    )
    for changed, diagnostic in invalid:
        with pytest.raises(SystemVerilogEmissionError, match=diagnostic) as caught:
            emit_artifact(changed)
        assert caught.value.code == "ZL-BACKEND-SYSTEMVERILOG-FEATURE-ACCOUNTING"


def test_standalone_csr_comparison_uses_public_access_ports(tmp_path: Path) -> None:
    source = tmp_path / "csr.zhl"
    source.write_text("""module Top {
    clock clk reset rst
    csr bank @0 { CONTROL @0 { value u8 @7:0 rw=0x5a } }
}
""")
    events = (
        {"reset": {"rst": True}, "set": {"addr": 0, "read": 1}, "edges": ["clk"]},
        {"reset": {"rst": False}, "set": {"write": 1, "wdata": 0xA5}, "edges": ["clk"]},
        {"set": {"addr": 4, "wdata": 0xFF}, "edges": ["clk"]},
        {"set": {"addr": 0, "write": 0}},
        {"reset": {"rst": True}, "edges": ["clk"]},
    )
    expected = (
        {"ready": 1, "rdata": 0x5A},
        {"ready": 1, "rdata": 0xA5},
        {"ready": 0, "rdata": 0},
        {"ready": 1, "rdata": 0xA5},
        {"ready": 1, "rdata": 0x5A},
    )

    program = sim.compile(source)
    comparison = compare_program(
        program, events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30,
    )
    assert comparison.native_trace == expected
    assert comparison.rtl_trace == expected

    module = program.module
    public_ports = {"addr", "write", "wdata", "read", "rdata", "ready"}
    child_projections = {port.name for port in module.ports} - public_ports
    assert "csr_field_0_0_0_state" in child_projections
    assert {leaf.external_name for leaf in module.top_physical_abi.leaves} == {
        "clk", "rst", *public_ports,
    }
    artifact = emit_artifact(module)
    bindings = {binding.semantic_signal_id: binding for binding in artifact.bindings}
    assert all(bindings[f"port:{name}"].rtl_path == name for name in public_ports)
    assert all(f"port:{name}" not in bindings for name in child_projections)


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
    assert "assign zlang_packed_y = {8'(zlang_top_core_y)};" in public
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
    assert "assign zlang_packed_y = {8'(zlang_top_core)};" in public
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
    assert f"assign {alias} = {{8'(zlang_packed_y)}};" in artifact.text
    assert f"assign y_value = {alias};" in artifact.text
    assert artifact.text == repeated.text
    assert artifact.artifact_hash == repeated.artifact_hash
    _strict_lint(artifact.text, "PackedAliasCollision")
