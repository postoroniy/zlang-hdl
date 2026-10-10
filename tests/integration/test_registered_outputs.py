from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.native_simulation import simulate_cycles
from zlang.toolchain import lint_with_verilator


VERILATOR = shutil.which("verilator")
YOSYS = shutil.which("yosys")

SOURCE = """
module RegisteredOutputParity {
    clock clk reset rst
    in load:bit in value:s8
    out reg held:s8=-1
    when load { held <- value }
}
"""

HARNESS = r"""
#include "VRegisteredOutputParity.h"
#include "verilated.h"
#include <cstdio>

static void tick(VRegisteredOutputParity& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
static void observe(VRegisteredOutputParity& d) {
  d.clk=0; d.eval(); std::printf("%d\n", int8_t(d.held));
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VRegisteredOutputParity d;
  d.rst=1; d.load=0; d.value=4; tick(d); observe(d);
  d.rst=0; d.load=1; d.value=7; tick(d); observe(d);
  d.load=0; d.value=0xf8; tick(d); observe(d);
  d.value=0; tick(d); observe(d);
  return 0;
}
"""


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required",
)
def test_registered_output_native_direct_sv_and_yosys_agree(tmp_path: Path) -> None:
    module = compile_source(SOURCE).ir
    native = simulate_cycles(
        module,
        (
            {"load": 0, "value": 4},
            {"load": 1, "value": 7},
            {"load": 0, "value": -8},
            {"load": 0, "value": 0},
            {"load": 0, "value": 0},
        ),
        reset=(True, False, False, False, False),
    )
    # Native traces expose pre-edge state; the RTL harness observes the same
    # state immediately after each edge, hence the one-cycle alignment.
    expected = [cycle["held"] for cycle in native[1:]]

    rtl = tmp_path / "RegisteredOutputParity.sv"
    rtl.write_text(emit_artifact(module).text, encoding="utf-8")
    lint_with_verilator((rtl,), module.name)
    yosys = subprocess.run(
        (
            YOSYS,
            "-q",
            "-p",
            (
                f"read_verilog -sv {rtl}; hierarchy -top {module.name}; "
                "proc; opt; check"
            ),
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert yosys.returncode == 0, yosys.stderr or yosys.stdout

    harness = tmp_path / "registered_output.cpp"
    harness.write_text(HARNESS, encoding="utf-8")
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    build = subprocess.run(
        (
            VERILATOR,
            "--cc",
            "--exe",
            "--build",
            "-Wno-DECLFILENAME",
            "--Mdir",
            str(obj),
            "--top-module",
            module.name,
            str(rtl),
            str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    run = subprocess.run(
        (str(obj / f"V{module.name}"),),
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr or run.stdout
    assert [int(line) for line in run.stdout.splitlines()] == expected
