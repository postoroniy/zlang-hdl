from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest

from zlang.compiler import compile_source
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "indexed_instance_array.zhl").read_text()


@pytest.mark.skipif(find_clash_executable() is None, reason="Clash unavailable")
def test_clash_instance_array_generates_and_lints_real_rtl(tmp_path: Path) -> None:
    result = compile_source(SOURCE, top="IndexedInstanceArray")
    assert result.clash.count("lane ::") == 0
    assert result.clash.count("arrayLane ::") == 1
    for index in range(4):
        assert f"zlang_instance_lane_{index}_" in result.clash
    files = generate_verilog(
        result.clash,
        "IndexedInstanceArray",
        tmp_path / "rtl",
        find_clash_executable(),
    )
    lint_with_verilator(files, "IndexedInstanceArray")
    harness = tmp_path / "test.cpp"
    harness.write_text(r'''#include "VIndexedInstanceArray.h"
#include "verilated.h"
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VIndexedInstanceArray dut;
  dut.values = 0x01020304u;
  dut.eval();
  return dut.y == 5 ? 0 : 1;
}
''')
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "--Mdir", str(tmp_path / "obj"),
            "--top-module", "IndexedInstanceArray",
            *(str(path) for path in files),
            str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    run = subprocess.run(
        (str(tmp_path / "obj" / "VIndexedInstanceArray"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
