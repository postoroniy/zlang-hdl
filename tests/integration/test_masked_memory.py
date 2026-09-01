from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.compiler import compile_source
from zlang.toolchain import find_clash_executable, generate_verilog


VERILATOR = shutil.which("verilator")
CLASH = find_clash_executable()

SOURCE = """
module MaskedScheduledMemory {
  clock clk reset rst
  in op:u2 in address:u2 in data:u16 in mask:bits<2>
  out q:u16
  memory table:mem<u16,4> { read_latency 1 collision write_first }
  rule full when op == 1 { table.write(address,data) }
  rule masked_collision when op == 2 {
    table.read(address)
    table.write(address,data,mask)
  }
  priority full > masked_collision
  q=table.read_data
}
"""

HARNESS = r"""
#include "VMaskedScheduledMemory.h"
static void tick(VMaskedScheduledMemory &d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VMaskedScheduledMemory d;
  d.op=0; d.address=1; d.data=0; d.mask=0; d.rst=1; tick(d); d.rst=0;
  d.op=1; d.data=0x1234; tick(d); if (d.q != 0) return 1;
  d.op=2; d.data=0xabcd; d.mask=1; tick(d); if (d.q != 0x12cd) return 2;
  d.op=2; d.data=0xee00; d.mask=2; tick(d); if (d.q != 0xeecd) return 3;
  d.op=2; d.data=0; d.mask=0; tick(d); if (d.q != 0xeecd) return 4;
  d.rst=1; tick(d); if (d.q != 0) return 5;
  return 0;
}
"""

GLOBAL_SOURCE = """
module MaskedGlobalMemory {
  clock clk reset rst
  in address:u2 in write_enable:bit in data:u16 in mask:bits<2>
  out q:u16
  memory table:mem<u16,4> { read_latency 1 collision write_first }
  table.read_address=address
  table.write_enable=write_enable
  table.write_address=address
  table.write_data=data
  table.write_mask=mask
  q=table.read_data
}
"""

GLOBAL_HARNESS = r"""
#include "VMaskedGlobalMemory.h"
static void tick(VMaskedGlobalMemory &d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VMaskedGlobalMemory d;
  d.address=1; d.write_enable=0; d.data=0; d.mask=0; d.rst=1; tick(d); d.rst=0;
  d.write_enable=1; d.data=0x1234; d.mask=3; tick(d); if (d.q != 0x1234) return 1;
  d.data=0xabcd; d.mask=1; tick(d); if (d.q != 0x12cd) return 2;
  d.data=0xee00; d.mask=2; tick(d); if (d.q != 0xeecd) return 3;
  d.data=0; d.mask=0; tick(d); if (d.q != 0xeecd) return 4;
  d.rst=1; tick(d); if (d.q != 0) return 5;
  return 0;
}
"""


def _simulate(
    files: list[Path] | tuple[Path, ...],
    root: Path,
    *,
    top: str = "MaskedScheduledMemory",
    harness_text: str = HARNESS,
) -> None:
    harness = root / "harness.cpp"
    harness.write_text(harness_text)
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--top-module",
            top, "--Mdir", str(obj), "-o", "masked_sim",
            *map(str, files), str(harness),
        ),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run(
        (str(obj / "masked_sim"),),
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_masked_memory_direct_sv_is_deterministic_and_simulates(
    tmp_path: Path,
) -> None:
    module = compile_source(SOURCE).ir
    emitted = emit_experimental(module)
    assert emit_experimental(module) == emitted
    assert "table_write_mask_expanded" in emitted
    assert "table_write_merged" in emitted
    artifact = emit_artifact(module)
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.bindings == artifact.bindings
    assert any(
        binding.semantic_signal_id == "port:mask"
        for binding in artifact.bindings
    )
    rtl = tmp_path / "MaskedScheduledMemory.sv"
    rtl.write_text(emitted)
    subprocess.run(
        ("verilator", "--lint-only", "-Wall", str(rtl)),
        check=True, capture_output=True, text=True,
    )
    _simulate((rtl,), tmp_path)


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
def test_masked_memory_real_clash_is_cycle_identical(tmp_path: Path) -> None:
    result = compile_source(SOURCE)
    assert "table_write_merged" in result.clash
    files = generate_verilog(
        result.clash, "MaskedScheduledMemory", tmp_path / "rtl", CLASH
    )
    _simulate(files, tmp_path)


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
def test_global_masked_memory_direct_sv_and_clash_are_cycle_identical(
    tmp_path: Path,
) -> None:
    result = compile_source(GLOBAL_SOURCE)
    direct = tmp_path / "direct"
    direct.mkdir()
    direct_rtl = direct / "MaskedGlobalMemory.sv"
    direct_rtl.write_text(emit_experimental(result.ir))
    _simulate(
        (direct_rtl,), direct, top="MaskedGlobalMemory",
        harness_text=GLOBAL_HARNESS,
    )

    clash = tmp_path / "clash"
    clash.mkdir()
    files = generate_verilog(
        result.clash, "MaskedGlobalMemory", clash / "rtl", CLASH
    )
    _simulate(
        files, clash, top="MaskedGlobalMemory", harness_text=GLOBAL_HARNESS,
    )
