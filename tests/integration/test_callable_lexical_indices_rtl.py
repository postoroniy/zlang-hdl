"""Independent full-value RTL identity for same-name caller/callee indices."""

from tests.semantic.test_callable_lexical_indices import source
from tests.support.stdlib_formal import run
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.ir.formal import FormalStatus,ProofMode


def test_callable_index_scope_actual_rtl_proven(tmp_path):
    rtl = emit(compile_source(source()).ir)
    relation = '''module contract(input clk,input [15:0] a);
 wire [15:0] y;Top dut(.*);always @* assert(y==a);endmodule'''
    result = run(rtl,relation,tmp_path,ProofMode.PROVE)
    assert result.status is FormalStatus.PROVEN,result.reason
