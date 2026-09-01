from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "indexed_instance_array.zl").read_text()


def test_direct_sv_reuses_specialization_and_emits_four_physical_instances() -> None:
    artifact = emit_artifact(
        compile_source(SOURCE, top="IndexedInstanceArray", include_clash=False).ir
    )
    text = artifact.text
    assert text.count("module ArrayLane__") == 1
    assert text.count("ArrayLane__") == 5  # definition plus four applications
    for index in range(4):
        assert f"zlang_instance_lane_{index}_" in text
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.manifest_version == artifact.manifest_version
    assert [item.semantic_signal_id for item in restored.bindings] == [
        item.semantic_signal_id for item in artifact.bindings
    ]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_indexed_array_behaves_under_verilator(tmp_path: Path) -> None:
    artifact = emit_artifact(
        compile_source(SOURCE, top="IndexedInstanceArray", include_clash=False).ir
    )
    rtl = tmp_path / "IndexedInstanceArray.sv"
    harness = tmp_path / "test.cpp"
    rtl.write_text(artifact.text)
    harness.write_text(r'''#include "VIndexedInstanceArray.h"
#include "verilated.h"
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VIndexedInstanceArray dut;
  dut.values[0] = 1;
  dut.values[1] = 2;
  dut.values[2] = 3;
  dut.values[3] = 4;
  dut.eval();
  return dut.y == 5 ? 0 : 1;
}
''')
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--Mdir", str(tmp_path / "obj"),
            "--top-module", "IndexedInstanceArray", str(rtl), str(harness),
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
