from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_systemverilog_artifact
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


NAMED_SOURCE = """
interface FirIfc {
    clock clk
    reset rst
    in a : u8 @clk
    in b : u8 @clk
    out y : u9 @clk
    timing { latency 4 ii 1 }
}

module TimedFir : FirIfc {
    clock clk
    reset rst
    in a : u8 @clk
    in b : u8 @clk
    out y : u9 @clk
    y = pipeline(4) { a + b }
    timing { latency 4 ii 1 }
}
"""


PLAIN_SOURCE = """
module TimedFir {
    clock clk
    reset rst
    in a : u8 @clk
    in b : u8 @clk
    out y : u9 @clk
    y = pipeline(4) { a + b }
    timing { latency 4 ii 1 }
}
"""


HARNESS = r'''
#include "VTimedFir.h"
#include "verilated.h"
static void tick(VTimedFir& d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VTimedFir d;
  d.a = 0; d.b = 0; d.rst = 1; tick(d);
  if (d.y != 0) return 1;
  d.rst = 0; d.a = 5; d.b = 7;
  tick(d); if (d.y != 0) return 2;
  d.a = 6; d.b = 8; tick(d); if (d.y != 0) return 3;
  d.a = 0; d.b = 0; tick(d); if (d.y != 0) return 4;
  tick(d); if (d.y != 12) return 5;
  tick(d); if (d.y != 14) return 6;
  d.rst = 1; tick(d); if (d.y != 0) return 7;
  d.rst = 0; d.a = 9; d.b = 10;
  tick(d); tick(d); tick(d); tick(d);
  return d.y == 19 ? 0 : 8;
}
'''


def _build_and_run(tmp_path: Path, rtl: tuple[Path, ...], suffix: str) -> None:
    harness = tmp_path / f"named_interface_{suffix}.cpp"
    harness.write_text(HARNESS, encoding="utf-8")
    obj = tmp_path / f"obj_{suffix}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(obj), "--top-module", "TimedFir",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(obj / "VTimedFir"),), capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator required")
def test_named_timed_fir_direct_sv_lints_and_simulates(tmp_path: Path) -> None:
    module = compile_source(NAMED_SOURCE).ir
    artifact = emit_systemverilog_artifact(module)
    rtl = tmp_path / "TimedFir.sv"
    rtl.write_text(artifact.text, encoding="utf-8")
    lint_with_verilator((rtl,), "TimedFir")
    _build_and_run(tmp_path, (rtl,), "sv")
