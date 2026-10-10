from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit
from zlang.backend.systemverilog.functional import region as functional_region
from zlang.backend.systemverilog.functional.scatter import (
    _functional_scatter_lowering_plan,
)
from zlang.backend.systemverilog.functional_scatter import (
    FunctionalScatterLoweringStrategy,
    MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES,
    MAX_SCATTER_CHUNK_WRITES,
)
from zlang.compiler import compile_source
from zlang.ir import FunctionalRegion
from zlang.native_simulation import simulate


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/structural/scatter_12x4x4.zhl").read_text(
    encoding="utf-8"
)


def _module():
    return compile_source(SOURCE, top="Scatter12x4x4Witness").ir


def _region(module, name: str) -> FunctionalRegion:
    expression = next(
        assignment.expression
        for assignment in module.assignments
        if assignment.target.name == name
    )
    assert isinstance(expression, FunctionalRegion)
    return expression


def _nested(values: list[int]) -> list[list[list[int]]]:
    return [
        [
            values[lane * 16 + group * 4 : lane * 16 + group * 4 + 4]
            for group in range(4)
        ]
        for lane in range(12)
    ]


def test_wide_scatter_plans_are_bounded_and_deterministic() -> None:
    module = _module()
    expected = {
        "hit": (FunctionalScatterLoweringStrategy.STRUCTURAL, 192),
        "result": (FunctionalScatterLoweringStrategy.CHUNKED_PROCEDURAL, 1536),
    }

    for name, (strategy, result_width) in expected.items():
        region = _region(module, name)
        emission = functional_region.functional_region_plan(
            region, f"{name}_result"
        )
        first = _functional_scatter_lowering_plan(region, emission)
        second = _functional_scatter_lowering_plan(region, emission)

        assert first == second
        assert first.strategy is strategy
        assert first.effective_candidate_count == 192
        assert first.result_width == result_width
        assert len(first.chunks) == 12
        assert tuple(chunk.write_count for chunk in first.chunks) == (16,) * 12
        assert tuple(
            value for chunk in first.chunks for _, value in chunk.fixed_binders
        ) == tuple(range(12))
        assert all(
            chunk.write_count <= MAX_SCATTER_CHUNK_WRITES
            and (
                first.strategy is FunctionalScatterLoweringStrategy.STRUCTURAL
                or chunk.write_count * first.result_width
                <= MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES
            )
            for chunk in first.chunks
        )


@pytest.mark.performance
@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys unavailable")
def test_wide_scatter_yosys_work_is_bounded(tmp_path: Path) -> None:
    module = _module()
    generated = emit(module)

    assert generated == emit(module)
    assert generated.count("always_comb begin") == 2
    assert generated.count("module zlang_scatter_chunk_helper_") == 2
    assert "module zlang_scatter_structural_helper_" in generated
    assert len(generated.encode("utf-8")) < 256_000
    assert generated.count("write_instance (") == 2
    assert "candidate < 16" in generated

    rtl = tmp_path / "Scatter12x4x4Witness.sv"
    rtl.write_text(generated, encoding="utf-8")
    metrics = tmp_path / "yosys.metrics"
    completed = subprocess.run(
        (
            "/usr/bin/time",
            "-f",
            "%e %M",
            "-o",
            str(metrics),
            "yosys",
            "-q",
            "-p",
            f"read_verilog -sv {rtl}; "
            "hierarchy -check -top Scatter12x4x4Witness; proc; opt; check; stat",
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wall_seconds, peak_rss_kib = metrics.read_text(encoding="utf-8").split()
    assert float(wall_seconds) <= 30
    assert int(peak_rss_kib) <= 512 * 1024


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="Icarus Verilog unavailable",
)
def test_wide_scatter_is_accepted_by_icarus(tmp_path: Path) -> None:
    rtl = tmp_path / "Scatter12x4x4Witness.sv"
    rtl.write_text(emit(_module()), encoding="utf-8")
    bench = tmp_path / "tb.sv"
    bench.write_text(
        "module tb;\n"
        "  logic valid;\n"
        "  logic [11:0][3:0][3:0][7:0] addresses;\n"
        "  logic [11:0][3:0][3:0][7:0] values;\n"
        "  logic [191:0] hit;\n"
        "  logic [191:0][7:0] result;\n"
        "  Scatter12x4x4Witness dut(.*);\n"
        "  initial begin\n"
        "    valid = 1'b0; addresses = '0; values = '1; #1;\n"
        "    if (hit !== '0) $fatal(1, \"hit mismatch\");\n"
        "    if (result !== '0) $fatal(1, \"result mismatch\");\n"
        "    $finish;\n"
        "  end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    executable = tmp_path / "wide-scatter.vvp"
    compiled = subprocess.run(
        (
            "iverilog",
            "-g2012",
            "-s",
            "tb",
            "-o",
            str(executable),
            str(rtl),
            str(bench),
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    simulated = subprocess.run(
        ("vvp", str(executable)), capture_output=True, text=True, timeout=30
    )
    assert simulated.returncode == 0, simulated.stdout + simulated.stderr


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_wide_scatter_matches_native_for_every_output(tmp_path: Path) -> None:
    module = _module()
    cases: list[tuple[int, list[int], list[int]]] = [
        (0, [0] * 192, [0xFF] * 192),
    ]
    collision_addresses = [255] * 192
    collision_values = [0] * 192
    for index, value in ((0, 1), (16, 2), (191, 4)):
        collision_addresses[index] = 5
        collision_values[index] = value
    cases.append((1, collision_addresses, collision_values))
    multi_addresses = [255] * 192
    multi_values = [0] * 192
    for index, address, value in (
        (0, 10, 0x11),
        (15, 20, 0x22),
        (16, 30, 0x44),
        (80, 40, 0x88),
        (191, 190, 0xAA),
    ):
        multi_addresses[index] = address
        multi_values[index] = value
    cases.append((1, multi_addresses, multi_values))

    checks: list[str] = []
    for ordinal, (valid, addresses, values) in enumerate(cases):
        expected = simulate(
            module,
            valid=valid,
            addresses=_nested(addresses),
            values=_nested(values),
        )
        packed_addresses = sum(
            value << (index * 8) for index, value in enumerate(addresses)
        )
        packed_values = sum(
            value << (index * 8) for index, value in enumerate(values)
        )
        packed_hit = sum(value << index for index, value in enumerate(expected["hit"]))
        packed_result = sum(
            value << (index * 8)
            for index, value in enumerate(expected["result"])
        )
        checks.extend(
            (
                f"    valid = 1'b{valid};",
                f"    addresses = 1536'h{packed_addresses:0384x};",
                f"    values = 1536'h{packed_values:0384x};",
                "    #1;",
                f"    if (hit !== 192'h{packed_hit:048x}) "
                f"$fatal(1, \"hit vector {ordinal} mismatch\");",
                f"    if (result !== 1536'h{packed_result:0384x}) "
                f"$fatal(1, \"result vector {ordinal} mismatch\");",
            )
        )

    rtl = tmp_path / "Scatter12x4x4Witness.sv"
    rtl.write_text(emit(module), encoding="utf-8")
    bench = tmp_path / "tb.sv"
    bench.write_text(
        "module tb;\n"
        "  logic valid;\n"
        "  logic [11:0][3:0][3:0][7:0] addresses;\n"
        "  logic [11:0][3:0][3:0][7:0] values;\n"
        "  logic [191:0] hit;\n"
        "  logic [191:0][7:0] result;\n"
        "  Scatter12x4x4Witness dut(.*);\n"
        "  initial begin\n"
        + "\n".join(checks)
        + "\n    $finish;\n"
        "  end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    obj = tmp_path / "obj"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    compiled = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "-Wno-fatal",
            "--top-module",
            "tb",
            str(rtl),
            str(bench),
            "-Mdir",
            str(obj),
        ),
        capture_output=True,
        text=True,
        timeout=180,
        env=environment,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    simulated = subprocess.run(
        (str(obj / "Vtb"),), capture_output=True, text=True, timeout=30
    )
    assert simulated.returncode == 0, simulated.stdout + simulated.stderr
