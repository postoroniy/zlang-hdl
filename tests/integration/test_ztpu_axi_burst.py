"""Focused ZTPU validation for the source-owned AXI burst subset.

The cycle traces deliberately exercise the ordinary typed hierarchy and
ready/valid simulator.  Neither the simulator nor either backend is told that
these channels are AXI channels.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import (
    emit_artifact as emit_sv_artifact,
    emit_formal_artifact,
)
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.equivalence import SignalRole
from zlang.ir.formal import FormalStatus, PropertyKind
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import PortDirection
from zlang.ir.types import StructType
from zlang.opt import lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate_cycles
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "ztpu_axi_burst.zhl").read_text()
READER = "ZtpuAxiBurstReader64x32"
WRITER = "ZtpuAxiBurstWriter64x32"
BASE = 0x100
CLASH = find_clash_executable()
VERILATOR = shutil.which("verilator")
FORMAL_TOOLS = all(
    shutil.which(tool) for tool in ("yosys", "sby", "yosys-smtbmc", "z3")
)


GENERIC_DELEGATION_SOURCE = """
protocol UserPipe {
    role producer
    role consumer
    channel t : rv<u8> producer -> consumer
}

module UserSink {
    clock clk reset rst
    interface bus : UserPipe.consumer @clk
    bus.t.ready = 1
}

module UserPipeDelegationTop {
    clock clk reset rst
    interface bus : UserPipe.consumer @clk
    sink : UserSink
    bus -> sink.bus
}
"""


COMBINED_AXI_SOURCE = """
import std.bus.axi_burst

module CombinedAxiBurstMaster {
    clock clk reset rst
    interface axi : AXI4BurstSubset<64,32>.master @clk

    axi.ar.valid = 0
    axi.ar.payload = AxiBurstAddress { addr=0 len=0 size=2 }
    axi.r.ready = 0
    axi.aw.valid = 0
    axi.aw.payload = AxiBurstAddress { addr=0 len=0 size=2 }
    axi.w.valid = 0
    axi.w.payload = AxiBurstWriteData { data=0 last=0 }
    axi.b.ready = 0
}
"""


def _module(top: str):
    return compile_source(SOURCE, top=top, include_clash=False).ir


def _reader_cycle(
    *,
    start: int = 0,
    base: int = BASE,
    length: int = 1,
    ar_ready: int = 0,
    r_valid: int = 0,
    r_data: int = 0,
    r_last: int = 0,
    r_resp: int = 0,
    data_ready: int = 1,
) -> dict[str, object]:
    return {
        "start": start,
        "base_addr": base,
        "length": length,
        "data": {"ready": data_ready},
        "axi__ar": {"ready": ar_ready},
        "axi__r": {
            "payload": {"data": r_data, "last": r_last, "resp": r_resp},
            "valid": r_valid,
        },
    }


def _writer_cycle(
    *,
    start: int = 0,
    base: int = BASE,
    length: int = 1,
    data_valid: int = 0,
    data_payload: int = 0,
    aw_ready: int = 0,
    w_ready: int = 0,
    b_valid: int = 0,
    b_resp: int = 0,
) -> dict[str, object]:
    return {
        "start": start,
        "base_addr": base,
        "length": length,
        "data": {"payload": data_payload, "valid": data_valid},
        "axi__aw": {"ready": aw_ready},
        "axi__w": {"ready": w_ready},
        "axi__b": {"payload": {"resp": b_resp}, "valid": b_valid},
    }


def test_aggregate_delegation_simulation_is_protocol_generic() -> None:
    module = compile_source(
        GENERIC_DELEGATION_SOURCE,
        top="UserPipeDelegationTop",
        include_clash=False,
    ).ir
    assert simulate_cycles(
        module,
        [
            {"bus__t": {"payload": 0x2A, "valid": 1}},
            {"bus__t": {"payload": 0x55, "valid": 0}},
        ],
        reset=(False, False),
    ) == [
        {"bus__t": {"ready": 1, "transfer": 1}},
        {"bus__t": {"ready": 1, "transfer": 0}},
    ]


@pytest.mark.parametrize(("address_width", "data_width"), ((1, 32), (64, 24)))
def test_axi_burst_specialization_rejects_invalid_bus_geometry(
    address_width: int,
    data_width: int,
) -> None:
    source = f"""
import std.bus.axi_burst
module InvalidGeometry {{
    clock clk reset rst
    in start:bit in base_addr:uint<{address_width}> in length:u16
    reader : AXI4BurstReader<{address_width},{data_width},16> {{
        start base_addr length
    }}
}}
"""
    with pytest.raises(SemanticError, match="parameter constraint is not satisfied"):
        compile_source(source, top="InvalidGeometry", include_clash=False)


def test_combined_axi_burst_schema_survives_typed_and_canonical_ir() -> None:
    typed = compile_source(
        COMBINED_AXI_SOURCE,
        top="CombinedAxiBurstMaster",
        include_clash=False,
    ).ir
    canonical = lower(typed)
    expected = {
        "ar": ("master", "slave", PortDirection.OUTPUT,
               (("addr", 64), ("len", 8), ("size", 3))),
        "r": ("slave", "master", PortDirection.INPUT,
              (("data", 32), ("last", 1), ("resp", 2))),
        "aw": ("master", "slave", PortDirection.OUTPUT,
               (("addr", 64), ("len", 8), ("size", 3))),
        "w": ("master", "slave", PortDirection.OUTPUT,
              (("data", 32), ("last", 1))),
        "b": ("slave", "master", PortDirection.INPUT, (("resp", 2),)),
    }

    for representation in (typed, canonical):
        endpoint, = representation.aggregate_protocol_endpoints
        assert (
            endpoint.name,
            endpoint.protocol,
            endpoint.role,
            endpoint.specialization_identity,
        ) == (
            "axi",
            "AXI4BurstSubset",
            "master",
            "AXI4BurstSubset<AW=64,DW=32>",
        )
        assert tuple(member.name for member in endpoint.members) == tuple(expected)
        ports = {port.name: port for port in representation.ports}
        for member in endpoint.members:
            source, sink, direction, fields = expected[member.name]
            assert member.protocol is InterfaceProtocol.READY_VALID
            assert (member.source_role, member.sink_role) == (source, sink)
            assert isinstance(member.payload_type, StructType)
            assert tuple(
                (field.name, field.type.width) for field in member.payload_type.fields
            ) == fields
            port = ports[f"axi__{member.name}"]
            assert port.protocol is InterfaceProtocol.READY_VALID
            assert port.direction is direction
            assert port.type == member.payload_type

    assert restore(canonical) == typed


def test_backend_artifact_v4_preserves_typed_axi_public_leaf_bindings() -> None:
    representative_ids = {
        READER: (
            f"aggregate:{READER}.axi.ar.payload.addr",
            f"aggregate:{READER}.axi.ar.ready",
            f"aggregate:{READER}.axi.r.payload.data",
            f"aggregate:{READER}.axi.r.payload.resp",
            f"aggregate:{READER}.axi.r.ready",
        ),
        WRITER: (
            f"aggregate:{WRITER}.axi.aw.payload.len",
            f"aggregate:{WRITER}.axi.w.payload.data",
            f"aggregate:{WRITER}.axi.w.payload.last",
            f"aggregate:{WRITER}.axi.w.ready",
            f"aggregate:{WRITER}.axi.b.payload.resp",
            f"aggregate:{WRITER}.axi.b.ready",
        ),
    }

    for top, semantic_ids in representative_ids.items():
        module = _module(top)
        typed_leaves = {
            leaf.leaf_semantic_id: leaf for leaf in module.top_aggregate_abi.leaves
        }
        recursive = build_recursive_formal_design(module)
        for emitter in (emit_clash_artifact, emit_sv_artifact):
            emitted = emitter(module, recursive_design=recursive)
            assert emitted.manifest_version == 4
            artifact = BackendArtifact.from_json(emitted.to_json())
            assert artifact.to_json() == emitted.to_json()
            bindings = {
                binding.semantic_signal_id: binding
                for binding in artifact.bindings
            }
            for semantic_id in semantic_ids:
                leaf = typed_leaves[semantic_id]
                binding = bindings[semantic_id]
                expected_role = (
                    SignalRole.INPUT
                    if leaf.direction is PortDirection.INPUT
                    else SignalRole.OUTPUT
                )
                assert binding.map_version == 4
                assert binding.aggregate_endpoint_id == leaf.aggregate_id
                assert binding.protocol_specialization_id == (
                    leaf.protocol_specialization_id
                )
                assert binding.protocol_role == leaf.role == "master"
                assert binding.member_path == leaf.member_path
                assert binding.width == leaf.width
                assert binding.role is expected_role
                assert binding.ownership == leaf.ownership
                assert binding.signal_kind == leaf.signal_kind
                assert binding.rtl_module == top
                assert binding.rtl_path == leaf.external_name
                assert binding.physical_available


@pytest.mark.parametrize(
    ("top", "protocol", "members", "child"),
    (
        (READER, "AXI4BurstReadSubset", ("ar", "r"), "AXI4BurstReader"),
        (WRITER, "AXI4BurstWriteSubset", ("aw", "w", "b"), "AXI4BurstWriter"),
    ),
)
def test_semantic_canonical_and_backend_artifacts_are_deterministic(
    top: str,
    protocol: str,
    members: tuple[str, ...],
    child: str,
) -> None:
    first = _module(top)
    second = _module(top)
    assert first == second
    canonical = lower(first)
    assert canonical == lower(second)
    assert restore(canonical) == first

    endpoint, = first.aggregate_protocol_endpoints
    assert (endpoint.name, endpoint.protocol, endpoint.role) == (
        "axi", protocol, "master"
    )
    assert tuple(member.name for member in endpoint.members) == members
    connection, = first.aggregate_protocol_connections
    assert (connection.source, connection.destination, connection.delegation) == (
        "axi", ("reader" if top == READER else "writer") + ".axi", True
    )
    concrete, = first.children
    assert concrete.name == child
    assert {register.name for register in concrete.registers} >= {
        "address", "burst_len", "remaining", "failed"
    }

    for emitter in (emit_clash_artifact, emit_sv_artifact):
        artifact = emitter(first)
        repeated = emitter(second)
        assert artifact.text == repeated.text
        assert artifact.artifact_hash == repeated.artifact_hash
        assert artifact.to_json() == repeated.to_json()
        assert BackendArtifact.from_json(artifact.to_json()).to_json() == artifact.to_json()
        assert [name for name, _digest in artifact.library_dependencies] == [
            "std.bus.axi_burst"
        ]


@pytest.mark.skipif(
    not FORMAL_TOOLS,
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
@pytest.mark.parametrize(
    ("top", "expected_assertions"),
    (
        (READER, {"ready_valid:data", "ready_valid:axi__ar"}),
        (WRITER, {"ready_valid:axi__aw", "ready_valid:axi__w"}),
    ),
)
def test_root_ready_valid_m35_executes_with_real_sby_z3(
    top: str, expected_assertions: set[str]
) -> None:
    compiled = compile_source(SOURCE, top=top, include_clash=False)
    artifact = emit_formal_artifact(
        compiled.ir, build_recursive_formal_design(compiled.ir)
    )
    design = connect_formal_design(compiled.formal_design, artifact)
    assert design.properties
    assert all(
        item.generated_from.startswith("ready_valid:")
        for item in design.properties
    )
    assertions = tuple(
        item
        for item in design.properties
        if item.kind is PropertyKind.ASSERTION
    )
    assert {item.generated_from for item in assertions} == expected_assertions
    assert all(item.non_executable_reason is None for item in assertions)

    result = run_verilog_formal(
        emit_harness(design, depth=6),
        top=f"{top}__m35_formal",
        property_id=f"m35.connected.ztpu-axi-burst.{top}",
        depth=6,
        systemverilog=True,
    )
    assert result.status is FormalStatus.BOUNDED_PASS


@pytest.mark.parametrize("length", (1, 256))
def test_reader_trace_holds_address_and_data_through_stalls(length: int) -> None:
    cycles = [
        _reader_cycle(length=length),
        _reader_cycle(start=1, length=length),
        _reader_cycle(length=length),
        # A new request is ignored while the original AR transaction is held.
        _reader_cycle(start=1, base=0x400, length=1),
        _reader_cycle(length=length, ar_ready=1),
    ]
    values = [0xA5000000 | index for index in range(length)]
    cycles.extend((
        _reader_cycle(
            length=length,
            r_valid=1,
            r_data=values[0],
            r_last=int(length == 1),
            data_ready=0,
        ),
        _reader_cycle(
            length=length,
            r_valid=1,
            r_data=values[0],
            r_last=int(length == 1),
        ),
    ))
    cycles.extend(
        _reader_cycle(
            length=length,
            r_valid=1,
            r_data=values[index],
            r_last=int(index == length - 1),
        )
        for index in range(1, length)
    )
    cycles.extend((_reader_cycle(length=length), _reader_cycle(length=length)))

    result = simulate_cycles(
        _module(READER), cycles, reset=[True] + [False] * (len(cycles) - 1)
    )
    address = {"addr": BASE, "len": length - 1, "size": 2}
    assert [result[index]["axi__ar"]["payload"] for index in (2, 3, 4)] == [
        address, address, address
    ]
    assert [result[index]["axi__ar"]["transfer"] for index in (2, 3, 4)] == [
        0, 0, 1
    ]
    assert sum(item["axi__ar"]["transfer"] for item in result) == 1
    assert result[5]["data"] == {
        "payload": values[0], "valid": 1, "transfer": 0
    }
    assert result[5]["axi__r"] == {"ready": 0, "transfer": 0}
    assert result[6]["data"] == {
        "payload": values[0], "valid": 1, "transfer": 1
    }
    assert [
        item["data"]["payload"]
        for item in result
        if item["data"]["transfer"]
    ] == values
    assert sum(item["axi__r"]["transfer"] for item in result) == length
    assert (result[-2]["busy"], result[-2]["done"], result[-2]["error"]) == (
        0, 1, 0
    )
    assert result[-1]["done"] == 0


def test_reader_rejects_bad_lengths_and_alignment_without_bus_activity() -> None:
    for base, length in ((BASE, 0), (BASE, 257), (BASE + 2, 1)):
        cycles = (
            _reader_cycle(base=base, length=length),
            _reader_cycle(start=1, base=base, length=length),
            _reader_cycle(base=base, length=length, ar_ready=1),
            _reader_cycle(base=base, length=length, ar_ready=1),
        )
        result = simulate_cycles(
            _module(READER), cycles, reset=(True, False, False, False)
        )
        assert (result[2]["busy"], result[2]["done"], result[2]["error"]) == (
            0, 1, 1
        )
        assert not any(item["axi__ar"]["valid"] for item in result)
        assert not any(item["axi__ar"]["transfer"] for item in result)


def test_reader_reports_response_and_framing_errors_then_reset_clears_state() -> None:
    response_sequences = (
        ((1, 0), (1, 1)),  # early RLAST only
        ((0, 0), (0, 0)),  # missing final RLAST only
        ((0, 2), (1, 0)),  # nonzero RRESP only
    )
    for first, second in response_sequences:
        error_cycles = (
            _reader_cycle(length=2),
            _reader_cycle(start=1, length=2),
            _reader_cycle(length=2, ar_ready=1),
            _reader_cycle(
                length=2,
                r_valid=1,
                r_data=0x11,
                r_last=first[0],
                r_resp=first[1],
            ),
            _reader_cycle(
                length=2,
                r_valid=1,
                r_data=0x22,
                r_last=second[0],
                r_resp=second[1],
            ),
            _reader_cycle(length=2),
        )
        result = simulate_cycles(
            _module(READER),
            error_cycles,
            reset=(True, False, False, False, False, False),
        )
        assert sum(item["axi__r"]["transfer"] for item in result) == 2
        assert (result[-1]["done"], result[-1]["error"]) == (1, 1)

    reset_cycles = (
        _reader_cycle(length=2),
        _reader_cycle(start=1, length=2),
        _reader_cycle(length=2, ar_ready=1),
        _reader_cycle(length=2, r_valid=1, r_data=0x33, data_ready=0),
        _reader_cycle(length=2, r_valid=1, r_data=0x33),
        _reader_cycle(length=2),
    )
    reset_result = simulate_cycles(
        _module(READER),
        reset_cycles,
        reset=(True, False, False, False, True, False),
    )
    assert (reset_result[4]["busy"], reset_result[4]["done"], reset_result[4]["error"]) == (
        0, 0, 0
    )
    assert reset_result[4]["data"]["valid"] == 0
    assert reset_result[4]["axi__r"]["ready"] == 0


@pytest.mark.parametrize("length", (1, 256))
def test_writer_trace_holds_address_and_data_and_generates_last(length: int) -> None:
    cycles = [
        _writer_cycle(length=length),
        _writer_cycle(start=1, length=length),
        _writer_cycle(length=length),
        # A new request is ignored while the original AW transaction is held.
        _writer_cycle(start=1, base=0x400, length=1),
        _writer_cycle(length=length, aw_ready=1),
    ]
    values = [0x5A000000 | index for index in range(length)]
    cycles.extend((
        _writer_cycle(
            length=length, data_valid=1, data_payload=values[0], w_ready=0
        ),
        _writer_cycle(
            length=length, data_valid=1, data_payload=values[0], w_ready=1
        ),
    ))
    cycles.extend(
        _writer_cycle(
            length=length,
            data_valid=1,
            data_payload=values[index],
            w_ready=1,
        )
        for index in range(1, length)
    )
    cycles.extend((
        _writer_cycle(length=length),
        _writer_cycle(length=length, b_valid=1),
        _writer_cycle(length=length),
        _writer_cycle(length=length),
    ))

    result = simulate_cycles(
        _module(WRITER), cycles, reset=[True] + [False] * (len(cycles) - 1)
    )
    address = {"addr": BASE, "len": length - 1, "size": 2}
    assert [result[index]["axi__aw"]["payload"] for index in (2, 3, 4)] == [
        address, address, address
    ]
    assert [result[index]["axi__aw"]["transfer"] for index in (2, 3, 4)] == [
        0, 0, 1
    ]
    assert sum(item["axi__aw"]["transfer"] for item in result) == 1
    assert not any(
        result[index]["axi__w"]["payload"]["last"]
        for index in (1, 2, 3, 4)
    )
    assert result[5]["axi__w"] == {
        "payload": {"data": values[0], "last": int(length == 1)},
        "valid": 1,
        "transfer": 0,
    }
    assert result[5]["data"] == {"ready": 0, "transfer": 0}
    transferred = [item["axi__w"] for item in result if item["axi__w"]["transfer"]]
    assert [item["payload"]["data"] for item in transferred] == values
    assert [item["payload"]["last"] for item in transferred] == [
        *([0] * (length - 1)), 1
    ]
    assert result[-4]["axi__b"] == {"ready": 1, "transfer": 0}
    assert result[-3]["axi__b"] == {"ready": 1, "transfer": 1}
    assert (result[-2]["busy"], result[-2]["done"], result[-2]["error"]) == (
        0, 1, 0
    )
    assert result[-1]["done"] == 0


def test_writer_rejects_bad_lengths_and_alignment_without_bus_activity() -> None:
    for base, length in ((BASE, 0), (BASE, 257), (BASE + 2, 1)):
        cycles = (
            _writer_cycle(base=base, length=length),
            _writer_cycle(start=1, base=base, length=length),
            _writer_cycle(base=base, length=length, aw_ready=1),
            _writer_cycle(base=base, length=length, aw_ready=1),
        )
        result = simulate_cycles(
            _module(WRITER), cycles, reset=(True, False, False, False)
        )
        assert (result[2]["busy"], result[2]["done"], result[2]["error"]) == (
            0, 1, 1
        )
        assert not any(item["axi__aw"]["valid"] for item in result)
        assert not any(item["axi__w"]["valid"] for item in result)


def test_writer_reports_bresp_error_then_reset_clears_state() -> None:
    error_cycles = (
        _writer_cycle(),
        _writer_cycle(start=1),
        _writer_cycle(aw_ready=1),
        _writer_cycle(data_valid=1, data_payload=0x44, w_ready=1),
        _writer_cycle(b_valid=1, b_resp=2),
        _writer_cycle(),
    )
    result = simulate_cycles(
        _module(WRITER), error_cycles, reset=(True, False, False, False, False, False)
    )
    assert result[4]["axi__b"] == {"ready": 1, "transfer": 1}
    assert (result[-1]["done"], result[-1]["error"]) == (1, 1)

    reset_cycles = (
        _writer_cycle(length=2),
        _writer_cycle(start=1, length=2),
        _writer_cycle(length=2, aw_ready=1),
        _writer_cycle(length=2, data_valid=1, data_payload=0x55),
        _writer_cycle(length=2, data_valid=1, data_payload=0x55, w_ready=1),
        _writer_cycle(length=2),
    )
    reset_result = simulate_cycles(
        _module(WRITER),
        reset_cycles,
        reset=(True, False, False, False, True, False),
    )
    assert (reset_result[4]["busy"], reset_result[4]["done"], reset_result[4]["error"]) == (
        0, 0, 0
    )
    assert reset_result[4]["data"]["ready"] == 0
    assert reset_result[4]["axi__w"]["valid"] == 0


def test_reset_discards_every_reader_and_writer_phase() -> None:
    reader_prefixes = (
        (_reader_cycle(), _reader_cycle(start=1)),
        (_reader_cycle(), _reader_cycle(start=1), _reader_cycle(ar_ready=1)),
        (
            _reader_cycle(),
            _reader_cycle(start=1),
            _reader_cycle(ar_ready=1),
            _reader_cycle(r_valid=1, r_data=0x11, r_last=1),
        ),
    )
    for prefix in reader_prefixes:
        cycles = (*prefix, _reader_cycle(), _reader_cycle())
        reset = (*([False] * len(prefix)), True, False)
        result = simulate_cycles(_module(READER), cycles, reset=reset)
        for item in result[-2:]:
            assert (item["busy"], item["done"], item["error"]) == (0, 0, 0)
            assert item["data"]["valid"] == 0
            assert item["axi__ar"]["valid"] == 0
            assert item["axi__r"]["ready"] == 0

    writer_prefixes = (
        (_writer_cycle(), _writer_cycle(start=1)),
        (_writer_cycle(), _writer_cycle(start=1), _writer_cycle(aw_ready=1)),
        (
            _writer_cycle(),
            _writer_cycle(start=1),
            _writer_cycle(aw_ready=1),
            _writer_cycle(data_valid=1, data_payload=0x11, w_ready=1),
        ),
        (
            _writer_cycle(),
            _writer_cycle(start=1),
            _writer_cycle(aw_ready=1),
            _writer_cycle(data_valid=1, data_payload=0x11, w_ready=1),
            _writer_cycle(b_valid=1),
        ),
    )
    for prefix in writer_prefixes:
        cycles = (*prefix, _writer_cycle(), _writer_cycle())
        reset = (*([False] * len(prefix)), True, False)
        result = simulate_cycles(_module(WRITER), cycles, reset=reset)
        for item in result[-2:]:
            assert (item["busy"], item["done"], item["error"]) == (0, 0, 0)
            assert item["data"]["ready"] == 0
            assert item["axi__aw"]["valid"] == 0
            assert item["axi__w"]["valid"] == 0
            assert item["axi__b"]["ready"] == 0


READER_BENCH = r"""
module tb;
  logic clk=0,rst=1,start=0; logic [63:0] base_addr=64'h100;
  logic [15:0] length=2; logic busy,done,error;
  logic [31:0] data_payload; logic data_valid,data_ready=1;
  logic [63:0] axi_ar_payload_addr; logic [7:0] axi_ar_payload_len;
  logic [2:0] axi_ar_payload_size; logic axi_ar_valid,axi_ar_ready=0;
  logic [31:0] axi_r_payload_data=0; logic axi_r_payload_last=0;
  logic [1:0] axi_r_payload_resp=0; logic axi_r_valid=0,axi_r_ready;
  ZtpuAxiBurstReader64x32 dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    tick; tick; rst=0; start=1; tick; start=0;
    if(!axi_ar_valid || axi_ar_payload_addr!=64'h100 ||
       axi_ar_payload_len!=1 || axi_ar_payload_size!=2)
      $fatal(1,"bad or missing AR");
    start=1; base_addr=64'h400; length=1; tick; start=0;
    if(!axi_ar_valid || axi_ar_payload_addr!=64'h100 || axi_ar_payload_len!=1)
      $fatal(1,"AR changed under stall or busy start was accepted");
    axi_ar_ready=1; tick; axi_ar_ready=0;
    axi_r_valid=1; axi_r_payload_data=32'h11; data_ready=0; #1;
    if(!data_valid || data_payload!=32'h11 || axi_r_ready)
      $fatal(1,"R backpressure was not propagated");
    tick;
    if(!data_valid || data_payload!=32'h11) $fatal(1,"R data changed under stall");
    data_ready=1; tick;
    axi_r_payload_data=32'h22; axi_r_payload_last=1; axi_r_payload_resp=2; tick;
    axi_r_valid=0;
    if(!done || !error) $fatal(1,"read completion did not report RRESP");
    rst=1; tick;
    if(busy || done || error || data_valid || axi_ar_valid || axi_r_ready)
      $fatal(1,"reader reset did not clear the epoch");
    $finish;
  end
endmodule
"""


WRITER_BENCH = r"""
module tb;
  logic clk=0,rst=1,start=0; logic [63:0] base_addr=64'h100;
  logic [15:0] length=2; logic [31:0] data_payload=0;
  logic data_valid=0,data_ready,busy,done,error;
  logic [63:0] axi_aw_payload_addr; logic [7:0] axi_aw_payload_len;
  logic [2:0] axi_aw_payload_size; logic axi_aw_valid,axi_aw_ready=0;
  logic [31:0] axi_w_payload_data; logic axi_w_payload_last;
  logic axi_w_valid,axi_w_ready=0; logic [1:0] axi_b_payload_resp=0;
  logic axi_b_valid=0,axi_b_ready;
  ZtpuAxiBurstWriter64x32 dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    tick; tick; rst=0; start=1; tick; start=0;
    if(!axi_aw_valid || axi_aw_payload_addr!=64'h100 ||
       axi_aw_payload_len!=1 || axi_aw_payload_size!=2)
      $fatal(1,"bad or missing AW");
    start=1; base_addr=64'h400; length=1; tick; start=0;
    if(!axi_aw_valid || axi_aw_payload_addr!=64'h100 || axi_aw_payload_len!=1)
      $fatal(1,"AW changed under stall or busy start was accepted");
    axi_aw_ready=1; tick; axi_aw_ready=0;
    data_valid=1; data_payload=32'h11; #1;
    if(!axi_w_valid || axi_w_payload_data!=32'h11 || axi_w_payload_last ||
       data_ready) $fatal(1,"bad stalled first W beat");
    tick;
    if(!axi_w_valid || axi_w_payload_data!=32'h11)
      $fatal(1,"W data changed under stall");
    axi_w_ready=1; tick;
    data_payload=32'h22; #1;
    if(!axi_w_payload_last) $fatal(1,"last W beat was not marked");
    tick; data_valid=0; axi_w_ready=0;
    if(!axi_b_ready) $fatal(1,"writer did not enter B phase");
    axi_b_valid=1; axi_b_payload_resp=2; tick; axi_b_valid=0;
    if(!done || !error) $fatal(1,"write completion did not report BRESP");
    rst=1; tick;
    if(busy || done || error || data_ready || axi_aw_valid || axi_w_valid ||
       axi_b_ready) $fatal(1,"writer reset did not clear the epoch");
    $finish;
  end
endmodule
"""


def _run_verilator(
    rtl: tuple[Path, ...], bench_text: str, tmp_path: Path, label: str
) -> None:
    bench = tmp_path / f"{label}_tb.sv"
    bench.write_text(bench_text)
    obj = tmp_path / f"{label}_obj"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "--Mdir", str(obj), "-o", "sim", "-Wno-DECLFILENAME",
            "-Wno-UNUSED", "-Wno-UNDRIVEN",
            *(str(path) for path in rtl), str(bench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    run = subprocess.run(
        (str(obj / "sim"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is unavailable")
@pytest.mark.parametrize(
    ("top", "bench"), ((READER, READER_BENCH), (WRITER, WRITER_BENCH))
)
def test_direct_systemverilog_executes_burst_witness(
    top: str, bench: str, tmp_path: Path
) -> None:
    artifact = emit_sv_artifact(_module(top))
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(artifact.text)
    _run_verilator((rtl,), bench, tmp_path, f"direct_{top}")


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="real Clash and Verilator are required",
)
@pytest.mark.parametrize(
    ("top", "bench"), ((READER, READER_BENCH), (WRITER, WRITER_BENCH))
)
def test_real_clash_verilog_executes_same_burst_witness(
    top: str, bench: str, tmp_path: Path
) -> None:
    compilation = compile_source(SOURCE, top=top)
    rtl = tuple(
        generate_verilog(compilation.clash, top, tmp_path / f"clash_{top}", CLASH)
    )
    _run_verilator(rtl, bench, tmp_path, f"clash_{top}")
