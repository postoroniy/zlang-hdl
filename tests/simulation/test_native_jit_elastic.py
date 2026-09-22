"""Elastic pipelines are erased to primitive DAG and state before execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import zlang
from tests.simulation.differential import run_differential
from zlang.compiler import compile_file
from zlang.ir import expressions as expr
from zlang.ir.traversal import ExpressionTraversalPolicy, walk_expression
from zlang.simulation_elastic import lower_elastic_pipeline_module


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples" / "elastic_pipeline_auto.zhl"


def _payload(value: int) -> dict[str, int]:
    return {
        "a": value,
        "b": 1,
        "c": 0,
        "d": 0,
        "e": 0,
        "f": 0,
        "g": 0,
        "h": 0,
    }


def _events() -> tuple[dict[str, object], ...]:
    return (
        {
            "reset": {"rst": True},
            "set": {
                "input": {"payload": _payload(0), "valid": 0},
                "output": {"ready": 1},
            },
            "edges": ("clk",),
        },
        {
            "reset": {"rst": False},
            "set": {"input": {"payload": _payload(1), "valid": 1}},
            "edges": ("clk",),
        },
        {
            "set": {"input": {"payload": _payload(2), "valid": 1}},
            "edges": ("clk",),
        },
        {
            "set": {
                "input": {"payload": _payload(3), "valid": 1},
                "output": {"ready": 0},
            },
            "edges": ("clk",),
        },
        {
            "set": {"input": {"payload": _payload(4), "valid": 1}},
            "edges": ("clk",),
        },
        {"edges": ("clk",)},
        {"set": {"output": {"ready": 1}}, "edges": ("clk",)},
        {
            "set": {"input": {"payload": _payload(0), "valid": 0}},
            "edges": ("clk",),
        },
        {"edges": ("clk",)},
        {"edges": ("clk",)},
        {
            "set": {"input": {"payload": _payload(5), "valid": 1}},
            "edges": ("clk",),
        },
        {
            "set": {"input": {"payload": _payload(6), "valid": 1}},
            "edges": ("clk",),
        },
        {
            "reset": {"rst": True},
            "set": {"input": {"payload": _payload(0), "valid": 0}},
            "edges": ("clk",),
        },
        {"reset": {"rst": False}, "edges": ("clk",)},
        {"edges": ("clk",)},
        {"edges": ("clk",)},
        {"edges": ("clk",)},
    )


def test_elastic_lowering_is_explicit_state_without_pipeline_nodes() -> None:
    module = compile_file(SOURCE, top="ElasticPipelineAuto").ir
    region = module.elastic_pipeline_regions[0]
    lowered = lower_elastic_pipeline_module(module)

    assert not lowered.elastic_pipeline_regions
    assert len(lowered.registers) == (
        sum(stages for _, stages in region.plan.data_stage_instances)
        + region.plan.valid_stage_count
    )
    assert len(lowered.next_assignments) == len(lowered.registers)
    assert all(item.activation is None for item in lowered.next_assignments)
    roots = (
        *(item.expression for item in lowered.assignments),
        *(item.expression for item in lowered.next_assignments),
    )
    assert not any(
        isinstance(node, (expr.Pipeline, expr.Delay))
        for root in roots
        for node in walk_expression(
            root,
            policy=ExpressionTraversalPolicy.SELECTED_IMPLEMENTATION,
        )
    )


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_elastic_public_api_and_global_stall_are_cycle_exact(engine: str) -> None:
    program = zlang.sim.compile(
        SOURCE,
        top="ElasticPipelineAuto",
        engine=engine,
    )
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"elastic_pipeline_regions"' not in encoded
    assert '"op": "pipeline"' not in encoded
    assert {node["op"] for node in program.plan.payload["nodes"]} <= {
        "constant",
        "load_input",
        "load_state",
        "load_event",
        "load_memory",
        "add",
        "sub",
        "mul",
        "and",
        "or",
        "xor",
        "not",
        "shl",
        "lshr",
        "ashr",
        "eq",
        "ult",
        "ule",
        "slt",
        "sle",
        "select",
        "extract_bits",
        "insert_bits",
        "concat_bits",
    }
    with program.create() as instance:
        trace = instance.run_events(_events())
    assert [item["input"]["ready"] for item in trace[3:7]] == [0, 0, 0, 1]
    assert [item["output"] for item in trace[3:7]] == [
        {"payload": 1, "valid": 1, "transfer": 0},
        {"payload": 1, "valid": 1, "transfer": 0},
        {"payload": 1, "valid": 1, "transfer": 0},
        {"payload": 2, "valid": 1, "transfer": 1},
    ]
    assert [
        item["output"]["payload"]
        for item in trace
        if item["output"]["transfer"]
    ] == [2, 3, 4]
    assert all(not item["output"]["valid"] for item in trace[12:])


def test_elastic_matches_reference_native_and_direct_sv(tmp_path: Path) -> None:
    trace = run_differential(
        SOURCE,
        top="ElasticPipelineAuto",
        events=_events(),
        directory=tmp_path / "elastic_rtl",
    )
    assert trace.reference == trace.native == trace.direct_sv


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_elastic_child_is_flattened_before_primitive_plan(
    tmp_path: Path,
    engine: str,
) -> None:
    source = tmp_path / "elastic_hierarchy.zhl"
    source.write_text(
        SOURCE.read_text(encoding="utf-8").replace(
            "module ElasticPipelineAuto",
            "module ElasticPipelineLeaf",
        )
        + """

module ElasticPipelineParent {
    clock clk reset rst
    in input : rv<ElasticProductsInput>
    out output : rv<u19>
    inst pipe : ElasticPipelineLeaf
    connect input -> pipe.input
    connect pipe.output -> output
}
""",
        encoding="utf-8",
    )
    program = zlang.sim.compile(
        source,
        top="ElasticPipelineParent",
        engine=engine,
    )
    assert program.plan.payload["canonical_ir_identity"].startswith("hierarchical:")
    encoded = json.dumps(program.plan.payload, sort_keys=True)
    assert '"instances"' not in encoded
    assert '"elastic' not in encoded
    with program.create() as instance:
        trace = instance.run_events(_events())
    assert trace[3]["output"] == {"payload": 1, "valid": 1, "transfer": 0}
