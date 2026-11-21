"""Singleton packed bit-vector projection is legal across actual RTL tools."""

import pytest

from tests.simulation.differential import run_differential
from tests.support.stdlib_formal import run
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.ir.formal import FormalStatus, ProofMode


SOURCE = '''struct Flag { value:bit }
union Choice { Off On }
module Top {clock clk reset rst in a:bit in en:bit out y:bit
 out field,tag,written:bit
 reg flag:Flag=Flag{value=0}
 reg choice_reg:Choice=Choice.Off
 reg lane:vec<1,bit>=[0]
 flag <- Flag{value=a}
 choice_reg <- a ? Choice.On : Choice.Off
 when en {lane[0] <- a}
 field=flag.value
 tag=match choice_reg {Choice.Off=>0 Choice.On=>1}
 written=lane[0]
 reg values:vec<1,bit>=[0]
 values <- [a]
 y=values[0]}
'''


def test_single_bit_vector_projection_native_and_sv(tmp_path):
    path = tmp_path/'top.zhl'
    path.write_text(SOURCE)
    events = [{'set': {'a': i % 2, 'en': int(i % 3 != 0)}, 'reset': {'rst': i == 0}, 'edges': ['clk']}
              for i in range(12)]
    trace = run_differential(path,top='Top',directory=tmp_path,events=events)
    assert trace.native == trace.direct_sv


@pytest.mark.parametrize('nested', (False, True))
def test_single_bit_vector_projection_universal_actual_rtl(tmp_path, nested):
    text = SOURCE.replace('values:vec<1,bit>=[0]','values:vec<1,vec<1,bit>>=[[0]]').replace(
        'values <- [a]', 'values <- [[a]]').replace('values[0]', 'values[0][0]') if nested else SOURCE
    rtl = emit(compile_source(text).ir)
    contract = '''module contract(input clk,rst,a,en);
 wire y,field,tag,written; Top dut(.*); reg value,held,started=0;
 always @(posedge clk) begin
 started<=1; if(!started) assume(rst);
 if(started) begin
   assert(y==value); assert(field==value); assert(tag==value); assert(written==held);
 end
 value<=rst ? 1'b0 : a;
 if(rst) held<=0; else if(en) held<=a;
 end endmodule'''
    result = run(rtl,contract,tmp_path,ProofMode.PROVE)
    assert result.status is FormalStatus.PROVEN, result.reason
