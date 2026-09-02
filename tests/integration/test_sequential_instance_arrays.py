from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.compiler import compile_source
from zlang.toolchain import generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "sequential_instance_array.zhl").read_text()

CONSTANT_SOURCE = """
module ConstantLane {
    clock clk reset rst
    in enable:bit in step:u8 out value:u8
    reg count:u8=0
    when enable { count <- truncate<8>(count + step) }
    value=count
}
module ConstantStateLaneArray {
    clock clk reset rst
    in enables:vec<2,bit> out values:vec<2,u8>
    inst lane[2]:ConstantLane
    generate(i in 0..2) { lane[i].enable=enables[i] lane[i].step=1 }
    values=generate(i in 0..2) lane[i].value
}
"""


def _build_and_run(
    tmp_path: Path, source: str, top: str, harness_text: str
) -> str:
    compilation = compile_source(source, top=top)
    rtl = generate_verilog(
        compilation.clash, top, tmp_path / "rtl", CLASH_EXECUTABLE
    )
    lint_with_verilator(rtl, top)
    harness = tmp_path / "test.cpp"
    harness.write_text(harness_text)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(tmp_path / "obj"), "--top-module", top,
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(tmp_path / "obj" / f"V{top}"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
    return compilation.clash


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_sequential_array_clash_has_exact_lane_order_and_reset(tmp_path: Path) -> None:
    clash = _build_and_run(tmp_path, SOURCE, "StateLaneArray", r'''
#include "VStateLaneArray.h"
#include "verilated.h"
static void tick(VStateLaneArray& dut) {
  dut.clk = 0; dut.eval(); dut.clk = 1; dut.eval(); dut.clk = 0; dut.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VStateLaneArray dut;
  dut.enables = 0; dut.steps = 0; dut.rst = 1; tick(dut);
  if (dut.values != 0x0000) return 1;
  dut.rst = 0; dut.enables = 3; dut.steps = 0x0507; tick(dut);
  if (dut.values != 0x0507) return 2;
  dut.steps = 0x0306; tick(dut);
  if (dut.values != 0x080d) return 3;
  dut.enables = 1; dut.steps = 0x0002; tick(dut);
  if (dut.values != 0x080f) return 4;
  dut.rst = 1; tick(dut);
  return dut.values == 0 ? 0 : 5;
}
''')
    assert "stateLane ((\\value_0 ->" in clash
    assert "stateLane ((\\value_0 ->" in clash
    assert "lane[" not in clash


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_constant_indexed_binding_is_parenthesized_for_real_clash(
    tmp_path: Path,
) -> None:
    clash = _build_and_run(
        tmp_path,
        CONSTANT_SOURCE,
        "ConstantStateLaneArray",
        r'''
#include "VConstantStateLaneArray.h"
#include "verilated.h"
static void tick(VConstantStateLaneArray& dut) {
  dut.clk = 0; dut.eval(); dut.clk = 1; dut.eval(); dut.clk = 0; dut.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VConstantStateLaneArray dut;
  dut.enables = 0; dut.rst = 1; tick(dut);
  dut.rst = 0; dut.enables = 3; tick(dut);
  if (dut.values != 0x0101) return 1;
  dut.enables = 1; tick(dut);
  return dut.values == 0x0102 ? 0 : 2;
}
''',
    )
    assert clash.count("(pure ((1 :: Unsigned 8)))") == 2
    assert "pure ((1 :: Unsigned 8))" in clash
