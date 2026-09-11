from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator is required")
def test_hardware_connected_csr_matches_frozen_priority_and_pulse(tmp_path: Path) -> None:
    module = compile_source(
        (ROOT / "examples" / "engine_csr.zhl").read_text()
    ).ir
    rtl = tmp_path / "EngineCsr.sv"
    harness = tmp_path / "test.cpp"
    rtl.write_text(emit_experimental(module))
    harness.write_text(r'''
#include "VEngineCsr.h"
static void tick(VEngineCsr& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main() {
  VEngineCsr d; d.addr=0; d.write=0; d.wdata=0; d.read=0;
  d.engine_busy=0; d.engine_error=0; d.rst=1; tick(d); d.rst=0;
  d.engine_error=1; tick(d); d.engine_error=0;
  d.addr=0x50000004; d.read=1; d.eval(); if (d.rdata != 2) return 1;
  d.read=0; d.write=1; d.wdata=2; d.engine_error=1; tick(d);
  d.write=0; d.engine_error=0; d.read=1; d.eval(); if (d.rdata != 2) return 2;
  d.read=0; d.write=1; d.wdata=2; tick(d);
  d.write=0; d.read=1; d.eval(); if (d.rdata != 0) return 3;
  d.engine_busy=1; d.eval(); if (d.rdata != 1) return 4;
  d.read=0; d.write=1; d.addr=0x50000000; d.wdata=1; tick(d);
  if (!d.engine_start) return 5;
  d.write=0; tick(d); return d.engine_start ? 6 : 0;
}
''')
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    result = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--top-module",
            "EngineCsr", "--Mdir", str(obj), str(rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run(
        (str(obj / "VEngineCsr"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout
