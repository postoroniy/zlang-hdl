"""Regression check for the public native-simulation cycle API."""

from pathlib import Path

from zlang.compiler import create_file_compilation_session
from zlang.sim import compile as compile_simulation


ROOT = Path(__file__).resolve().parents[2]


def test_native_benchmark_reports_the_post_edge_state() -> None:
    source = ROOT / "examples/counter.zhl"
    session = create_file_compilation_session(source, top="Counter")
    native = compile_simulation(source, top="Counter", engine="native")

    assert native.plan == session.simulation_plan
    with native.create() as simulator:
        assert simulator.run_cycles("clk", 10) == {"y": 10}
