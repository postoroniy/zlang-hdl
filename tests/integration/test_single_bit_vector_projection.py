"""Singleton packed bit-vector projection is legal across actual RTL tools."""

import pytest

from tests.simulation.differential import run_differential
from tests.support.stdlib_formal import run
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.ir.formal import FormalStatus, ProofMode


SOURCE = '''module Top {clock clk reset rst in a:bit out y:bit
 reg values:vec<1,bit>=[0]
 values <- [a]
 y=values[0]}
'''


def test_single_bit_vector_projection_native_and_sv(tmp_path):
    path = tmp_path/'top.zhl'
    path.write_text(SOURCE)
    events = [{'set': {'a': i % 2}, 'reset': {'rst': i == 0}, 'edges': ['clk']}
              for i in range(12)]
    trace = run_differential(path,top='Top',directory=tmp_path,events=events)
    assert trace.native == trace.direct_sv


@pytest.mark.parametrize('nested', (False, True))
def test_single_bit_vector_projection_universal_actual_rtl(tmp_path, nested):
    text = SOURCE.replace('vec<1,bit>=[0]','vec<1,vec<1,bit>>=[[0]]').replace(
        'values <- [a]', 'values <- [[a]]').replace('values[0]', 'values[0][0]') if nested else SOURCE
    rtl = emit(compile_source(text).ir)
    contract = '''module contract(input clk,rst,a);
 wire y; Top dut(.*); reg value,started=0;
 always @(posedge clk) begin
 started<=1; if(!started) assume(rst);
 if(started) assert(y==value);
 value<=rst ? 1'b0 : a;
 end endmodule'''
    result = run(rtl,contract,tmp_path,ProofMode.PROVE)
    assert result.status is FormalStatus.PROVEN, result.reason
