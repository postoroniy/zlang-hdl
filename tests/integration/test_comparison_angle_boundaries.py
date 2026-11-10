# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Source-bound comparison variants preserve a 35-bit unsigned boundary."""

import itertools

import pytest

from zlang import sim
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulation_compare import compare_program


SOURCE = """
struct Operands {upper:u35 lower:u34}
module Top {
    in a:u35 in b:u34
    out inline_value,temporary,negated,dotted:bit
    operands=Operands{upper=a lower=b}
    extended=extend<35>(b)
    inline_value=a<=extend<35>(b)
    temporary=a<=extended
    negated=!(a>extend<35>(b))
    dotted=operands.upper<=extend<35>(operands.lower)
}
"""


@pytest.mark.parametrize("logic_state", (False, True))
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_all_four_comparison_forms_execute_with_nonzero_upper_bits(tmp_path, logic_state, simulator):
    path = tmp_path / "comparison.zhl"
    path.write_text(SOURCE)
    pairs = itertools.product((0, 1, (1 << 34) - 1, 1 << 34, (1 << 35) - 1),
                              (0, 1, (1 << 33), (1 << 34) - 1))
    events, expected = [], []
    for a, b in pairs:
        events.append({"set": {"a": a, "b": b}, "edges": []})
        expected.append(dict.fromkeys(("inline_value", "temporary", "negated", "dotted"), int(a <= b)))
    result = compare_program(sim.compile(path, logic_state=logic_state), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30)
    assert result.native_trace == result.rtl_trace == tuple(expected)


@pytest.mark.parametrize("mutation", (None, "width", "relation"))
def test_comparison_actual_rtl_matches_independent_unsigned_relation(tmp_path, mutation):
    source = SOURCE
    if mutation == "width":
        source = source.replace("dotted=operands.upper<=", "dotted=extend<35>(truncate<34>(operands.upper))<=")
    elif mutation == "relation":
        source = source.replace("dotted=operands.upper<=", "dotted=operands.upper<")
    result = compile_source(source)
    artifact = emit_artifact(result.ir, selected_ir_identity=result.selected_ir_identity)
    rtl = artifact.text + f"""
    module contract(input [34:0] a,input [33:0] b);
      wire inline_value,temporary,negated,dotted;
      {artifact.module} dut(.*);
      wire expected=a <= {{1'b0,b}};
      always @* begin assert(inline_value==expected); assert(temporary==expected);
        assert(negated==expected); assert(dotted==expected); end
    endmodule"""
    proof = run_verilog_formal(rtl, top="contract", property_id="comparison-angle."+str(mutation),
        mode=ProofMode.BMC if mutation else ProofMode.PROVE, depth=8, timeout_seconds=30,
        route="smtbmc", solver="z3", systemverilog=True, flatten=True, work_directory=tmp_path / "proof")
    assert proof.status is (FormalStatus.FAILED if mutation else FormalStatus.PROVEN), proof.reason
    if mutation:
        assert proof.counterexample is not None
