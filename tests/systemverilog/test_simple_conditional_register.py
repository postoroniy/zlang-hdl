# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Direct register branches preserve unified admission and four-state behavior."""

from dataclasses import replace
from itertools import product
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit, emit_artifact
from zlang.backend.systemverilog import state_planning
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode


SOURCE = """module Accumulator {
    clock clk
    async reset rst @clk { release externally_synchronized }
    in enable : bit
    in value : u8
    out reg total : u9 = 0
    when enable {
        total <- truncate<9>(total + value)
    } else {
        total <- 0
    }
}
"""

WRAPPER = """module Wrapper {
    clock clk
    async reset rst @clk { release externally_synchronized }
    in enable : bit
    in value : u8
    out total : u9
    child : Accumulator { enable value }
    total = child.total
}
"""


def test_hierarchical_conditional_register_uses_direct_branches():
    module = compile_source(SOURCE + WRAPPER, top="Wrapper").ir
    rtl = emit(module)
    assert "rule_when_00" not in rtl
    assert "zlang_condition_" not in rtl
    assert "if (enable)" in rtl
    assert "total <= total;" not in rtl


def test_isolated_conditional_register_uses_direct_branches(monkeypatch):
    module = compile_source(SOURCE).ir
    compact = emit_artifact(module)
    assert not state_planning.requires_unified_state(module)
    assert "rule_when_00" not in compact.text
    assert "zlang_condition_" not in compact.text
    assert "@(posedge clk or posedge rst)" in compact.text
    assert "if (enable)" in compact.text
    assert "total <= total;" not in compact.text
    with monkeypatch.context() as patch:
        patch.setattr(state_planning, "requires_unified_state", lambda _: True)
        unified = emit_artifact(module)
    assert "rule_when_00_fire" in unified.text
    assert compact.selected_ir_identity == unified.selected_ir_identity
    assert len(compact.bindings) == len(unified.bindings)
    for binding, previous in zip(compact.bindings, unified.bindings, strict=True):
        assert replace(binding, artifact_hash=previous.artifact_hash) == previous
    assert compact.physical_domains == unified.physical_domains
    assert compact.timing_contract == unified.timing_contract
    assert compact.artifact_hash != unified.artifact_hash


@pytest.mark.parametrize("variant", ("unreset", "two_resources", "two_groups", "default"))
def test_conditional_registers_requiring_admission_keep_unified_state(variant):
    source = SOURCE
    if variant == "unreset":
        source = source.replace("out reg total : u9 = 0", "out reg total : u9")
    else:
        source = source.replace("    when enable", "    out reg other : u9 = 0\n    when enable", 1)
        if variant == "two_resources":
            source = source.replace("total <- 0", "total <- 0\n        other <- 1")
        elif variant == "two_groups":
            source = source.replace("\n}\n", "\n    when enable { other <- 1 }\n}\n")
        else:
            source = source.replace("    when enable", "    other <- 1\n    when enable", 1)
    module = compile_source(source).ir
    assert state_planning.requires_unified_state(module)
    assert "rule_when_00_fire" in emit(module)


@pytest.mark.parametrize("complete_branch", (False, True))
def test_direct_register_matches_unified_rtl_for_unknown_controls(
    tmp_path, monkeypatch, complete_branch,
):
    source_text = SOURCE
    if not complete_branch:
        source_text = source_text.replace("        total <- 0\n", "")
    module = compile_source(source_text).ir
    actual = emit(module)
    assert "total <= total;" not in actual
    with monkeypatch.context() as patch:
        patch.setattr(state_planning, "requires_unified_state", lambda _: True)
        baseline = emit(module).replace("module Accumulator", "module Baseline", 1)
    steps = []
    for rst, enable, value in product(("0", "1", "x", "z"), ("0", "1", "x", "z"), ("ff", "xx", "zz")):
        steps.append(
            f"rst=1'b{rst}; enable=1'b{enable}; value=8'h{value}; "
            "#1; clk=1; #1; if (actual !== expected) $fatal(1, \"branch mismatch\"); "
            "clk=0; #1;"
        )
    testbench = """module tb;
reg clk=0, rst=1, enable=0;
reg [7:0] value=0;
wire [8:0] actual, expected;
Accumulator dut(clk,rst,enable,value,actual);
Baseline reference_dut(clk,rst,enable,value,expected);
initial begin
 #1; clk=1; #1; clk=0; #1;
""" + "\n".join(steps) + "\n$display(\"PASS\"); $finish; end\nendmodule\n"
    source = tmp_path / "dut.sv"
    source.write_text(actual + "\n" + baseline + "\n" + testbench)
    compiler, runner = shutil.which("iverilog"), shutil.which("vvp")
    assert compiler and runner
    binary = tmp_path / "simulation"
    subprocess.run([compiler, "-g2012", "-s", "tb", "-o", str(binary), str(source)],
                   check=True, capture_output=True, text=True, timeout=30)
    result = subprocess.run([runner, str(binary)], check=True, capture_output=True,
                            text=True, timeout=30)
    assert result.stdout.splitlines()[0] == "PASS"


@pytest.mark.parametrize("mutation", (False, True))
@pytest.mark.parametrize("hierarchical", (False, True))
def test_actual_rtl_accumulator_matches_independent_reference(tmp_path, mutation, hierarchical):
    top = "Wrapper" if hierarchical else "Accumulator"
    source = SOURCE + WRAPPER if hierarchical else SOURCE
    rtl = emit(compile_source(source, top=top).ir)
    if mutation:
        before, match, after = rtl.rpartition("total <= 9'd0;")
        assert match and "total <= 9'd0;" in before
        rtl = before + "total <= 9'd1;" + after
    harness = """module equivalence(input clk);
(* anyseq *) reg rst, enable;
(* anyseq *) reg [7:0] value;
wire [8:0] actual;
reg [8:0] expected;
Accumulator dut(clk,rst,enable,value,actual);
always @(posedge clk or posedge rst) begin
 if (rst) expected <= 0;
 else if (enable) expected <= expected + {1'b0,value};
 else expected <= 0;
end
reg started=0;
always @(posedge clk) begin
 if (!started) assume(rst);
 if (started) assert(actual == expected);
 started <= 1;
end
endmodule
"""
    harness = harness.replace("Accumulator dut(", f"{top} dut(")
    result = run_verilog_formal(
        rtl + "\n" + harness, top="equivalence", property_id="conditional_register",
        mode=ProofMode.PROVE, depth=8, timeout_seconds=30,
        systemverilog=True, flatten=True, work_directory=tmp_path / "proof",
    )
    assert result.status is (FormalStatus.FAILED if mutation else FormalStatus.PROVEN)
