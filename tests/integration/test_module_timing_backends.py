from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.architecture import expand_architectures
from zlang.backend.manifest import BackendArtifact, IMPLEMENTATION_MANIFEST_VERSION
from zlang.backend.naming import module_rtl_names
from zlang.backend.systemverilog import emit_experimental
from zlang.backend.systemverilog.target import emit_target_artifact
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.module import Module
from zlang.ir.timing import InstanceOutputTiming, TimingKnowledge, ValueTiming
from zlang.ir.types import UIntType
from zlang.targets import generic_implementation_graph
from zlang.timing import timing_info
from zlang.toolchain import generate_verilog, lint_with_verilator


SOURCE = """
module Child {
    clock clk
    reset rst
    in x : u8
    out y : u8
    y = pipeline(2) { x }
    timing { latency 2 ii 1 }
}

module Top {
    clock clk
    reset rst
    in x : u8
    out y : u8
    inst child : Child { x = x }
    y = child.y
    timing { latency 2 ii 1 }
}
"""


def test_recursive_timing_and_generic_graph_use_exact_public_records() -> None:
    type8 = UIntType(8)
    type9 = UIntType(9)
    source = expr.InputRef("x", type8)
    staged = expr.Pipeline(3, source, 7, type8)
    nested = expr.Extend(staged, type9)

    assert timing_info(nested).latency == 3
    assert expand_architectures(nested)[0].timing_contract.latency == 3

    module = compile_source(SOURCE, top="Top", include_clash=False).ir
    graph = generic_implementation_graph(module)
    assert graph.latency == 2
    assert graph.latency_knowledge == TimingKnowledge.KNOWN.value
    assert graph.realization_backend == "backend_independent"

    uncontracted = compile_source(
        "module M { clock clk reset rst in x:u8 out y:u9 "
        "y=extend<9>(pipeline(3) { x }) }",
        include_clash=False,
    )
    assert uncontracted.implementation_graph.latency == 3
    assert uncontracted.implementation_graph.latency_knowledge == "known"

    stateful = compile_source(
        "module S { clock clk reset rst in en:bit out y:u8 reg q:u8=0 "
        "when en { q <- truncate<8>(q+1) } y=q }",
        include_clash=False,
    )
    assert stateful.implementation_graph.latency_knowledge == "unknown"

    reference = expr.InstanceOutputRef("child", "y", type8)
    manual = Module(
        "Manual", (), (),
        instance_output_timings=(
            InstanceOutputTiming("child", "y", ValueTiming.known(5)),
        ),
    )
    assert timing_info(reference, module=manual).latency == 5


def test_timing_and_realization_backend_round_trip_in_artifact() -> None:
    result = compile_source(SOURCE, top="Top", include_clash=False)
    artifact = emit_target_artifact(result.ir, result.implementation_graph)
    restored = BackendArtifact.from_json(artifact.to_json())

    assert restored.manifest_version == IMPLEMENTATION_MANIFEST_VERSION
    assert restored.timing_contract == result.ir.timing_contract
    assert restored.output_timings == result.ir.output_timings
    assert restored.instance_output_timings == result.ir.instance_output_timings
    assert restored.implementation is not None
    assert restored.implementation.realization_backend == "backend_independent"
    assert restored.implementation.latency_knowledge == "known"
    assert restored.implementation.latency == 2


def _harness() -> str:
    return r'''
#include "VTop.h"
#include "verilated.h"
static void tick(VTop& d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VTop d; d.x = 0; d.rst = 1; tick(d);
  if (d.y != 0) return 1;
  d.rst = 0; d.x = 5; tick(d); if (d.y != 0) return 2;
  d.x = 7; tick(d); if (d.y != 5) return 3;
  d.x = 9; tick(d); if (d.y != 7) return 4;
  d.rst = 1; tick(d); if (d.y != 0) return 5;
  d.rst = 0; d.x = 11; tick(d); if (d.y != 0) return 6;
  d.x = 13; tick(d); return d.y == 11 ? 0 : 7;
}
'''


def _build_and_run(rtl: tuple[Path, ...], tmp_path: Path) -> None:
    harness = tmp_path / "test.cpp"
    harness.write_text(_harness())
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    result = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(obj), "--top-module", "Top",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    run = subprocess.run(
        (str(obj / "VTop"),), capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator required")
def test_direct_sv_keeps_timed_child_pipeline_physical_and_deterministic(
    tmp_path: Path,
) -> None:
    module = compile_source(SOURCE, top="Top", include_clash=False).ir
    first = emit_experimental(module)
    second = emit_experimental(module)
    assert first == second
    child_names = module_rtl_names(module.children[0])
    for stage in (1, 2):
        assert first.count(
            f"logic [7:0] {child_names.stage('pipeline', 0, stage)};"
        ) == 1
    assert "module Child_s" in first and " child (" in first
    rtl = tmp_path / "Top.sv"
    rtl.write_text(first)
    lint_with_verilator((rtl,), "Top")
    _build_and_run((rtl,), tmp_path)


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_clash_keeps_timed_child_pipeline_and_matches_reset_fill(
    tmp_path: Path,
) -> None:
    result = compile_source(SOURCE, top="Top")
    child_names = module_rtl_names(result.ir.children[0])
    for stage in (1, 2):
        assert (
            f"{child_names.stage('pipeline', 0, stage)} = register"
            in result.clash
        )
    rtl = generate_verilog(
        result.clash, "Top", tmp_path / "clash", CLASH_EXECUTABLE
    )
    lint_with_verilator(rtl, "Top")
    _build_and_run(rtl, tmp_path)
