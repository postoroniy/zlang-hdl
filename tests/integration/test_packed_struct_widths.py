"""Independent non-coding reproducer for packed callable finite-width fields."""

from tests.support.stdlib_formal import run
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.ir.formal import FormalStatus,ProofMode
import pytest


@pytest.mark.parametrize('conditional',(False,True))
@pytest.mark.parametrize('width',(3,5,9))
@pytest.mark.parametrize('length',(1,3,17))
def test_widened_subtraction_in_aggregate_actual_rtl(tmp_path,conditional,width,length):
    text = '''struct Record {a:vec<17,u8> b:vec<17,u8> c:u5 d:u5 e:u8}
fn change(state:Record,step:u5)->Record {
 Record{a=state.a b=state.a c=truncate<5>(step+1-state.c) d=1 e=state.e}
}
module Top {in state:Record in step:u5 out result:Record result=change(state,step)}'''
    if conditional:
        text = text.replace('a=state.a b=state.a c=truncate<5>(step+1-state.c) d=1 e=state.e',
            'a=step!=0 ? state.a : state.b b=step!=0 ? state.a : state.b '
            'c=step!=0 ? truncate<5>(step+1-state.c) : state.c '
            'd=step!=0 ? 1 : truncate<5>(state.d+1) e=step!=0 ? 17 : state.e')
    text = text.replace('u5',f'uint<{width}>').replace('truncate<5>',f'truncate<{width}>').replace('vec<17,',f'vec<{length},')
    rtl = emit(compile_source(text).ir)
    relation = '''module contract(input clk,input [135:0] state_a,state_b,
 input [4:0] state_c,state_d,input [7:0] state_e,input [4:0] step);
 wire [135:0] result_a,result_b;wire [4:0] result_c,result_d;wire [7:0] result_e;
 Top dut(.*);always @* begin assert(result_a==state_a);assert(result_b==state_a);
 assert(result_c==5'(step+1-state_c));assert(result_d==1);assert(result_e==state_e);
 end endmodule'''
    if conditional:
        relation = relation.replace('result_a==state_a','result_a==(step!=0 ? state_a : state_b)').replace(
            'result_b==state_a','result_b==(step!=0 ? state_a : state_b)').replace(
            "result_c==5'(step+1-state_c)","result_c==(step!=0 ? 5'(step+1-state_c) : state_c)").replace(
            'result_d==1',"result_d==(step!=0 ? 5'd1 : 5'(state_d+1))").replace(
            'result_e==state_e',"result_e==(step!=0 ? 8'd17 : state_e)")
    relation = relation.replace('[4:0]',f'[{width-1}:0]').replace("5'",f"{width}'").replace('[135:0]',f'[{8*length-1}:0]')
    result = run(rtl,relation,tmp_path,ProofMode.PROVE)
    assert result.status is FormalStatus.PROVEN,result.reason
