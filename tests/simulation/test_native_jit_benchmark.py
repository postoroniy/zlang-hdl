"""Regression checks for the post-boundary simulation benchmark harness."""

from pathlib import Path

from tools.benchmark_native_jit import (
    BenchmarkCase,
    _plan_executor_once,
    _semantic_reference_once,
)
from zlang.compiler import create_file_compilation_session
from zlang.sim import Program
from zlang.simulation_reference import compile_reference_plan


ROOT = Path(__file__).resolve().parents[2]


def test_semantic_and_plan_reference_report_the_same_post_edge_state() -> None:
    case = BenchmarkCase(
        "counter",
        ROOT / "examples/counter.zhl",
        "Counter",
        "single_clock",
        {},
        ("clk",),
    )
    session = create_file_compilation_session(case.source, top=case.top)
    plan = session.simulation_plan
    reference = Program(
        plan=plan,
        module=session.planning.module,
        _native=compile_reference_plan(plan),
    )

    assert _semantic_reference_once(case, session.planning.module, 10) == {"y": 10}
    assert _plan_executor_once(case, reference, 10) == {"y": 10}
