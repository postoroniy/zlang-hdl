from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source


SOURCE = (
    "module RuntimePackedSelection { "
    "in raw:bits<8> in bit_index:u3 in offset:u2 "
    "out selected:bit out window:bits<3> "
    "selected=raw[bit_index] window=raw[offset +: 3] }"
)


def test_direct_sv_runtime_packed_selection_uses_sized_shift_and_truncate() -> None:
    module = compile_source(SOURCE).ir
    text = emit(module)

    assert "assign selected =" in text
    assert "$unsigned((raw)) >>" in text
    assert "bit_index" in text
    assert "assign window = 3'(($unsigned((raw)) >>" in text
    assert "runtime_packed" not in text


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_runtime_packed_selection_matches_lsb_reference(tmp_path: Path) -> None:
    artifact = emit(compile_source(SOURCE).ir)
    rtl = tmp_path / "RuntimePackedSelection.sv"
    harness = tmp_path / "test.cpp"
    rtl.write_text(artifact)
    harness.write_text(r'''#include "VRuntimePackedSelection.h"
#include "verilated.h"
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VRuntimePackedSelection dut;
  for (unsigned raw : {0u, 1u, 0x96u, 0xd6u, 0xffu}) {
    dut.raw = raw;
    for (unsigned bit_index = 0; bit_index < 8; ++bit_index) {
      dut.bit_index = bit_index;
      for (unsigned offset = 0; offset < 4; ++offset) {
        dut.offset = offset;
        dut.eval();
        if (dut.selected != ((raw >> bit_index) & 1u)) return 1;
        if (dut.window != ((raw >> offset) & 7u)) return 2;
      }
    }
  }
  return 0;
}
''')
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--Mdir", str(tmp_path / "obj"),
            "--top-module", "RuntimePackedSelection", str(rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    run = subprocess.run(
        (str(tmp_path / "obj" / "VRuntimePackedSelection"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
