from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


SOURCE = """
enum WifiRate : bits<3> {
    Continue = 0
    Bpsk6 = 1
    Qpsk12 = 2
    Qam16_24 = 4
}

module EncodedEnumRTL {
    in raw : bits<3>
    out result : bits<4>

    rate : WifiRate =
        enum_decode<WifiRate>(raw, WifiRate.Continue)
    result = concat(enum_encode(rate), enum_valid<WifiRate>(raw))
}
"""


STATE_SOURCE = """
enum WifiRate : bits<3> {
    Continue = 0
    Bpsk6 = 1
    Qpsk12 = 2
    Qam16_24 = 4
}

module EncodedEnumStateRTL {
    clock clk
    reset rst
    in raw : bits<3>
    in load : bit
    out encoded : bits<3>

    reg rate : WifiRate = WifiRate.Continue
    when load {
        rate <- enum_decode<WifiRate>(raw, WifiRate.Continue)
    }
    encoded = enum_encode(rate)
}
"""


HARNESS = r'''
#include "VEncodedEnumRTL.h"
#include "verilated.h"

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VEncodedEnumRTL dut;
  const unsigned legal[] = {0, 1, 2, 4};
  for (unsigned raw = 0; raw < 8; ++raw) {
    dut.raw = raw;
    dut.eval();
    bool valid = false;
    for (unsigned code : legal) valid |= raw == code;
    const unsigned decoded = valid ? raw : 0;
    const unsigned expected = (decoded << 1) | unsigned(valid);
    if (dut.result != expected) return int(raw + 1);
  }
  return 0;
}
'''


def _verilate_and_run(tmp_path: Path, rtl: tuple[Path, ...], suffix: str) -> None:
    harness = tmp_path / f"encoded_enum_{suffix}.cpp"
    object_dir = tmp_path / f"obj_{suffix}"
    harness.write_text(HARNESS)
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
            "EncodedEnumRTL",
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
        (str(object_dir / "VEncodedEnumRTL"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_direct_sv_sparse_enum_artifact_is_deterministic() -> None:
    module = compile_source(SOURCE).ir
    first = emit_artifact(module)
    second = emit_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    restored = BackendArtifact.from_json(first.to_json())
    assert restored.artifact_hash == first.artifact_hash
    assert restored.bindings == first.bindings
    result = next(
        binding for binding in restored.bindings
        if binding.semantic_signal_id == "port:result"
    )
    assert (result.width, result.canonical_type) == (4, "bits<4>")


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_sparse_enum_is_bit_exact(tmp_path: Path) -> None:
    artifact = emit_artifact(compile_source(SOURCE).ir)
    rtl = tmp_path / "EncodedEnumRTL.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "EncodedEnumRTL")
    _verilate_and_run(tmp_path, (rtl,), "sv")




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_sparse_enum_register_path_lints(tmp_path: Path) -> None:
    artifact = emit_artifact(compile_source(STATE_SOURCE).ir)
    rtl = tmp_path / "EncodedEnumStateRTL.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "EncodedEnumStateRTL")
