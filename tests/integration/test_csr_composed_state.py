"""CSR storage composed with ordinary resolved register/rule state."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.compiler import compile_source
from zlang.opt import lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate_csr_cycles
from zlang.toolchain import find_clash_executable, generate_verilog


VERILATOR = shutil.which("verilator")
CLASH = find_clash_executable()


SOURCE = """
module CsrCounter {
  clock clk
  reset rst
  in tick:bit
  out count:u8
  out command:bit

  reg counter:u8=0
  rule increment when tick {
    counter <- truncate<8>(counter + 1)
  }
  count=counter

  csr control @0 {
    CONTROL @0 {
      enable bit @0 rw = 0
      start bit @1 pulse -> command
      reserved bits<30> @31:2 reserved
    }
  }
}
"""


def _cycle(*, tick: int = 0, addr: int = 0, write: int = 0,
           wdata: int = 0, read: int = 0) -> dict[str, int]:
    return {
        "tick": tick,
        "addr": addr,
        "write": write,
        "wdata": wdata,
        "read": read,
    }


HARNESS = r"""
#include "VCsrCounter.h"
static void tick(VCsrCounter &d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VCsrCounter d;
  d.tick=0; d.addr=0; d.write=0; d.wdata=0; d.read=0;
  d.rst=1; tick(d);
  if (d.count != 0 || d.command != 0) return 1;
  d.rst=0; d.tick=1; tick(d);
  if (d.count != 1 || d.command != 0) return 2;
  d.write=1; d.wdata=3; tick(d);
  if (d.count != 2 || d.command != 1 || d.ready != 1) return 3;
  d.tick=0; d.write=0; d.wdata=0; tick(d);
  if (d.count != 2 || d.command != 0) return 4;
  d.read=1; d.eval();
  if (d.rdata != 1 || d.ready != 1) return 5;
  d.read=0; d.rst=1; tick(d);
  if (d.count != 0 || d.command != 0) return 6;
  return 0;
}
"""


def _simulate(files: tuple[Path, ...] | list[Path], root: Path) -> None:
    harness = root / "harness.cpp"
    harness.write_text(HARNESS)
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "--top-module", "CsrCounter", "--Mdir", str(obj),
            "-o", "csr_counter_sim", *map(str, files), str(harness),
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        (str(obj / "csr_counter_sim"),),
        check=True,
        capture_output=True,
        text=True,
    )


def test_csr_and_user_state_round_trip_and_simulate_from_one_snapshot() -> None:
    module = compile_source(SOURCE, include_clash=False).ir
    assert restore(lower(module)) == module
    assert module.resolved_transition is not None
    assert [item.name for item in module.resolved_transition.resources] == [
        "counter"
    ]
    results = simulate_csr_cycles(
        module,
        [
            _cycle(),
            _cycle(tick=1),
            _cycle(tick=1, write=1, wdata=3),
            _cycle(),
            _cycle(read=1),
        ],
        reset=[True, False, False, False, False],
    )
    assert [item["count"] for item in results] == [0, 0, 1, 2, 2]
    assert [item["command"] for item in results] == [0, 0, 0, 1, 0]
    assert results[-1]["rdata"] == 1
    assert results[-1]["ready"] == 1


def test_csr_composed_direct_artifact_is_deterministic_and_complete() -> None:
    module = compile_source(SOURCE, include_clash=False).ir
    text = emit_experimental(module)
    assert emit_experimental(module) == text
    assert "logic [7:0] counter;" in text
    assert "csr_control_control_start" in text
    assert "assign count = counter;" in text
    artifact = emit_artifact(module)
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.bindings == artifact.bindings
    assert {
        (item.semantic_signal_id, item.rtl_path)
        for item in artifact.bindings
        if item.semantic_signal_id in {"port:count", "port:command"}
    } == {("port:count", "count"), ("port:command", "command")}


def test_csr_command_and_rule_cannot_drive_the_same_output() -> None:
    source = """module BadCsrDriver {
      clock clk reset rst out command:bit reg armed:bit=0
      rule drive when armed { command <- 1 }
      csr control @0 { CONTROL @0 {
        start bit @0 pulse -> command
        reserved bits<31> @31:1 reserved
      } }
    }"""
    with pytest.raises(
        SemanticError,
        match="driven by both a CSR command binding and a rule action",
    ):
        compile_source(source, include_clash=False)


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_csr_composed_direct_sv_lints_and_simulates(tmp_path: Path) -> None:
    rtl = tmp_path / "CsrCounter.sv"
    rtl.write_text(
        emit_experimental(compile_source(SOURCE, include_clash=False).ir)
    )
    subprocess.run(
        (
            "verilator", "--lint-only", "-Wall", "-Wno-DECLFILENAME",
            "-Wno-UNUSEDSIGNAL",
            str(rtl),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    _simulate([rtl], tmp_path)


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
def test_csr_composed_clash_lints_and_simulates(tmp_path: Path) -> None:
    result = compile_source(SOURCE)
    assert "rule_increment_fire" in result.clash
    assert "csr_control_control_start" in result.clash
    files = generate_verilog(
        result.clash, "CsrCounter", tmp_path / "rtl", CLASH
    )
    subprocess.run(
        (
            "verilator", "--lint-only", "-Wall", "-Wno-DECLFILENAME",
            "-Wno-UNUSEDSIGNAL", "-Wno-PROCASSINIT",
            *map(str, files),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    _simulate(files, tmp_path)
