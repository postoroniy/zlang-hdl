"""Real-solver checks against RTL emitted by the direct-SV backend."""

from pathlib import Path
import shutil
import unittest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus


ROOT = Path(__file__).resolve().parents[2]
TOOLS_AVAILABLE = all(shutil.which(tool) for tool in ("yosys", "sby", "z3"))


def emitted(name: str, *, top: str | None = None) -> str:
    module = compile_source((ROOT / "examples" / name).read_text(), top=top).ir
    return emit_experimental(module)


RULE_HARNESS = r"""
module DirectRuleFormal(input clk,input rst,input increment,input clear);
  wire [7:0] count_out;
  RuleCounter dut(clk,rst,increment,clear,count_out);
  always @(posedge clk) if (!$initstate && !$past(rst)) begin
    if ($past(clear)) assert(count_out == 0);
    else if ($past(increment)) assert(count_out == $past(count_out) + 1'b1);
    else assert(count_out == $past(count_out));
  end
endmodule
"""

COUNTER_HARNESS = r"""
module DirectStateFormal(input clk,input rst);
  wire [7:0] y;
  Counter dut(clk,rst,y);
  always @(posedge clk) if (!$initstate && !$past(rst))
    assert(y == $past(y) + 1'b1);
endmodule
"""

FIFO_HARNESS = r"""
module DirectFifoFormal(input clk,input rst,input [7:0] rx_payload,input rx_valid,input tx_ready);
  wire rx_ready,tx_valid; wire [7:0] tx_payload;
  RvBuffer dut(clk,rst,rx_payload,rx_valid,rx_ready,tx_payload,tx_valid,tx_ready);
  initial assume(rst);
  reg [1:0] model_count;
  always @(posedge clk) begin
    if (rst) model_count <= 0;
    else case ({rx_valid && rx_ready,tx_valid && tx_ready})
      2'b10: model_count <= model_count + 1'b1;
      2'b01: model_count <= model_count - 1'b1;
      default: model_count <= model_count;
    endcase
    if (!$initstate && !$past(rst)) begin
      // A full FIFO may accept a replacement item when the current front is
      // consumed on the same edge.  This is the frozen simultaneous
      // pop/push behavior, not an overflow.
      assert(rx_ready == ((model_count < 2) ||
                          ((model_count != 0) && tx_ready)));
      assert(tx_valid == (model_count != 0));
      assert(model_count <= 2);
      if ($past(tx_valid && !tx_ready)) begin
        assert(tx_valid);
        assert(tx_payload == $past(tx_payload));
      end
    end
  end
endmodule
"""

CSR_HARNESS = r"""
module DirectCsrFormal(input clk,input rst,input write,input [31:0] wdata);
  wire [31:0] rdata; wire ready;
  ControlCsr dut(clk,rst,32'h40000004,write,wdata,1'b1,rdata,ready);
  initial assume(rst);
  reg model_error;
  always @(posedge clk) begin
    if (rst) model_error <= 1;
    else if (write) model_error <= model_error & ~wdata[1];
    if (!$initstate && !$past(rst)) begin
      assert(rdata[1] == model_error);
      assert(ready);
    end
  end
endmodule
"""

RR_HARNESS = r"""
module DirectRrFormal(input clk,input rst,input fire,input accept_request,
                      input accept_response,input [7:0] data);
  wire response_seen;
  HierarchicalRequestResponse dut(clk,rst,fire,accept_request,accept_response,data,response_seen);
  initial assume(rst);
  reg model_outstanding;
  wire model_request_accept =
      !rst && fire && accept_request && !model_outstanding;
  wire model_response_consume = model_request_accept && accept_response;
  always @(posedge clk) begin
    if (rst) model_outstanding <= 1'b0;
    else case ({model_request_accept, model_response_consume})
      2'b10: model_outstanding <= 1'b1;
      2'b01: model_outstanding <= 1'b0;
      default: model_outstanding <= model_outstanding;
    endcase
    if (!rst) begin
      assert(response_seen == model_request_accept);
      assert(model_outstanding <= 1);
    end
  end
endmodule
"""


@unittest.skipUnless(TOOLS_AVAILABLE, "Yosys, SymbiYosys, and Z3 are required")
class DirectSystemVerilogFormalTests(unittest.TestCase):
    def check(self, rtl: str, harness: str, top: str, property_id: str) -> FormalStatus:
        result = run_verilog_formal(
            rtl + harness, top=top, property_id=property_id,
            depth=8, systemverilog=True,
        )
        self.assertIsNot(result.status, FormalStatus.SKIPPED)
        if result.status is FormalStatus.FAILED:
            self.assertIsNotNone(result.counterexample)
            self.assertTrue(result.counterexample.raw_trace)
        return result.status

    def test_emitted_m35_safety_families_execute(self) -> None:
        targets = (
            (emitted("rule_counter.zhl"), RULE_HARNESS, "DirectRuleFormal", "direct.rules"),
            (emitted("counter.zhl"), COUNTER_HARNESS, "DirectStateFormal", "direct.state"),
            (emitted("rv_buffer.zhl"), FIFO_HARNESS, "DirectFifoFormal", "direct.fifo_ready_valid"),
            (emitted("control_csr.zhl"), CSR_HARNESS, "DirectCsrFormal", "direct.csr"),
            (emitted("hierarchical_request_response_m40.zhl", top="HierarchicalRequestResponse"),
             RR_HARNESS, "DirectRrFormal", "direct.request_response"),
        )
        for rtl, harness, top, property_id in targets:
            with self.subTest(property=property_id):
                self.assertIs(
                    self.check(rtl, harness, top, property_id),
                    FormalStatus.BOUNDED_PASS,
                )

    def test_direct_rtl_mutations_fail_with_counterexamples(self) -> None:
        rule = emitted("rule_counter.zhl")
        counter = emitted("counter.zhl")
        fifo = emitted("rv_buffer.zhl")
        csr = emitted("control_csr.zhl")
        rr = emitted("hierarchical_request_response_m40.zhl", top="HierarchicalRequestResponse")
        mutations = (
            (rule.replace("count} + {{1{1'b0}}, 8'd1", "count} - {{1{1'b0}}, 8'd1"),
             RULE_HARNESS, "DirectRuleFormal", "arithmetic"),
            (counter.replace(
                "count <= 8'(({{1{1'b0}}, count} + {{1{1'b0}}, 8'd1}));",
                "count <= count;"), COUNTER_HARNESS, "DirectStateFormal", "state_transition"),
            (fifo.replace("count <= count + 1'b1", "count <= count - 1'b1"),
             FIFO_HARNESS, "DirectFifoFormal", "fifo_accounting"),
            (rr.replace(
                "assign bus_response_valid = (bus_request_valid && bus_request_ready);",
                "assign bus_response_valid = (bus_request_valid || bus_request_ready);"),
             RR_HARNESS, "DirectRrFormal", "response_accounting"),
            (csr.replace(
                "csr_control_status_error & ~wdata[1]",
                "csr_control_status_error | wdata[1]"),
             CSR_HARNESS, "DirectCsrFormal", "w1c"),
            (rule.replace(
                "if (clear) count <= 8'd0;\n      else if (increment)",
                "if (increment) count <= 8'd0;\n      else if (clear)"),
             RULE_HARNESS, "DirectRuleFormal", "priority"),
        )
        for mutated, harness, top, name in mutations:
            with self.subTest(mutation=name):
                self.assertIs(
                    self.check(mutated, harness, top, f"direct.mutation.{name}"),
                    FormalStatus.FAILED,
                )


if __name__ == "__main__":
    unittest.main()
