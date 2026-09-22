"""Wide packed storage uses bounded bit spans, not wide arithmetic."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.simulation.differential import run_differential


SOURCE = Path(__file__).resolve().parents[1] / "fixtures/wide_packed_simulation.zhl"
FIRST = (1 << 1066) | (1 << 513) | (1 << 65) | 0x1234
LAST = (1 << 1059) | (1 << 255) | 0xABCD


@pytest.mark.parametrize("top", ("WidePackedQueue", "WideIndexedCounters"))
def test_wide_packed_state_matches_reference_native_and_rtl(
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
    assert trace.reference == trace.native == trace.direct_sv
    for event, value in expected.items():
        assert trace.reference[event][output] == value
