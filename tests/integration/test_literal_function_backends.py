from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.simulate import simulate
from zlang.toolchain import lint_with_verilator


SOURCE = """
fn exact_tag(high : bits<2>) {
    concat(high, zeros<2>, ones<2>)
}

module LiteralFunctionWitness {
    in high : bits<2>
    out negative : s8
    out clear : bits<8>
    out fill : bits<8>
    out tag : bits<6>

    negative = -123
    clear = zeros<8>
    fill = ones<8>
    tag = exact_tag(high)
}
"""


HARNESS = r'''
#include "VLiteralFunctionWitness.h"
#include "verilated.h"

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VLiteralFunctionWitness dut;
  for (unsigned high = 0; high < 4; ++high) {
    dut.high = high;
    dut.eval();
    if ((dut.negative & 0xffu) != 0x85u) return 1;
    if (dut.clear != 0u) return 2;
    if (dut.fill != 0xffu) return 3;
    if (dut.tag != ((high << 4) | 0x3u)) return 4;
  }
  return 0;
}
'''


def _run_verilator(tmp_path: Path, rtl: tuple[Path, ...], suffix: str) -> None:
    harness = tmp_path / f"literal_function_{suffix}.cpp"
    harness.write_text(HARNESS)
    obj = tmp_path / f"obj_{suffix}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(obj), "--top-module", "LiteralFunctionWitness",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(obj / "VLiteralFunctionWitness"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.fixture(scope="module")
def direct_compilation():
    return compile_source(SOURCE)


def test_exact_literals_constants_and_inferred_return_semantics_and_artifact(
    direct_compilation,
) -> None:
    compilation = direct_compilation
    assert simulate(compilation.ir, high=2) == {
        "negative": -123,
        "clear": 0,
        "fill": 0xFF,
        "tag": 0x23,
    }

    first = emit_sv_artifact(
        compilation.ir,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    second = emit_sv_artifact(
        compilation.ir,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    restored = BackendArtifact.from_json(first.to_json())
    assert restored.artifact_hash == first.artifact_hash
    assert restored.bindings == first.bindings


@pytest.mark.skipif(
    shutil.which("verilator") is None,
    reason="Verilator is required for direct-SystemVerilog execution",
)
def test_exact_literals_constants_and_inferred_return_direct_systemverilog(
    tmp_path: Path,
    direct_compilation,
) -> None:
    compilation = direct_compilation
    artifact = emit_sv_artifact(
        compilation.ir,
        selected_ir_identity=compilation.selected_ir_identity,
    )

    direct = tmp_path / "LiteralFunctionWitness.sv"
    direct.write_text(artifact.text)
    lint_with_verilator((direct,), compilation.ir.name)
    _run_verilator(tmp_path, (direct,), "direct")
