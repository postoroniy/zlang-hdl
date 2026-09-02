from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.cli import main
from zlang.compiler import compile_source
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


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


def _verilate_and_run(
    rtl: tuple[Path, ...], root: Path, *, clash_rom_index_waiver: bool = False
) -> None:
    harness = root / "rom_test.cpp"
    harness.write_text(ROM_HARNESS)
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    command = [
        "verilator", "--cc", "--exe", "--build", "--top-module", "RomTop",
        "--Mdir", str(obj), "-o", "rom_sim",
    ]
    if clash_rom_index_waiver:
        command.append("-Wno-WIDTHTRUNC")
    completed = subprocess.run(
        (*command, *(str(path) for path in rtl), str(harness)),
        cwd=root, env=environment, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(obj / "rom_sim"),), cwd=root, text=True, capture_output=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_cli_publishes_identical_backend_companions(tmp_path: Path) -> None:
    source = tmp_path / "rom.zhl"
    source.write_text(ROM_SOURCE)
    clash_path = tmp_path / "clash" / "RomTop.hs"
    sv_path = tmp_path / "sv" / "RomTop.sv"
    manifest = tmp_path / "sv" / "RomTop.json"
    assert main([
        str(source), "-o", str(clash_path), "--systemverilog", str(sv_path),
        "--implementation-manifest", str(manifest),
    ]) == 0

    cli_artifact = BackendArtifact.from_json(manifest.read_text())
    image = cli_artifact.companions[0]
    assert (clash_path.parent / image.logical_path).read_bytes() == (
        sv_path.parent / image.logical_path
    ).read_bytes()
    assert f'$readmemb("{image.logical_path}"' in sv_path.read_text()
    assert image.logical_path in clash_path.read_text()


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_rom_is_strict_verilator_clean(tmp_path: Path) -> None:
    artifact = emit_sv_artifact(compile_source(ROM_SOURCE, include_clash=False).ir)
    source = tmp_path / "RomTop.sv"
    source.write_text(artifact.text)
    for companion in artifact.companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    lint_with_verilator((source,), "RomTop")
    _verilate_and_run((source,), tmp_path)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_rom_composes_with_scheduled_fifo_in_direct_sv(tmp_path: Path) -> None:
    artifact = emit_sv_artifact(
        compile_source(ROM_SCHEDULED_FIFO_SOURCE, include_clash=False).ir
    )
    source = tmp_path / "RomScheduledFifo.sv"
    source.write_text(artifact.text)
    for companion in artifact.companions:
        (tmp_path / companion.logical_path).write_text(companion.text)
    assert "q_push_data" in artifact.text
    assert "table_read_data" in artifact.text
    lint_with_verilator((source,), "RomScheduledFifo")


@pytest.mark.skipif(find_clash_executable() is None, reason="Clash unavailable")
def test_real_clash_uses_staged_rom_image_and_verilator(tmp_path: Path) -> None:
    artifact = emit_clash_artifact(compile_source(ROM_SOURCE).ir)
    rtl = generate_verilog(
        artifact.text,
        "RomTop",
        tmp_path / "rtl",
        companions=artifact.companions,
    )
    assert (tmp_path / "rtl" / artifact.companions[0].logical_path).is_file()
    # Clash 1.11's upstream romFile primitive widens Enum addresses to host
    # Int in emitted RTL.  Verilator 5.044 reports that safe array-index cast
    # as WIDTHTRUNC; keep every other warning fatal while acknowledging only
    # this compiler-owned primitive shape.
    if verilator := shutil.which("verilator"):
        completed = subprocess.run(
            [verilator, "--lint-only", "-Wno-WIDTHTRUNC", "--top-module", "RomTop",
             *(str(path) for path in rtl)],
            text=True, capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        # The generated RTL opens the same logical image at runtime.  Publish a
        # copy beside the simulation executable while retaining the toolchain's
        # required copy in the persistent RTL output bundle.
        for companion in artifact.companions:
            (tmp_path / companion.logical_path).write_text(companion.text)
        _verilate_and_run(rtl, tmp_path, clash_rom_index_waiver=True)


@pytest.mark.skipif(find_clash_executable() is None, reason="Clash unavailable")
def test_rom_composes_with_scheduled_fifo_in_real_clash(tmp_path: Path) -> None:
    artifact = emit_clash_artifact(compile_source(ROM_SCHEDULED_FIFO_SOURCE).ir)
    assert "q_data_request" in artifact.text
    assert "table_read_data" in artifact.text
    rtl = generate_verilog(
        artifact.text, "RomScheduledFifo", tmp_path / "rtl",
        companions=artifact.companions,
    )
    assert rtl
