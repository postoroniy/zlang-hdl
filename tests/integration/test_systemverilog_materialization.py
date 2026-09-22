"""Regression tests for unified-state direct-SV expression materialization."""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from zlang.backend.expression_materialization import build_direct_sv_dag_plan
from zlang.backend.systemverilog import emit_experimental
from zlang.backend.systemverilog import emitter as sv_emitter
from zlang.compiler import _inline_locals
from zlang.ir import expressions as expr
from zlang.ir.types import UIntType, VecType
from zlang.parser import parse
from zlang.semantic import analyze
from tools.benchmark_frontend_scalability import mixer_source
from zlang.compiler import create_file_compilation_session


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
    dynamic_selects = [
        line for line in rtl.splitlines()
        if "zlang_expr_" in line and "zlang_packed_values[" in line
    ]
    assert len(dynamic_selects) == 1
    match = re.search(
        r"assign (\w+) = zlang_packed_values\[", dynamic_selects[0]
    )
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
    assert first.count(
        "assign zlang_expr_0 = zlang_packed_frame[34:3];"
    ) == 1
    assert (
        "assign y = zlang_expr_0[(32'(index) * 32'd8) +: 8];"
        in first
    )
    assert "zlang_packed_frame[34:3][" not in first


def test_runtime_select_from_compound_vector_avoids_postfix_part_select() -> None:
    byte = UIntType(8)
    vector = expr.Generate(
        "i",
        0,
        4,
        tuple(expr.Constant(value, byte) for value in range(4)),
        VecType(4, byte),
    )
    selected = expr.RuntimeIndex(
        vector,
        expr.InputRef("index", UIntType(2)),
        4,
        expr.ValueRange(0, 3),
        byte,
    )

    rendered = sv_emitter._expression(selected)

    assert "}[" not in rendered
    assert rendered.startswith("8'(($unsigned({")
    assert ">> ((32'(index) * 32'd8))" in rendered


def test_direct_sv_dag_plan_materializes_one_shared_producer() -> None:
    type8 = UIntType(8)
    type16 = UIntType(16)
    shared = expr.Binary(
        expr.BinaryOperator.MULTIPLY,
        expr.InputRef("a", type8),
        expr.InputRef("b", type8),
        type8,
        type16,
    )
    root = expr.Add(
        shared,
        shared,
        UIntType(17),
    )

    plan = build_direct_sv_dag_plan((root,), minimum_shared_size=2)
    shared_node = next(node for node in plan.nodes if node.expression is shared)

    assert shared_node.fanout == 2
    assert shared_node.temporary == "zlang_expr_0"
    assert tuple(item.expression for item in plan.materialized).count(shared) == 1


def test_shared_mixer_dag_emits_linearly_without_logical_path_expansion(
    tmp_path,
) -> None:
    source = tmp_path / "shared_mixer.zhl"
    source.write_text(mixer_source(64))
    module = create_file_compilation_session(
        source, top="FrontendSharedDagScalability"
    ).planning.module

    rtl = emit_experimental(module)

    assert len(rtl.encode("utf-8")) < 16_384
    assert max(len(line.encode("utf-8")) for line in rtl.splitlines()) < 256
    assert rtl.count("assign zlang_expr_") >= 60


@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys unavailable")
def test_runtime_select_from_compound_vector_is_yosys_accepted(tmp_path) -> None:
    byte = UIntType(8)
    vector = expr.Generate(
        "i",
        0,
        4,
        tuple(expr.Constant(value, byte) for value in range(4)),
        VecType(4, byte),
    )
    selected = expr.RuntimeIndex(
        vector,
        expr.InputRef("index", UIntType(2)),
        4,
        expr.ValueRange(0, 3),
        byte,
    )
    rtl = tmp_path / "CompoundSelect.sv"
    rtl.write_text(
        "module CompoundSelect(input logic [1:0] index, output logic [7:0] y);\n"
        f"  assign y = {sv_emitter._expression(selected)};\n"
        "endmodule\n"
    )

    result = subprocess.run(
        (
            "yosys",
            "-q",
            "-p",
            "read_verilog -sv " + str(rtl)
            + "; hierarchy -check -top CompoundSelect; proc; check",
        ),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_large_vector_literal_is_split_before_frontend_token_limits() -> None:
    bit = UIntType(1)
    generated = expr.Generate(
        "i",
        0,
        5000,
        tuple(expr.Constant(index & 1, bit) for index in range(5000)),
        VecType(5000, bit),
    )

    rendered = sv_emitter._expression(generated)

    assert "\n" in rendered
    assert max(map(len, rendered.splitlines())) < 16_384


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_dynamic_select_compound_prefix_is_bit_exact_in_verilator(tmp_path) -> None:
    rtl = tmp_path / "CompoundRuntimePrefix.sv"
    rtl.write_text(_compound_runtime_prefix_rtl())
    bench = tmp_path / "tb.sv"
    bench.write_text(
        """
module tb;
  logic [3:0][7:0] frame_samples;
  logic [2:0] frame_tag;
  logic [1:0] index;
  wire [7:0] y;
  CompoundRuntimePrefix dut (.*);
  initial begin
    frame_samples[0] = 8'h11;
    frame_samples[1] = 8'h22;
    frame_samples[2] = 8'h33;
    frame_samples[3] = 8'h44;
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
