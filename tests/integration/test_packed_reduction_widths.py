"""Packed reduction elements keep their exact width in actual Yosys RTL."""

import pytest

from tests.support.stdlib_formal import run
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.ir.formal import FormalStatus, ProofMode


@pytest.mark.parametrize('body', (
    '[parity(x),parity(x >> 1),parity(x >> 2)]',
    'generate(i in 0..3) parity(x >> i)',
))
def test_packed_parity_elements_match_universal_rtl_reference(tmp_path, body):
    source = f"""module Top{{in x:bits<12> out y:bits<3>
 y=bitcast<bits<3>>({body})}}"""
    rtl = emit(compile_source(source).ir)
    contract = """module contract(input clk,input [11:0] x);
 wire [2:0] y; Top dut(.*);
 always @* assert(y=={^(x>>2),^(x>>1),^x});
 endmodule"""
    result = run(rtl, contract, tmp_path, ProofMode.PROVE)
    assert result.status is FormalStatus.PROVEN, result.reason
