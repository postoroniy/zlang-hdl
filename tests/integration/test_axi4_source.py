"""AXI4 source-profile tests; protocol encodings are checked independently."""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
from random import Random
import shutil
import subprocess

import pytest
import zlang.sim

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact, emit_formal_artifact
from zlang.compiler import compile_file, compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
)
from zlang.ir.module import PortDirection
from zlang.ir.top_abi import build_top_physical_abi
from zlang.ir.types import StructType
from zlang.simulate import simulate, simulate_cycles
from zlang.semantic.errors import SemanticError
from tests.simulation.differential import run_differential


ADDRESS_SOURCE = """
import std.bus.axi4
module Axi4AddressCheck {
    in request : Axi4Address<32,4>
    out legal : bit
    legal = axi4_address_valid<AW=32,DW=32,IW=4>(request)
}
"""

EXCLUSIVE_SOURCE = """
import std.bus.axi4
module Axi4ExclusiveCheck {
    in request : Axi4Address<32,4>
    out legal : bit
    legal = axi4_exclusive_address_valid<AW=32,DW=32,IW=4>(request)
}
"""

PROTOCOL_SOURCE = """
import std.bus.axi4
module Axi4Schema {
    clock clk reset rst
    interface axi : AXI4WithUser<64,128,3,5,2,4,6,8,10>.master @clk
    axi.aw.valid = 0
    axi.aw.payload = Axi4AddressUser {
        id=0 addr=0 len=0 size=0 burst=1 lock=0 cache=0
        prot=0 qos=0 region=0 user=0
    }
    axi.w.valid = 0
    axi.w.payload = Axi4WriteDataUser {data=0 strb=0 last=0 user=0}
    axi.b.ready = 0
    axi.ar.valid = 0
    axi.ar.payload = Axi4AddressUser {
        id=0 addr=0 len=0 size=0 burst=1 lock=0 cache=0
        prot=0 qos=0 region=0 user=0
    }
    axi.r.ready = 0
}
"""

STROBE_SOURCE = """
import std.bus.axi4
module Axi4StrobeCheck {
    in address : u32
    in size : u3
    in strobe : bits<4>
    out legal : bit
    legal = axi4_write_strobe_valid<AW=32,DW=32>(address,size,strobe)
}
"""

BEAT_ADDRESS_SOURCE = """
import std.bus.axi4
module Axi4BeatAddress {
    in request : Axi4Address<32,4>
    in index : u9
    out address : u32 = axi4_beat_address<AW=32,IW=4>(request,index)
}
"""

READ_MANAGER_SOURCE = """
import std.bus.axi4
module Axi4ReadTop {
    clock clk reset rst
    in command : rv<Axi4Address<32,4>>
    out data : rv<Axi4ReadData<32,4>>
    out failed : bit
    interface axi : AXI4Read<32,32,4>.master @clk
    reader : AXI4ReadManager<32,32,4,4>
    command -> reader.command
    reader.data -> data
    axi -> reader.axi
    failed = reader.protocol_error
}
"""

WRITE_MANAGER_SOURCE = """
import std.bus.axi4
module Axi4WriteTop {
    clock clk reset rst
    in command : rv<Axi4Address<32,4>>
    in write : rv<Axi4WriteData<32>>
    out completion : rv<Axi4WriteResponse<4>>
    out failed : bit
    interface axi : AXI4Write<32,32,4>.master @clk
    writer : AXI4WriteManager<32,32,4,4>
    command -> writer.command
    write -> writer.write
    writer.completion -> completion
    axi -> writer.axi
    failed = writer.protocol_error
}
"""

AXI4_LOOPBACK_SOURCE = """
import std.bus.axi4
import std.bus.axi4_subordinate
module Axi4Loopback {
    clock clk reset rst
    in read_command : rv<Axi4Address<32,4>>
    out read_data : rv<Axi4ReadData<32,4>>
    in write_command : rv<Axi4Address<32,4>>
    in write_data : rv<Axi4WriteData<32>>
    out write_completion : rv<Axi4WriteResponse<4>>
    out backend_read_request : rv<Axi4BackendReadRequest<32,4,2>>
    in backend_read_beat : rv<Axi4BackendReadBeat<32,2>>
    out backend_write_request : rv<Axi4BackendWriteRequest<32,4,2>>
    out backend_write_beat : rv<Axi4BackendWriteBeat<32,2>>
    in backend_write_result : rv<Axi4BackendWriteResult<2>>
    out protocol_error : bit = manager.protocol_error | subordinate.protocol_error
    manager : AXI4Manager<32,32,4,4,4>
    subordinate : AXI4Subordinate<32,32,4,4,4,2>
    read_command -> manager.read_command
    manager.read_data -> read_data
    write_command -> manager.write_command
    write_data -> manager.write_data
    manager.write_completion -> write_completion
    manager.axi -> subordinate.axi
    subordinate.backend_read_request -> backend_read_request
    backend_read_beat -> subordinate.backend_read_beat
    subordinate.backend_write_request -> backend_write_request
    subordinate.backend_write_beat -> backend_write_beat
    backend_write_result -> subordinate.backend_write_result
}
"""

AXI4_READ_STABILITY_SOURCE = """
import std.bus.axi4_subordinate
module Axi4ReadStabilityProof {
    clock clk reset rst
    in request_valid : bit
    in beat_valid : bit
    out result : rv<Axi4ReadData<8,1>>
    reader : AXI4ReadSubordinate<8,8,1,2,1>
    reader.axi.ar.payload = Axi4Address {
        id=1 addr=16 len=0 size=0 burst=1 lock=0
        cache=0 prot=0 qos=0 region=0
    }
    reader.axi.ar.valid = request_valid
    reader.axi.r.ready = result.ready
    result.payload = reader.axi.r.payload
    result.valid = reader.axi.r.valid
    reader.backend_request.ready = 1
    reader.backend_beat.payload = Axi4BackendReadBeat {
        slot=0 data=90 resp=0 last=1
    }
    reader.backend_beat.valid = beat_valid
    reg was_stalled : bit = 0
    reg held_payload : Axi4ReadData<8,1> = Axi4ReadData {
        id=0 data=0 resp=0 last=0
    }
    was_stalled <- reader.axi.r.valid & ~result.ready
    held_payload <- reader.axi.r.payload
    assert held_r @ clk {
        ~was_stalled |
        (result.valid & (result.payload == held_payload))
    }
}
"""

def _axi4_loopback_event_inputs() -> dict[str, object]:
    return {
        "read_command": {
            "valid": 0, "payload": _read_request(1),
        },
        "read_data": {"ready": 1},
        "write_command": {
            "valid": 0, "payload": _read_request(2),
        },
        "write_data": {
            "valid": 1,
            "payload": {"data": 0xDEADBEEF, "strb": 15, "last": 1},
        },
        "write_completion": {"ready": 1},
        "backend_read_request": {"ready": 1},
        "backend_read_beat": {
            "valid": 0,
            "payload": {"slot": 0, "data": 0x12345678,
                        "resp": 0, "last": 1},
        },
        "backend_write_request": {"ready": 1},
        "backend_write_beat": {"ready": 1},
        "backend_write_result": {
            "valid": 0, "payload": {"slot": 0, "resp": 0},
        },
    }

FIVE_CHANNEL_SOURCE = """
import std.bus.axi4
module Axi4FiveMaster {
    clock clk reset rst
    in run : bit
    interface axi : AXI4<32,32,3,5>.master @clk
    axi.aw.valid = run
    axi.aw.payload = Axi4Address {id=3 addr=256 len=0 size=2 burst=1
        lock=0 cache=0 prot=0 qos=0 region=0}
    axi.w.valid = run
    axi.w.payload = Axi4WriteData {data=42 strb=15 last=1}
    axi.b.ready = run
    axi.ar.valid = run
    axi.ar.payload = Axi4Address {id=5 addr=512 len=0 size=2 burst=1
        lock=0 cache=0 prot=0 qos=0 region=0}
    axi.r.ready = run
}
module Axi4FiveSlave {
    clock clk reset rst
    interface axi : AXI4<32,32,3,5>.slave @clk
    out aw_seen : bit = axi.aw.transfer
    out w_seen : bit = axi.w.transfer
    out b_seen : bit = axi.b.transfer
    out ar_seen : bit = axi.ar.transfer
    out r_seen : bit = axi.r.transfer
    axi.aw.ready = 1
    axi.w.ready = 1
    axi.b.valid = 1
    axi.b.payload = Axi4WriteResponse {id=3 resp=0}
    axi.ar.ready = 1
    axi.r.valid = 1
    axi.r.payload = Axi4ReadData {id=5 data=77 resp=0 last=1}
}
module Axi4FiveTop {
    clock clk reset rst
    in run : bit
    out observed : bits<5>
    master : Axi4FiveMaster
    slave : Axi4FiveSlave
    master.run = run
    master.axi -> slave.axi
    observed = concat(slave.aw_seen, slave.w_seen, slave.b_seen,
                      slave.ar_seen, slave.r_seen)
}
"""

GENERIC_CHILD_SIGNAL_SOURCE = """
module GenericChildSignals {
    clock clk reset rst
    in request : rv<u8>
    out response : rv<u8>
    response.payload = request.payload
    response.valid = request.valid
    request.ready = response.ready
}
module GenericChildSignalsTop {
    clock clk reset rst
    in input_data : u8
    in input_valid : bit
    out input_ready : bit
    out output_data : u8
    out output_valid : bit
    in output_ready : bit
    child : GenericChildSignals
    child.request.payload = input_data
    child.request.valid = input_valid
    input_ready = child.request.ready
    output_data = child.response.payload
    output_valid = child.response.valid
    child.response.ready = output_ready
}
"""

GENERIC_CHILD_FIELDS_SOURCE = """
struct GenericPayload { first : u8 second : u8 }
module GenericFieldChild {
    clock clk reset rst
    in request : rv<GenericPayload>
    out response : rv<GenericPayload>
    response.payload = request.payload
    response.valid = request.valid
    request.ready = response.ready
}
module GenericFieldTop {
    clock clk reset rst
    in first : u8
    in second : u8
    in incoming : bit
    out observed : GenericPayload = child.response.payload
    child : GenericFieldChild
    child.request.payload.first = first
    child.request.payload.second = second
    child.request.valid = incoming
    child.response.ready = 1
}
"""

AXI_CHILD_FIELD_SOURCE = FIVE_CHANNEL_SOURCE + """
module Axi4ChildFieldTop {
    clock clk reset rst
    in run : bit
    in AWREADY : bit
    in WREADY : bit
    in ARREADY : bit
    in BVALID : bit
    in RVALID : bit
    out AWADDR : u32 = child.axi.aw.payload.addr
    out AWVALID : bit = child.axi.aw.valid
    out BREADY : bit = child.axi.b.ready
    child : Axi4FiveMaster
    child.run = run
    child.axi.aw.ready = AWREADY
    child.axi.w.ready = WREADY
    child.axi.ar.ready = ARREADY
    child.axi.b.payload = Axi4WriteResponse {id=0 resp=0}
    child.axi.b.valid = BVALID
    child.axi.r.payload = Axi4ReadData {id=0 data=0 resp=0 last=1}
    child.axi.r.valid = RVALID
}
"""


@pytest.mark.parametrize(
    ("addr", "length", "size", "burst", "legal"),
    (
        (0, 0, 2, 1, 1),
        (0xFF0, 3, 2, 1, 1),
        (0xFF0, 7, 2, 1, 0),
        (0xFFF, 0, 0, 1, 1),
        (0xFFF, 0, 2, 0, 1),
        (0x104, 3, 2, 2, 1),
        (0x105, 3, 2, 2, 0),
        (0xFF8, 3, 2, 2, 1),
        (0x100, 2, 2, 2, 0),
        (0x100, 16, 2, 0, 0),
        (0x100, 255, 2, 1, 1),
        (0x100, 0, 3, 1, 0),
        (0x100, 0, 2, 3, 0),
    ),
)
def test_axi4_burst_address_legality(
    addr: int, length: int, size: int, burst: int, legal: int
) -> None:
    module = compile_source(ADDRESS_SOURCE, top="Axi4AddressCheck").ir
    request = {
        "id": 0, "addr": addr, "len": length, "size": size,
        "burst": burst, "lock": 0, "cache": 0, "prot": 0,
        "qos": 0, "region": 0,
    }
    assert simulate(module, request=request) == {"legal": legal}


@pytest.mark.parametrize(
    ("addr", "length", "size", "burst", "legal"),
    (
        (0x100, 0, 2, 1, 1),
        (0x100, 3, 2, 1, 1),
        (0x102, 3, 2, 1, 0),
        (0x100, 2, 2, 1, 0),
        (0x100, 15, 2, 1, 1),
        (0x100, 16, 2, 1, 0),
        (0x100, 7, 3, 1, 0),
    ),
)
def test_axi4_exclusive_shape(
    addr: int, length: int, size: int, burst: int, legal: int
) -> None:
    module = compile_source(EXCLUSIVE_SOURCE, top="Axi4ExclusiveCheck").ir
    request = {
        "id": 3, "addr": addr, "len": length, "size": size,
        "burst": burst, "lock": 1, "cache": 0, "prot": 0,
        "qos": 0, "region": 0,
    }
    assert simulate(module, request=request) == {"legal": legal}


@pytest.mark.parametrize(
    ("address", "size", "strobe", "legal"),
    (
        (0, 2, 0b1111, 1), (0, 2, 0, 1),
        (1, 2, 0b1110, 1), (1, 2, 0b0001, 0),
        (2, 1, 0b1100, 1), (2, 1, 0b0010, 0),
        (3, 0, 0b1000, 1), (3, 0, 0b0100, 0),
        (0, 3, 0, 0),
    ),
)
def test_axi4_sparse_strobes_and_byte_lanes(
    address: int, size: int, strobe: int, legal: int
) -> None:
    module = compile_source(STROBE_SOURCE, top="Axi4StrobeCheck").ir
    assert simulate(module, address=address, size=size, strobe=strobe) == {
        "legal": legal
    }


@pytest.mark.parametrize(
    ("address", "length", "size", "burst", "expected"),
    (
        (0x103, 3, 2, 1, (0x103, 0x104, 0x108, 0x10C)),
        (0x108, 3, 2, 2, (0x108, 0x10C, 0x100, 0x104)),
        (0x103, 3, 0, 0, (0x103, 0x103, 0x103, 0x103)),
    ),
)
def test_axi4_beat_address_independent_oracle(
    address: int, length: int, size: int, burst: int,
    expected: tuple[int, ...],
) -> None:
    module = compile_source(BEAT_ADDRESS_SOURCE, top="Axi4BeatAddress").ir
    request = _read_request(0, length=length)
    request.update(addr=address, size=size, burst=burst)
    assert tuple(
        simulate(module, request=request, index=index)["address"]
        for index in range(len(expected))
    ) == expected


def test_axi4_user_fields_and_independent_id_widths() -> None:
    module = compile_source(PROTOCOL_SOURCE, top="Axi4Schema").ir
    endpoint, = module.aggregate_protocol_endpoints
    assert endpoint.protocol == "AXI4WithUser"
    assert tuple(member.name for member in endpoint.members) == (
        "aw", "w", "b", "ar", "r"
    )
    expected = {
        "aw": (PortDirection.OUTPUT, 5, 2),
        "w": (PortDirection.OUTPUT, None, 4),
        "b": (PortDirection.INPUT, 5, 6),
        "ar": (PortDirection.OUTPUT, 3, 8),
        "r": (PortDirection.INPUT, 3, 10),
    }
    for member in endpoint.members:
        direction, id_width, user_width = expected[member.name]
        assert isinstance(member.payload_type, StructType)
        fields = {field.name: field.type.width for field in member.payload_type.fields}
        assert fields["user"] == user_width
        assert fields.get("id") == id_width
        assert next(port for port in module.ports
                    if port.name == f"axi__{member.name}").direction is direction


def test_axi4_no_user_profile_omits_optional_fields() -> None:
    source = """
import std.bus.axi4
module Axi4NoUser {
    clock clk reset rst
    interface axi : AXI4<32,64,3,5>.master @clk
    axi.aw.valid=0
    axi.aw.payload=Axi4Address{id=0 addr=0 len=0 size=3 burst=1 lock=0
                               cache=0 prot=0 qos=0 region=0}
    axi.w.valid=0
    axi.w.payload=Axi4WriteData{data=0 strb=0 last=0}
    axi.b.ready=0
    axi.ar.valid=0
    axi.ar.payload=Axi4Address{id=0 addr=0 len=0 size=3 burst=1 lock=0
                               cache=0 prot=0 qos=0 region=0}
    axi.r.ready=0
}
"""
    module = compile_source(source, top="Axi4NoUser").ir
    assert all(
        "user" not in {field.name for field in member.payload_type.fields}
        for member in module.aggregate_protocol_endpoints[0].members
    )


def test_axi4_user_manager_specializes_five_independent_user_widths() -> None:
    module = compile_source(
        "import std.bus.axi4\n"
        "module Widths { clock clk reset rst "
        "manager:AXI4ManagerWithUser<64,128,3,5,2,4,6,8,10,4> }",
        top="Widths",
    ).ir
    child = module.children[0]
    user_widths = {}
    for endpoint in child.aggregate_protocol_endpoints:
        for member in endpoint.members:
            user = next(
                field for field in member.payload_type.fields
                if field.name == "user"
            )
            user_widths[member.name] = user.type.width
    assert user_widths == {"aw": 2, "w": 4, "b": 6, "ar": 8, "r": 10}


def test_axi4_user_subordinate_specializes_five_independent_user_widths() -> None:
    module = compile_source(
        "import std.bus.axi4_user_subordinate\n"
        "module Widths { clock clk reset rst "
        "device:AXI4SubordinateWithUser<64,128,3,5,2,4,6,8,10,4,2> }",
        top="Widths",
    ).ir
    child = module.children[0]
    user_widths = {}
    for endpoint in child.aggregate_protocol_endpoints:
        for member in endpoint.members:
            user = next(
                field for field in member.payload_type.fields
                if field.name == "user"
            )
            user_widths[member.name] = user.type.width
    assert user_widths == {"aw": 2, "w": 4, "b": 6, "ar": 8, "r": 10}


@pytest.mark.parametrize(
    ("top", "user"),
    (
        ("AXI4MasterPins", False),
        ("AXI4SubordinatePins", False),
        ("AXI4MasterPinsWithUser", True),
        ("AXI4SubordinatePinsWithUser", True),
    ),
)
def test_axi4_source_pin_adapters_have_exact_axi_boundary(
    top: str, user: bool,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_pins.zhl"
    module = compile_file(source, top=top).ir
    abi = build_top_physical_abi(module)
    pins = {leaf.external_name: leaf for leaf in abi.leaves}
    standard = {
        "ACLK", "ARESETn", "AWID", "AWADDR", "AWLEN", "AWSIZE",
        "AWBURST", "AWLOCK", "AWCACHE", "AWPROT", "AWQOS",
        "AWREGION", "AWVALID", "AWREADY", "WDATA", "WSTRB",
        "WLAST", "WVALID", "WREADY", "BID", "BRESP", "BVALID",
        "BREADY", "ARID", "ARADDR", "ARLEN", "ARSIZE", "ARBURST",
        "ARLOCK", "ARCACHE", "ARPROT", "ARQOS", "ARREGION",
        "ARVALID", "ARREADY", "RID", "RDATA", "RRESP", "RLAST",
        "RVALID", "RREADY",
    }
    user_pins = {"AWUSER", "WUSER", "BUSER", "ARUSER", "RUSER"}
    assert standard <= pins.keys()
    assert (pins.keys() & user_pins) == (user_pins if user else set())
    assert not any(name.startswith("axi__") for name in pins)
    assert pins["AWID"].width == pins["BID"].width == 4
    assert pins["ARID"].width == pins["RID"].width == 4
    assert pins["WSTRB"].width == 4
    if user:
        assert all(pins[name].width == 1 for name in user_pins)
    rtl = emit_artifact(module).text
    assert f"module {top} (" in rtl
    assert f"{top}_zlang_core" not in rtl


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_user_master_pins_round_trip_all_five_channels(
    engine: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_pins.zhl"
    address = {
        "id": 7, "addr": 0x124, "len": 3, "size": 2, "burst": 1,
        "lock": 0, "cache": 10, "prot": 5, "qos": 6, "region": 9,
        "user": 1,
    }
    with zlang.sim.load(
        source, top="AXI4MasterPinsWithUser", engine=engine
    ) as sim:
        sim.set("request_aw", {"valid": 1, "payload": address})
        sim.set("request_w", {
            "valid": 1,
            "payload": {"data": 0xAABBCCDD, "strb": 0b0101,
                        "last": 1, "user": 1},
        })
        sim.set("request_ar", {"valid": 1, "payload": address})
        sim.set("response_b", {"ready": 1})
        sim.set("response_r", {"ready": 1})
        for name, value in {
            "AWREADY": 1, "WREADY": 1, "ARREADY": 1,
            "BID": 7, "BRESP": 1, "BUSER": 1, "BVALID": 1,
            "RID": 7, "RDATA": 0x12345678, "RRESP": 2,
            "RLAST": 1, "RUSER": 1, "RVALID": 1,
        }.items():
            sim.set(name, value)
        result = sim.eval()
        for prefix in ("AW", "AR"):
            for field, value in address.items():
                assert result[f"{prefix}{field.upper()}"] == value
            assert result[f"{prefix}VALID"] == 1
        assert result["WDATA"] == 0xAABBCCDD
        assert result["WSTRB"] == 0b0101
        assert result["WLAST"] == result["WUSER"] == result["WVALID"] == 1
        assert result["BREADY"] == result["RREADY"] == 1
        assert sim.get("response_b")["payload"] == {
            "id": 7, "resp": 1, "user": 1,
        }
        assert sim.get("response_r")["payload"] == {
            "id": 7, "data": 0x12345678, "resp": 2,
            "last": 1, "user": 1,
        }


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_user_subordinate_pins_preserve_all_sidebands(
    engine: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_pins.zhl"
    with zlang.sim.load(
        source, top="AXI4SubordinatePinsWithUser", engine=engine
    ) as sim:
        for prefix in ("AW", "AR"):
            for field, value in {
                "ID": 3, "ADDR": 0x124, "LEN": 3, "SIZE": 2,
                "BURST": 1, "LOCK": 0, "CACHE": 10, "PROT": 5,
                "QOS": 6, "REGION": 9, "USER": 1, "VALID": 1,
            }.items():
                sim.set(f"{prefix}{field}", value)
        for name, value in {
            "WDATA": 0xAABBCCDD, "WSTRB": 0b0101, "WLAST": 1,
            "WUSER": 1, "WVALID": 1, "BREADY": 1, "RREADY": 1,
        }.items():
            sim.set(name, value)
        sim.set("request_aw", {"ready": 1})
        sim.set("request_w", {"ready": 1})
        sim.set("request_ar", {"ready": 1})
        sim.set("response_b", {
            "valid": 1, "payload": {"id": 3, "resp": 1, "user": 1},
        })
        sim.set("response_r", {
            "valid": 1,
            "payload": {"id": 3, "data": 0x12345678, "resp": 2,
                        "last": 1, "user": 1},
        })
        result = sim.eval()
        for name in ("request_aw", "request_ar"):
            assert result[name]["valid"] == 1
            assert result[name]["payload"] == {
                "id": 3, "addr": 0x124, "len": 3, "size": 2,
                "burst": 1, "lock": 0, "cache": 10, "prot": 5,
                "qos": 6, "region": 9, "user": 1,
            }
        assert result["request_w"]["payload"] == {
            "data": 0xAABBCCDD, "strb": 0b0101, "last": 1, "user": 1,
        }
        assert result["AWREADY"] == result["WREADY"] == result["ARREADY"] == 1
        assert (result["BID"], result["BRESP"], result["BUSER"],
                result["BVALID"]) == (3, 1, 1, 1)
        assert (result["RID"], result["RDATA"], result["RRESP"],
                result["RLAST"], result["RUSER"], result["RVALID"]) == (
                    3, 0x12345678, 2, 1, 1, 1,
                )


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_static_child_protocol_signals_are_generic(
    tmp_path: Path, engine: str,
) -> None:
    source = tmp_path / "generic_child_signals.zhl"
    source.write_text(GENERIC_CHILD_SIGNAL_SOURCE)
    module = compile_source(
        GENERIC_CHILD_SIGNAL_SOURCE, top="GenericChildSignalsTop"
    ).ir
    assert len(module.instance_bindings) == 3
    with zlang.sim.load(source, top="GenericChildSignalsTop", engine=engine) as sim:
        sim.set("input_data", 0x5A)
        sim.set("input_valid", 1)
        sim.set("output_ready", 1)
        output = sim.eval()
        assert output["input_ready"] == 1
        assert output["output_data"] == 0x5A
        assert output["output_valid"] == 1
    rtl = emit_artifact(module).text
    assert "module GenericChildSignalsTop (" in rtl
    assert "GenericChildSignalsTop_zlang_core" not in rtl


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_static_child_protocol_payload_fields_build_exact_record(
    tmp_path: Path, engine: str,
) -> None:
    source = tmp_path / "generic_child_fields.zhl"
    source.write_text(GENERIC_CHILD_FIELDS_SOURCE)
    with zlang.sim.load(source, top="GenericFieldTop", engine=engine) as sim:
        sim.set("first", 0x12)
        sim.set("second", 0x34)
        sim.set("incoming", 1)
        assert sim.eval()["observed"] == {"first": 0x12, "second": 0x34}
    rtl = emit_artifact(compile_source(
        GENERIC_CHILD_FIELDS_SOURCE, top="GenericFieldTop"
    ).ir).text
    assert "module GenericFieldTop (" in rtl
    assert "GenericFieldTop_zlang_core" not in rtl


@pytest.mark.parametrize(
    ("replacement", "message"),
    (
        ("", "missing field 'second'"),
        ("child.request.payload.first = first", "multiple drivers"),
        ("child.request.payload = GenericPayload { first=first second=second }",
         "both whole-value and field drivers"),
    ),
)
def test_static_child_payload_field_bindings_fail_closed(
    replacement: str, message: str,
) -> None:
    source = GENERIC_CHILD_FIELDS_SOURCE.replace(
        "child.request.payload.second = second", replacement,
    )
    with pytest.raises(SemanticError, match=message):
        compile_source(source, top="GenericFieldTop")


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            GENERIC_CHILD_SIGNAL_SOURCE.replace(
                "child.request.payload = input_data",
                "child.response.valid = input_valid\n"
                "    child.request.payload = input_data",
            ),
            "child-owned",
        ),
        (
            GENERIC_CHILD_SIGNAL_SOURCE.replace(
                "child.request.payload = input_data",
                "child.request.payload = output_ready",
            ),
            "expected u8",
        ),
        (
            GENERIC_CHILD_SIGNAL_SOURCE.replace(
                "child.request.valid = input_valid",
                "child.request.valid = input_valid\n"
                "    child.request.valid = input_valid",
            ),
            "multiple drivers",
        ),
        (
            GENERIC_CHILD_SIGNAL_SOURCE.replace(
                "in input_data : u8", "in forwarded : rv<u8>\n"
                "    in input_data : u8",
            ).replace(
                "child.request.payload = input_data",
                "forwarded -> child.request\n"
                "    child.request.payload = input_data",
            ),
            "both a binding and a hierarchical connection",
        ),
    ),
)
def test_static_child_protocol_signal_rejects_unsafe_bindings(
    source: str, message: str,
) -> None:
    with pytest.raises(SemanticError, match=message):
        compile_source(source, top="GenericChildSignalsTop")


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_child_payload_field_projection(
    tmp_path: Path, engine: str,
) -> None:
    source = tmp_path / "axi4_child_field.zhl"
    source.write_text(AXI_CHILD_FIELD_SOURCE)
    module = compile_source(AXI_CHILD_FIELD_SOURCE, top="Axi4ChildFieldTop").ir
    with zlang.sim.load(source, top="Axi4ChildFieldTop", engine=engine) as sim:
        for name, value in {
            "run": 1, "AWREADY": 1, "WREADY": 1,
            "ARREADY": 1, "BVALID": 1, "RVALID": 1,
        }.items():
            sim.set(name, value)
        assert sim.eval()["AWADDR"] == 256
        assert sim.eval()["AWVALID"] == 1
        assert sim.eval()["BREADY"] == 1
    rtl = emit_artifact(module).text
    assert "module Axi4ChildFieldTop (" in rtl
    assert "Axi4ChildFieldTop_zlang_core" not in rtl


@pytest.mark.parametrize(
    ("source", "top"),
    (
        (GENERIC_CHILD_SIGNAL_SOURCE, "GenericChildSignalsTop"),
        (AXI_CHILD_FIELD_SOURCE, "Axi4ChildFieldTop"),
    ),
)
def test_child_protocol_projection_direct_sv_tools(
    source: str, top: str, tmp_path: Path,
) -> None:
    rtl = emit_artifact(compile_source(source, top=top).ir).text
    path = tmp_path / f"{top}.sv"
    path.write_text(rtl)
    if shutil.which("verilator"):
        subprocess.run(
            ["verilator", "--lint-only", "--Werror-PINMISSING",
             "--top-module", top, str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("iverilog"):
        subprocess.run(
            ["iverilog", "-g2012", "-tnull", "-s", top, str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("yosys"):
        subprocess.run(
            ["yosys", "-Q", "-T", "-p",
             f"read_verilog -sv {path}; hierarchy -top {top}; "
             "proc; opt_expr; opt_clean; check"],
            check=True, capture_output=True, text=True,
        )


def test_child_protocol_projection_formal_and_manifest_round_trip() -> None:
    module = compile_source(
        GENERIC_CHILD_SIGNAL_SOURCE, top="GenericChildSignalsTop",
    ).ir
    recursive = build_recursive_formal_design(module)
    artifact = emit_formal_artifact(module, recursive)
    assert "module GenericChildSignalsTop__formal" in artifact.text
    assert "GenericChildSignalsTop_zlang_core" not in artifact.text
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.to_json() == artifact.to_json()
    assert restored.artifact_hash == artifact.artifact_hash
    stale = json.loads(artifact.to_json())
    stale["naming_schema"] = "direct-sv-hierarchical-names-v3"
    with pytest.raises(ValueError, match="unsupported backend naming_schema"):
        BackendArtifact.from_json(stale)


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_generic_child_ready_valid_projection_in_parent_state(
    tmp_path: Path, engine: str,
) -> None:
    text = GENERIC_CHILD_SIGNAL_SOURCE.replace(
        "out output_valid : bit", "out output_valid : bit\n"
        "    out delayed_valid : bit",
    ).replace(
        "child.response.ready = output_ready",
        "child.response.ready = output_ready\n"
        "    reg seen : bit = 0\n"
        "    seen <- child.response.valid\n"
        "    delayed_valid = seen",
    )
    source = tmp_path / "generic_child_state.zhl"
    source.write_text(text)
    with zlang.sim.load(source, top="GenericChildSignalsTop", engine=engine) as sim:
        sim.set("input_data", 17)
        sim.set("input_valid", 1)
        sim.set("output_ready", 1)
        assert sim.eval()["delayed_valid"] == 0
        sim.edge("clk")
        assert sim.eval()["delayed_valid"] == 1


def test_axi4_read_response_stability_formal_artifact(tmp_path: Path) -> None:
    compiled = compile_source(
        AXI4_READ_STABILITY_SOURCE, top="Axi4ReadStabilityProof",
    )
    recursive = build_recursive_formal_design(compiled.ir)
    artifact = emit_formal_artifact(compiled.ir, recursive)
    design = connect_formal_design(compiled.formal_design, artifact)
    assert design.properties
    assert all(item.non_executable_reason is None for item in design.properties)
    harness = emit_harness(design, depth=4)
    assert "Axi4ReadStabilityProof__safety_verification_formal" in harness
    assert "non-executable property report" not in harness
    if shutil.which("yosys"):
        path = tmp_path / "axi4_read_stability_formal.sv"
        path.write_text(harness)
        subprocess.run(
            ["yosys", "-Q", "-T", "-p",
             f"read_verilog -sv -formal {path}; "
             "hierarchy -top Axi4ReadStabilityProof__safety_verification_formal; "
             "proc; opt_expr; opt_clean; check"],
            check=True, capture_output=True, text=True,
        )


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_five_channel_composition_is_protocol_generic(
    tmp_path: Path, engine: str,
) -> None:
    module = compile_source(FIVE_CHANNEL_SOURCE, top="Axi4FiveTop").ir
    source = tmp_path / "axi4_five.zhl"
    source.write_text(FIVE_CHANNEL_SOURCE)
    with zlang.sim.load(source, top="Axi4FiveTop", engine=engine) as sim:
        sim.set("run", 1)
        assert sim.eval()["observed"] == 31
        sim.set("run", 0)
        assert sim.eval()["observed"] == 0
    rtl = emit_artifact(module).text
    assert "module Axi4FiveTop (" in rtl
    assert "Axi4FiveTop_zlang_core" not in rtl


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_connected_child_protocol_signal_remains_readable(
    tmp_path: Path, engine: str,
) -> None:
    source_text = FIVE_CHANNEL_SOURCE.replace(
        "out observed : bits<5>",
        "out observed : bits<5>\n"
        "    out master_aw : bit = master.axi.aw.valid",
    )
    source = tmp_path / "axi4_connected_projection.zhl"
    source.write_text(source_text)
    module = compile_source(source_text, top="Axi4FiveTop").ir
    with zlang.sim.load(source, top="Axi4FiveTop", engine=engine) as sim:
        sim.set("run", 1)
        assert sim.eval()["master_aw"] == 1
        sim.set("run", 0)
        assert sim.eval()["master_aw"] == 0
    rtl = emit_artifact(module).text
    assert "module Axi4FiveTop (" in rtl


def _read_request(identity: int, *, length: int = 0) -> dict[str, int]:
    return {
        "id": identity, "addr": 0x100 + 16 * identity,
        "len": length, "size": 2, "burst": 1, "lock": 0,
        "cache": 0, "prot": 0, "qos": 0, "region": 0,
    }


def _read_cycle(
    *, command_id: int = 0, command_valid: int = 0,
    command_length: int = 0, response_id: int = 0,
    response_valid: int = 0, response_last: int = 0,
) -> dict[str, object]:
    return {
        "command": {
            "valid": command_valid,
            "payload": _read_request(command_id, length=command_length),
        },
        "axi__ar": {"ready": 1},
        "axi__r": {"valid": response_valid, "payload": {
            "id": response_id, "data": response_id + 0x20,
            "resp": 0, "last": response_last,
        }},
        "data": {"ready": 1},
    }


def test_axi4_read_manager_multiple_ids_and_counted_last() -> None:
    module = compile_source(READ_MANAGER_SOURCE, top="Axi4ReadTop").ir
    trace = simulate_cycles(module, (
        _read_cycle(command_id=1, command_valid=1, command_length=1),
        _read_cycle(command_id=2, command_valid=1,
                    response_id=1, response_valid=1),
        _read_cycle(command_id=1, command_valid=1,
                    response_id=2, response_valid=1, response_last=1),
        _read_cycle(response_id=1, response_valid=1, response_last=1),
        _read_cycle(command_id=1, command_valid=1),
    ), reset=(False,) * 5)
    assert [item["command"]["transfer"] for item in trace] == [1, 1, 1, 0, 1]
    assert [item["data"]["transfer"] for item in trace] == [0, 1, 1, 1, 0]
    assert [item["axi__ar"]["payload"]["id"] for item in trace] == [1, 2, 1, 0, 1]
    assert all(item["failed"] == 0 for item in trace)


def test_axi4_read_manager_flags_early_last_without_losing_count() -> None:
    module = compile_source(READ_MANAGER_SOURCE, top="Axi4ReadTop").ir
    trace = simulate_cycles(module, (
        _read_cycle(command_id=3, command_valid=1, command_length=1),
        _read_cycle(response_id=3, response_valid=1, response_last=1),
        _read_cycle(response_id=3, response_valid=1, response_last=1),
        _read_cycle(),
    ), reset=(False,) * 4)
    assert [item["axi__r"]["transfer"] for item in trace] == [0, 1, 1, 0]
    assert trace[-1]["failed"] == 1


def test_axi4_read_manager_repeated_id_preserves_transaction_order() -> None:
    module = compile_source(READ_MANAGER_SOURCE, top="Axi4ReadTop").ir
    trace = simulate_cycles(module, (
        _read_cycle(command_id=3, command_valid=1, command_length=1),
        _read_cycle(command_id=3, command_valid=1),
        _read_cycle(response_id=3, response_valid=1),
        _read_cycle(response_id=3, response_valid=1, response_last=1),
        _read_cycle(response_id=3, response_valid=1, response_last=1),
        _read_cycle(command_id=3, command_valid=1),
    ), reset=(False,) * 6)
    assert [item["command"]["transfer"] for item in trace] == [1, 1, 0, 0, 0, 1]
    assert [item["data"]["transfer"] for item in trace] == [0, 0, 1, 1, 1, 0]
    assert all(item["failed"] == 0 for item in trace)


def test_axi4_write_manager_compiles() -> None:
    compile_source(WRITE_MANAGER_SOURCE, top="Axi4WriteTop")


def _write_cycle(
    *, command_id: int = 0, command_valid: int = 0,
    command_length: int = 0,
    aw_ready: int = 1, write_valid: int = 0, write_last: int = 1,
    response_id: int = 0, response_valid: int = 0,
) -> dict[str, object]:
    return {
        "command": {"valid": command_valid, "payload": _read_request(
            command_id, length=command_length,
        )},
        "write": {"valid": write_valid, "payload": {
            "data": 0x11223344, "strb": 15, "last": write_last,
        }},
        "axi__aw": {"ready": aw_ready},
        "axi__w": {"ready": 1},
        "axi__b": {"valid": response_valid, "payload": {
            "id": response_id, "resp": 0,
        }},
        "completion": {"ready": 1},
    }


@pytest.mark.parametrize(
    ("source", "top", "cycle", "address_channel"),
    (
        (READ_MANAGER_SOURCE, "Axi4ReadTop", _read_cycle, "axi__ar"),
        (WRITE_MANAGER_SOURCE, "Axi4WriteTop", _write_cycle, "axi__aw"),
    ),
)
def test_axi4_manager_rejects_reserved_burst_without_bus_request(
    source: str, top: str, cycle: object, address_channel: str,
) -> None:
    module = compile_source(source, top=top).ir
    first = cycle(command_id=1, command_valid=1)
    first["command"]["payload"]["burst"] = 3
    trace = simulate_cycles(module, (first, cycle()), reset=(False, False))
    assert trace[0]["command"]["transfer"] == 1
    assert trace[0][address_channel]["valid"] == 0
    assert trace[1]["failed"] == 1


def test_axi4_write_manager_w_before_aw_and_outstanding_b() -> None:
    module = compile_source(WRITE_MANAGER_SOURCE, top="Axi4WriteTop").ir
    trace = simulate_cycles(module, (
        _write_cycle(command_id=1, command_valid=1, aw_ready=0),
        _write_cycle(command_id=2, command_valid=1, aw_ready=0, write_valid=1),
        _write_cycle(command_id=2, command_valid=1),
        _write_cycle(command_id=2, command_valid=1),
        _write_cycle(response_id=1, response_valid=1, write_valid=1),
        _write_cycle(response_id=2, response_valid=1),
        _write_cycle(),
    ), reset=(False,) * 7)
    assert [item["axi__aw"]["transfer"] for item in trace] == [0, 0, 1, 0, 1, 0, 0]
    assert [item["axi__w"]["transfer"] for item in trace] == [0, 1, 0, 0, 1, 0, 0]
    assert [item["command"]["transfer"] for item in trace] == [1, 0, 0, 1, 0, 0, 0]
    assert [item["completion"]["transfer"] for item in trace] == [0, 0, 0, 0, 1, 1, 0]
    assert all(item["failed"] == 0 for item in trace)


def test_axi4_write_manager_reordered_b_and_reset() -> None:
    module = compile_source(WRITE_MANAGER_SOURCE, top="Axi4WriteTop").ir
    trace = simulate_cycles(module, (
        _write_cycle(command_id=1, command_valid=1),
        _write_cycle(write_valid=1),
        _write_cycle(command_id=2, command_valid=1),
        _write_cycle(write_valid=1),
        _write_cycle(response_id=2, response_valid=1),
        _write_cycle(response_id=1, response_valid=1),
        _write_cycle(),
    ), reset=(False,) * 7)
    assert [item["completion"]["transfer"] for item in trace] == [
        0, 0, 0, 0, 1, 1, 0,
    ]
    assert [item["completion"]["payload"]["id"] for item in trace[4:6]] == [2, 1]
    assert trace[-1]["failed"] == 0


def test_axi4_write_manager_repeated_id_preserves_response_order() -> None:
    module = compile_source(WRITE_MANAGER_SOURCE, top="Axi4WriteTop").ir
    trace = simulate_cycles(module, (
        _write_cycle(command_id=2, command_valid=1),
        _write_cycle(write_valid=1),
        _write_cycle(command_id=2, command_valid=1),
        _write_cycle(write_valid=1),
        _write_cycle(response_id=2, response_valid=1),
        _write_cycle(response_id=2, response_valid=1),
        _write_cycle(command_id=2, command_valid=1),
    ), reset=(False,) * 7)
    assert [item["command"]["transfer"] for item in trace] == [1, 0, 1, 0, 0, 0, 1]
    assert [item["completion"]["transfer"] for item in trace] == [0, 0, 0, 0, 1, 1, 0]
    assert all(item["failed"] == 0 for item in trace)


def test_axi4_read_manager_max_length_and_reset() -> None:
    module = compile_source(READ_MANAGER_SOURCE, top="Axi4ReadTop").ir
    cycles = [_read_cycle(command_id=1, command_valid=1, command_length=255)]
    cycles.extend(
        _read_cycle(response_id=1, response_valid=1,
                    response_last=int(index == 255))
        for index in range(256)
    )
    cycles.append(_read_cycle(command_id=1, command_valid=1))
    trace = simulate_cycles(module, cycles, reset=(False,) * len(cycles))
    assert sum(item["data"]["transfer"] for item in trace) == 256
    assert trace[-1]["command"]["transfer"] == 1
    assert trace[-1]["failed"] == 0


def _subordinate_read_cycle(
    *, ar_valid: int = 0, ar_id: int = 0, ar_length: int = 0,
    ar_burst: int = 1, ar_lock: int = 0,
    beat_valid: int = 0, beat_data: int = 0,
    beat_last: int = 0, beat_slot: int = 0, beat_resp: int = 0,
    r_ready: int = 1,
) -> dict[str, object]:
    request = _read_request(ar_id, length=ar_length)
    request["burst"] = ar_burst
    request["lock"] = ar_lock
    return {
        "axi__ar": {"valid": ar_valid, "payload": request},
        "axi__r": {"ready": r_ready},
        "backend_request": {"ready": 1},
        "backend_beat": {
            "valid": beat_valid,
            "payload": {
                "slot": beat_slot, "data": beat_data,
                "resp": beat_resp, "last": beat_last,
            },
        },
    }


def test_axi4_read_subordinate_counted_decerr_and_registered_output() -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    module = compile_file(source, top="AXI4ReadSubordinate").ir
    trace = simulate_cycles(module, (
        _subordinate_read_cycle(ar_valid=1, ar_id=3, ar_burst=3),
        _subordinate_read_cycle(r_ready=0),
        _subordinate_read_cycle(r_ready=0),
        _subordinate_read_cycle(),
        _subordinate_read_cycle(),
    ), reset=(False,) * 5)
    assert trace[0]["axi__ar"]["transfer"] == 1
    assert trace[0]["backend_request"]["valid"] == 0
    assert trace[1]["axi__r"]["valid"] == 0
    assert trace[2]["axi__r"]["payload"] == {
        "id": 3, "data": 0, "resp": 3, "last": 1,
    }
    assert trace[2]["axi__r"]["valid"] == 1
    assert trace[3]["axi__r"]["payload"] == trace[2]["axi__r"]["payload"]
    assert trace[3]["axi__r"]["transfer"] == 1
    assert trace[4]["protocol_error"] == 1


def test_axi4_read_subordinate_backend_two_beats() -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    module = compile_file(source, top="AXI4ReadSubordinate").ir
    trace = simulate_cycles(module, (
        _subordinate_read_cycle(ar_valid=1, ar_id=2, ar_length=1),
        _subordinate_read_cycle(beat_valid=1, beat_data=0xAA),
        _subordinate_read_cycle(beat_valid=1, beat_data=0xBB, beat_last=1),
        _subordinate_read_cycle(),
        _subordinate_read_cycle(),
    ), reset=(False,) * 5)
    assert trace[0]["backend_request"]["transfer"] == 1
    assert [item["backend_beat"]["transfer"] for item in trace] == [
        0, 1, 1, 0, 0,
    ]
    assert [item["axi__r"]["payload"]["data"] for item in trace[2:4]] == [
        0xAA, 0xBB,
    ]
    assert [item["axi__r"]["payload"]["last"] for item in trace[2:4]] == [
        0, 1,
    ]
    assert trace[-1]["protocol_error"] == 0


def test_axi4_ordinary_subordinates_never_claim_exclusive_success() -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    reader = compile_file(source, top="AXI4ReadSubordinate").ir
    read_trace = simulate_cycles(reader, (
        _subordinate_read_cycle(ar_valid=1, ar_id=1, ar_lock=1),
        _subordinate_read_cycle(beat_valid=1, beat_slot=0,
                                beat_resp=1, beat_last=1),
        _subordinate_read_cycle(),
    ), reset=(False,) * 3)
    assert read_trace[2]["axi__r"]["payload"]["resp"] == 0

    writer = compile_file(source, top="AXI4WriteSubordinate").ir
    write_trace = simulate_cycles(writer, (
        _subordinate_write_cycle(aw_valid=1, aw_id=1, aw_lock=1),
        _subordinate_write_cycle(w_valid=1),
        _subordinate_write_cycle(backend_result_valid=1,
                                 backend_result_resp=1),
        _subordinate_write_cycle(),
    ), reset=(False,) * 4)
    assert write_trace[3]["axi__b"]["payload"]["resp"] == 0


def test_axi4_read_subordinate_interleaves_ids_but_orders_same_id() -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    module = compile_file(source, top="AXI4ReadSubordinate").ir
    trace = simulate_cycles(module, (
        _subordinate_read_cycle(ar_valid=1, ar_id=1),
        _subordinate_read_cycle(ar_valid=1, ar_id=1),
        _subordinate_read_cycle(ar_valid=1, ar_id=2),
        _subordinate_read_cycle(beat_valid=1, beat_slot=1, beat_data=0xB1),
        _subordinate_read_cycle(beat_valid=1, beat_slot=2, beat_data=0xC2),
        _subordinate_read_cycle(beat_valid=1, beat_slot=0, beat_data=0xA0),
        _subordinate_read_cycle(),
        _subordinate_read_cycle(beat_valid=1, beat_slot=1, beat_data=0xB1),
        _subordinate_read_cycle(),
        _subordinate_read_cycle(),
    ), reset=(False,) * 10)
    assert trace[3]["backend_beat"]["ready"] == 0
    assert trace[4]["backend_beat"]["transfer"] == 1
    assert trace[5]["backend_beat"]["transfer"] == 1
    assert trace[7]["backend_beat"]["transfer"] == 1
    assert [sample["axi__r"]["payload"]["id"] for sample in trace
            if sample["axi__r"]["transfer"]] == [2, 1, 1]
    assert trace[-1]["protocol_error"] == 1  # premature slot-1 beat was rejected


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_read_subordinate_randomized_slot_scoreboard(
    engine: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    rng = Random(2701)
    accepted = 0
    completed = 0
    serial = 0
    slots: dict[int, dict[str, int]] = {}
    expected: deque[tuple[int, dict[str, int]]] = deque()
    pending_command: dict[str, int] | None = None
    pending_beat: dict[str, int] | None = None
    held_output: dict[str, int] | None = None
    with zlang.sim.load(source, top="AXI4ReadSubordinate", engine=engine) as sim:
        sim.set("backend_request", {"ready": 1})
        for cycle in range(600):
            if pending_command is None and accepted < 30 and rng.randrange(4):
                pending_command = _read_request(
                    rng.randrange(3), length=rng.randrange(4),
                )
            eligible = [
                slot for slot, entry in slots.items()
                if entry["remaining"] and not any(
                    older["id"] == entry["id"] and
                    older["serial"] < entry["serial"]
                    for older in slots.values()
                )
            ]
            if pending_beat is None and eligible and rng.randrange(4):
                slot = rng.choice(eligible)
                entry = slots[slot]
                pending_beat = {
                    "slot": slot,
                    "data": (entry["serial"] << 8) | entry["remaining"],
                    "resp": 0,
                    "last": int(entry["remaining"] == 1),
                }
            sim.set("axi__ar", {
                "valid": int(pending_command is not None),
                "payload": pending_command or _read_request(0),
            })
            r_ready = int(rng.randrange(3) != 0)
            sim.set("axi__r", {"ready": r_ready})
            sim.set("backend_beat", {
                "valid": int(pending_beat is not None),
                "payload": pending_beat or {
                    "slot": 0, "data": 0, "resp": 0, "last": 0,
                },
            })
            result = sim.eval()
            response = result["axi__r"]
            if held_output is not None:
                assert response["valid"] == 1
                assert response["payload"] == held_output
            held_output = (
                response["payload"] if response["valid"] and
                not r_ready else None
            )
            if response["transfer"]:
                assert expected
                slot, payload = expected.popleft()
                assert response["payload"] == payload
                if payload["last"]:
                    del slots[slot]
                    completed += 1
            if result["backend_beat"]["transfer"]:
                assert pending_beat is not None
                slot = pending_beat["slot"]
                entry = slots[slot]
                expected.append((slot, {
                    "id": entry["id"], "data": pending_beat["data"],
                    "resp": 0, "last": pending_beat["last"],
                }))
                entry["remaining"] -= 1
                pending_beat = None
            if result["backend_request"]["transfer"]:
                assert pending_command is not None
                slot = result["backend_request"]["payload"]["slot"]
                assert slot not in slots
                slots[slot] = {
                    "id": pending_command["id"],
                    "remaining": pending_command["len"] + 1,
                    "serial": serial,
                }
                serial += 1
                accepted += 1
                pending_command = None
            assert result["protocol_error"] == 0
            sim.edge("clk")
            if accepted == 30 and completed == 30:
                break
        else:
            pytest.fail("randomized read scoreboard did not drain in 600 cycles")
    assert cycle < 600
    assert not slots and not expected and pending_beat is None


def test_axi4_read_subordinate_counted_256_beat_error() -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    module = compile_file(source, top="AXI4ReadSubordinate").ir
    cycles = [_subordinate_read_cycle(ar_valid=1, ar_id=1,
                                      ar_length=255, ar_burst=3)]
    cycles.extend(_subordinate_read_cycle() for _ in range(258))
    trace = simulate_cycles(module, cycles, reset=(False,) * len(cycles))
    responses = [sample["axi__r"] for sample in trace if sample["axi__r"]["transfer"]]
    assert len(responses) == 256
    assert all(item["payload"]["resp"] == 3 for item in responses)
    assert [index for index, item in enumerate(responses)
            if item["payload"]["last"]] == [255]
    assert all(sample["backend_request"]["valid"] == 0 for sample in trace)


@pytest.mark.parametrize(
    "top", ("AXI4ReadSubordinate", "AXI4WriteSubordinate", "AXI4Subordinate")
)
def test_axi4_subordinate_direct_sv_tools(top: str, tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    rtl = emit_artifact(
        compile_file(source, top=top).ir
    ).text
    path = tmp_path / f"{top}.sv"
    path.write_text(rtl)
    if shutil.which("verilator"):
        subprocess.run(
            ["verilator", "--lint-only", "--Werror-PINMISSING",
             "--top-module", top, str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("iverilog"):
        subprocess.run(
            ["iverilog", "-g2012", "-tnull", "-s",
             top, str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("yosys"):
        subprocess.run(
            ["yosys", "-Q", "-T", "-p",
             f"read_verilog -sv {path}; hierarchy -top {top}; "
             "proc; opt_expr; opt_clean; check"],
            check=True, capture_output=True, text=True,
        )


def _subordinate_write_cycle(
    *, aw_valid: int = 0, aw_id: int = 0, aw_length: int = 0,
    aw_burst: int = 1, aw_lock: int = 0,
    w_valid: int = 0, w_last: int = 1,
    b_ready: int = 1, backend_result_valid: int = 0,
    backend_result_slot: int = 0, backend_result_resp: int = 0,
) -> dict[str, object]:
    request = _read_request(aw_id, length=aw_length)
    request["burst"] = aw_burst
    request["lock"] = aw_lock
    return {
        "axi__aw": {"valid": aw_valid, "payload": request},
        "axi__w": {
            "valid": w_valid,
            "payload": {"data": 0x11223344, "strb": 15, "last": w_last},
        },
        "axi__b": {"ready": b_ready},
        "backend_request": {"ready": 1},
        "backend_beat": {"ready": 1},
        "backend_result": {
            "valid": backend_result_valid,
            "payload": {"slot": backend_result_slot, "resp": backend_result_resp},
        },
    }


def test_axi4_write_subordinate_drains_invalid_burst_to_decerr() -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    module = compile_file(source, top="AXI4WriteSubordinate").ir
    trace = simulate_cycles(module, (
        _subordinate_write_cycle(aw_valid=1, aw_id=2, aw_length=1,
                                 aw_burst=3, w_valid=1, w_last=0),
        _subordinate_write_cycle(w_valid=1, w_last=0),
        _subordinate_write_cycle(w_valid=1, w_last=1),
        _subordinate_write_cycle(b_ready=0),
        _subordinate_write_cycle(b_ready=0),
        _subordinate_write_cycle(),
        _subordinate_write_cycle(),
    ), reset=(False,) * 7)
    assert trace[0]["axi__aw"]["transfer"] == 1
    assert trace[0]["axi__w"]["ready"] == 0
    assert trace[0]["backend_request"]["valid"] == 0
    assert [item["axi__w"]["transfer"] for item in trace] == [
        0, 1, 1, 0, 0, 0, 0,
    ]
    assert all(item["backend_beat"]["valid"] == 0 for item in trace)
    assert trace[4]["axi__b"]["payload"] == {
        "id": 2, "resp": 3,
    }
    assert trace[5]["axi__b"]["payload"] == trace[4]["axi__b"]["payload"]
    assert trace[5]["axi__b"]["transfer"] == 1
    assert trace[-1]["protocol_error"] == 1


def test_axi4_write_subordinate_backend_response_waits_for_last_w() -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    module = compile_file(source, top="AXI4WriteSubordinate").ir
    trace = simulate_cycles(module, (
        _subordinate_write_cycle(aw_valid=1, aw_id=1, w_valid=1),
        _subordinate_write_cycle(w_valid=1, w_last=1,
                                 backend_result_valid=1),
        _subordinate_write_cycle(backend_result_valid=1),
        _subordinate_write_cycle(),
        _subordinate_write_cycle(),
    ), reset=(False,) * 5)
    assert trace[0]["backend_request"]["transfer"] == 1
    assert trace[1]["backend_result"]["ready"] == 0
    assert trace[1]["backend_beat"]["transfer"] == 1
    assert trace[2]["backend_result"]["transfer"] == 1
    assert trace[3]["axi__b"]["payload"] == {"id": 1, "resp": 0}
    assert trace[3]["axi__b"]["transfer"] == 1
    assert trace[-1]["protocol_error"] == 1


def test_axi4_write_subordinate_valid_backend_result() -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    module = compile_file(source, top="AXI4WriteSubordinate").ir
    trace = simulate_cycles(module, (
        _subordinate_write_cycle(aw_valid=1, aw_id=1),
        _subordinate_write_cycle(w_valid=1),
        _subordinate_write_cycle(backend_result_valid=1),
        _subordinate_write_cycle(),
        _subordinate_write_cycle(),
    ), reset=(False,) * 5)
    assert trace[2]["backend_result"]["transfer"] == 1
    assert trace[3]["axi__b"]["transfer"] == 1
    assert trace[-1]["protocol_error"] == 0


def test_axi4_write_subordinate_reorders_b_between_ids_not_within_id() -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    module = compile_file(source, top="AXI4WriteSubordinate").ir
    trace = simulate_cycles(module, (
        _subordinate_write_cycle(aw_valid=1, aw_id=1),
        _subordinate_write_cycle(w_valid=1),
        _subordinate_write_cycle(aw_valid=1, aw_id=1),
        _subordinate_write_cycle(w_valid=1),
        _subordinate_write_cycle(aw_valid=1, aw_id=2),
        _subordinate_write_cycle(w_valid=1),
        _subordinate_write_cycle(backend_result_valid=1,
                                 backend_result_slot=2),
        _subordinate_write_cycle(backend_result_valid=1,
                                 backend_result_slot=0),
        _subordinate_write_cycle(),
        _subordinate_write_cycle(backend_result_valid=1,
                                 backend_result_slot=1),
        _subordinate_write_cycle(),
        _subordinate_write_cycle(),
    ), reset=(False,) * 12)
    assert [sample["backend_result"]["transfer"] for sample in trace] == [
        0, 0, 0, 0, 0, 0, 1, 1, 0, 1, 0, 0,
    ]
    assert [sample["axi__b"]["payload"]["id"] for sample in trace
            if sample["axi__b"]["transfer"]] == [2, 1, 1]
    assert trace[-1]["protocol_error"] == 0


def _axi4_held_backend_result_events() -> tuple[dict[str, object], ...]:
    return (
        _subordinate_write_cycle(aw_valid=1, aw_id=1),
        _subordinate_write_cycle(aw_valid=1, aw_id=1),
        _subordinate_write_cycle(w_valid=1),
        _subordinate_write_cycle(backend_result_valid=1,
                                 backend_result_slot=0, b_ready=0),
        _subordinate_write_cycle(backend_result_valid=1,
                                 backend_result_slot=0),
        _subordinate_write_cycle(backend_result_valid=1,
                                 backend_result_slot=0),
        _subordinate_write_cycle(w_valid=1),
        _subordinate_write_cycle(backend_result_valid=1,
                                 backend_result_slot=1),
        _subordinate_write_cycle(),
    )


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_write_subordinate_never_accepts_duplicate_held_result(
    engine: str,
) -> None:
    """A result already buffered for B cannot be accepted twice on retirement."""

    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    events = _axi4_held_backend_result_events()
    trace = []
    with zlang.sim.load(source, top="AXI4WriteSubordinate", engine=engine) as sim:
        for event in events:
            for name, value in event.items():
                sim.set(name, value)
            trace.append(sim.eval())
            sim.edge("clk")
    assert [item["backend_result"]["transfer"] for item in trace] == [
        0, 0, 0, 1, 0, 0, 0, 1, 0,
    ]
    assert [index for index, item in enumerate(trace)
            if item["axi__b"]["transfer"]] == [4, 8]
    assert [item["axi__b"]["payload"] for item in trace
            if item["axi__b"]["transfer"]] == [
        {"id": 1, "resp": 0}, {"id": 1, "resp": 0},
    ]
    assert trace[-1]["protocol_error"] == 1


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_axi4_write_subordinate_duplicate_result_rtl_parity(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    trace = run_differential(
        source, top="AXI4WriteSubordinate",
        events=[{"set": event, "edges": ("clk",)}
                for event in _axi4_held_backend_result_events()],
        directory=tmp_path,
    )
    assert trace.reference == trace.native == trace.direct_sv


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_write_subordinate_randomized_transaction_scoreboard(
    engine: str,
) -> None:
    """Independent AW/W/B model with repeated IDs, slot reuse, and stalls."""

    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    rng = Random(0xA714)
    transactions = [
        {
            "ordinal": index,
            "id": (index * 7 + index // 3) % 4,
            "length": rng.randrange(4),
            "response": (0, 2, 3)[index % 3],
            "address": None,
            "slot": None,
            "remaining": None,
            "beats_sent": 0,
            "completed": False,
            "result_sent": False,
            "retired": False,
        }
        for index in range(30)
    ]
    by_slot: dict[int, dict[str, object]] = {}
    expected_by_id: dict[int, deque[dict[str, object]]] = {
        index: deque() for index in range(4)
    }
    aw_index = 0
    w_index = 0
    held_result: dict[str, object] | None = None
    retired = 0
    with zlang.sim.load(
        source, top="AXI4WriteSubordinate", engine=engine,
    ) as sim:
        for _cycle in range(1200):
            aw = transactions[aw_index] if aw_index < len(transactions) else None
            w = transactions[w_index] if w_index < len(transactions) else None
            candidates = [
                item for item in transactions
                if item["completed"] and not item["result_sent"] and
                all(previous["retired"] or previous["id"] != item["id"]
                    for previous in transactions[:int(item["ordinal"])])
            ]
            if held_result is None and candidates and rng.randrange(3):
                held_result = rng.choice(candidates)
            request = _read_request(int(aw["id"]) if aw else 0,
                                    length=int(aw["length"]) if aw else 0)
            if aw is not None:
                request["addr"] = 0x100 + 4 * int(aw["ordinal"])
            sim.set("axi__aw", {"valid": int(aw is not None),
                                 "payload": request})
            sim.set("axi__w", {"valid": int(w is not None), "payload": {
                "data": 0x11000000 + (int(w["ordinal"]) if w else 0),
                "strb": 15,
                "last": int(w is not None and
                            w["beats_sent"] == w["length"]),
            }})
            sim.set("axi__b", {"ready": rng.randrange(2)})
            sim.set("backend_request", {"ready": rng.randrange(2)})
            sim.set("backend_beat", {"ready": rng.randrange(2)})
            sim.set("backend_result", {
                "valid": int(held_result is not None),
                "payload": {
                    "slot": int(held_result["slot"]) if held_result else 0,
                    "resp": int(held_result["response"]) if held_result else 0,
                },
            })
            sample = sim.eval()
            if sample["axi__aw"]["transfer"]:
                assert aw is not None
                assert sample["backend_request"]["transfer"] == 1
                slot = int(sample["backend_request"]["payload"]["slot"])
                assert slot not in by_slot
                aw["slot"] = slot
                aw["remaining"] = int(aw["length"]) + 1
                by_slot[slot] = aw
                expected_by_id[int(aw["id"])].append(aw)
                aw_index += 1
            if sample["axi__w"]["transfer"]:
                assert w is not None and w["slot"] is not None
                assert sample["backend_beat"]["transfer"] == 1
                assert sample["backend_beat"]["payload"]["slot"] == w["slot"]
                assert sample["backend_beat"]["payload"]["last"] == int(
                    w["remaining"] == 1
                )
                w["beats_sent"] = int(w["beats_sent"]) + 1
                w["remaining"] = int(w["remaining"]) - 1
                if w["remaining"] == 0:
                    w["completed"] = True
                    w_index += 1
            if sample["backend_result"]["transfer"]:
                assert held_result is not None and held_result["completed"]
                held_result["result_sent"] = True
                held_result = None
            if sample["axi__b"]["transfer"]:
                response = sample["axi__b"]["payload"]
                expected = expected_by_id[int(response["id"])].popleft()
                assert expected["result_sent"]
                assert response["resp"] == expected["response"]
                expected["retired"] = True
                del by_slot[int(expected["slot"])]
                retired += 1
            assert sample["protocol_error"] == 0
            sim.edge("clk")
            if retired == len(transactions):
                break
    assert retired == len(transactions)
    assert all(not pending for pending in expected_by_id.values())


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_five_channel_subordinate_composition(engine: str) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_subordinate.zhl"
    module = compile_file(source, top="AXI4Subordinate").ir
    with zlang.sim.load(source, top="AXI4Subordinate", engine=engine) as sim:
        sim.set("axi__ar", {
            "valid": 1, "payload": _read_request(1),
        })
        sim.set("axi__r", {"ready": 1})
        sim.set("axi__aw", {
            "valid": 1, "payload": _read_request(2),
        })
        sim.set("axi__w", {
            "valid": 0,
            "payload": {"data": 0, "strb": 0, "last": 0},
        })
        sim.set("axi__b", {"ready": 1})
        sim.set("backend_read_request", {"ready": 1})
        sim.set("backend_read_beat", {
            "valid": 0,
            "payload": {"slot": 0, "data": 0, "resp": 0, "last": 0},
        })
        sim.set("backend_write_request", {"ready": 1})
        sim.set("backend_write_beat", {"ready": 1})
        sim.set("backend_write_result", {
            "valid": 0, "payload": {"slot": 0, "resp": 0},
        })
        output = sim.eval()
        assert output["axi__aw"]["ready"] == 1
        assert output["axi__ar"]["ready"] == 1
        assert output["backend_read_request"]["valid"] == 1
        assert output["backend_write_request"]["valid"] == 1
    rtl = emit_artifact(module).text
    assert "module AXI4Subordinate (" in rtl
    assert "AXI4Subordinate_zlang_core" not in rtl


def test_axi4_write_manager_max_length_counted_wlast() -> None:
    module = compile_source(WRITE_MANAGER_SOURCE, top="Axi4WriteTop").ir
    cycles = [_write_cycle(command_id=1, command_valid=1, command_length=255)]
    cycles.extend(
        _write_cycle(write_valid=1, write_last=int(index == 255))
        for index in range(256)
    )
    cycles.append(_write_cycle(response_id=1, response_valid=1))
    trace = simulate_cycles(module, cycles, reset=(False,) * len(cycles))
    assert sum(item["axi__w"]["transfer"] for item in trace) == 256
    assert trace[-1]["completion"]["transfer"] == 1
    assert trace[-1]["failed"] == 0


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_write_manager_engine_parity(tmp_path: Path, engine: str) -> None:
    source = tmp_path / "axi4_write.zhl"
    source.write_text(WRITE_MANAGER_SOURCE)
    with zlang.sim.load(source, top="Axi4WriteTop", engine=engine) as sim:
        cycles = (
            _write_cycle(command_id=1, command_valid=1, aw_ready=0),
            _write_cycle(command_id=2, command_valid=1, aw_ready=0, write_valid=1),
            _write_cycle(command_id=2, command_valid=1),
            _write_cycle(response_id=1, response_valid=1),
        )
        outputs = []
        for inputs in cycles:
            for port, value in inputs.items():
                sim.set(port, value)
            outputs.append(sim.eval())
            sim.edge("clk")
        assert [item["axi__w"]["transfer"] for item in outputs] == [0, 1, 0, 0]
        assert [item["axi__aw"]["transfer"] for item in outputs] == [0, 0, 1, 0]
        assert [item["failed"] for item in outputs] == [0, 0, 0, 0]


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_read_manager_engine_parity(tmp_path: Path, engine: str) -> None:
    source = tmp_path / "axi4_read.zhl"
    source.write_text(READ_MANAGER_SOURCE)
    with zlang.sim.load(source, top="Axi4ReadTop", engine=engine) as sim:
        outputs = []
        for inputs in (
            _read_cycle(command_id=1, command_valid=1, command_length=1),
            _read_cycle(command_id=2, command_valid=1,
                        response_id=1, response_valid=1),
            _read_cycle(response_id=2, response_valid=1, response_last=1),
            _read_cycle(response_id=1, response_valid=1, response_last=1),
        ):
            for port, value in inputs.items():
                sim.set(port, value)
            outputs.append(sim.eval())
            sim.edge("clk")
        assert [item["axi__ar"]["payload"]["id"]
                for item in outputs] == [1, 2, 0, 0]
        assert [item["data"]["transfer"] for item in outputs] == [0, 1, 1, 1]
        assert [item["failed"] for item in outputs] == [0, 0, 0, 0]


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_user_read_manager_keeps_address_and_response_user(
    engine: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4.zhl"
    address = {**_read_request(3), "user": 1}
    response = {"id": 3, "data": 0x12345678, "resp": 0,
                "last": 1, "user": 1}
    with zlang.sim.load(
        source, top="AXI4ReadManagerWithUser", engine=engine
    ) as sim:
        sim.set("command", {"valid": 1, "payload": address})
        sim.set("axi__ar", {"ready": 1})
        sim.set("axi__r", {"valid": 0, "payload": response})
        sim.set("data", {"ready": 1})
        first = sim.eval()
        assert first["command"]["transfer"] == 1
        assert first["axi__ar"]["payload"] == address
        assert first["axi__ar"]["transfer"] == 1
        sim.edge("clk")
        sim.set("command", {"valid": 0, "payload": address})
        sim.set("axi__r", {"valid": 1, "payload": response})
        second = sim.eval()
        assert second["data"]["payload"] == response
        assert second["data"]["transfer"] == 1
        assert second["protocol_error"] == 0


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_user_write_manager_keeps_independent_sidebands(
    engine: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4.zhl"
    address = {**_read_request(3), "user": 1}
    write = {"data": 0xAABBCCDD, "strb": 15, "last": 1, "user": 1}
    response = {"id": 3, "resp": 1, "user": 1}
    with zlang.sim.load(
        source, top="AXI4WriteManagerWithUser", engine=engine
    ) as sim:
        sim.set("command", {"valid": 1, "payload": address})
        sim.set("write", {"valid": 1, "payload": write})
        sim.set("completion", {"ready": 1})
        sim.set("axi__aw", {"ready": 0})
        sim.set("axi__w", {"ready": 1})
        sim.set("axi__b", {"valid": 0, "payload": response})
        assert sim.eval()["command"]["transfer"] == 1
        sim.edge("clk")
        sim.set("command", {"valid": 0, "payload": address})
        stalled = sim.eval()
        assert stalled["axi__aw"]["valid"] == 1
        assert stalled["axi__aw"]["payload"] == address
        assert stalled["axi__w"]["valid"] == 1
        assert stalled["axi__w"]["payload"] == write
        assert stalled["axi__w"]["transfer"] == 1
        sim.edge("clk")
        sim.set("write", {"valid": 0, "payload": write})
        sim.set("axi__aw", {"ready": 1})
        assert sim.eval()["axi__aw"]["transfer"] == 1
        sim.edge("clk")
        sim.set("axi__b", {"valid": 1, "payload": response})
        accepted = sim.eval()
        assert accepted["completion"]["payload"] == response
        assert accepted["completion"]["transfer"] == 1
        assert accepted["protocol_error"] == 0


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_axi4_read_manager_direct_sv_lint(tmp_path: Path) -> None:
    module = compile_source(READ_MANAGER_SOURCE, top="Axi4ReadTop").ir
    rtl = emit_artifact(module).text
    assert "module Axi4ReadTop (" in rtl
    assert "Axi4ReadTop_zlang_core" not in rtl
    source = tmp_path / "axi4_read.sv"
    source.write_text(rtl)
    subprocess.run(
        ["verilator", "--lint-only", "--Werror-PINMISSING", "--top-module",
         "Axi4ReadTop", str(source)],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_axi4_write_manager_direct_sv_lint(tmp_path: Path) -> None:
    module = compile_source(WRITE_MANAGER_SOURCE, top="Axi4WriteTop").ir
    rtl = emit_artifact(module).text
    assert "module Axi4WriteTop (" in rtl
    assert "Axi4WriteTop_zlang_core" not in rtl
    source = tmp_path / "axi4_write.sv"
    source.write_text(rtl)
    subprocess.run(
        ["verilator", "--lint-only", "--Werror-PINMISSING", "--top-module",
         "Axi4WriteTop", str(source)],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.parametrize(
    ("source", "top"),
    ((READ_MANAGER_SOURCE, "Axi4ReadTop"),
     (WRITE_MANAGER_SOURCE, "Axi4WriteTop")),
)
def test_axi4_manager_iverilog_and_staged_yosys(
    source: str, top: str, tmp_path: Path,
) -> None:
    rtl = emit_artifact(compile_source(source, top=top).ir).text
    path = tmp_path / f"{top}.sv"
    path.write_text(rtl)
    if shutil.which("iverilog"):
        subprocess.run(
            ["iverilog", "-g2012", "-tnull", "-s", top, str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("yosys"):
        subprocess.run(
            ["yosys", "-Q", "-T", "-p",
             f"read_verilog -sv {path}; hierarchy -top {top}; "
             "proc; opt_expr; opt_clean; check"],
            check=True, capture_output=True, text=True,
        )


def test_axi4_five_channel_manager_composes_without_wrapper(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4.zhl"
    rtl = emit_artifact(compile_file(source, top="AXI4Manager").ir).text
    assert "module AXI4Manager (" in rtl
    assert "AXI4Manager_zlang_core" not in rtl
    path = tmp_path / "AXI4Manager.sv"
    path.write_text(rtl)
    if shutil.which("verilator"):
        subprocess.run(
            ["verilator", "--lint-only", "--Werror-PINMISSING",
             "--top-module", "AXI4Manager", str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("iverilog"):
        subprocess.run(
            ["iverilog", "-g2012", "-tnull", "-s", "AXI4Manager",
             str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("yosys"):
        subprocess.run(
            ["yosys", "-Q", "-T", "-p",
             f"read_verilog -sv {path}; hierarchy -top AXI4Manager; "
             "proc; opt_expr; opt_clean; check"],
            check=True, capture_output=True, text=True,
        )


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_five_channel_loopback_transactions(
    tmp_path: Path, engine: str,
) -> None:
    source = tmp_path / "axi4_loopback.zhl"
    source.write_text(AXI4_LOOPBACK_SOURCE)
    inputs = _axi4_loopback_event_inputs()
    with zlang.sim.load(source, top="Axi4Loopback", engine=engine) as sim:
        for name, value in inputs.items():
            sim.set(name, value)
        sim.set("read_command", {
            "valid": 1, "payload": _read_request(1),
        })
        sim.set("write_command", {
            "valid": 1, "payload": _read_request(2),
        })
        initial = sim.eval()
        assert initial["read_command"]["transfer"] == 1
        assert initial["write_command"]["transfer"] == 1
        assert initial["backend_read_request"]["transfer"] == 1
        assert initial["backend_read_request"]["payload"]["address"]["id"] == 1
        sim.edge("clk")
        sim.set("read_command", {"valid": 0, "payload": _read_request(1)})
        sim.set("write_command", {"valid": 0, "payload": _read_request(2)})
        issued = sim.eval()
        assert issued["backend_write_request"]["transfer"] == 1
        assert issued["backend_write_request"]["payload"]["address"]["id"] == 2
        sim.edge("clk")
        sim.set("backend_read_beat", {
            "valid": 1,
            "payload": {"slot": 0, "data": 0x12345678,
                        "resp": 0, "last": 1},
        })
        transfer = sim.eval()
        assert transfer["backend_read_beat"]["transfer"] == 1
        assert transfer["backend_write_beat"]["transfer"] == 1
        sim.edge("clk")
        sim.set("backend_read_beat", {
            "valid": 0,
            "payload": {"slot": 0, "data": 0x12345678,
                        "resp": 0, "last": 1},
        })
        sim.set("backend_write_result", {
            "valid": 1, "payload": {"slot": 0, "resp": 0},
        })
        read_result = sim.eval()
        assert read_result["read_data"]["payload"]["data"] == 0x12345678
        assert read_result["read_data"]["transfer"] == 1
        assert read_result["backend_write_result"]["transfer"] == 1
        sim.edge("clk")
        sim.set("backend_write_result", {
            "valid": 0, "payload": {"slot": 0, "resp": 0},
        })
        completion = sim.eval()
        assert completion["write_completion"]["payload"] == {
            "id": 2, "resp": 0,
        }
        assert completion["write_completion"]["transfer"] == 1
        assert completion["protocol_error"] == 0


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_axi4_five_channel_loopback_reference_native_rtl_differential(
    tmp_path: Path,
) -> None:
    source = tmp_path / "axi4_loopback.zhl"
    source.write_text(AXI4_LOOPBACK_SOURCE)
    initial = _axi4_loopback_event_inputs()
    initial["read_command"] = {"valid": 1, "payload": _read_request(1)}
    initial["write_command"] = {"valid": 1, "payload": _read_request(2)}
    events = [
        {"set": initial, "edges": ("clk",)},
        {"set": {
            "read_command": {"valid": 0, "payload": _read_request(1)},
            "write_command": {"valid": 0, "payload": _read_request(2)},
        }, "edges": ("clk",)},
        {"set": {"backend_read_beat": {
            "valid": 1,
            "payload": {"slot": 0, "data": 0x12345678,
                        "resp": 0, "last": 1},
        }}, "edges": ("clk",)},
        {"set": {
            "backend_read_beat": {"valid": 0,
                                  "payload": {"slot": 0, "data": 0x12345678,
                                              "resp": 0, "last": 1}},
            "backend_write_result": {"valid": 1,
                                     "payload": {"slot": 0, "resp": 0}},
        }, "edges": ("clk",)},
        {"set": {"backend_write_result": {
            "valid": 0, "payload": {"slot": 0, "resp": 0},
        }}, "edges": ("clk",)},
    ]
    trace = run_differential(
        source, top="Axi4Loopback", events=events, directory=tmp_path,
    )
    assert trace.reference == trace.native == trace.direct_sv


def _exclusive_monitor_event(
    *, reserve: int = 0, conditional: int = 0,
    write: int = 0, write_address: int = 0,
    write_strobe: int = 0, request_address: int = 0x100,
    identity: int = 1,
) -> dict[str, object]:
    request = _read_request(identity)
    request["addr"] = request_address
    request["lock"] = 1
    return {
        "reserve": {"valid": reserve, "payload": request},
        "conditional": {"valid": conditional, "payload": request},
        "committed_write": {
            "valid": write,
            "payload": {"beat_address": write_address,
                        "strb": write_strobe},
        },
    }


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_exclusive_monitor_atomic_conflict_wins(
    engine: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_exclusive.zhl"
    events = (
        _exclusive_monitor_event(reserve=1),
        _exclusive_monitor_event(write=1, write_address=0x200,
                                 write_strobe=15),
        _exclusive_monitor_event(conditional=1, write=1,
                                 write_address=0x100, write_strobe=1),
        _exclusive_monitor_event(reserve=1),
        _exclusive_monitor_event(conditional=1),
        _exclusive_monitor_event(conditional=1),
    )
    with zlang.sim.load(source, top="AXI4ExclusiveMonitor", engine=engine) as sim:
        grants = []
        errors = []
        for event in events:
            for name, value in event.items():
                sim.set(name, value)
            result = sim.eval()
            grants.append(result["grant"])
            errors.append(result["protocol_error"])
            sim.edge("clk")
    assert grants == [0, 0, 0, 0, 1, 0]
    assert errors == [0] * len(events)


@pytest.mark.parametrize("engine", ("reference", "native"))
@pytest.mark.parametrize("field,value", (
    ("region", 1), ("cache", 1), ("prot", 1),
))
def test_axi4_exclusive_monitor_requires_matching_access_attributes(
    engine: str, field: str, value: int,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_exclusive.zhl"
    reservation = _read_request(1)
    reservation["lock"] = 1
    conditional = dict(reservation)
    conditional[field] = value
    with zlang.sim.load(source, top="AXI4ExclusiveMonitor", engine=engine) as sim:
        sim.set("reserve", {"valid": 1, "payload": reservation})
        sim.set("conditional", {"valid": 0, "payload": reservation})
        sim.set("committed_write", {
            "valid": 0, "payload": {"beat_address": 0, "strb": 0},
        })
        sim.edge("clk")
        sim.set("reserve", {"valid": 0, "payload": reservation})
        sim.set("conditional", {"valid": 1, "payload": conditional})
        assert sim.eval()["grant"] == 0


@pytest.mark.parametrize("top", (
    "AXI4ExclusiveMonitor", "AXI4ExclusiveSubordinate",
))
def test_axi4_exclusive_monitor_direct_sv_tools(
    tmp_path: Path, top: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_exclusive.zhl"
    rtl = emit_artifact(compile_file(source, top=top).ir).text
    path = tmp_path / f"{top}.sv"
    path.write_text(rtl)
    if shutil.which("verilator"):
        subprocess.run(
            ["verilator", "--lint-only", "--Werror-PINMISSING",
             "--top-module", top, str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("iverilog"):
        subprocess.run(
            ["iverilog", "-g2012", "-tnull", "-s", top,
             str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("yosys"):
        subprocess.run(
            ["yosys", "-Q", "-T", "-p",
             f"read_verilog -sv {path}; hierarchy -top {top}; "
             "proc; opt_expr; opt_clean; check"],
            check=True, capture_output=True, text=True,
        )


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_exclusive_ram_witness_gates_atomic_write(engine: str) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_exclusive.zhl"
    request = _read_request(1)
    request["addr"] = 0
    request["lock"] = 1
    with zlang.sim.load(source, top="AXI4ExclusiveRamWitness", engine=engine) as sim:
        sim.set("reserve", {"valid": 1, "payload": request})
        sim.set("conditional", {"valid": 0, "payload": request})
        sim.set("normal_write", {
            "valid": 0, "payload": {"beat_address": 0, "strb": 0},
        })
        sim.set("read_address", 0)
        sim.set("conditional_data", 0x11223344)
        sim.set("normal_data", 0xAABBCCDD)
        sim.edge("clk")
        sim.set("reserve", {"valid": 0, "payload": request})
        sim.set("conditional", {"valid": 1, "payload": request})
        assert sim.eval()["exclusive_result"] == 1
        sim.edge("clk")
        sim.set("conditional", {"valid": 0, "payload": request})
        sim.edge("clk")
        assert sim.eval()["read_data"] == 0x11223344
        sim.set("conditional", {"valid": 1, "payload": request})
        assert sim.eval()["exclusive_result"] == 0
        sim.set("normal_write", {
            "valid": 1, "payload": {"beat_address": 0, "strb": 15},
        })
        assert sim.eval()["conditional"]["ready"] == 0
        sim.edge("clk")
        sim.set("normal_write", {
            "valid": 0, "payload": {"beat_address": 0, "strb": 0},
        })
        sim.set("conditional", {"valid": 0, "payload": request})
        sim.edge("clk")
        assert sim.eval()["read_data"] == 0xAABBCCDD


@pytest.mark.parametrize("engine", ("reference", "native"))
@pytest.mark.parametrize("conflict", (0, 1))
@pytest.mark.parametrize("result_resp", (0, 1, 2))
@pytest.mark.parametrize("reset_between", (False, True))
@pytest.mark.parametrize("malformed_read_last", (False, True))
@pytest.mark.parametrize("malformed_write_last", (False, True))
def test_axi4_exclusive_subordinate_same_edge_commit_contract(
    engine: str, conflict: int, result_resp: int, reset_between: bool,
    malformed_read_last: bool, malformed_write_last: bool,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_exclusive.zhl"
    request = _read_request(1)
    request["lock"] = 1
    write = {"data": 0x12345678, "strb": 15,
             "last": int(not malformed_write_last)}
    with zlang.sim.load(
        source, top="AXI4ExclusiveSubordinate", engine=engine,
    ) as sim:
        for name, value in {
            "axi__ar": {"valid": 0, "payload": request},
            "axi__r": {"ready": 1},
            "axi__aw": {"valid": 0, "payload": request},
            "axi__w": {"valid": 0, "payload": write},
            "axi__b": {"ready": 1},
            "backend_read_request": {"ready": 1},
            "backend_read_beat": {"valid": 0, "payload": {
                "slot": 0, "data": 0, "resp": 0, "last": 1,
            }},
            "backend_write_request": {"ready": 1},
            "backend_write_beat": {"ready": 1},
            "backend_write_result": {"valid": 0, "payload": {
                "slot": 0, "resp": 0,
            }},
            "committed_write": {"valid": 0, "payload": {
                "beat_address": 0, "strb": 0,
            }},
        }.items():
            sim.set(name, value)
        sim.set("axi__ar", {"valid": 1, "payload": request})
        assert sim.eval()["backend_read_request"]["transfer"] == 1
        sim.edge("clk")
        sim.set("axi__ar", {"valid": 0, "payload": request})
        sim.set("backend_read_beat", {"valid": 1, "payload": {
            "slot": 0, "data": 0xAABBCCDD, "resp": 0,
            "last": int(not malformed_read_last),
        }})
        assert sim.eval()["backend_read_beat"]["transfer"] == 1
        sim.edge("clk")
        sim.set("backend_read_beat", {"valid": 0, "payload": {
            "slot": 0, "data": 0, "resp": 0, "last": 1,
        }})
        assert sim.eval()["axi__r"]["payload"]["resp"] == (
            2 if malformed_read_last else 1
        )
        sim.edge("clk")
        if reset_between:
            sim.reset("rst", asserted=True)
            sim.edge("clk")
            sim.reset("rst", asserted=False)
            sim.edge("clk")
        sim.set("axi__aw", {"valid": 1, "payload": request})
        assert sim.eval()["backend_write_request"]["transfer"] == 1
        sim.edge("clk")
        sim.set("axi__aw", {"valid": 0, "payload": request})
        sim.set("axi__w", {"valid": 1, "payload": write})
        assert sim.eval()["backend_write_beat"]["transfer"] == 1
        sim.edge("clk")
        sim.set("axi__w", {"valid": 0, "payload": write})
        sim.set("backend_write_result", {"valid": 1, "payload": {
            "slot": 0, "resp": result_resp,
        }})
        sim.set("committed_write", {"valid": conflict, "payload": {
            "beat_address": request["addr"], "strb": 15,
        }})
        assert sim.eval()["backend_write_result"]["transfer"] == 1
        expected_grant = int(result_resp == 0 and not conflict and
                             not reset_between and not malformed_read_last and
                             not malformed_write_last)
        assert sim.eval()["exclusive_grant"] == expected_grant
        sim.edge("clk")
        sim.set("backend_write_result", {"valid": 0, "payload": {
            "slot": 0, "resp": 0,
        }})
        expected_resp = expected_grant if result_resp <= 1 else result_resp
        assert sim.eval()["axi__b"]["payload"] == {
            "id": 1, "resp": expected_resp,
        }
        assert sim.eval()["axi__b"]["transfer"] == 1
        assert sim.eval()["protocol_error"] == int(
            result_resp == 1 or malformed_write_last or
            (malformed_read_last and not reset_between)
        )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_axi4_exclusive_subordinate_reference_native_rtl_differential(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_exclusive.zhl"
    request = _read_request(1)
    request["lock"] = 1
    write = {"data": 0x12345678, "strb": 15, "last": 1}
    events = [
        {"set": {
            "axi__ar": {"valid": 1, "payload": request},
            "axi__r": {"ready": 1},
            "axi__aw": {"valid": 0, "payload": request},
            "axi__w": {"valid": 0, "payload": write},
            "axi__b": {"ready": 1},
            "backend_read_request": {"ready": 1},
            "backend_read_beat": {"valid": 0, "payload": {
                "slot": 0, "data": 0, "resp": 0, "last": 1,
            }},
            "backend_write_request": {"ready": 1},
            "backend_write_beat": {"ready": 1},
            "backend_write_result": {"valid": 0, "payload": {
                "slot": 0, "resp": 0,
            }},
            "committed_write": {"valid": 0, "payload": {
                "beat_address": 0, "strb": 0,
            }},
        }, "edges": ("clk",)},
        {"set": {
            "axi__ar": {"valid": 0, "payload": request},
            "backend_read_beat": {"valid": 1, "payload": {
                "slot": 0, "data": 0xAABBCCDD, "resp": 0, "last": 1,
            }},
        }, "edges": ("clk",)},
        {"set": {"backend_read_beat": {"valid": 0, "payload": {
            "slot": 0, "data": 0, "resp": 0, "last": 1,
        }}}, "edges": ("clk",)},
        {"set": {"axi__aw": {"valid": 1, "payload": request}},
         "edges": ("clk",)},
        {"set": {
            "axi__aw": {"valid": 0, "payload": request},
            "axi__w": {"valid": 1, "payload": write},
        }, "edges": ("clk",)},
        {"set": {
            "axi__w": {"valid": 0, "payload": write},
            "backend_write_result": {"valid": 1, "payload": {
                "slot": 0, "resp": 0,
            }},
            "committed_write": {"valid": 1, "payload": {
                "beat_address": request["addr"], "strb": 15,
            }},
        }, "edges": ("clk",)},
        {"set": {
            "backend_write_result": {"valid": 0, "payload": {
                "slot": 0, "resp": 0,
            }},
            "committed_write": {"valid": 0, "payload": {
                "beat_address": 0, "strb": 0,
            }},
        }, "edges": ("clk",)},
    ]
    trace = run_differential(
        source, top="AXI4ExclusiveSubordinate", events=events,
        directory=tmp_path,
    )
    assert trace.reference == trace.native == trace.direct_sv


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_user_read_subordinate_preserves_address_and_beat_user(
    engine: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_user_subordinate.zhl"
    request = {**_read_request(2), "user": 1}
    with zlang.sim.load(
        source, top="AXI4ReadSubordinateWithUser", engine=engine
    ) as sim:
        sim.set("axi__ar", {"valid": 1, "payload": request})
        sim.set("axi__r", {"ready": 1})
        sim.set("backend_request", {"ready": 1})
        sim.set("backend_beat", {
            "valid": 0,
            "payload": {"slot": 0, "data": 0xCAFE, "resp": 0,
                        "last": 1, "user": 1},
        })
        first = sim.eval()
        assert first["backend_request"]["payload"]["address"] == request
        assert first["backend_request"]["transfer"] == 1
        sim.edge("clk")
        sim.set("axi__ar", {"valid": 0, "payload": request})
        sim.set("backend_beat", {
            "valid": 1,
            "payload": {"slot": 0, "data": 0xCAFE, "resp": 0,
                        "last": 1, "user": 1},
        })
        assert sim.eval()["backend_beat"]["transfer"] == 1
        sim.edge("clk")
        response = sim.eval()["axi__r"]
        assert response["payload"] == {
            "id": 2, "data": 0xCAFE, "resp": 0, "last": 1, "user": 1,
        }
        assert response["transfer"] == 1


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_user_write_subordinate_preserves_all_three_user_fields(
    engine: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_user_subordinate.zhl"
    request = {**_read_request(2), "user": 1}
    write = {"data": 0x12345678, "strb": 15, "last": 1, "user": 1}
    result = {"slot": 0, "resp": 0, "user": 1}
    with zlang.sim.load(
        source, top="AXI4WriteSubordinateWithUser", engine=engine
    ) as sim:
        sim.set("axi__aw", {"valid": 1, "payload": request})
        sim.set("axi__w", {"valid": 0, "payload": write})
        sim.set("axi__b", {"ready": 1})
        sim.set("backend_request", {"ready": 1})
        sim.set("backend_beat", {"ready": 1})
        sim.set("backend_result", {"valid": 0, "payload": result})
        first = sim.eval()
        assert first["backend_request"]["payload"]["address"] == request
        assert first["backend_request"]["transfer"] == 1
        sim.edge("clk")
        sim.set("axi__aw", {"valid": 0, "payload": request})
        sim.set("axi__w", {"valid": 1, "payload": write})
        second = sim.eval()
        assert second["backend_beat"]["payload"]["user"] == 1
        assert second["backend_beat"]["transfer"] == 1
        sim.edge("clk")
        sim.set("axi__w", {"valid": 0, "payload": write})
        sim.set("backend_result", {"valid": 1, "payload": result})
        assert sim.eval()["backend_result"]["transfer"] == 1
        sim.edge("clk")
        response = sim.eval()["axi__b"]
        assert response["payload"] == {"id": 2, "resp": 0, "user": 1}
        assert response["transfer"] == 1


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_axi4_user_five_channel_subordinate_issues_both_addresses(
    engine: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_user_subordinate.zhl"
    read = {**_read_request(1), "user": 1}
    write = {**_read_request(2), "user": 1}
    with zlang.sim.load(
        source, top="AXI4SubordinateWithUser", engine=engine
    ) as sim:
        sim.set("axi__ar", {"valid": 1, "payload": read})
        sim.set("axi__r", {"ready": 1})
        sim.set("axi__aw", {"valid": 1, "payload": write})
        sim.set("axi__w", {
            "valid": 0,
            "payload": {"data": 0, "strb": 0, "last": 0, "user": 0},
        })
        sim.set("axi__b", {"ready": 1})
        sim.set("backend_read_request", {"ready": 1})
        sim.set("backend_read_beat", {
            "valid": 0,
            "payload": {"slot": 0, "data": 0, "resp": 0,
                        "last": 0, "user": 0},
        })
        sim.set("backend_write_request", {"ready": 1})
        sim.set("backend_write_beat", {"ready": 1})
        sim.set("backend_write_result", {
            "valid": 0, "payload": {"slot": 0, "resp": 0, "user": 0},
        })
        result = sim.eval()
        assert result["backend_read_request"]["payload"]["address"] == read
        assert result["backend_write_request"]["payload"]["address"] == write
        assert result["axi__ar"]["ready"] == 1
        assert result["axi__aw"]["ready"] == 1
        assert result["protocol_error"] == 0


@pytest.mark.parametrize(
    "top",
    ("AXI4ReadSubordinateWithUser", "AXI4WriteSubordinateWithUser",
     "AXI4SubordinateWithUser"),
)
def test_axi4_user_subordinate_direct_sv_tools(
    top: str, tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[2] / "stdlib/bus/axi4_user_subordinate.zhl"
    rtl = emit_artifact(compile_file(source, top=top).ir).text
    assert f"module {top} (" in rtl
    assert f"{top}_zlang_core" not in rtl
    path = tmp_path / f"{top}.sv"
    path.write_text(rtl)
    if shutil.which("verilator"):
        subprocess.run(
            ["verilator", "--lint-only", "--Werror-PINMISSING",
             "--top-module", top, str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("iverilog"):
        subprocess.run(
            ["iverilog", "-g2012", "-tnull", "-s", top, str(path)],
            check=True, capture_output=True, text=True,
        )
    if shutil.which("yosys"):
        subprocess.run(
            ["yosys", "-Q", "-T", "-p",
             f"read_verilog -sv {path}; hierarchy -top {top}; "
             "proc; opt_expr; opt_clean; check"],
            check=True, capture_output=True, text=True,
        )
