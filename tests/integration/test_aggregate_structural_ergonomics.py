"""Backend witnesses for the concise aggregate and structural surface.

These fixtures deliberately keep one physical output per top so the legacy
simple Clash emitter remains an independent witness for the already-normalized
typed IR.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.simulate import simulate, simulate_cycles
from zlang.toolchain import generate_verilog, lint_with_verilator


AGGREGATE_SOURCE = """
struct Beat { data:u8 last:bit }

module AggregateRTL {
    in a : Beat
    in b : Beat
    out y : bits<59>

    literal : vec<3,u8> = [1, 2, 3]
    repeated : vec<3,u8> = repeat(a.data)
    updated = a with { last = 1 }
    same = updated == b
    different = updated != b

    y = concat(
        pack(literal),
        pack(repeated),
        pack(updated),
        same,
        different
    )
}
"""


FSM_SOURCE = """
enum Phase { Idle Run }

module QualifiedFsmRTL {
    clock clk reset rst
    in toggle : bit
    out phase : Phase

    fsm state = Phase.Idle {
        Idle { when toggle -> Run {} }
        Run { when toggle -> Idle {} }
    }

    phase = state
}
"""


CHAIN_SOURCE = """
interface PipeIfc { clock clk reset rst in rx:rv<u8> out tx:rv<u8> }

module ChainStage : PipeIfc {
    tx.payload = rx.payload
    tx.valid = rx.valid
    rx.ready = tx.ready
}

module ChainRTL {
    clock clk reset rst
    in input : rv<u8>
    out output : rv<u8>
    first : ChainStage
    second : ChainStage
    input -> first -> second -> output
}
"""


CHAIN_HARNESS = r'''
#include "VChainRTL.h"
#include "verilated.h"

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VChainRTL dut;
  dut.clk = 0;
  dut.rst = 0;
  dut.input_payload = 0xa5;
  dut.input_valid = 1;
  dut.output_ready = 0;
  dut.eval();
  if (dut.output_payload != 0xa5 || !dut.output_valid) return 1;
  if (dut.input_ready) return 2;
  dut.output_ready = 1;
  dut.eval();
  if (!dut.input_ready) return 3;
  return 0;
}
'''


AGGREGATE_HARNESS = r'''
#include "VAggregateRTL.h"
#include "verilated.h"
#include <cstdint>

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VAggregateRTL dut;

  dut.a_data = 0x5a;
  dut.a_last = 0;
  dut.b_data = 0x5a;
  dut.b_last = 1;
  dut.eval();
  if (uint64_t(dut.y) != 0x08101ad2d2d2d6ULL) return 1;

  dut.a_data = 0xa5;
  dut.a_last = 1;
  dut.b_data = 0x5b;
  dut.b_last = 1;
  dut.eval();
  if (uint64_t(dut.y) != 0x08101d2d2d2d2dULL) return 2;

  return 0;
}
'''


FSM_HARNESS = r'''
#include "VQualifiedFsmRTL.h"
#include "verilated.h"

static void tick(VQualifiedFsmRTL& dut) {
  dut.clk = 0; dut.eval();
  dut.clk = 1; dut.eval();
  dut.clk = 0; dut.eval();
}

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VQualifiedFsmRTL dut;

  dut.toggle = 0;
  dut.rst = 1;
  tick(dut);
  if (dut.phase != 0) return 1;

  dut.rst = 0;
  tick(dut);
  if (dut.phase != 0) return 2;

  dut.toggle = 1;
  tick(dut);
  if (dut.phase != 1) return 3;

  dut.toggle = 0;
  tick(dut);
  if (dut.phase != 1) return 4;

  dut.toggle = 1;
  tick(dut);
  if (dut.phase != 0) return 5;

  dut.rst = 1;
  tick(dut);
  return dut.phase == 0 ? 0 : 6;
}
'''


def _verilate_and_run(
    tmp_path: Path,
    rtl: tuple[Path, ...],
    *,
    top: str,
    suffix: str,
    harness_text: str,
) -> None:
    harness = tmp_path / f"{top}_{suffix}.cpp"
    object_dir = tmp_path / f"obj_{top}_{suffix}"
    harness.write_text(harness_text)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--Mdir",
            str(object_dir),
            "--top-module",
            top,
            *(str(path) for path in rtl),
            str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / f"V{top}"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_aggregate_surface_is_bit_exact_in_the_semantic_simulator() -> None:
    module = compile_source(AGGREGATE_SOURCE, include_clash=False).ir
    assert simulate(
        module,
        a={"data": 0x5A, "last": 0},
        b={"data": 0x5A, "last": 1},
    ) == {"y": 0x08101AD2D2D2D6}
    assert simulate(
        module,
        a={"data": 0xA5, "last": 1},
        b={"data": 0x5B, "last": 1},
    ) == {"y": 0x08101D2D2D2D2D}


def test_qualified_fsm_and_compact_clock_reset_simulate_cycle_exactly() -> None:
    module = compile_source(FSM_SOURCE, include_clash=False).ir
    trace = simulate_cycles(
        module,
        [
            {"toggle": 0},
            {"toggle": 0},
            {"toggle": 1},
            {"toggle": 0},
            {"toggle": 1},
            {"toggle": 0},
        ],
        reset=[True, False, False, False, False, True],
    )
    assert [cycle["phase"] for cycle in trace] == [0, 0, 0, 1, 1, 0]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize(
    ("source", "top", "harness"),
    (
        (AGGREGATE_SOURCE, "AggregateRTL", AGGREGATE_HARNESS),
        (FSM_SOURCE, "QualifiedFsmRTL", FSM_HARNESS),
        (CHAIN_SOURCE, "ChainRTL", CHAIN_HARNESS),
    ),
)
def test_direct_sv_surface_is_deterministic_strict_lint_clean_and_bit_exact(
    tmp_path: Path,
    source: str,
    top: str,
    harness: str,
) -> None:
    module = compile_source(source, top=top, include_clash=False).ir
    first = emit_artifact(module)
    second = emit_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    restored = BackendArtifact.from_json(first.to_json())
    assert restored.artifact_hash == first.artifact_hash
    assert restored.bindings == first.bindings

    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(first.text)
    lint_with_verilator((rtl,), top)
    _verilate_and_run(
        tmp_path,
        (rtl,),
        top=top,
        suffix="direct_sv",
        harness_text=harness,
    )


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(
    ("source", "top", "harness"),
    (
        (AGGREGATE_SOURCE, "AggregateRTL", AGGREGATE_HARNESS),
        (FSM_SOURCE, "QualifiedFsmRTL", FSM_HARNESS),
        (CHAIN_SOURCE, "ChainRTL", CHAIN_HARNESS),
    ),
)
def test_real_clash_surface_is_strict_lint_clean_and_bit_exact(
    tmp_path: Path,
    source: str,
    top: str,
    harness: str,
) -> None:
    compilation = compile_source(source, top=top)
    rtl = generate_verilog(
        compilation.clash,
        top,
        tmp_path / f"{top}_clash_rtl",
        CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, top)
    _verilate_and_run(
        tmp_path,
        tuple(rtl),
        top=top,
        suffix="clash",
        harness_text=harness,
    )
