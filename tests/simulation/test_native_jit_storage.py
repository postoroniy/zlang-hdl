from __future__ import annotations

import random
from pathlib import Path

import pytest

import zlang.sim
from zlang.simulation_lowering import PRIMITIVE_OPS


ROOT = Path(__file__).resolve().parents[2]


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


def _run_scalar(
    source: Path,
    top: str,
    inputs: list[dict[str, int]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    traces = []
    for engine in ("reference", "native"):
        instance = zlang.sim.load(source, top=top, engine=engine)
        trace = []
        with instance:
            for values in inputs:
                for name, value in values.items():
                    instance.set(name, value)
                trace.append(instance.eval())
                instance.edge("clk")
        traces.append(trace)
    return traces[0], traces[1]


@pytest.mark.parametrize("collision", ["old", "new", "no_change"])
def test_native_same_clock_true_dual_port_memory_matches_reference(
    tmp_path: Path,
    collision: str,
) -> None:
    source = _source(
        tmp_path,
        f"dual_port_{collision}",
        f"""
        module DualPort {{
          clock clk reset rst
          in a_re:bit in a_we:bit in a_addr:u2 in a_data:u8
          in b_re:bit in b_we:bit in b_addr:u2 in b_data:u8
          out a_q:u8 out b_q:u8
          memory table:mem<u8,4> {{
            read_write_port a read_write_port b
            read_latency 1 collision {collision} write_priority a > b
          }}
          table.a.read_enable=a_re table.a.write_enable=a_we
          table.a.address=a_addr table.a.write_data=a_data
          table.b.read_enable=b_re table.b.write_enable=b_we
          table.b.address=b_addr table.b.write_data=b_data
          a_q=table.a.read_data b_q=table.b.read_data
        }}
        """,
    )
    idle = {
        "a_re": 1,
        "a_we": 0,
        "a_addr": 1,
        "a_data": 0,
        "b_re": 1,
        "b_we": 0,
        "b_addr": 1,
        "b_data": 0,
    }
    reference, native = _run_scalar(
        source,
        "DualPort",
        [
            {**idle, "a_we": 1, "a_data": 5},
            idle,
            {**idle, "a_we": 1, "a_data": 9, "b_we": 1, "b_data": 99},
            idle,
            {**idle, "a_we": 1, "a_addr": 2, "a_data": 22,
             "b_we": 1, "b_addr": 3, "b_data": 33},
            {**idle, "a_addr": 2, "b_addr": 3},
            idle,
        ],
    )
    assert native == reference
    assert native[3]["a_q"] == {"old": 5, "new": 9, "no_change": 5}[collision]
    assert native[-1] == {"a_q": 22, "b_q": 33}


def test_native_latency_zero_ported_memory_matches_reference(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "combinational_memory",
        """
        module CombinationalMemory {
          clock clk reset rst
          in we:bit in wa:u2 in wd:u8 in ra:u2 out q:u8
          memory table:mem<u8,4> {
            write_port wr read_port rd read_latency 0 collision new
          }
          table.wr.enable=we table.wr.address=wa table.wr.data=wd
          table.rd.address=ra q=table.rd.data
        }
        """,
    )
    reference, native = _run_scalar(
        source,
        "CombinationalMemory",
        [
            {"we": 1, "wa": 2, "wd": 17, "ra": 2},
            {"we": 0, "wa": 0, "wd": 0, "ra": 2},
            {"we": 1, "wa": 1, "wd": 31, "ra": 2},
        ],
    )
    assert native == reference == [{"q": 17}, {"q": 17}, {"q": 17}]
    for engine in ("reference", "native"):
        instance = zlang.sim.load(source, top="CombinationalMemory", engine=engine)
        with instance:
            instance.set("we", 0)
            instance.set("wa", 0)
            instance.set("wd", 0)
            instance.set("ra", 2)
            instance.reset("rst", asserted=True)
            assert instance.eval() == {"q": 0}


def test_latency_zero_preserved_read_data_matches_reference(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "preserved_combinational_memory",
        """
        module PreservedCombinationalMemory {
          clock clk reset rst
          in we:bit in wa:u2 in wd:u8 in ra:u2 out q:u8
          memory table:mem<u8,4> {
            read_latency 0 collision old
            reset { contents preserve read_data preserve }
          }
          table.read_address=ra
          table.write_enable=we table.write_address=wa table.write_data=wd
          q=table.read_data
        }
        """,
    )
    for engine in ("reference", "native"):
        instance = zlang.sim.load(
            source,
            top="PreservedCombinationalMemory",
            engine=engine,
        )
        with instance:
            for name, value in {
                "we": 1,
                "wa": 2,
                "wd": 17,
                "ra": 2,
            }.items():
                instance.set(name, value)
            instance.edge("clk")
            instance.set("we", 0)
            assert instance.eval() == {"q": 17}
            instance.reset("rst", asserted=True)
            assert instance.eval() == {"q": 17}
            instance.edge("clk")
            assert instance.eval() == {"q": 17}


def test_native_legacy_fifo_matches_reference_under_stall_and_wraparound() -> None:
    source = ROOT / "examples" / "fifo_bridge.zhl"
    rng = random.Random(91)
    events = [
        (
            {"payload": rng.randrange(256), "valid": rng.randrange(2)},
            {"ready": rng.randrange(2)},
        )
        for _ in range(80)
    ]
    traces = []
    for engine in ("reference", "native"):
        instance = zlang.sim.load(source, top="FifoBridge", engine=engine)
        trace = []
        with instance:
            for source_value, sink_value in events:
                instance.set("rx", source_value)
                instance.set("tx", sink_value)
                trace.append(instance.eval())
                instance.edge("clk")
        traces.append(trace)
    assert traces[1] == traces[0]


def test_native_scheduled_fifo_and_memory_match_reference() -> None:
    fifo_source = ROOT / "examples" / "fft_sdf_stage_atomic_transition.zhl"
    rng = random.Random(17)
    events = [
        (
            {"payload": rng.randrange(256), "valid": rng.randrange(2)},
            {"ready": rng.randrange(2)},
        )
        for _ in range(64)
    ]
    traces = []
    for engine in ("reference", "native"):
        instance = zlang.sim.load(fifo_source, top="SDFStateStage", engine=engine)
        trace = []
        with instance:
            for source_value, sink_value in events:
                instance.set("input", source_value)
                instance.set("output", sink_value)
                trace.append(instance.eval())
                instance.edge("clk")
        traces.append(trace)
    assert traces[1] == traces[0]

    memory_source = ROOT / "examples" / "rule_local_memory.zhl"
    memory_inputs = [
        {"read_enable": 1, "write_enable": 1, "address": 1, "write_data": 9},
        {"read_enable": 1, "write_enable": 0, "address": 1, "write_data": 0},
        {"read_enable": 0, "write_enable": 1, "address": 2, "write_data": 23},
        {"read_enable": 1, "write_enable": 0, "address": 2, "write_data": 0},
    ]
    reference, native = _run_scalar(
        memory_source, "RuleLocalMemory", memory_inputs
    )
    assert native == reference


def test_storage_is_erased_before_the_primitive_runtime_boundary() -> None:
    for source, top in (
        (ROOT / "examples" / "fifo_bridge.zhl", "FifoBridge"),
        (ROOT / "examples" / "fft_sdf_stage_atomic_transition.zhl", "SDFStateStage"),
        (ROOT / "examples" / "rule_local_memory.zhl", "RuleLocalMemory"),
    ):
        first = zlang.sim.compile(source, top=top, engine="reference").plan
        second = zlang.sim.compile(source, top=top, engine="reference").plan
        assert first.to_bytes() == second.to_bytes()
        assert "fifos" not in first.payload
        assert "transitions" not in first.payload
        assert "scheduler_fifos" not in first.payload
        assert {node["op"] for node in first.payload["nodes"]} <= PRIMITIVE_OPS
        assert {effect["op"] for edge in first.payload["edge_programs"]
                for effect in edge["effects"]} <= {
            "commit_state", "store_memory", "fill_memory"
        }
