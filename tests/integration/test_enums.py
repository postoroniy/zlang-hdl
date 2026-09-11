from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


SOURCE = """
enum Phase { Idle Active Done }
module EnumFsm {
  clock clk reset rst
  in start:bit in finish:bit out phase:Phase
  reg state:Phase=Phase.Idle
  when 1 {
    state <- switch state {
      Phase.Idle=>start ? Phase.Active : Phase.Idle
      Phase.Active=>finish ? Phase.Done : Phase.Active
      Phase.Done=>start ? Phase.Active : Phase.Idle
    }
  }
  phase=state
}
"""

HARNESS = r'''
#include "VEnumFsm.h"
#include "verilated.h"
static void tick(VEnumFsm& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VEnumFsm d;
  d.start=0; d.finish=0; d.rst=1; tick(d);
  if (d.phase != 0) return 1;
  d.rst=0; d.start=1; tick(d);
  if (d.phase != 1) return 2;
  d.start=0; d.finish=1; tick(d);
  if (d.phase != 2) return 3;
  d.finish=0; tick(d);
  if (d.phase != 0) return 4;
  d.start=1; tick(d);
  if (d.phase != 1) return 5;
  d.rst=1; tick(d);
  return d.phase == 0 ? 0 : 6;
}
'''


def _verilate_and_run(tmp_path: Path, rtl: tuple[Path, ...], suffix: str) -> None:
    harness = tmp_path / f"enum_{suffix}.cpp"
    obj = tmp_path / f"obj_{suffix}"
    harness.write_text(HARNESS)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(obj), "--top-module", "EnumFsm",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(obj / "VEnumFsm"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_enum_fsm_direct_systemverilog_is_width_exact_and_behaves(
    tmp_path: Path,
) -> None:
    compilation = compile_source(SOURCE, top="EnumFsm")
    first = emit_artifact(compilation.ir)
    second = emit_artifact(compilation.ir)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    phase = next(
        binding for binding in first.bindings
        if binding.semantic_signal_id == "port:phase"
    )
    assert phase.width == 2
    assert phase.canonical_type is not None
    assert phase.canonical_type.startswith("enum<Phase:")
    rtl = tmp_path / "EnumFsm.sv"
    rtl.write_text(first.text)
    _verilate_and_run(tmp_path, (rtl,), "sv")
