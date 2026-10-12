from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


VERILATOR = shutil.which("verilator")


SOURCE = """
enum TxPhase { Idle Header Payload Done }
module Controller {
  clock clk reset rst
  in start:bit in advance:bit in cancel:bit in finish:bit
  out state_out:TxPhase
  fsm phase:TxPhase=Idle {
    Idle { when start -> Header {} }
    Header { when advance -> Payload {} }
    Payload {
      priority {
        when cancel -> Idle {}
        when finish -> Done {}
      }
    }
    Done { -> Idle {} }
  }
  state_out=phase
}
"""


HARNESS = r"""
#include "VController.h"
#include "verilated.h"
static void tick(VController& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
static bool state(VController& d, unsigned phase) {
  d.eval();
  return d.state_out == phase;
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VController d;
  d.start=0; d.advance=0; d.cancel=0; d.finish=0;
  d.rst=1; tick(d);
  if (!state(d, 0)) return 1;
  d.rst=0; d.start=1; tick(d);
  if (!state(d, 1)) return 2;
  d.start=0; d.advance=1; tick(d);
  if (!state(d, 2)) return 3;
  d.advance=0; d.cancel=1; d.finish=1; tick(d);
  if (!state(d, 0)) return 4;
  d.cancel=0; d.finish=0; d.start=1; tick(d);
  d.start=0; d.advance=1; tick(d);
  d.advance=0; d.finish=1; tick(d);
  if (!state(d, 3)) return 5;
  d.finish=0; tick(d);
  if (!state(d, 0)) return 6;
  d.start=1; tick(d);
  d.rst=1; tick(d);
  return state(d, 0) ? 0 : 7;
}
"""


def _build_and_run(root: Path, rtl: tuple[Path, ...], tag: str) -> None:
    harness = root / f"fsm_{tag}.cpp"
    harness.write_text(HARNESS)
    obj = root / f"obj_{tag}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(obj), "--top-module", "Controller",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(obj / "VController"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_concise_fsm_direct_sv_is_deterministic_and_cycle_exact(
    tmp_path: Path,
) -> None:
    module = compile_source(SOURCE).ir
    first = emit_artifact(module)
    second = emit_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    rtl = tmp_path / "Controller.sv"
    rtl.write_text(first.text)
    lint_with_verilator((rtl,), "Controller")
    _build_and_run(tmp_path, (rtl,), "sv")
