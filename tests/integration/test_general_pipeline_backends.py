from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


SOURCE = """
module GeneralExpressionPipeline {
  clock clk reset rst
  in a,b,c,d,e:u8
  in f:u25
  out y:u26
  y=pipeline(3){(a*b+c*d)*e+f}
}
"""


def _harness() -> str:
    return r'''
#include "VGeneralExpressionPipeline.h"
#include "verilated.h"
static void tick(VGeneralExpressionPipeline& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
static void drive(VGeneralExpressionPipeline& d, unsigned n) {
  d.a=n+1; d.b=2; d.c=n+3; d.d=4; d.e=5; d.f=n;
}
static unsigned expected(unsigned n) {
  return (((n+1)*2 + (n+3)*4)*5 + n);
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VGeneralExpressionPipeline d; d.rst=1; drive(d,0); tick(d);
  if (d.y != 0) return 1;
  d.rst=0;
  for (unsigned n=0; n<8; ++n) {
    drive(d,n); tick(d);
    unsigned want = n < 2 ? 0 : expected(n-2);
    if (d.y != want) return 10+n;
  }
  d.rst=1; tick(d); return d.y == 0 ? 0 : 30;
}
'''


def _build_and_run(rtl: tuple[Path, ...], tmp_path: Path) -> None:
    harness = tmp_path / "test.cpp"
    harness.write_text(_harness())
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(obj), "--top-module", "GeneralExpressionPipeline",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(obj / "VGeneralExpressionPipeline"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_direct_sv_contains_real_internal_boundaries() -> None:
    result = compile_source(SOURCE, include_clash=False)
    direct = emit_experimental(result.ir)

    # Stage zero registers the two products and the short f bypass is delayed
    # to the final add.  The production backend does not contain a single full
    # expression followed by two empty output-delay stages.
    assert direct.count("always_ff") == 1
    assert direct.count(" <= (") >= 3
    assert "pipeline_0_s1 <= " in direct
    assert "pipeline_1_s1 <= " in direct
    assert "pipeline_5_s1 <= " in direct
    assert "requested_latency=3" in result.pipeline_report
    assert "scheduler=dag_partition_v1" in result.pipeline_report
    assert "cost_source=structural_estimate" in result.pipeline_report
    assert "timed_equivalence=verified" in result.pipeline_report

    first_artifact = emit_artifact(result.ir)
    second_artifact = emit_artifact(compile_source(SOURCE, include_clash=False).ir)
    assert first_artifact.text == second_artifact.text
    assert first_artifact.artifact_hash == second_artifact.artifact_hash


def test_shared_dag_node_is_materialized_once_in_direct_sv() -> None:
    source = (
        "module Shared { clock clk reset rst in a,b,c,d:u8 out y:u34 "
        "t=a*b y=pipeline(3){(t+c)*(t+d)} }"
    )
    result = compile_source(source, include_clash=False)
    direct = emit_experimental(result.ir)
    assert direct.count("assign zlang_stage_expr_0 = ") == 1
    assert direct.count("zlang_stage_expr_0}") == 2


@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys required")
def test_scheduled_pipeline_reduces_measured_logic_depth(tmp_path: Path) -> None:
    """Yosys structural evidence: staged computation shortens the longest path."""
    from dataclasses import replace
    from zlang.ir import expressions as expression_ir
    from zlang.pipeline_scheduling import erase_pipeline_timing

    result = compile_source(SOURCE, include_clash=False)
    assignment = next(item for item in result.ir.assignments if item.target.name == "y")
    base = erase_pipeline_timing(assignment.expression)
    legacy = replace(
        result.ir,
        assignments=tuple(
            replace(item, expression=expression_ir.Pipeline(3, base, 9901, assignment.expression.type))
            if item is assignment else item
            for item in result.ir.assignments
        ),
    )
    scheduled_path = tmp_path / "GeneralExpressionPipeline.sv"
    legacy_path = tmp_path / "GeneralExpressionPipelineLegacy.sv"
    scheduled_path.write_text(emit_artifact(result.ir).text)
    legacy_path.write_text(emit_artifact(legacy).text.replace(
        "GeneralExpressionPipeline", "GeneralExpressionPipelineLegacy"
    ))

    def depth(path: Path, top: str) -> int:
        completed = subprocess.run(
            ("yosys", "-Q", "-p", f"read_verilog -sv {path}; hierarchy -top {top}; synth -top {top} -flatten; abc -lut 6; clean; ltp -noff"),
            capture_output=True, text=True, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        marker = "Longest topological path"
        line = next(line for line in completed.stdout.splitlines() if marker in line)
        return int(line.rsplit("length=", 1)[1].split(")", 1)[0])

    scheduled_depth = depth(scheduled_path, "GeneralExpressionPipeline")
    legacy_depth = depth(legacy_path, "GeneralExpressionPipelineLegacy")
    assert scheduled_depth < legacy_depth


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator required")
def test_direct_sv_staged_pipeline_is_cycle_exact(tmp_path: Path) -> None:
    rtl = tmp_path / "GeneralExpressionPipeline.sv"
    rtl.write_text(emit_experimental(compile_source(SOURCE, include_clash=False).ir))
    lint_with_verilator((rtl,), "GeneralExpressionPipeline")
    _build_and_run((rtl,), tmp_path)
