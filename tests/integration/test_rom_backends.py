from __future__ import annotations

import hashlib
from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.cli import main
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


ROM_SOURCE = """
module RomTop {
    clock clk
    reset rst
    in address : u2
    out y : u8

    rom table : rom<u8,4> {
        read_latency 1
        init generate(i in 0..4) i
    }
    table.read_address = address
    y = table.read_data
}
"""

ROM_SCHEDULED_FIFO_SOURCE = """
module RomScheduledFifo {
    clock clk
    reset rst
    in address : u2
    in push : bit
    out front : u8
    out count : u2
    fifo q : fifo<u8,2>
    rom table : rom<u8,4> {
        read_latency 1
        init generate(i in 0..4) i
    }
    table.read_address = address
    rule enqueue when push { q.push(table.read_data) }
    front = q.front
    count = q.count
}
"""


ROM_HARNESS = r'''
#include "VRomTop.h"
#include "verilated.h"
static void tick(VRomTop& d) {
  d.clk = 0; d.eval();
  d.clk = 1; d.eval();
  d.clk = 0; d.eval();
}
int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  VRomTop d;
  d.address = 3; d.rst = 1; tick(d);
  if (d.y != 0) return 1;
  d.rst = 0; d.address = 1;
  d.eval(); if (d.y != 0) return 2;
  tick(d); if (d.y != 1) return 3;
  d.address = 3;
  d.eval(); if (d.y != 1) return 4;
  tick(d); if (d.y != 3) return 5;
  d.address = 2; d.rst = 1; tick(d);
  if (d.y != 0) return 6;
  d.rst = 0; tick(d);
  return d.y == 2 ? 0 : 7;
}
'''


def _verilate_and_run(rtl: tuple[Path, ...], root: Path) -> None:
    harness = root / "rom_test.cpp"
    harness.write_text(ROM_HARNESS)
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    command = [
        "verilator", "--cc", "--exe", "--build", "--top-module", "RomTop",
        "--Mdir", str(obj), "-o", "rom_sim",
    ]
    completed = subprocess.run(
        (*command, *(str(path) for path in rtl), str(harness)),
        cwd=root, env=environment, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(obj / "rom_sim"),), cwd=root, text=True, capture_output=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_cli_publishes_direct_sv_companion(tmp_path: Path) -> None:
    source = tmp_path / "rom.zhl"
    source.write_text(ROM_SOURCE)
    sv_path = tmp_path / "sv" / "RomTop.sv"
    manifest = tmp_path / "sv" / "RomTop.json"
    assert main([
        str(source), "--systemverilog", str(sv_path),
        "--implementation-manifest", str(manifest),
    ]) == 0

    cli_artifact = BackendArtifact.from_json(manifest.read_text())
    image = cli_artifact.companions[0]
    published = (sv_path.parent / image.logical_path).read_bytes()
    assert hashlib.sha256(published).hexdigest() == image.file_hash
    assert f'$readmemb("{image.logical_path}"' in sv_path.read_text()


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_rom_is_strict_verilator_clean(tmp_path: Path) -> None:
    artifact = emit_sv_artifact(compile_source(ROM_SOURCE).ir)
    source = tmp_path / "RomTop.sv"
    source.write_text(artifact.text)
    for companion in artifact.companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    lint_with_verilator((source,), "RomTop")
    _verilate_and_run((source,), tmp_path)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_rom_composes_with_scheduled_fifo_in_direct_sv(tmp_path: Path) -> None:
    artifact = emit_sv_artifact(
        compile_source(ROM_SCHEDULED_FIFO_SOURCE).ir
    )
    source = tmp_path / "RomScheduledFifo.sv"
    source.write_text(artifact.text)
    for companion in artifact.companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    assert "q_push_data" in artifact.text
    assert "table_read_data" in artifact.text
    lint_with_verilator((source,), "RomScheduledFifo")
