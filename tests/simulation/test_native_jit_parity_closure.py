"""Executable parity excludes only validated non-runtime compiler metadata."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import zlang
from zlang.compiler import create_file_compilation_session
from zlang.simulation_plan import (
    JitUnsupportedFeatureError,
    MAX_PLAN_NODES,
    build_simulation_plan,
)
from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[2]
ALL_SYNTAX = ROOT / "examples/all_syntax.zhl"


@pytest.mark.parametrize("engine", ("reference", "native"))
def test_global_equivalence_is_compile_time_only_for_execution(engine: str) -> None:
    with zlang.sim.load(
        ALL_SYNTAX,
        top="ScalarSyntax",
        engine=engine,
    ) as instance:
        instance.set("a", 4)
        instance.set("b", 7)
        instance.set("select", 1)
        assert instance.eval() == {"y": 11}
        payload = instance.program.plan.to_json()

    assert "equivalence" not in payload
    assert "or_zero" not in payload


def test_legacy_contracts_execute_only_through_exact_overlay_probes() -> None:
    traces = []
    for engine in ("reference", "native"):
        with zlang.sim.load(
            ALL_SYNTAX,
            top="ContractSyntax",
            engine=engine,
        ) as instance:
            instance.reset("rst", asserted=True)
            instance.edge("clk")
            instance.reset("rst", asserted=False)
            instance.set("a", 9)
            instance.set("b", 0)
            outputs = instance.edge("clk")
            traces.append(
                (
                    outputs,
                    tuple(
                        (event.category, event.clause_name)
                        for event in instance.drain_events()
                    ),
                )
            )

    assert traces[0] == traces[1]
    assert traces[0] == (
        {"y": 9},
        (
            ("requirement_violation", "bounded"),
            ("requirement_violation", "legal_inputs"),
        ),
    )


def test_missing_contract_overlay_mirror_fails_closed() -> None:
    module = create_file_compilation_session(
        ALL_SYNTAX,
        top="ContractSyntax",
    ).planning.module
    broken = replace(module, verification_scopes=())

    with pytest.raises(
        JitUnsupportedFeatureError,
        match="has no exact runtime verification-overlay mirror",
    ):
        build_simulation_plan(broken)


@pytest.mark.parametrize("depth,count_width", ((2, 2), (32, 6), (512, 10)))
def test_scheduler_plan_size_is_independent_of_fifo_depth(
    depth: int,
    count_width: int,
) -> None:
    module = analyze(parse(f"""
      module DeepScheduler {{
        clock clk reset rst
        in push:bit in pop:bit in value:u8 out count:u{count_width}
        fifo q:fifo<u8,{depth}>
        rule enqueue when push {{ q.push(value) }}
        rule dequeue when pop {{ q.pop() }}
        count=q.count
      }}
    """))

    plan = build_simulation_plan(module)

    assert len(plan.payload["nodes"]) == 148


def test_fft512_plan_stays_bounded_after_callable_and_scheduler_lowering() -> None:
    module = create_file_compilation_session(
        ROOT / "examples/fft/sdf_stage_numeric.zhl",
        top="FFT512SDFReference",
    ).planning.module

    plan = build_simulation_plan(module)

    assert len(plan.payload["nodes"]) <= MAX_PLAN_NODES
    assert len(plan.canonical_bytes) < 2_000_000
