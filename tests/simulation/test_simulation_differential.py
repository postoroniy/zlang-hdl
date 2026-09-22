"""Cycle/event parity across reference, native, and Direct-SV execution."""

from __future__ import annotations

from pathlib import Path

from tests.simulation.differential import run_differential


ROOT = Path(__file__).resolve().parents[2]


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


def test_odd_and_wide_combinational_values_match_all_paths(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "wide_comb",
        """
        module WideComb {
            in a,b:uint<65>
            in shift:u7
            out sum:uint<66>=a+b
            out mixed:uint<65>=(a ^ b) | (a & b)
            out shifted:uint<65>=a << shift
        }
        """,
    )
    events = (
        {
            "set": {
                "a": (1 << 64) | 0x1234,
                "b": (1 << 63) | 0x55,
                "shift": 3,
            }
        },
        {
            "set": {
                "a": (1 << 65) - 1,
                "b": 1,
                "shift": 64,
            }
        },
    )
    trace = run_differential(
        source,
        top="WideComb",
        events=events,
        directory=tmp_path / "rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv


def test_ready_valid_protocol_erasure_matches_direct_sv(tmp_path: Path) -> None:
    trace = run_differential(
        ROOT / "examples/rv_passthrough.zhl",
        top="RvPassthrough",
        events=(
            {
                "set": {
                    "rx": {"payload": 17, "valid": 1},
                    "tx": {"ready": 0},
                }
            },
            {"set": {"tx": {"ready": 1}}},
            {"set": {"rx": {"payload": 33, "valid": 0}}},
        ),
        directory=tmp_path / "rv_rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv


def test_direct_ready_valid_hierarchy_matches_direct_sv(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "rv_hierarchy",
        """
        module Pass {
            in rx:rv<u8>
            out tx:rv<u8>
            tx.payload=rx.payload
            tx.valid=rx.valid
            rx.ready=tx.ready
        }
        module Top {
            in source:rv<u8>
            out sink:rv<u8>
            inst pass:Pass
            connect source -> pass.rx
            connect pass.tx -> sink
        }
        """,
    )
    trace = run_differential(
        source,
        top="Top",
        events=(
            {
                "set": {
                    "source": {"payload": 91, "valid": 1},
                    "sink": {"ready": 1},
                }
            },
        ),
        directory=tmp_path / "rv_hierarchy_rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv


def test_hierarchy_and_atomic_parent_child_state_match_all_paths(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "hierarchical_atomic",
        """
        module Child {
            clock clk reset rst
            in next:u8 out current:u8
            reg value:u8=1
            value <- next
            current=value
        }
        module Top {
            clock clk reset rst
            in next:u8 out child_value:u8 out captured:u8
            inst child:Child
            reg seen:u8=0
            child.next=next
            seen <- child.current
            child_value=child.current
            captured=seen
        }
        """,
    )
    events = (
        {"reset": {"rst": True}, "edges": ("clk",)},
        {"reset": {"rst": False}, "set": {"next": 9}, "edges": ("clk",)},
        {"edges": ("clk",)},
        {"set": {"next": 17}},
    )
    trace = run_differential(
        source,
        top="Top",
        events=events,
        directory=tmp_path / "rtl",
    )
    assert trace.reference == (
        {"child_value": 1, "captured": 0},
        {"child_value": 9, "captured": 1},
        {"child_value": 9, "captured": 9},
        {"child_value": 9, "captured": 9},
    )


def test_hierarchical_synchronous_memories_match_all_paths(tmp_path: Path) -> None:
    events = (
        {"reset": {"rst": True}, "edges": ("clk",)},
        {
            "reset": {"rst": False},
            "set": {
                "read_address": [0, 0],
                "write_enable": [1, 1],
                "write_address": [0, 0],
                "write_data": [0x22, 0x11],
            },
            "edges": ("clk",),
        },
        {
            "set": {
                "read_address": [0, 1],
                "write_enable": [0, 1],
                "write_address": [0, 1],
                "write_data": [0, 0x33],
            },
            "edges": ("clk",),
        },
    )
    trace = run_differential(
        ROOT / "examples/storage_instance_array.zhl",
        top="MemoryLaneArray",
        events=events,
        directory=tmp_path / "rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv


def test_scheduled_fifo_matches_all_execution_paths(tmp_path: Path) -> None:
    events = (
        {"reset": {"rst": True}, "edges": ("clk",)},
        {
            "reset": {"rst": False},
            "set": {"input": {"payload": 11, "valid": 1},
                    "output": {"ready": 0}},
            "edges": ("clk",),
        },
        {
            "set": {"input": {"payload": 22, "valid": 1},
                    "output": {"ready": 1}},
            "edges": ("clk",),
        },
        {
            "set": {"input": {"payload": 0, "valid": 0},
                    "output": {"ready": 1}},
            "edges": ("clk",),
        },
    )
    trace = run_differential(
        ROOT / "examples/fft_sdf_stage_atomic_transition.zhl",
        top="SDFStateStage",
        events=events,
        directory=tmp_path / "rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv


def test_coincident_multi_clock_event_is_atomic_in_all_paths(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "multi_clock",
        """
        module MultiClock {
            clock left_clk
            reset left_rst @left_clk
            clock right_clk
            reset right_rst @right_clk
            in left_step:u8 @left_clk
            in right_step:u8 @right_clk
            out left:u8 @left_clk
            out right:u8 @right_clk
            reg left_state:u8 @left_clk=1
            reg right_state:u8 @right_clk=10
            rule Left @left_clk when 1 {
                left_state <- truncate<8>(left_state + left_step)
            }
            rule Right @right_clk when 1 {
                right_state <- truncate<8>(right_state + right_step)
            }
            left=left_state
            right=right_state
        }
        """,
    )
    events = (
        {
            "reset": {"left_rst": True, "right_rst": True},
            "edges": ("left_clk", "right_clk"),
        },
        {
            "reset": {"left_rst": False, "right_rst": False},
            "set": {"left_step": 2, "right_step": 3},
            "edges": ("left_clk", "right_clk"),
        },
        {"edges": ("right_clk", "left_clk")},
    )
    trace = run_differential(
        source,
        top="MultiClock",
        events=events,
        directory=tmp_path / "rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv
