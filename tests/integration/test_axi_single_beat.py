"""Source-owned single-beat AXI subset, not a compiler bus special case."""

from __future__ import annotations

from pathlib import Path

import pytest

import zlang
from tests.simulation.differential import run_differential
from zlang.compiler import compile_file
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples/axi_single_beat.zhl"


@pytest.mark.parametrize("engine", ("reference", "native"))
@pytest.mark.parametrize(
    ("top", "aligned", "unaligned", "size"),
    (
        ("AxiSingleBeatAddressExample", 0x100, 0x102, 2),
        ("AxiSingleBeatAddress64Example", 0x100, 0x104, 3),
    ),
)
def test_single_beat_address_helper_reports_alignment(
    engine: str, top: str, aligned: int, unaligned: int, size: int
) -> None:
    program = zlang.sim.compile(
        SOURCE, top=top, engine=engine
    )
    with program.create() as instance:
        instance.set("address", aligned)
        assert instance.eval() == {
            "payload": {"addr": aligned, "len": 0, "size": size},
            "valid": 1,
        }
        instance.set("address", unaligned)
        assert instance.eval()["valid"] == 0


def test_single_beat_address_helper_fails_closed_for_unsupported_width(
    tmp_path: Path,
) -> None:
    source = tmp_path / "invalid_geometry.zhl"
    source.write_text(
        "import std.bus.axi_burst\n"
        "module InvalidGeometry { in address:u32 out valid:bit "
        "valid=axi_single_beat_address<32,24>(address).valid }\n",
        encoding="utf-8",
    )
    program = zlang.sim.compile(source, top="InvalidGeometry", engine="reference")
    with program.create() as instance:
        instance.set("address", 0x100)
        assert instance.eval() == {"valid": 0}


def _writer_cycle(
    *,
    start: int = 0,
    address: int = 0x100,
    data: int = 0x12345678,
    aw_ready: int = 0,
    w_ready: int = 0,
    b_valid: int = 0,
    b_resp: int = 0,
) -> dict[str, object]:
    return {
        "start": start,
        "address": address,
        "data": data,
        "axi__aw": {"ready": aw_ready},
        "axi__w": {"ready": w_ready},
        "axi__b": {"payload": {"resp": b_resp}, "valid": b_valid},
    }


@pytest.mark.parametrize("first", ("aw", "w"))
def test_single_beat_writer_accepts_either_channel_first(first: str) -> None:
    cycles = [
        _writer_cycle(),
        _writer_cycle(start=1),
        _writer_cycle(start=1, address=0x200, data=0xDEADBEEF),
        _writer_cycle(aw_ready=int(first == "aw"), w_ready=int(first == "w")),
        _writer_cycle(aw_ready=int(first == "aw"), w_ready=int(first == "w")),
        _writer_cycle(aw_ready=int(first == "w"), w_ready=int(first == "aw")),
        _writer_cycle(b_valid=1, b_resp=2),
        _writer_cycle(),
        _writer_cycle(),
    ]
    module = compile_file(SOURCE, top="AxiSingleBeatWriteExample").ir
    result = simulate_cycles(module, cycles, reset=[True] + [False] * 8)
    assert result[2]["axi__aw"]["payload"] == {
        "addr": 0x100,
        "len": 0,
        "size": 2,
    }
    assert result[2]["axi__w"]["payload"] == {
        "data": 0x12345678,
        "last": 1,
    }
    assert sum(item["axi__aw"]["transfer"] for item in result) == 1
    assert sum(item["axi__w"]["transfer"] for item in result) == 1
    assert result[6]["axi__b"]["transfer"] == 1
    assert (result[7]["busy"], result[7]["done"], result[7]["error"]) == (
        0,
        1,
        1,
    )
    assert result[8]["done"] == 0


def test_single_beat_writer_accepts_aw_and_w_on_same_edge() -> None:
    module = compile_file(SOURCE, top="AxiSingleBeatWriteExample").ir
    cycles = [
        _writer_cycle(),
        _writer_cycle(start=1),
        _writer_cycle(aw_ready=1, w_ready=1),
        _writer_cycle(b_valid=1),
        _writer_cycle(),
    ]
    result = simulate_cycles(module, cycles, reset=[True] + [False] * 4)
    assert result[2]["axi__aw"]["transfer"] == 1
    assert result[2]["axi__w"]["transfer"] == 1
    assert result[3]["axi__b"]["transfer"] == 1
    assert (result[4]["busy"], result[4]["done"], result[4]["error"]) == (
        0, 1, 0,
    )


def test_single_beat_writer_rejects_unaligned_address_without_bus_activity() -> None:
    cycles = [
        _writer_cycle(),
        _writer_cycle(start=1, address=0x102),
        _writer_cycle(aw_ready=1, w_ready=1, b_valid=1),
        _writer_cycle(),
    ]
    module = compile_file(SOURCE, top="AxiSingleBeatWriteExample").ir
    result = simulate_cycles(module, cycles, reset=[True, False, False, False])
    assert all(
        item[channel]["transfer"] == 0
        for item in result
        for channel in ("axi__aw", "axi__w", "axi__b")
    )
    assert (result[2]["done"], result[2]["error"]) == (1, 1)


def test_single_beat_writer_reset_discards_partial_write() -> None:
    cycles = [
        _writer_cycle(),
        _writer_cycle(start=1),
        _writer_cycle(aw_ready=1),
        _writer_cycle(w_ready=0),
        _writer_cycle(w_ready=1, b_valid=1),
        _writer_cycle(start=1, address=0x200, data=0xABCD),
        _writer_cycle(aw_ready=1, w_ready=1),
        _writer_cycle(b_valid=1),
        _writer_cycle(),
    ]
    module = compile_file(SOURCE, top="AxiSingleBeatWriteExample").ir
    result = simulate_cycles(
        module, cycles,
        reset=[True, False, False, True, False, False, False, False, False],
    )
    assert result[4]["axi__w"]["valid"] == 0
    assert result[4]["axi__b"]["ready"] == 0
    assert result[6]["axi__aw"]["payload"]["addr"] == 0x200
    assert result[6]["axi__w"]["payload"]["data"] == 0xABCD
    assert result[8]["done"] == 1


def test_single_beat_reader_is_fixed_length_burst_reader() -> None:
    module = compile_file(SOURCE, top="AxiSingleBeatReadExample").ir
    cycles = [
        {"start": 0, "address": 0x100, "data": {"ready": 1},
         "axi__ar": {"ready": 0},
         "axi__r": {"payload": {"data": 0, "last": 0, "resp": 0}, "valid": 0}},
        {"start": 1, "address": 0x100, "data": {"ready": 1},
         "axi__ar": {"ready": 0},
         "axi__r": {"payload": {"data": 0, "last": 0, "resp": 0}, "valid": 0}},
        {"start": 0, "address": 0x200, "data": {"ready": 1},
         "axi__ar": {"ready": 1},
         "axi__r": {"payload": {"data": 0, "last": 0, "resp": 0}, "valid": 0}},
        {"start": 0, "address": 0x200, "data": {"ready": 1},
         "axi__ar": {"ready": 0},
         "axi__r": {"payload": {"data": 0xCAFE, "last": 1, "resp": 0}, "valid": 1}},
        {"start": 0, "address": 0x200, "data": {"ready": 1},
         "axi__ar": {"ready": 0},
         "axi__r": {"payload": {"data": 0, "last": 0, "resp": 0}, "valid": 0}},
    ]
    result = simulate_cycles(module, cycles, reset=[True] + [False] * 4)
    assert result[2]["axi__ar"]["payload"] == {
        "addr": 0x100, "len": 0, "size": 2
    }
    assert result[3]["data"]["payload"] == 0xCAFE
    assert result[3]["data"]["transfer"] == 1
    assert (result[4]["done"], result[4]["error"]) == (1, 0)


def test_single_beat_reader_reports_bad_last_and_response() -> None:
    module = compile_file(SOURCE, top="AxiSingleBeatReadExample").ir
    idle = {
        "start": 0, "address": 0x100, "data": {"ready": 1},
        "axi__ar": {"ready": 0},
        "axi__r": {"payload": {"data": 0, "last": 0, "resp": 0},
                   "valid": 0},
    }
    cycles = [
        idle,
        {**idle, "start": 1},
        {**idle, "axi__ar": {"ready": 1}},
        {**idle, "axi__r": {"payload": {"data": 5, "last": 0,
                                       "resp": 2}, "valid": 1}},
        idle,
    ]
    result = simulate_cycles(module, cycles, reset=[True] + [False] * 4)
    assert result[3]["axi__r"]["transfer"] == 1
    assert (result[4]["done"], result[4]["error"]) == (1, 1)


@pytest.mark.parametrize("top", (
    "AxiSingleBeatReadExample", "AxiSingleBeatWriteExample"
))
def test_single_beat_reference_native_and_rtl_agree(
    tmp_path: Path,
    top: str,
) -> None:
    if top == "AxiSingleBeatReadExample":
        events = (
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}},
            {"set": {"start": 1, "address": 0x100,
                     "data": {"ready": 1},
                     "axi__ar": {"ready": 0},
                     "axi__r": {"payload": {"data": 0, "last": 0,
                                             "resp": 0}, "valid": 0}},
             "edges": ["clk"]},
            {"set": {"start": 0, "axi__ar": {"ready": 1}},
             "edges": ["clk"]},
            {"set": {"axi__r": {"payload": {"data": 0xCAFE,
                                            "last": 1, "resp": 0},
                                  "valid": 1}}, "edges": ["clk"]},
            {"set": {"axi__r": {"payload": {"data": 0, "last": 0,
                                            "resp": 0}, "valid": 0}}},
        )
    else:
        events = (
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}},
            {"set": {"start": 1, "address": 0x100, "data": 0x12345678,
                     "axi__aw": {"ready": 0}, "axi__w": {"ready": 0},
                     "axi__b": {"payload": {"resp": 0}, "valid": 0}},
             "edges": ["clk"]},
            {"set": {"start": 0, "axi__w": {"ready": 1}},
             "edges": ["clk"]},
            {"set": {"axi__w": {"ready": 0}, "axi__aw": {"ready": 1}},
             "edges": ["clk"]},
            {"set": {"axi__aw": {"ready": 0},
                     "axi__b": {"payload": {"resp": 2}, "valid": 1}},
             "edges": ["clk"]},
            {"set": {"axi__b": {"payload": {"resp": 0}, "valid": 0}}},
        )
    trace = run_differential(
        SOURCE, top=top, events=events, directory=tmp_path / top
    )
    assert trace.reference == trace.native == trace.direct_sv
