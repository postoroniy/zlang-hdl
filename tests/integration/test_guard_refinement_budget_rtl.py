"""Actual RTL preserves the opaque guard; only optional inference is skipped."""

import pytest

from tests.semantic.test_guard_refinement_budget import source
from tests.support.stdlib_formal import run
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.ir.formal import FormalStatus, ProofMode


@pytest.mark.parametrize("nested", (False, True))
def test_large_guard_actual_rtl_proven(tmp_path, nested):
    rtl = emit(compile_source(source(nested=nested)).ir)
    relation = '''module contract(input clk,input rst,input [7:0] a);
wire y;Top dut(.*);reg expected;reg past_valid=0;
wire [7:0] mixed=a^(a<<1);
always @(posedge clk) begin
 past_valid<=1;if(!past_valid)assume(rst);
 if(rst)expected<=0;else if(mixed!=0)expected<=1;
 if(past_valid)assert(y==expected);
end endmodule'''
    result = run(rtl, relation, tmp_path, ProofMode.PROVE)
    assert result.status is FormalStatus.PROVEN, result.reason
