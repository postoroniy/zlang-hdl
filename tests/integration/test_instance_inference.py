# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Independent execution and actual-RTL controls for deferred input inference."""

from hashlib import sha256
import json
import re
import shutil
import subprocess

import pytest

from zlang import sim
from zlang.backend.systemverilog import emit_artifact
from zlang.common.subprocess import run_bounded_process, subprocess_text
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulation_compare import compare_program


SPELLINGS = ("sibling", "typed-local", "untyped-local", "single-anchor")


def _source(spelling):
    if spelling == "single-anchor":
        # Registered rule outputs cut current-cycle child dependencies.
        return """
module Anchor<W> {
    clock clk
    reset rst
    in x,anchor : bits<W>
    in enable : bit
    out reg y : bits<W> = 0
    when enable { y <- x ^ anchor }
}
module Cell<W> {
    clock clk
    reset rst
    in x : bits<W>
    in enable : bit
    out reg y : bits<W> = 0
    when enable { y <- x }
}
module Top {
    clock clk
    reset rst
    in sample : bits<10>
    in take,advance : bit
    out y,mid : bits<10>
    first : Anchor
    later : Cell
    first.x = later.y
    first.anchor = sample
    first.enable = take
    later.x = first.y
    later.enable = advance
    y = first.y
    mid = later.y
}
"""
    aliases = {
        "sibling": ("", "later.y"),
        "typed-local": ("late : bits<10> = later.y", "late"),
        "untyped-local": ("late = later.y", "late"),
    }
    alias, actual = aliases[spelling]
    return f"""
module Cell<W=8,P=W+2> where W >= 1 && P == W+2 {{
    clock clk
    reset rst
    in x : bits<P>
    in enable : bit
    out reg y : bits<P> = 0
    when enable {{ y <- x }}
}}
module Top {{
    clock clk
    reset rst
    in sample : bits<10>
    in take,advance : bit
    out y,mid : bits<10>
    first : Cell<W=8>
    later : Cell<W=8,P=10>
    {alias}
    first.x = {actual}
    first.enable = take
    later.x = sample
    later.enable = advance
    y = first.y
    mid = later.y
}}
"""


def _schedule(spelling):
    events, expected = [], []
    first, later = 0, 0
    for cycle in range(42):
        sample = (0, 1023, 512, 1, 1022)[cycle % 5] ^ ((cycle * 173) & 1023)
        values = {"sample": sample, "take": int(cycle % 3 != 1),
                  "advance": int(cycle % 4 != 2)}
        reset = cycle in (0, 17, 33)
        # Synchronous RTL state has no defined value before the first reset edge.
        edges_per_cycle = (["clk"],) if cycle == 0 else ([], ["clk"])
        for edges in edges_per_cycle:
            if edges:
                old_first, old_later = first, later
                first_input = old_later ^ sample if spelling == "single-anchor" else old_later
                later_input = old_first if spelling == "single-anchor" else sample
                first = 0 if reset else first_input if values["take"] else old_first
                later = 0 if reset else later_input if values["advance"] else old_later
            events.append({"set": values, "reset": {"rst": reset}, "edges": edges})
            expected.append({"y": first, "mid": later})
    return events, tuple(expected)


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("logic_state", (False, True))
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_deferred_input_inference_matches_independent_two_stage_execution(
    tmp_path, spelling, logic_state, simulator,
):
    path = tmp_path / "inference.zhl"
    path.write_text(_source(spelling))
    events, expected = _schedule(spelling)
    result = compare_program(
        sim.compile(path, top="Top", logic_state=logic_state), events,
        simulator=simulator, artifact_directory=tmp_path / "comparison",
        build_timeout=60, timeout=30,
    )
    assert result.native_trace == result.rtl_trace == expected
    # Check actual typed values too: no undefined tag may be hidden by a trace
    # conversion. This uses the common public/private value-boundary contract.
    typed = tuple({name: value.require_binary(name=name)
                   if isinstance(value, sim.LogicVector) else value
                   for name, value in row.items()} for row in result.native_outputs)
    assert typed == expected


def _replace_output(rtl, expression):
    result, count = re.subn(r"(?m)^(\s*assign y\s*=\s*)[^;]+;",
                            lambda match: match[1] + expression + ";", rtl)
    assert count == 1, "mutation must reach the one public output assignment"
    return result


def _formal(tmp_path, spelling, mutation=None):
    source = _source(spelling)
    compiled = compile_source(source, top="Top")
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    rtl = artifact.text
    if mutation == "wrong-reset":
        old, new = "y <= 10'd0;", "y <= 10'd1;"
        assert rtl.count(old) == 1, "mutation must reach the shared child reset"
        rtl = rtl.replace(old, new)
    elif mutation == "lost-stage":
        rtl = _replace_output(rtl, "mid")
    elif mutation == "wrong-connection":
        rtl = _replace_output(rtl, "sample")
    elif mutation == "wrong-output-bit":
        result, count = re.subn(r"(?m)^(\s*assign y\s*=\s*)([^;]+);",
                                lambda match: match[1] + "(" + match[2] + ") ^ 10'd1;", rtl)
        assert count == 1, "mutation must reach the one public output assignment"
        rtl = result
    if mutation is not None:
        assert rtl != artifact.text
    reference_first_input = "reference_later ^ sample" if spelling == "single-anchor" else "reference_later"
    reference_later_input = "reference_first" if spelling == "single-anchor" else "sample"
    harness = f"""
module contract(input clk,rst,take,advance,input [9:0] sample);
    wire [9:0] y,mid;
    {artifact.module} dut(.*);
    reg [9:0] reference_first,reference_later;
    always @(posedge clk) begin
        if(rst) begin reference_first<=0; reference_later<=0; end
        else begin
            if(take) reference_first<={reference_first_input};
            if(advance) reference_later<={reference_later_input};
        end
    end
    reg started=0;
    always @(posedge clk) begin
        if(!started) assume(rst);
        started<=1;
    end
    always @* if(started) begin
        assert(y==reference_first);
        assert(mid==reference_later);
    end
endmodule
"""
    assert harness.count("assert(") == 2 and harness.count("assume(") == 1
    actual = rtl + "\n" + harness
    (tmp_path / "source.zhl").write_text(source)
    (tmp_path / "actual-rtl-and-reference.sv").write_text(actual)
    proof = run_verilog_formal(
        actual, top="contract",
        property_id=f"instance-inference.{spelling}.{mutation or 'positive'}",
        mode=ProofMode.BMC if mutation else ProofMode.PROVE,
        depth=8, timeout_seconds=30, route="smtbmc", solver="z3",
        systemverilog=True, flatten=True, work_directory=tmp_path / "proof",
    )
    (tmp_path / "receipt.json").write_text(json.dumps({
        "source_sha256": sha256(source.encode()).hexdigest(),
        "selected_ir_identity": compiled.selected_ir_identity,
        "artifact_hash": artifact.artifact_hash,
        "emitted_rtl_sha256": sha256(artifact.text.encode()).hexdigest(),
        "actual_rtl_sha256": sha256(rtl.encode()).hexdigest(),
        "harness_sha256": sha256(harness.encode()).hexdigest(),
        "depth": 8, "timeout_seconds": 30, "mutation": mutation,
        "status": proof.status.value, "reason": proof.reason,
    }, indent=2) + "\n")
    return proof


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_deferred_input_inference_actual_rtl_is_proven(tmp_path, spelling):
    proof = _formal(tmp_path, spelling)
    assert proof.status is FormalStatus.PROVEN, (proof.status, proof.reason)


@pytest.mark.parametrize("mutation", (
    "wrong-reset", "lost-stage", "wrong-connection", "wrong-output-bit",
))
def test_corrupt_deferred_input_rtl_has_counterexamples(tmp_path, mutation):
    proof = _formal(tmp_path, "untyped-local", mutation)
    assert proof.status is FormalStatus.FAILED, (proof.status, proof.reason)
    assert proof.counterexample is not None


def test_deferred_parent_local_preserves_unknown_payloads_and_reset(tmp_path):
    path = tmp_path / "unknown-inference.zhl"
    path.write_text(_source("untyped-local"))
    tags = "u0x101u10x"
    stored = tags.replace("u", "x")
    with sim.compile(path, top="Top", logic_state=True).create() as machine:
        machine.set("sample", 0)
        machine.set("take", 0)
        machine.set("advance", 0)
        machine.reset("rst", asserted=True)
        machine.edge("clk")
        machine.reset("rst", asserted=False)
        machine.set_logic("sample", tags)
        machine.set("advance", 1)
        machine.edge("clk")
        assert machine.get_logic("mid").to_bits() == stored
        assert machine.get_logic("y").to_bits() == "0" * 10
        machine.set_logic("sample", "1111u0x001")
        machine.set("advance", 0)
        machine.set("take", 1)
        machine.edge("clk")
        assert machine.get_logic("mid").to_bits() == stored
        assert machine.get_logic("y").to_bits() == stored
        machine.set("take", 0)
        machine.edge("clk")
        assert machine.get_logic("mid").to_bits() == stored
        assert machine.get_logic("y").to_bits() == stored
        machine.set("advance", 1)
        machine.set("take", 1)
        machine.reset("rst", asserted=True)
        machine.edge("clk")
        assert machine.get_logic("mid").to_bits() == "0" * 10
        assert machine.get_logic("y").to_bits() == "0" * 10
        machine.reset("rst", asserted=False)
        machine.set("sample", 682)
        machine.edge("clk")
        assert machine.get_logic("mid").to_bits() == "1010101010"
        assert machine.get_logic("y").to_bits() == "0" * 10
        machine.set("advance", 0)
        machine.edge("clk")
        assert machine.get_logic("y").to_bits() == "1010101010"

    compiled = compile_source(path.read_text(), top="Top")
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    # RTL has one X tag; source-native U bits become computed X when stored.
    bench = f"""
module tb;
    reg clk=0,rst=1,take=0,advance=0;
    reg [9:0] sample=0;
    wire [9:0] y,mid;
    {artifact.module} dut(.*);
    task tick; begin #1; clk=1; #1; clk=0; #1; end endtask
    initial begin
        tick;
        if(mid !== 0 || y !== 0) $fatal(1,"initial reset");
        rst=0; sample=10'b{stored}; advance=1; tick;
        if(mid !== 10'b{stored} || y !== 0) $fatal(1,"unknown capture");
        sample=10'b1111x0x001; advance=0; take=1; tick;
        if(mid !== 10'b{stored} || y !== 10'b{stored}) $fatal(1,"snapshot");
        take=0; tick;
        if(mid !== 10'b{stored} || y !== 10'b{stored}) $fatal(1,"stall");
        rst=1; advance=1; take=1; tick;
        if(mid !== 0 || y !== 0) $fatal(1,"reset unknown state");
        rst=0; sample=10'd682; tick;
        if(mid !== 10'd682 || y !== 0) $fatal(1,"post-reset pipeline");
        advance=0; tick;
        if(y !== 10'd682) $fatal(1,"post-reset transfer");
        $finish;
    end
endmodule
"""
    rtl = tmp_path / "unknown-inference.sv"
    rtl.write_text(artifact.text + "\n" + bench)
    executable = tmp_path / "unknown-inference.vvp"
    for index, command in enumerate((
        ("iverilog", "-g2012", "-s", "tb", "-o", str(executable), str(rtl)),
        ("vvp", str(executable)),
    )):
        tool = shutil.which(command[0])
        assert tool, f"required RTL tool is missing: {command[0]}"
        try:
            result = run_bounded_process(
                (tool, *command[1:]), cwd=tmp_path,
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired as error:
            (tmp_path / f"unknown-{index}.log").write_text(
                "timeout\n" + subprocess_text(error.stdout) + subprocess_text(error.stderr)
            )
            raise
        (tmp_path / f"unknown-{index}.log").write_text(
            result.stdout + "\n" + result.stderr
        )
        assert result.returncode == 0, result.stdout + result.stderr
