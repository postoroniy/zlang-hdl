from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from tests.semantic.test_stdlib_coherence import WITNESSES
from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.toolchain import find_clash_executable, generate_verilog


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="Clash or Verilator unavailable",
)
@pytest.mark.parametrize(
    "witness",
    (
        "fft_butterfly",
        "fixed_helpers",
        "stream_core",
        "serializer",
        "storage_queue",
        "storage_ping_pong",
    ),
)
def test_stdlib_family_witness_reaches_real_clash_and_verilator(
    witness: str,
    tmp_path: Path,
) -> None:
    result = compile_source(WITNESSES[witness], top="Top")
    files = generate_verilog(
        result.clash,
        "Top",
        tmp_path / witness,
        public_wrapper=ClashPublicTopWrapper.build(result.ir),
    )
    completed = subprocess.run(
        ("verilator", "--lint-only", "-Wall", "-Wno-fatal", *map(str, files)),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        completed.stdout,
        completed.stderr,
    )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_rv_register_slice_stalls_and_replaces_on_simultaneous_transfer(
    tmp_path: Path,
) -> None:
    result = compile_source(
        WITNESSES["stream_register_slice"],
        top="Top",
        include_clash=False,
    )
    rtl = tmp_path / "Top.sv"
    rtl.write_text(
        emit_sv_artifact(result.ir, selected_ir_identity="stdlib:rv-slice").text
    )
    bench = tmp_path / "tb.sv"
    bench.write_text(
        r"""
module tb;
  logic clk=0, rst=1;
  logic [7:0] input_payload=0;
  logic input_valid=0, output_ready=0;
  wire input_ready, output_valid;
  wire [7:0] output_payload;
  Top dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; #1; end endtask
  initial begin
    tick; rst=0;
    input_payload=8'h11; input_valid=1; output_ready=0;
    #1;
    if (!input_ready) $fatal(1,"empty slice did not accept");
    tick;
    if (!output_valid || output_payload != 8'h11)
      $fatal(1,"accepted payload not held");
    input_payload=8'h22; output_ready=1;
    #1;
    if (!input_ready) $fatal(1,"full simultaneous replacement blocked");
    tick;
    if (!output_valid || output_payload != 8'h22)
      $fatal(1,"replacement payload mismatch");
    input_valid=0; tick;
    if (output_valid) $fatal(1,"slice did not drain");
    $finish;
  end
endmodule
"""
    )
    completed = subprocess.run(
        (
            "verilator",
            "--binary",
            "--top-module",
            "tb",
            "-Wno-fatal",
            str(rtl),
            str(bench),
            "-Mdir",
            str(tmp_path / "obj"),
        ),
        env={**os.environ, "CCACHE_DISABLE": "1"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    run = subprocess.run(
        (str(tmp_path / "obj" / "Vtb"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_ping_pong_blocks_reordering_and_allows_atomic_retire_commit(
    tmp_path: Path,
) -> None:
    result = compile_source(
        WITNESSES["storage_ping_pong"],
        top="Top",
        include_clash=False,
    )
    rtl = tmp_path / "Top.sv"
    rtl.write_text(
        emit_sv_artifact(result.ir, selected_ir_identity="stdlib:ping-pong").text
    )
    bench = tmp_path / "tb.sv"
    bench.write_text(
        r"""
module tb;
  logic clk=0, rst=1, write=0, commit=0, retire=0;
  logic [1:0] write_index=0, read_index=0;
  logic [7:0] write_data=0;
  wire write_ready, commit_ready, read_valid;
  wire [7:0] read_data;
  Top dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; #1; end endtask
  initial begin
    tick; rst=0;
    write=1; write_index=0; write_data=8'h11; tick;
    write_index=1; write_data=8'h22; tick;
    write=0; commit=1;
    if (!commit_ready) $fatal(1,"first bank could not commit");
    tick; commit=0;
    read_index=0; #1;
    if (!read_valid || read_data != 8'h11)
      $fatal(1,"first bank was not published");

    write=1; write_index=0; write_data=8'h33; tick; write=0;
    commit=1; #1;
    if (commit_ready) $fatal(1,"second bank reordered before retire");
    tick;
    if (!read_valid || read_data != 8'h11)
      $fatal(1,"blocked commit changed visible bank");

    retire=1; #1;
    if (!commit_ready) $fatal(1,"retire did not open atomic commit");
    tick; retire=0; commit=0;
    if (!read_valid || read_data != 8'h33)
      $fatal(1,"atomic retire/commit lost the second bank");
    retire=1; tick; retire=0;
    if (read_valid) $fatal(1,"retired bank remained valid");
    $finish;
  end
endmodule
"""
    )
    completed = subprocess.run(
        (
            "verilator",
            "--binary",
            "--top-module",
            "tb",
            "-Wno-fatal",
            str(rtl),
            str(bench),
            "-Mdir",
            str(tmp_path / "obj_ping_pong"),
        ),
        env={**os.environ, "CCACHE_DISABLE": "1"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    run = subprocess.run(
        (str(tmp_path / "obj_ping_pong" / "Vtb"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
