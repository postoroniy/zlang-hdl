import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.simulate import simulate_cycles
from zlang.timing import timing_info


ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    8: (ROOT / "examples/dot_product_pipelined.zhl").read_text(),
    12: (ROOT / "examples/dot_product_pipelined_12.zhl").read_text(),
}


def _vectors():
    return (
        ((0,) * 8, (0,) * 8),
        ((1,) * 8, (1,) * 8),
        ((255,) * 8, (255,) * 8),
        ((1, 2, 3, 4, 5, 6, 7, 8), (8, 7, 6, 5, 4, 3, 2, 1)),
        ((255, 0, 128, 64, 32, 16, 8, 4), (3, 5, 7, 11, 13, 17, 19, 23)),
    )


@pytest.mark.parametrize("latency", (8, 12))
def test_dot_pipeline_is_explicit_fixed_latency_and_full_width(latency: int) -> None:
    result = compile_source(SOURCES[latency])
    assignment = next(item for item in result.ir.assignments if item.target.name == "y")
    assert isinstance(assignment.expression, expr.Pipeline)
    assert timing_info(assignment.expression).latency == latency
    assert assignment.expression.pipeline_plan is not None
    assert assignment.expression.pipeline_plan.requested_latency == latency
    assert assignment.expression.pipeline_plan.scheduler == "dag_partition_v1"
    assert assignment.expression.type.width == 19
    assert result.implementation_graph.is_generic


@pytest.mark.parametrize("latency", (8, 12))
def test_dot_pipeline_semantic_latency_and_no_overflow(latency: int) -> None:
    vectors = _vectors()
    cycles = [{"a": a, "b": b} for a, b in (*vectors, *(((0,) * 8, (0,) * 8),) * latency)]
    observed = simulate_cycles(compile_source(SOURCES[latency]).ir, cycles)
    expected = [sum(x * y for x, y in zip(a, b, strict=True)) for a, b in vectors]
    assert [item["y"] for item in observed][latency:latency + len(expected)] == expected
    assert max(expected) <= (1 << 19) - 1


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize("latency", (8, 12))
def test_direct_sv_dot_pipeline_is_bit_exact(tmp_path: Path, latency: int) -> None:
    top = "DotProductPipelined" if latency == 8 else "DotProductPipelined12"
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(emit_experimental(compile_source(SOURCES[latency]).ir))

    def packed(values):
        return "'{" + ",".join(f"8'd{value}" for value in values) + "}"

    expected = [sum(x * y for x, y in zip(a, b, strict=True)) for a, b in _vectors()]
    lines = []
    all_vectors = (*_vectors(), *(((0,) * 8, (0,) * 8),) * (latency - 1))
    for cycle, (a, b) in enumerate(all_vectors):
        lines.append(f"a={packed(a)}; b={packed(b)}; tick;")
        index = cycle - latency + 1
        if 0 <= index < len(expected):
            lines.append(f"if (y !== 19'd{expected[index]}) $fatal(1,\"vector {index}: %0d\",y);")
    bench = tmp_path / "tb.sv"
    bench.write_text(
        "module tb; logic clk=0,rst=1; logic [7:0] a[0:7],b[0:7]; wire [18:0] y; "
        f"{top} dut(.clk,.rst,.a,.b,.y); "
        "task tick; begin #1 clk=1; #1; clk=0; #1; end endtask "
        f"initial begin tick; rst=0; {''.join(lines)} $finish; end endmodule\n"
    )
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        ("verilator", "--binary", "--timing", "-Wno-fatal", "--top-module", "tb",
         str(rtl), str(bench), "-Mdir", str(obj)),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run((str(obj / "Vtb"),), check=True, capture_output=True, text=True)
