# SPDX-License-Identifier: Apache-2.0
"""Actual Direct-SV guarded output writes and independent state references."""
import pytest
from zlang.backend.systemverilog import emit
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode

def source(width):
    return f"""module Guarded {{
 clock clk reset rst
 in index:u64
 in data:u{width}
 in enable:bit
 out reg state:vec<4,u{width}> = [0,0,0,0]
 when enable {{ when index < 4 {{ state[index] <- data }} }}
}}
"""


@pytest.mark.parametrize("width,mutation", ((1, None), (8, None), (64, None),
                                           (8, "guard"), (8, "reset")))
def test_actual_rtl_guarded_write_induction_and_mutations(tmp_path, width, mutation):
    text = source(width)
    if mutation == "guard":
        text = text.replace("index < 4", "index < 3")
    if mutation == "reset":
        text = text.replace("[0,0,0,0]", "[1,0,0,0]")
    rtl = emit(compile_source(text, top="Guarded").ir)
    harness = f"""
module bounds_formal(input clk);
 reg started = 0;
 wire rst = !started;
 (* anyseq *) reg [63:0] index;
 (* anyseq *) reg [{width-1}:0] data;
 (* anyseq *) reg enable;
 wire [{4*width-1}:0] state;
 reg [{width-1}:0] expected [0:3];
 Guarded dut(.clk(clk), .rst(rst), .index(index), .data(data),
             .enable(enable), .state(state));
 always @(posedge clk) started <= 1;
 genvar lane;
 generate for (lane=0; lane<4; lane=lane+1) begin
  always @(posedge clk) begin
   if (rst) expected[lane] <= 0;
   else if (enable && index == lane) expected[lane] <= data;
   if (started) assert(state[lane*{width} +: {width}] == expected[lane]);
  end
 end endgenerate
endmodule
"""
    result = run_verilog_formal(
        rtl + harness, top="bounds_formal", property_id="guarded_vector_bounds",
        mode=ProofMode.PROVE, depth=8, timeout_seconds=30, work_directory=tmp_path / "proof",
        systemverilog=True, flatten=True,
    )
    assert result.status is (FormalStatus.FAILED if mutation else FormalStatus.PROVEN), result
