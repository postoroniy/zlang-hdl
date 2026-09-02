from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.toolchain import ToolchainError, generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "storage_instance_array.zhl").read_text()


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_fifo_instance_array_clash_preserves_independent_state_and_reset(
    tmp_path: Path,
) -> None:
    compilation = compile_source(SOURCE, top="FifoLaneArray")
    assert compilation.clash.count("fifoLane ::") == 1
    assert compilation.clash.count("= fifoLane (") == 2
    assert "data_zlang" in compilation.clash
    rtl = generate_verilog(
        compilation.clash,
        "FifoLaneArray",
        tmp_path / "rtl",
        CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, "FifoLaneArray")
    harness = tmp_path / "test.cpp"
    harness.write_text(r'''
#include "VFifoLaneArray.h"
#include "verilated.h"
static void tick(VFifoLaneArray& dut) {
  dut.clk = 0; dut.eval(); dut.clk = 1; dut.eval(); dut.clk = 0; dut.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VFifoLaneArray dut;
  dut.data=0; dut.push=0; dut.pop=0; dut.rst=1; tick(dut);
  dut.rst=0; dut.data=0x1122; dut.push=3; tick(dut);
  if (dut.front != 0x1122) return 1;
  dut.data=0x3344; dut.push=3; tick(dut);
  if (dut.front != 0x1122) return 2;
  dut.data=0x5566; dut.push=3; dut.pop=0; tick(dut);
  if (dut.front != 0x1122) return 3;
  dut.data=0x0055; dut.push=1; dut.pop=1; tick(dut);
  if (dut.front != 0x1144) return 4;
  dut.push=0; dut.pop=2; tick(dut);
  if (dut.front != 0x3344) return 5;
  dut.rst=1; dut.push=0; dut.pop=0; tick(dut);
  dut.rst=0; dut.data=0xaabb; dut.push=3; tick(dut);
  return dut.front == 0xaabb ? 0 : 6;
}
''')
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(tmp_path / "obj"), "--top-module", "FifoLaneArray",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(tmp_path / "obj" / "VFifoLaneArray"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(
    ("top", "harness"),
    (
        ("MemoryLaneArray", r'''
#include "VMemoryLaneArray.h"
#include "verilated.h"
static void tick(VMemoryLaneArray& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }
int main(int argc,char**argv) {
  Verilated::commandArgs(argc,argv); VMemoryLaneArray d;
  d.read_address=0; d.write_enable=0; d.write_address=0; d.write_data=0; d.rst=1; tick(d);
  if (d.read_data != 0) return 1;
  d.rst=0; d.write_enable=3; d.write_address=0; d.write_data=0x1122; tick(d);
  if (d.read_data != 0x1122) return 2;
  d.read_address=2; d.write_enable=2; d.write_address=2; d.write_data=0x3300; tick(d);
  if (d.read_data != 0x3322) return 3;
  d.read_address=1; d.write_enable=0; tick(d); if (d.read_data != 0x1100) return 4;
  d.rst=1; tick(d); if (d.read_data != 0) return 5;
  d.rst=0; d.read_address=0; tick(d); return d.read_data == 0 ? 0 : 6;
}
'''),
    ),
)
def test_memory_and_rom_arrays_real_clash_exact_behavior(
    tmp_path: Path, top: str, harness: str
) -> None:
    compilation = compile_source(SOURCE, top=top)
    child = top.removesuffix("Array")
    assert compilation.clash.count(f"{child[:1].lower() + child[1:]} ::") == 1
    artifact = emit_sv_artifact(
        compile_source(SOURCE, top=top, include_clash=False).ir
    )
    companions = artifact.companions
    rtl = generate_verilog(
        compilation.clash, top, tmp_path / "rtl", CLASH_EXECUTABLE,
        companions=companions,
    )
    lint_with_verilator(rtl, top)
    for companion in companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    harness_path = tmp_path / "test.cpp"
    harness_path.write_text(harness)
    environment = os.environ.copy(); environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN", "-Wno-WIDTHTRUNC",
            "--Mdir", str(tmp_path / "obj"), "--top-module", top,
            *(str(path) for path in rtl), str(harness_path),
        ), cwd=tmp_path, env=environment, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run((str(tmp_path / "obj" / f"V{top}"),), cwd=tmp_path)
    assert run.returncode == 0


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_rom_array_clash_reaches_documented_primitive_width_blocker(
    tmp_path: Path,
) -> None:
    """Do not hide the existing romFilePow2 64-bit selector warning.

    Changing to a mux ROM would alter the generic physical ROM strategy, so
    this bounded array slice records the strict-lint blocker instead.
    """

    compilation = compile_source(SOURCE, top="RomLaneArray")
    artifact = emit_sv_artifact(
        compile_source(SOURCE, top="RomLaneArray", include_clash=False).ir
    )
    rtl = generate_verilog(
        compilation.clash,
        "RomLaneArray",
        tmp_path / "rtl",
        CLASH_EXECUTABLE,
        companions=artifact.companions,
    )
    with pytest.raises(ToolchainError, match="WIDTHTRUNC"):
        lint_with_verilator(rtl, "RomLaneArray")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_rom_array_real_clash_exact_behavior_with_frozen_rom_waiver(
    tmp_path: Path,
) -> None:
    """Exercise two physical ROM read states under the existing Clash waiver."""

    first = compile_source(SOURCE, top="RomLaneArray")
    second = compile_source(SOURCE, top="RomLaneArray")
    first_artifact = emit_sv_artifact(
        compile_source(SOURCE, top="RomLaneArray", include_clash=False).ir
    )
    second_artifact = emit_sv_artifact(
        compile_source(SOURCE, top="RomLaneArray", include_clash=False).ir
    )
    assert first.clash == second.clash
    assert first_artifact.companions == second_artifact.companions
    assert len(first_artifact.companions) == 1
    assert first.clash.count("romLane ::") == 1
    assert first.clash.count("= romLane (") == 2

    rtl = generate_verilog(
        first.clash,
        "RomLaneArray",
        tmp_path / "rtl",
        CLASH_EXECUTABLE,
        companions=first_artifact.companions,
    )
    for companion in first_artifact.companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    harness = tmp_path / "test.cpp"
    harness.write_text(r'''
#include "VRomLaneArray.h"
#include "verilated.h"
static void tick(VRomLaneArray& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc,char**argv) {
  Verilated::commandArgs(argc,argv); VRomLaneArray d;
  // Reset masks both independent registered read results.
  d.address=0; d.rst=1; tick(d); if (d.data != 0) return 1;
  // v[0] is the MSB region: lane0 reads 1, lane1 reads 0.
  d.rst=0; d.address=2; tick(d); if (d.data != 0x0b0a) return 2;
  // Swap addresses and prove the two physical read states update separately.
  d.address=1; tick(d); if (d.data != 0x0a0b) return 3;
  // Holding the address retains the same deterministic one-cycle result.
  tick(d); if (d.data != 0x0a0b) return 4;
  d.rst=1; tick(d); if (d.data != 0) return 5;
  d.rst=0; d.address=3; tick(d);
  return d.data == 0x0b0b ? 0 : 6;
}
''')
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            # Frozen standalone-ROM waiver for Clash 1.11 romFile indexing.
            "-Wno-WIDTHTRUNC",
            "--Mdir", str(tmp_path / "obj"), "--top-module", "RomLaneArray",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(tmp_path / "obj" / "VRomLaneArray"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
