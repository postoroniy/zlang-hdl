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
    generate(i in 0..2) {
        lane[i].enable=enables[i]
        lane[i].step=1
    }
    values=generate(i in 0..2) lane[i].value
}
"""


def _emit(source: str = SOURCE, top: str = "StateLaneArray") -> BackendArtifact:
    return emit_artifact(compile_source(source, top=top, include_clash=False).ir)


def _run_verilator(
    tmp_path: Path, artifact: BackendArtifact, top: str, harness_text: str
) -> None:
    rtl = tmp_path / f"{top}.sv"
    harness = tmp_path / "test.cpp"
    rtl.write_text(artifact.text)
    harness.write_text(harness_text)
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
        (str(tmp_path / "obj" / f"V{top}"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_sequential_array_direct_sv_is_structural_and_deterministic() -> None:
    first = _emit()
    second = _emit()

    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert first.to_json() == second.to_json()
    assert first.text.count("module StateLane__") == 1
    assert first.text.count("StateLane__") == 3  # definition plus two instances
    assert first.text.count("always_ff @(posedge clk)") == 1
    assert "assign values = {zlang_instance_lane_0_" in first.text
    assert "zlang_instance_lane_1_" in first.text
    assert ".enable(enables[1])" in first.text
    assert ".enable(enables[0])" in first.text

    restored = BackendArtifact.from_json(first.to_json())
    assert restored.artifact_hash == first.artifact_hash
    assert restored.manifest_version == first.manifest_version
    assert [item.semantic_signal_id for item in restored.bindings] == [
        "port:enables", "port:steps", "port:values", "clock", "reset"
    ]
    assert all(item.artifact_hash == first.artifact_hash for item in first.bindings)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_sequential_array_direct_sv_has_exact_lane_order_and_reset(
    tmp_path: Path,
) -> None:
    _run_verilator(tmp_path, _emit(), "StateLaneArray", r'''
#include "VStateLaneArray.h"
#include "verilated.h"
static void tick(VStateLaneArray& dut) {
  dut.clk = 0; dut.eval();
  dut.clk = 1; dut.eval();
  dut.clk = 0; dut.eval();
}
static void set_bits(VStateLaneArray& dut, unsigned value) {
  dut.enables[0] = (value >> 1) & 1u;
  dut.enables[1] = value & 1u;
}
static void set_steps(VStateLaneArray& dut, unsigned value) {
  dut.steps[0] = (value >> 8) & 0xffu;
  dut.steps[1] = value & 0xffu;
}
static int expect(VStateLaneArray& dut, unsigned value, int line) {
  const unsigned actual = (unsigned(dut.values[0]) << 8) | dut.values[1];
  return actual == value ? 0 : line;
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VStateLaneArray dut;
  set_bits(dut, 0); set_steps(dut, 0); dut.rst = 1; tick(dut);
  if (int rc = expect(dut, 0x0000, __LINE__)) return rc;
  dut.rst = 0; set_bits(dut, 3); set_steps(dut, 0x0507); tick(dut);
  if (int rc = expect(dut, 0x0507, __LINE__)) return rc;
  set_steps(dut, 0x0306); tick(dut);
  if (int rc = expect(dut, 0x080d, __LINE__)) return rc;
  set_bits(dut, 1); set_steps(dut, 0x0002); tick(dut);
  if (int rc = expect(dut, 0x080f, __LINE__)) return rc;
  dut.rst = 1; tick(dut);
  if (int rc = expect(dut, 0x0000, __LINE__)) return rc;
  dut.rst = 0; set_bits(dut, 3); set_steps(dut, 0x0101); tick(dut);
  return expect(dut, 0x0101, __LINE__);
}
''')


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_constant_indexed_binding_is_one_signal_per_child_in_direct_sv(
    tmp_path: Path,
) -> None:
    artifact = _emit(CONSTANT_SOURCE, "ConstantStateLaneArray")
    assert artifact.text.count(".step(8'd1)") == 2
    _run_verilator(tmp_path, artifact, "ConstantStateLaneArray", r'''
#include "VConstantStateLaneArray.h"
#include "verilated.h"
static void tick(VConstantStateLaneArray& dut) {
  dut.clk = 0; dut.eval(); dut.clk = 1; dut.eval(); dut.clk = 0; dut.eval();
}
static void set_enables(VConstantStateLaneArray& dut, unsigned value) {
  dut.enables[0] = (value >> 1) & 1u;
  dut.enables[1] = value & 1u;
}
static unsigned values(VConstantStateLaneArray& dut) {
  return (unsigned(dut.values[0]) << 8) | dut.values[1];
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VConstantStateLaneArray dut;
  set_enables(dut, 0); dut.rst = 1; tick(dut);
  dut.rst = 0; set_enables(dut, 3); tick(dut);
  if (values(dut) != 0x0101) return 1;
  set_enables(dut, 1); tick(dut);
  return values(dut) == 0x0102 ? 0 : 2;
}
''')
