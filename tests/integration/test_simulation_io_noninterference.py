# SPDX-License-Identifier: Apache-2.0
from dataclasses import replace

import pytest

from zlang import sim
from zlang.backend.systemverilog import emit
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulation_io_product import build_io_product


@pytest.mark.parametrize(
    "width,mutation", [(1, False), (8, False), (16, False), (8, True)]
)
def test_actual_rtl_probe_noninterference_and_mutation(tmp_path, width, mutation):
    path = tmp_path / "dut.zhl"
    path.write_text(f"""module Pilot {{clock clk reset rst in a:u{width} in enable:bit
out reg y:u{width}=0 when enable {{y <- a}}
simulation io @clk {{when ~rst {{write log {{value=y}}}}}}
}}""")
    program = sim.compile(path)
    product = build_io_product(program.module, program.simulation_blocks, "io")
    baseline = emit(program.module)
    observed = emit(replace(product.module, name="ObservedPilot"))
    if mutation:
        needle = "y <= a;"
        assert observed.count(needle) == 1
        observed = observed.replace(needle, f"y <= (a ^ {width}'d1);")
    reset_name = product.block.reset_bindings[0][1]
    harness = f"""module isolation(input clk);
(* anyseq *) reg rst, enable, mirror;
(* anyseq *) reg [{width - 1}:0] a;
wire [{width - 1}:0] expected, actual;
Pilot reference_dut(.clk(clk),.rst(rst),.a(a),.enable(enable),.y(expected));
ObservedPilot observed_dut(.clk(clk),.rst(rst),.a(a),.enable(enable),.y(actual),.{reset_name}(mirror));
reg started=0;
always @(posedge clk) begin
 if (!started) assume(rst);
 started <= 1;
 if (started) assert(expected == actual);
end
endmodule"""
    result = run_verilog_formal(
        baseline + "\n" + observed + "\n" + harness,
        top="isolation",
        property_id="simulation_io_noninterference",
        mode=ProofMode.PROVE,
        depth=8,
        timeout_seconds=30,
        systemverilog=True,
        flatten=True,
        work_directory=tmp_path / "proof",
    )
    assert result.status is (FormalStatus.FAILED if mutation else FormalStatus.PROVEN)
