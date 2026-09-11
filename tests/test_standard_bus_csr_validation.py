import shutil
import unittest

from zlang import compile_source
from zlang.backend.systemverilog.emitter import emit
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode, generate_properties
from zlang.simulate import simulate_csr_cycles


AXI_TOP = """
import std.bus.reg
import std.bus.axi_lite
module AxiCsrTop {
  clock clk reset rst
  interface axi : AXI4Lite<32,32>.slave
  inst frontend : AXI4LiteToRegBus<32,32>
  inst csr : RegBusCSRTarget<32,32>
  connect axi -> frontend.axi
  connect frontend.regbus -> csr.regbus
  out done:bit done=csr.done
}
"""

APB_TOP = """
import std.bus.reg
import std.bus.apb
module ApbCsrTop {
  clock clk reset rst
  interface apb : APB<32,32>.slave
  inst frontend : APBToRegBus<32,32>
  inst csr : RegBusCSRTarget<32,32>
  connect apb -> frontend.apb
  connect frontend.regbus -> csr.regbus
  out done:bit done=csr.done
}
"""


class SourceAuthoritativeCsrTests(unittest.TestCase):
    def test_real_source_target_delegates_rw_w1c_and_pulse_state(self):
        for source, frontend in ((AXI_TOP, "AXI4LiteToRegBus"),
                                 (APB_TOP, "APBToRegBus")):
            with self.subTest(frontend=frontend):
                result = compile_source(source)
                target = next(child for child in result.ir.children
                              if child.name == "RegBusCSRTarget")
                self.assertEqual(
                    [register.name for register in target.registers],
                    ["response_pending", "response_addr"],
                )
                bank = next(child for child in target.children
                            if child.name == "RegBusCSRBank")
                self.assertEqual(
                    [item.behavior.value for item in bank.csr_blocks[0].state_bindings],
                    ["rw", "w1c", "pulse"],
                )
                generated = emit(result.ir)
                self.assertIn("csr_registers_w1c_value <=", generated)
                self.assertIn("csr_registers_pulse_value <=", generated)
                self.assertIn(f"module {frontend}_s", generated)

    def test_child_formal_design_retains_state_and_handshake_bindings(self):
        result = compile_source(AXI_TOP)
        target = next(child for child in result.ir.children
                      if child.name == "RegBusCSRTarget")
        design = generate_properties(target)
        ids = {item.id.split(".")[1] for item in design.properties}
        self.assertIn("register", ids)
        self.assertIn("ready_valid", ids)
        self.assertGreaterEqual(len(design.bindings), 3)

    def test_existing_csr_model_defines_w1c_and_pulse_behavior(self):
        source = """
        module CsrSemantics {
          clock clk reset rst
          in event:bit
          csr control @0 {
            CONTROL @0 {
              enable bit rw = 0
              clear bit w1c <- sticky(event)
              kick bit pulse
            }
          }
        }
        """
        module = compile_source(source).ir
        results = simulate_csr_cycles(module, [
            {"addr": 0, "write": 1, "wdata": 0b111, "read": 0, "event": 1},
            {"addr": 0, "write": 0, "wdata": 0, "read": 1, "event": 0},
            {"addr": 0, "write": 1, "wdata": 0b010, "read": 0, "event": 0},
            {"addr": 0, "write": 0, "wdata": 0, "read": 0, "event": 0},
        ])
        self.assertEqual(results[1]["rdata"], 0b011)
        self.assertEqual(results[2]["state"]["control.CONTROL.clear"], 1)
        self.assertEqual(results[3]["state"]["control.CONTROL.clear"], 0)

    @unittest.skipUnless(shutil.which("sby") and shutil.which("z3"),
                         "real SBY/Z3 unavailable")
    def test_failure_first_mutation_classification(self):
        mutants = {
            "axi.duplicate_write": """
                module axi_mutant(input clk, input rst, input valid);
                  reg [1:0] count;
                  always @(posedge clk) begin
                    if (rst) count <= 0; else if (valid) count <= count + 1;
                    assert (rst || count <= 1);
                  end
                endmodule
            """,
            "axi.phantom_b": """
                module b_mutant(input clk, input rst);
                  reg b;
                  always @(posedge clk) begin
                    if (rst) b <= 0; else b <= 1;
                    assert (rst || !b);
                  end
                endmodule
            """,
            "apb.bad_completion": """
                module apb_mutant(input clk, input rst, input penable);
                  reg pready;
                  always @(posedge clk) begin
                    if (rst) pready <= 0; else pready <= 1;
                    assert (rst || !pready || penable);
                  end
                endmodule
            """,
            "csr.duplicate_w1c": """
                module csr_mutant(input clk, input rst, input write);
                  reg state;
                  always @(posedge clk) begin
                    if (rst) state <= 1; else state <= 0;
                    assert (rst || write || state);
                  end
                endmodule
            """,
        }
        for property_id, mutant in mutants.items():
            top = mutant.split("module ", 1)[1].split("(", 1)[0].strip()
            result = run_verilog_formal(mutant, top=top,
                                        property_id=property_id,
                                        mode=ProofMode.BMC, depth=4)
            self.assertEqual(result.status, FormalStatus.FAILED, property_id)
            self.assertIsNotNone(result.counterexample, property_id)


if __name__ == "__main__":
    unittest.main()
