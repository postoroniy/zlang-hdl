"""Regression tests for unified-state direct-SV expression materialization."""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import _inline_locals
from zlang.parser import parse
from zlang.semantic import analyze


SOURCE = """
struct Pair { left:u8 right:u8 }

module AggregateSelectState {
    clock clk
    reset rst
    in fire : bit
    in values : vec<4,Pair>
    in index : u2

    reg result : u8 = 0
    fifo storage : fifo<Pair,4>

    selected = values[index]

    rule step when fire {
        result <- selected.left
        storage.push(selected)
    }
}
"""


COMPOUND_RUNTIME_PREFIX_SOURCE = """
struct Frame {
    samples : vec<4,u8>
    tag : u3
}

module CompoundRuntimePrefix {
    in frame : Frame
    in index : u2
    out y : u8

    y = frame.samples[index]
}
"""


def _rtl() -> str:
    return emit_experimental(_inline_locals(analyze(parse(SOURCE))))


def _compound_runtime_prefix_rtl() -> str:
    return emit_experimental(
        _inline_locals(analyze(parse(COMPOUND_RUNTIME_PREFIX_SOURCE)))
    )


def test_unified_state_materializes_runtime_selected_struct_once() -> None:
    rtl = _rtl()

    # The aggregate vector selection is a single typed temporary.  Field
    # projection is then a legal slice of that temporary, never a second slice
    # applied to the dynamic indexed part-select expression.
    dynamic_selects = [line for line in rtl.splitlines() if "values[" in line]
    assert len(dynamic_selects) == 1
    match = re.search(r"assign (\w+) = values\[", dynamic_selects[0])
    assert match is not None
    temporary = match.group(1)
    assert f"{temporary}[15:8]" in rtl
    assert "+: 16][15:8]" not in rtl
    assert rtl.count(f"assign {temporary} =") == 1


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_unified_state_materialization_is_verilator_lint_clean(tmp_path) -> None:
    rtl = tmp_path / "AggregateSelectState.sv"
    rtl.write_text(_rtl())
    result = subprocess.run(
        (
            "verilator", "--lint-only", "-Wno-fatal", "--top-module",
            "AggregateSelectState", str(rtl),
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_dynamic_select_materializes_compound_prefix_once() -> None:
    first = _compound_runtime_prefix_rtl()
    second = _compound_runtime_prefix_rtl()

    assert first == second
    assert first.count("logic [31:0] zlang_expr_0;") == 1
    assert first.count("assign zlang_expr_0 = frame[34:3];") == 1
    assert (
        "assign y = zlang_expr_0[((32'd3 - 32'(index)) * 32'd8) +: 8];"
        in first
    )
    assert "frame[34:3][" not in first


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_dynamic_select_compound_prefix_is_bit_exact_in_verilator(tmp_path) -> None:
    rtl = tmp_path / "CompoundRuntimePrefix.sv"
    rtl.write_text(_compound_runtime_prefix_rtl())
    bench = tmp_path / "tb.sv"
    bench.write_text(
        """
module tb;
  logic [7:0] frame_samples [0:3];
  logic [2:0] frame_tag;
  logic [1:0] index;
  wire [7:0] y;
  CompoundRuntimePrefix dut (.*);
  initial begin
    frame_samples = '{8'h11, 8'h22, 8'h33, 8'h44};
    frame_tag = 3'b101;
    index = 0; #1; if (y !== 8'h11) $fatal(1, "index 0");
    index = 1; #1; if (y !== 8'h22) $fatal(1, "index 1");
    index = 2; #1; if (y !== 8'h33) $fatal(1, "index 2");
    index = 3; #1; if (y !== 8'h44) $fatal(1, "index 3");
    $finish;
  end
endmodule
"""
    )
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    build = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "--Mdir", str(obj), "-o", "sim", str(rtl), str(bench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    run = subprocess.run(
        (str(obj / "sim"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
