# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Source/native/actual-RTL controls for independent conditional state owners."""

from hashlib import sha256
import json
import random

import pytest

from tests.support.independent_state_cases import source
from zlang import sim
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulation_compare import compare_program


@pytest.mark.parametrize("groups", (2, 13))
@pytest.mark.parametrize("logic_state", (False, True))
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_independent_owners_match_snapshot_scoreboard(tmp_path, groups, logic_state, simulator):
    lanes = 3
    path = tmp_path / "owners.zhl"
    path.write_text(source(groups, lanes))
    random_ = random.Random(20261110)
    count = groups * lanes
    state = [0] * count
    events, expected = [], []
    for cycle in range(80):
        values = {"guards": random_.getrandbits(groups),
                  "active": random_.getrandbits(count), "step": cycle % 4}
        reset = cycle in (0, 31, 63, 79)
        for edges in ((["clk"],) if cycle == 0 else ([], ["clk"])):
            if edges:
                state = [0 if reset else (value + values["step"]) & 3
                         if (values["guards"] >> (i // lanes)) & (values["active"] >> i) & 1
                         else value for i, value in enumerate(state)]
            events.append({"set": values, "reset": {"rst": reset}, "edges": edges})
            expected.append({f"q{i}": value for i, value in enumerate(state)})
    result = compare_program(
        sim.compile(path, logic_state=logic_state), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30,
    )
    assert result.native_trace == result.rtl_trace == tuple(expected)


def prove(tmp_path, groups, mutation=None):
    text = source(groups)
    replacements = {
        "guard": ("owner00 when guards[0]", "owner00 when guards[1]"),
        "activation": ("when active[0]", "when active[1]"),
        "dropped-write": ("q0+step", "q0"),
        "reset": ("out reg q0:u2=0", "out reg q0:u2=1"),
    }
    if mutation is not None:
        before, after = replacements[mutation]
        assert before in text
        text = text.replace(before, after, 1)
    compiled = compile_source(text)
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    count = groups * 3
    wires = "\n".join(f"wire [1:0] q{i}; reg [1:0] ref{i};" for i in range(count))
    connections = ",".join(f".q{i}(q{i})" for i in range(count))
    reference = "\n".join(
        f"always @(posedge clk) if(rst) ref{i}<=0; "
        f"else if(guards[{i // 3}] && active[{i}]) ref{i}<=ref{i}+step;"
        for i in range(count)
    )
    assertions = "\n".join(f"assert(q{i}==ref{i});" for i in range(count))
    harness = f"""module contract(input clk,rst,input [{groups - 1}:0] guards,
        input [{count - 1}:0] active,input [1:0] step);
        {wires}
        {artifact.module} dut(.clk(clk),.rst(rst),.guards(guards),.active(active),.step(step),{connections});
        {reference}
        reg started=0;
        always @(posedge clk) begin started<=1; if(!started) assume(rst); end
        always @* if(started) begin {assertions} end
    endmodule"""
    assert harness.count("assert(") == count
    actual = artifact.text + "\n" + harness
    (tmp_path / "actual-rtl-and-reference.sv").write_text(actual)
    result = run_verilog_formal(
        actual, top="contract", property_id=f"independent-owners.{groups}.{mutation or 'positive'}",
        mode=ProofMode.BMC if mutation else ProofMode.PROVE,
        depth=8, timeout_seconds=30, route="smtbmc", solver="z3", systemverilog=True,
        flatten=True, work_directory=tmp_path / "proof",
    )
    (tmp_path / "receipt.json").write_text(json.dumps({
        "source_sha256": sha256(text.encode()).hexdigest(),
        "selected_ir_identity": compiled.selected_ir_identity,
        "rtl_sha256": sha256(artifact.text.encode()).hexdigest(),
        "harness_sha256": sha256(harness.encode()).hexdigest(),
        "depth": 8, "timeout_seconds": 30, "assertions": count,
        "status": result.status.value, "reason": result.reason,
    }, indent=2) + "\n")
    return result


@pytest.mark.parametrize("groups", (2, 13))
def test_independent_owners_actual_rtl_is_proven(tmp_path, groups):
    result = prove(tmp_path, groups)
    assert result.status is FormalStatus.PROVEN, (result.status, result.reason)


@pytest.mark.parametrize("mutation", ("guard", "activation", "dropped-write", "reset"))
def test_independent_owners_mutations_have_counterexamples(tmp_path, mutation):
    result = prove(tmp_path, 2, mutation)
    assert result.status is FormalStatus.FAILED, (result.status, result.reason)
    assert result.counterexample is not None
