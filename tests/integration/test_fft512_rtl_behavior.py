"""Complete direct-SystemVerilog RTL behavior check for the FFT512 reference."""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tests.integration.test_fft512_sdf_reference import (
    OUTPUT_DIGEST,
    _canonical_digest,
    _fixture,
    _staged_oracle,
)
from zlang.backend.companions import publish_companion_bundle
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples" / "fft" / "sdf_stage_numeric.zhl"
TOP = "FFT512SDFReference"


@lru_cache(maxsize=1)
def _module():
    return compile_file(SOURCE, top=TOP).ir


def _bench(*, direct: bool) -> str:
    connection = (
        ".clk(clk), .rst(rst), "
        ".input_payload_re(in_payload_re), .input_payload_im(in_payload_im), "
        ".input_valid(in_valid), .input_ready(in_ready), "
        ".output_payload_re(out_payload_re), "
        ".output_payload_im(out_payload_im), "
        ".output_valid(out_valid), .output_ready(out_ready)"
    )
    fixture = "\n".join(
        f"  send(18'h{real & 0x3FFFF:05x}, 18'h{imag & 0x3FFFF:05x});"
        for real, imag in _fixture()
    )
    return f"""module tb;
logic clk = 0;
always #5 clk = ~clk;
logic rst;
logic signed [17:0] in_payload_re;
logic signed [17:0] in_payload_im;
logic in_valid;
wire in_ready;
wire signed [17:0] out_payload_re;
wire signed [17:0] out_payload_im;
wire out_valid;
logic out_ready;
integer cycle;
integer sample;
logic accepted;
integer stall_start;

{TOP} dut({connection});

task tick;
begin
  out_ready = !(cycle >= stall_start && cycle < stall_start + 5);
  #1;
  accepted = in_valid && in_ready;
  if (out_valid && !out_ready)
    $display("STALL %0d %0d %0d", cycle,
      $signed(out_payload_re), $signed(out_payload_im));
  if (out_valid && out_ready)
    $display("OUT %0d %0d %0d", cycle,
      $signed(out_payload_re), $signed(out_payload_im));
  @(posedge clk); #1;
  cycle = cycle + 1;
end
endtask

task send(input logic signed [17:0] payload_re,
          input logic signed [17:0] payload_im);
begin
  in_payload_re = payload_re;
  in_payload_im = payload_im;
  in_valid = 1;
  accepted = 0;
  while (!accepted)
    tick();
end
endtask

initial begin
  cycle = 0;
  rst = 1;
  in_valid = 0;
  in_payload_re = 0;
  in_payload_im = 0;
  out_ready = 1;
  stall_start = 1 << 28;
  tick();

  // Discard a partial pre-reset epoch before starting the checked frame.
  rst = 0;
  send(18'h00001, 18'h00002);
  send(18'h00003, 18'h00004);
  send(18'h00005, 18'h00006);
  send(18'h00007, 18'h00008);
  rst = 1;
  in_valid = 0;
  tick();

  rst = 0;
  // The first clean-epoch output is valid after 520 cycles. Hold it for five
  // cycles to exercise end-to-end backpressure and source-token retention.
  stall_start = cycle + 520;
{fixture}
  for (sample = 0; sample < 511; sample = sample + 1)
    send(0, 0);

  in_valid = 0;
  in_payload_re = 0;
  in_payload_im = 0;
  for (sample = 0; sample < 9; sample = sample + 1)
    tick();
  $finish;
end
endmodule
"""


def _run_rtl(
    rtl: Path | Sequence[Path],
    *,
    direct: bool,
    work: Path,
    lint_waivers: tuple[str, ...] = (),
) -> tuple[
    tuple[tuple[int, int, int], ...],
    tuple[tuple[int, int, int], ...],
]:
    rtl_sources = (
        (rtl,)
        if isinstance(rtl, Path)
        else tuple(sorted((Path(item) for item in rtl), key=lambda item: item.as_posix()))
    )
    assert rtl_sources, "Verilator requires at least one generated RTL source"
    rtl_working_directory = rtl_sources[0].parent
    rtl_arguments = tuple(str(item) for item in rtl_sources)
    work.mkdir(parents=True)
    bench = work / "tb.sv"
    bench.write_text(_bench(direct=direct))
    common_waivers = (
        "-Wno-DECLFILENAME",
        "-Wno-UNUSEDSIGNAL",
        "-Wno-UNUSEDPARAM",
        "-Wno-UNDRIVEN",
    )
    lint = subprocess.run(
        (
            "verilator",
            "--lint-only",
            "--timing",
            *common_waivers,
            *lint_waivers,
            "--top-module",
            TOP,
            *rtl_arguments,
        ),
        cwd=rtl_working_directory,
        capture_output=True,
        text=True,
    )
    assert lint.returncode == 0, lint.stderr

    output = work / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    build = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            *common_waivers,
            *lint_waivers,
            "--Mdir",
            str(output),
            "--top-module",
            "tb",
            *rtl_arguments,
            str(bench),
        ),
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        (str(output / "Vtb"),),
        cwd=rtl_working_directory,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
    output_pattern = re.compile(r"OUT (\d+) (-?\d+) (-?\d+)")
    stall_pattern = re.compile(r"STALL (\d+) (-?\d+) (-?\d+)")
    lines = run.stdout.splitlines()
    return (
        tuple(
            tuple(map(int, match.groups()))
            for line in lines
            if (match := output_pattern.fullmatch(line)) is not None
        ),
        tuple(
            tuple(map(int, match.groups()))
            for line in lines
            if (match := stall_pattern.fullmatch(line)) is not None
        ),
    )
