from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.compiler import compile_source


VERILATOR = shutil.which("verilator")

SOURCE = """
module RuleMemory {
  clock clk reset rst
  in rd:bit in wr:bit in address:u2 in data:u8
  out q:u8
  memory table:mem<u8,4> { read_latency 1 collision write_first }
  rule fetch when rd { table.read(address) }
  rule store when wr { table.write(address,data) }
  q=table.read_data
}
"""

HARNESS = r"""
#include "VRuleMemory.h"
static void tick(VRuleMemory &d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VRuleMemory d;
  d.rd=0; d.wr=0; d.address=0; d.data=0; d.rst=1; tick(d); d.rst=0;
  d.wr=1; d.address=1; d.data=7; tick(d); if (d.q != 0) return 1;
  d.wr=0; d.rd=1; d.address=1; tick(d); if (d.q != 7) return 2;
  d.rd=0; d.address=0; tick(d); if (d.q != 7) return 3;
  d.rd=1; d.wr=1; d.address=1; d.data=9; tick(d); if (d.q != 9) return 4;
  d.rd=0; d.wr=0; tick(d); if (d.q != 9) return 5;
  d.rst=1; tick(d); if (d.q != 0) return 6;
  d.rst=0; d.rd=1; d.address=1; tick(d); return d.q == 0 ? 0 : 7;
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
            "verilator", "--cc", "--exe", "--build", "--top-module",
            "RuleMemory", "--Mdir", str(obj), "-o", "rule_memory_sim",
            *map(str, files), str(harness),
        ),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run(
        (str(obj / "rule_memory_sim"),),
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_rule_local_memory_direct_sv_lints_and_simulates(tmp_path: Path) -> None:
    module = compile_source(SOURCE).ir
    first = emit_experimental(module)
    assert emit_experimental(module) == first
    artifact = emit_artifact(module)
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.bindings == artifact.bindings
    assert any(
        item.semantic_signal_id == "port:q" and item.rtl_path == "q"
        for item in artifact.bindings
    )
    rtl = tmp_path / "RuleMemory.sv"
    rtl.write_text(first)
    subprocess.run(
        ("verilator", "--lint-only", "-Wall", "-Wno-fatal", str(rtl)),
        check=True, capture_output=True, text=True,
    )
    _simulate([rtl], tmp_path)
