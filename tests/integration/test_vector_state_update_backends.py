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


STRUCT_PROJECTED_SOURCE = """
struct VectorCursor { tag:u2 }

module StructProjectedVectorStateRTL {
  clock clk reset rst
  in fire:bit in next_tag:u2 in value:u8
  out observation:bits<40>
  reg cursor:VectorCursor=VectorCursor{tag=0}
  reg samples:vec<4,u8>=generate(i in 0..4) 0
  rule write when fire {
    samples[cursor.tag] <- value
    cursor <- cursor with { tag=next_tag }
  }
  snapshot:bits<32>=pack(samples)
  observation=concat(snapshot,samples[cursor.tag])
}
"""


STRUCT_PROJECTED_HARNESS = r'''#include "VStructProjectedVectorStateRTL.h"
#include "verilated.h"
static void tick(VStructProjectedVectorStateRTL& d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
static bool state(
    VStructProjectedVectorStateRTL& d,
    unsigned values,
    unsigned selected) {
  d.eval();
  unsigned long long expected =
      (static_cast<unsigned long long>(values) << 8) | selected;
  return d.observation == expected;
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VStructProjectedVectorStateRTL d;
  d.fire=0; d.next_tag=0; d.value=0; d.rst=1; tick(d);
  if (!state(d, 0x00000000, 0x00)) return 1;

  // The write uses the pre-edge cursor (0), then the cursor becomes 2.
  d.rst=0; d.fire=1; d.next_tag=2; d.value=0x11; tick(d);
  if (!state(d, 0x11000000, 0x00)) return 2;

  // The next write therefore targets element 2, independently of next_tag=1.
  d.next_tag=1; d.value=0x22; tick(d);
  if (!state(d, 0x11002200, 0x00)) return 3;

  d.fire=0; d.next_tag=0; d.value=0; tick(d);
  if (!state(d, 0x11002200, 0x00)) return 4;

  // Holding next_tag at 1 makes the newly written element directly observable.
  d.fire=1; d.next_tag=1; d.value=0x33; tick(d);
  if (!state(d, 0x11332200, 0x33)) return 5;

  d.next_tag=3; d.value=0x44; tick(d);
  if (!state(d, 0x11442200, 0x00)) return 6;
  d.next_tag=3; d.value=0x55; tick(d);
  if (!state(d, 0x11442255, 0x55)) return 7;

  d.rst=1; d.fire=1; d.next_tag=2; d.value=0xff; tick(d);
  if (!state(d, 0x00000000, 0x00)) return 8;

  // The first post-reset write again uses the reset cursor value (0).
  d.rst=0; d.fire=1; d.next_tag=0; d.value=0x66; tick(d);
  if (!state(d, 0x66000000, 0x66)) return 9;
  d.fire=0; tick(d);
  return state(d, 0x66000000, 0x66) ? 0 : 10;
}
'''


def _simulate(
    files: tuple[Path, ...],
    tmp_path: Path,
    tag: str,
    *,
    top_module: str = "VectorStateRTL",
    harness_source: str = HARNESS,
) -> None:
    harness = tmp_path / f"harness_{tag}.cpp"
    harness.write_text(harness_source)
    object_directory = tmp_path / f"obj_{tag}"
    binary_name = f"vector_state_{tag}_sim"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--top-module", top_module, "--Mdir", str(object_directory),
            "-o", binary_name, *(str(path) for path in files), str(harness),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_directory / binary_name),),
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


def test_struct_projected_vector_update_artifacts_are_deterministic() -> None:
    module = compile_source(STRUCT_PROJECTED_SOURCE, include_clash=False).ir
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
def test_direct_sv_struct_projected_vector_update_cycle_behavior(
    tmp_path: Path,
) -> None:
    artifact = emit_sv_artifact(
        compile_source(STRUCT_PROJECTED_SOURCE, include_clash=False).ir
    )
    rtl = tmp_path / "StructProjectedVectorStateRTL.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "StructProjectedVectorStateRTL")
    _simulate(
        (rtl,),
        tmp_path,
        "struct_sv",
        top_module="StructProjectedVectorStateRTL",
        harness_source=STRUCT_PROJECTED_HARNESS,
    )


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_clash_struct_projected_vector_update_cycle_behavior(tmp_path: Path) -> None:
    compilation = compile_source(STRUCT_PROJECTED_SOURCE)
    rtl = tuple(generate_verilog(
        compilation.clash,
        "StructProjectedVectorStateRTL",
        tmp_path / "struct_clash",
        find_clash_executable(),
    ))
    assert rtl
    lint_with_verilator(rtl, "StructProjectedVectorStateRTL")
    _simulate(
        rtl,
        tmp_path,
        "struct_clash",
        top_module="StructProjectedVectorStateRTL",
        harness_source=STRUCT_PROJECTED_HARNESS,
    )
