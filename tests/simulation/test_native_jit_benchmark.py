"""Regression checks for the post-boundary simulation benchmark harness."""

from pathlib import Path

from tools.benchmark_native_jit import (
    BenchmarkCase,
    _plan_executor_once,
)
from zlang.compiler import create_file_compilation_session
from zlang.sim import compile as compile_simulation


ROOT = Path(__file__).resolve().parents[2]


def test_native_benchmark_reports_the_post_edge_state() -> None:
    case = BenchmarkCase(
        "counter",
        ROOT / "examples/counter.zhl",
        "Counter",
        "single_clock",
        {},
        ("clk",),
    )
    session = create_file_compilation_session(case.source, top=case.top)
    native = compile_simulation(case.source, top=case.top, engine="native")

    assert native.plan == session.simulation_plan
    assert _plan_executor_once(case, native, 10) == {"y": 10}
