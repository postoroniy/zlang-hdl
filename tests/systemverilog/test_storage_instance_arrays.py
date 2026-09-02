from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "storage_instance_array.zhl").read_text()


def _artifact() -> BackendArtifact:
    module = compile_source(
        SOURCE, top="FifoLaneArray", include_clash=False
    ).ir
    return emit_artifact(module)


def _artifact_for(top: str) -> BackendArtifact:
    return emit_artifact(
        compile_source(SOURCE, top=top, include_clash=False).ir
    )


def _run_verilator(
    tmp_path: Path, artifact: BackendArtifact, top: str, harness_text: str
) -> None:
    rtl = tmp_path / f"{top}.sv"
    harness = tmp_path / "test.cpp"
    rtl.write_text(artifact.text)
    harness.write_text(harness_text)
    for companion in artifact.companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(tmp_path / "obj"), "--top-module", top,
            str(rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(tmp_path / "obj" / f"V{top}"),), cwd=tmp_path,
        capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_fifo_instance_array_is_structural_deterministic_and_bound() -> None:
    first = _artifact()
    second = _artifact()

    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert first.to_json() == second.to_json()
    assert first.text.count("module FifoLane__") == 1
    assert first.text.count("FifoLane__") == 3
    assert first.text.count("logic [1:0] queue_count;") == 1
    assert first.text.count("zlang_instance_lane_0_") > 1
    assert first.text.count("zlang_instance_lane_1_") > 1
    assert ".data(data[15:8])" in first.text
    assert ".data(data[7:0])" in first.text

    restored = BackendArtifact.from_json(first.to_json())
    # Artifact JSON is a publication manifest and deliberately omits source
    # text; its identity and typed bindings must round-trip exactly.
    assert restored.artifact_hash == first.artifact_hash
    assert restored.bindings == first.bindings
    assert restored.manifest_version == first.manifest_version
    assert [item.semantic_signal_id for item in restored.bindings] == [
        "port:data", "port:push", "port:pop", "port:front", "clock", "reset"
    ]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_fifo_instance_array_direct_sv_preserves_independent_state_and_reset(
    tmp_path: Path,
) -> None:
    artifact = _artifact()
    rtl = tmp_path / "FifoLaneArray.sv"
    harness = tmp_path / "test.cpp"
    rtl.write_text(artifact.text)
    harness.write_text(r'''
#include "VFifoLaneArray.h"
#include "verilated.h"
static void tick(VFifoLaneArray& dut) {
  dut.clk = 0; dut.eval(); dut.clk = 1; dut.eval(); dut.clk = 0; dut.eval();
}
static void set_data(VFifoLaneArray& dut, unsigned value) {
  dut.data[0] = (value >> 8) & 0xffu; dut.data[1] = value & 0xffu;
}
static void set_push(VFifoLaneArray& dut, unsigned value) {
  dut.push[0] = (value >> 1) & 1u; dut.push[1] = value & 1u;
}
static void set_pop(VFifoLaneArray& dut, unsigned value) {
  dut.pop[0] = (value >> 1) & 1u; dut.pop[1] = value & 1u;
}
static unsigned front(VFifoLaneArray& dut) {
  return (unsigned(dut.front[0]) << 8) | dut.front[1];
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VFifoLaneArray dut;
  set_data(dut,0); set_push(dut,0); set_pop(dut,0); dut.rst=1; tick(dut);
  dut.rst=0; set_data(dut,0x1122); set_push(dut,3); tick(dut);
  if (front(dut) != 0x1122) return 1;
  set_data(dut,0x3344); set_push(dut,3); tick(dut);
  if (front(dut) != 0x1122) return 2;
  // Both FIFOs are full: unaccompanied pushes must stall.
  set_data(dut,0x5566); set_push(dut,3); set_pop(dut,0); tick(dut);
  if (front(dut) != 0x1122) return 3;
  // Lane 1 accepts simultaneous pop/push while lane 0 holds independently.
  set_data(dut,0x0055); set_push(dut,1); set_pop(dut,1); tick(dut);
  if (front(dut) != 0x1144) return 4;
  set_push(dut,0); set_pop(dut,2); tick(dut);
  if (front(dut) != 0x3344) return 5;
  dut.rst=1; set_push(dut,0); set_pop(dut,0); tick(dut);
  dut.rst=0; set_data(dut,0xaabb); set_push(dut,3); tick(dut);
  return front(dut) == 0xaabb ? 0 : 6;
}
''')
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(tmp_path / "obj"), "--top-module", "FifoLaneArray",
            str(rtl), str(harness),
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


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize(
    ("top", "harness"),
    (
        ("MemoryLaneArray", r'''
#include "VMemoryLaneArray.h"
#include "verilated.h"
static void tick(VMemoryLaneArray& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }
static void read_address(VMemoryLaneArray& d, unsigned value) {
  d.read_address[0]=(value >> 1) & 1u; d.read_address[1]=value & 1u;
}
static void write_enable(VMemoryLaneArray& d, unsigned value) {
  d.write_enable[0]=(value >> 1) & 1u; d.write_enable[1]=value & 1u;
}
static void write_address(VMemoryLaneArray& d, unsigned value) {
  d.write_address[0]=(value >> 1) & 1u; d.write_address[1]=value & 1u;
}
static void write_data(VMemoryLaneArray& d, unsigned value) {
  d.write_data[0]=(value >> 8) & 0xffu; d.write_data[1]=value & 0xffu;
}
static unsigned read_data(VMemoryLaneArray& d) {
  return (unsigned(d.read_data[0]) << 8) | d.read_data[1];
}
int main(int argc,char**argv) {
  Verilated::commandArgs(argc,argv); VMemoryLaneArray d;
  read_address(d,0); write_enable(d,0); write_address(d,0); write_data(d,0); d.rst=1; tick(d);
  if (read_data(d) != 0) return 1;
  d.rst=0; write_enable(d,3); write_address(d,0); write_data(d,0x1122); tick(d);
  if (read_data(d) != 0x1122) return 2; // write-first collision in both lanes
  read_address(d,2); write_enable(d,2); write_address(d,2); write_data(d,0x3300); tick(d);
  if (read_data(d) != 0x3322) return 3;
  read_address(d,1); write_enable(d,0); tick(d);
  if (read_data(d) != 0x1100) return 4; // independent addresses/cells
  d.rst=1; tick(d); if (read_data(d) != 0) return 5;
  d.rst=0; read_address(d,0); tick(d);
  return read_data(d) == 0 ? 0 : 6;
}
'''),
        ("RomLaneArray", r'''
#include "VRomLaneArray.h"
#include "verilated.h"
static void tick(VRomLaneArray& d) { d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }
static void address(VRomLaneArray& d, unsigned value) {
  d.address[0]=(value >> 1) & 1u; d.address[1]=value & 1u;
}
static unsigned data(VRomLaneArray& d) {
  return (unsigned(d.data[0]) << 8) | d.data[1];
}
int main(int argc,char**argv) {
  Verilated::commandArgs(argc,argv); VRomLaneArray d;
  address(d,0); d.rst=1; tick(d); if (data(d) != 0) return 1;
  d.rst=0; address(d,2); tick(d); if (data(d) != 0x0b0a) return 2;
  address(d,1); tick(d); if (data(d) != 0x0a0b) return 3;
  d.rst=1; tick(d); if (data(d) != 0) return 4;
  d.rst=0; address(d,3); tick(d);
  return data(d) == 0x0b0b ? 0 : 5;
}
'''),
    ),
)
def test_memory_and_rom_arrays_direct_sv_exact_behavior(
    tmp_path: Path, top: str, harness: str
) -> None:
    artifact = _artifact_for(top)
    assert artifact.text.count(f"module {top.removesuffix('Array')}__") == 1
    assert artifact.text.count(f"{top.removesuffix('Array')}__") == 3
    assert artifact == _artifact_for(top)
    source = tmp_path / f"{top}.sv"
    source.write_text(artifact.text)
    for companion in artifact.companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    if top == "RomLaneArray":
        assert len(artifact.companions) == 1
        assert artifact.text.count("$readmemb") == 1
    _run_verilator(tmp_path, artifact, top, harness)
