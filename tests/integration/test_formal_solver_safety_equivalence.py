"""Real safety verification/semantic-reference equivalence solver coverage (skips only when the formal toolchain is absent)."""

import unittest

from zlang.equivalence import make_equivalence_property, run_equivalence_formal
from zlang.formal import run_verilog_targets
from zlang.ir import EquivalenceStatus, InputRef, UIntType
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.timing import TimingInfo
from zlang.ir import Pipeline


SAFE = """module safety_verification(input clk, input [7:0] a, b);
wire [8:0] sum = a + b;
always @(posedge clk) assert(sum == a + b);
endmodule
"""


class RealFormalValidationTests(unittest.TestCase):
    def test_all_six_safety_verification_targets_execute(self):
        targets = tuple((f"safety_verification.{family}.safe", "safety_verification", SAFE) for family in
                        ("counter", "fifo", "ready_valid", "credit", "csr", "rules"))
        results = run_verilog_targets(targets, mode=ProofMode.BMC, depth=4)
        for result in results:
            if result.status is FormalStatus.SKIPPED:
                self.assertIn("missing formal tools", result.reason or "")
            else:
                self.assertEqual(result.status.value, "bounded_pass")
                self.assertIsNotNone(result.tool_versions)
        proven = run_verilog_targets((targets[0],), mode=ProofMode.PROVE, depth=4)[0]
        if proven.status is not FormalStatus.SKIPPED:
            self.assertEqual(proven.status, FormalStatus.PROVEN)

    def test_mutations_fail_with_counterexample_metadata(self):
        mutations = {
            "wrong_arithmetic": """module mut(input clk, input [3:0] a,b);
wire [4:0] y = a - b; always @(posedge clk) assert(y == a + b); endmodule""",
            "wrong_reduction": """module mut(input clk, input [3:0] a,b,c);
wire [4:0] y = a + b; always @(posedge clk) assert(y == a + b + c); endmodule""",
            "broken_credit": """module mut(input clk, input rst);
reg [1:0] credits; always @(posedge clk) begin if (rst) credits <= 0; else credits <= credits - 1'b1; assert(credits != 3); end endmodule""",
        }
        for name, source in mutations.items():
            result = run_verilog_targets(((name, "mut", source),), depth=4)[0]
            if result.status is not FormalStatus.SKIPPED:
                self.assertEqual(result.status.value, "failed", name)
                self.assertIsNotNone(result.counterexample, name)
                self.assertTrue(result.counterexample.raw_trace, name)

    def test_semantic_equivalence_same_cycle_and_fixed_latency_real_execution(self):
        u8 = UIntType(8)
        x = InputRef("x", u8)
        source = """module semantic_equivalence(input clk, input [7:0] x);
wire [7:0] reference_value = x; wire [7:0] implementation_value = x;
always @(posedge clk) assert(reference_value == implementation_value);
endmodule"""
        for candidate_class in ("guarded_rewrite", "architecture_alternatives", "exact_reduction"):
            candidate = make_equivalence_property(
                x, x, candidate_class=candidate_class, reference_root="r",
                implementation_root=candidate_class)
            result = run_equivalence_formal(candidate, source, top="semantic_equivalence", depth=4)
            if result.status is not EquivalenceStatus.SKIPPED:
                self.assertEqual(result.status, EquivalenceStatus.BOUNDED_PASS)

        pipeline = Pipeline(1, x, 1, u8)
        timed = make_equivalence_property(
            x, pipeline, candidate_class="pipeline_scheduler", reference_root="r", implementation_root="p",
            reference_timing=TimingInfo(0, 1, "clk", "rst"),
            implementation_timing=TimingInfo(1, 1, "clk", "rst"))
        timed_source = """module semantic_equivalence_proof(input clk, input rst, input [7:0] x);
reg [7:0] history; reg valid; wire [7:0] implementation_value = history;
always @(posedge clk) begin
  if (rst) begin history <= 0; valid <= 0; end
  else begin history <= x; valid <= 1; end
  if (!rst && valid) assert(history == implementation_value);
end endmodule"""
        result = run_equivalence_formal(timed, timed_source, top="semantic_equivalence_proof", depth=6)
        if result.status is not EquivalenceStatus.SKIPPED:
            self.assertEqual(result.status, EquivalenceStatus.BOUNDED_PASS)

    def test_semantic_equivalence_wrong_latency_fails_and_reset_fill_is_checked(self):
        u8 = UIntType(8)
        x = InputRef("x", u8)
        timed = make_equivalence_property(
            x, Pipeline(1, x, 1, u8), candidate_class="pipeline_scheduler", reference_root="r", implementation_root="bad",
            reference_timing=TimingInfo(0, 1, "clk", "rst"),
            implementation_timing=TimingInfo(1, 1, "clk", "rst"))
        source = """module badpipe(input clk, input rst, input [7:0] x);
reg [7:0] history; reg valid; always @(posedge clk) begin
 if (rst) begin history <= 0; valid <= 0; end
 else begin history <= x; valid <= 1; end
 if (!rst && valid) assert(history == x); // wrong latency, including after mid-stream reset
end endmodule"""
        shallow = run_equivalence_formal(timed, source, top="badpipe", depth=4)
        self.assertEqual(shallow.status, EquivalenceStatus.UNKNOWN)
        self.assertIn("comparison_window_unreached", shallow.reason or "")
        for depth in (5, 6):
            result = run_equivalence_formal(
                timed, source, top="badpipe", depth=depth
            )
            if result.status is not EquivalenceStatus.SKIPPED:
                self.assertEqual(result.status, EquivalenceStatus.FAILED)
                self.assertIsNotNone(result.counterexample)


if __name__ == "__main__":
    unittest.main()
