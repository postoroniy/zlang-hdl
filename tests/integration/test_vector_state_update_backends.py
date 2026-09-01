from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


SOURCE = """
module VectorStateRTL {
  clock clk reset rst
  in fire:bit in index:u2 in value:u8
  out observation:bits<40>
  reg samples:vec<4,u8>=generate(i in 0..4) 0
  reg seen:u8=0
  rule write when fire {
    samples[index] <- value
    seen <- samples[index]
  }
  packed:bits<32>=pack(samples)
  observation=concat(packed,seen)
}
"""


HARNESS = r'''#include "VVectorStateRTL.h"
#include "verilated.h"
static void tick(VVectorStateRTL& d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
static bool state(VVectorStateRTL& d, unsigned values, unsigned previous) {
  d.eval();
  unsigned long long expected =
      (static_cast<unsigned long long>(values) << 8) | previous;
  return d.observation == expected;
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VVectorStateRTL d;
  d.fire=0; d.index=0; d.value=0; d.rst=1; tick(d);
  if (!state(d, 0x00000000, 0)) return 1;
  d.rst=0; d.fire=1; d.index=2; d.value=11; tick(d);
  if (!state(d, 0x00000b00, 0)) return 2;
  d.index=0; d.value=0xaa; tick(d);
  if (!state(d, 0xaa000b00, 0)) return 3;
  d.fire=0; d.index=2; d.value=0; tick(d);
  if (!state(d, 0xaa000b00, 0)) return 4;
  d.fire=1; d.index=2; d.value=17; tick(d);
  if (!state(d, 0xaa001100, 11)) return 5;
  d.rst=1; d.fire=1; d.index=1; d.value=9; tick(d);
  if (!state(d, 0x00000000, 0)) return 6;
  d.rst=0; tick(d);
  return state(d, 0x00090000, 0) ? 0 : 7;
}
'''


def _simulate(files: tuple[Path, ...], tmp_path: Path, tag: str) -> None:
    harness = tmp_path / f"harness_{tag}.cpp"
    harness.write_text(HARNESS)
    object_directory = tmp_path / f"obj_{tag}"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--top-module", "VectorStateRTL", "--Mdir", str(object_directory),
            "-o", "vector_state_sim", *(str(path) for path in files), str(harness),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_directory / "vector_state_sim"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_vector_update_backend_artifacts_are_deterministic_and_round_trip() -> None:
    module = compile_source(SOURCE, include_clash=False).ir
    for emitter in (emit_sv_artifact, emit_clash_artifact):
        first = emitter(module)
        second = emitter(module)
        assert first.text == second.text
        assert first.artifact_hash == second.artifact_hash
        restored = BackendArtifact.from_json(first.to_json())
        assert restored.artifact_hash == first.artifact_hash
        assert restored.bindings == first.bindings
        bindings = {item.semantic_signal_id: item for item in first.bindings}
        assert bindings["port:observation"].width == 40


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_vector_update_strict_lint_and_cycle_behavior(tmp_path: Path) -> None:
    artifact = emit_sv_artifact(compile_source(SOURCE, include_clash=False).ir)
    assert "32'hff <<" in artifact.text
    assert artifact.text.count("32'hff <<") == 1
    assert "else samples <= samples;" in artifact.text
    rtl = tmp_path / "VectorStateRTL.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "VectorStateRTL")
    _simulate((rtl,), tmp_path, "sv")


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_clash_vector_update_strict_lint_and_cycle_behavior(tmp_path: Path) -> None:
    compilation = compile_source(SOURCE)
    assert "zlangUpdateVector" in compilation.clash
    assert "replace (0 :: Index 4)" in compilation.clash
    rtl = tuple(generate_verilog(
        compilation.clash,
        "VectorStateRTL",
        tmp_path / "clash",
        find_clash_executable(),
    ))
    assert rtl
    lint_with_verilator(rtl, "VectorStateRTL")
    _simulate(rtl, tmp_path, "clash")
