"""Hierarchy is erased in Python before the primitive runtime boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

import zlang


ROOT = Path(__file__).resolve().parents[2]


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.zhl"
    path.write_text(text, encoding="utf-8")
    return path


def test_scalar_instance_array_is_one_deterministic_primitive_plan() -> None:
    programs = [
        zlang.sim.compile(
            ROOT / "examples/sequential_instance_array.zhl",
            top="StateLaneArray",
            engine="reference",
        )
        for _ in range(2)
    ]
    first, second = programs

    assert first.plan.to_bytes() == second.plan.to_bytes()
    assert first.plan.identity == second.plan.identity
    assert first.plan.payload["canonical_ir_identity"].startswith("hierarchical:")
    assert {item["name"] for item in first.plan.payload["registers"]} == {
        "lane[0].count",
        "lane[1].count",
    }
    assert set(first.plan.payload) == {
        "schema",
        "runtime_abi",
        "packing_layout_schema",
        "canonical_ir_identity",
        "native_target",
        "module",
        "identity",
        "ports",
        "nodes",
        "regions",
        "outputs",
        "registers",
        "memories",
        "events",
        "domains",
        "edge_programs",
    }
    child_origins = [
        origin
        for node in first.plan.payload["nodes"]
        for origin in node["origins"]
        if origin.get("hierarchy_path")
        == ["StateLaneArray", "lane[0]"]
    ]
    assert child_origins


def test_scalar_instance_array_matches_reference_and_native() -> None:
    results = []
    for engine in ("reference", "native"):
        instance = zlang.sim.load(
            ROOT / "examples/sequential_instance_array.zhl",
            top="StateLaneArray",
            engine=engine,
        )
        with instance:
            instance.set("enables", [1, 1])
            instance.set("steps", [2, 3])
            trace = [instance.eval(), instance.edge("clk"), instance.edge("clk")]
            instance.reset("rst", asserted=True)
            trace.append(instance.edge("clk"))
            instance.reset("rst", asserted=False)
            results.append(trace)
    assert results[0] == results[1] == [
        {"values": [0, 0]},
        {"values": [2, 3]},
        {"values": [4, 6]},
        {"values": [0, 0]},
    ]


def test_parent_and_child_commit_from_one_pre_edge_snapshot(tmp_path: Path) -> None:
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
    traces = []
    for engine in ("reference", "native"):
        instance = zlang.sim.load(source, top="Top", engine=engine)
        with instance:
            instance.set("next", 9)
            traces.append(
                [instance.eval(), instance.edge("clk"), instance.edge("clk")]
            )
    assert traces[0] == traces[1] == [
        {"captured": 0, "child_value": 1},
        {"captured": 1, "child_value": 9},
        {"captured": 9, "child_value": 9},
    ]


def test_nested_combinational_hierarchy_disappears_before_plan(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "nested_comb",
        """
        module Leaf { in x:u8 out y:u8 y=truncate<8>(x+1) }
        module Middle {
            in x:u8 out y:u8
            inst leaf:Leaf
            leaf.x=x
            y=leaf.y
        }
        module Top {
            in x:u8 out y:u8
            inst middle:Middle
            middle.x=x
            y=middle.y
        }
        """,
    )
    outputs = []
    for engine in ("reference", "native"):
        instance = zlang.sim.load(source, top="Top", engine=engine)
        with instance:
            instance.set("x", 41)
            outputs.append(instance.eval())
    assert outputs == [{"y": 42}, {"y": 42}]


def test_identical_child_combinational_nodes_share_one_plan_producer(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        "shared_children",
        """
        module Child { in x:u8 out y:u8 y=truncate<8>(x+1) }
        module Top {
            in x:u8 out left:u8 out right:u8
            inst first:Child
            inst second:Child
            first.x=x
            second.x=x
            left=first.y
            right=second.y
        }
        """,
    )
    plans = [
        zlang.sim.compile(source, top="Top", engine=engine)
        for engine in ("reference", "native")
    ]
    assert plans[0].plan.to_bytes() == plans[1].plan.to_bytes()
    payload = plans[0].plan.payload
    outputs = {item["name"]: item["node"] for item in payload["outputs"]}
    assert outputs["left"] == outputs["right"]
    with plans[0].create() as reference, plans[1].create() as native:
        for instance in (reference, native):
            instance.set("x", 41)
            assert instance.eval() == {"left": 42, "right": 42}


def test_child_memories_are_namespaced_and_cycle_exact() -> None:
    traces = []
    for engine in ("reference", "native"):
        instance = zlang.sim.load(
            ROOT / "examples/storage_instance_array.zhl",
            top="MemoryLaneArray",
            engine=engine,
        )
        with instance:
            events = [
                {
                    "set": {
                        "read_address": [0, 0],
                        "write_enable": [1, 1],
                        "write_address": [0, 0],
                        "write_data": [0x22, 0x11],
                    },
                    "edges": ["clk"],
                },
                {
                    "set": {
                        "read_address": [0, 1],
                        "write_enable": [0, 1],
                        "write_address": [0, 1],
                        "write_data": [0, 0x33],
                    },
                    "edges": ["clk"],
                },
                {
                    "set": {
                        "read_address": [1, 0],
                        "write_enable": [0, 0],
                    },
                    "edges": ["clk"],
                },
            ]
            traces.append(instance.run_events(events))
            assert {item["name"] for item in instance.program.plan.payload["memories"]} == {
                "lane[0].table",
                "lane[1].table",
            }
    assert traces[0] == traces[1] == [
        {"read_data": [0x22, 0x11]},
        {"read_data": [0x22, 0x33]},
        {"read_data": [0, 0x11]},
    ]


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_buffered_protocol_hierarchy_is_flattened(engine: str) -> None:
    with zlang.sim.load(
            ROOT / "examples/hierarchical_protocol.zhl",
            top="ProtocolTop",
            engine=engine,
    ) as instance:
        instance.reset("rst", asserted=True)
        instance.edge("clk")
        instance.reset("rst", asserted=False)
        assert instance.edge("clk")["seen"] == 7
