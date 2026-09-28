"""Wide packed storage uses bounded bit spans, not wide arithmetic."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.simulation.differential import run_differential


SOURCE = Path(__file__).resolve().parents[1] / "fixtures/wide_packed_simulation.zhl"
FIRST = (1 << 1066) | (1 << 513) | (1 << 65) | 0x1234
LAST = (1 << 1059) | (1 << 255) | 0xABCD


@pytest.mark.parametrize("top", ("WidePackedQueue", "WideIndexedCounters"))
def test_wide_packed_state_matches_native_and_rtl(
    tmp_path: Path, top: str
) -> None:
    if top == "WidePackedQueue":
        events = (
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}},
            {"set": {"write": 1, "index": 0, "data": FIRST}, "edges": ["clk"]},
            {"set": {"write": 0}},
            {"set": {"write": 1, "index": 1, "data": LAST}, "edges": ["clk"]},
            {"set": {"write": 0}},
            {"set": {"write": 1, "index": 2, "data": FIRST}, "edges": ["clk"]},
            {"set": {"write": 0}},
            {"set": {"write": 1, "index": 3, "data": LAST}, "edges": ["clk"]},
            {"set": {"write": 0, "index": 0}},
            {"set": {"index": 3}},
            {"reset": {"rst": True}, "edges": ["clk"]},
        )
        expected = {
            3: FIRST,
            5: LAST,
            7: FIRST,
            9: FIRST,
            10: LAST,
            11: 0,
        }
        output = "read_data"
    else:
        events = (
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}},
            {"set": {"increment": 1, "index": 255}, "edges": ["clk"]},
            {"edges": ["clk"]},
            {"set": {"increment": 0, "index": 0}},
            {"set": {"index": 255}},
            {"reset": {"rst": True}, "edges": ["clk"]},
        )
        expected = {3: 2, 4: 0, 5: 2, 6: 0}
        output = "count"
    trace = run_differential(
        SOURCE, top=top, events=events, directory=tmp_path / top
    )
    assert trace.native == trace.direct_sv
    for event, value in expected.items():
        assert trace.native[event][output] == value


def test_wide_memory_cell_matches_native_and_rtl(tmp_path: Path) -> None:
    source = tmp_path / "wide_memory.zhl"
    source.write_text(
        """module WideMemoryCell {
    clock clk reset rst
    in write : bit
    in address : u2
    in data : bits<1067>
    out result : bits<1067>

    memory cells : mem<bits<1067>,4> {
        read_latency 0
        collision read_first
        reset { contents preserve read_data preserve }
    }
    cells.read_address = address
    cells.write_enable = write
    cells.write_address = address
    cells.write_data = data
    result = cells.read_data
}
""",
        encoding="utf-8",
    )
    events = (
        {"reset": {"rst": True}, "edges": ["clk"]},
        {"reset": {"rst": False}},
        {"set": {"write": 1, "address": 0, "data": FIRST}, "edges": ["clk"]},
        {"set": {"write": 0}},
        {"set": {"write": 1, "address": 1, "data": LAST}, "edges": ["clk"]},
        {"set": {"write": 0, "address": 0}},
        {"set": {"address": 1}},
    )
    trace = run_differential(
        source, top="WideMemoryCell", events=events, directory=tmp_path / "rtl"
    )
    assert trace.native == trace.direct_sv
    assert trace.native[3]["result"] == FIRST
    assert trace.native[5]["result"] == FIRST
    assert trace.native[6]["result"] == LAST
