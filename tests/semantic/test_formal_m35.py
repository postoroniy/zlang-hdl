import unittest
from subprocess import TimeoutExpired
from unittest.mock import patch

from zlang.compiler import compile_source
from zlang.formal import build_formal_design, emit_sby, run_formal, run_verilog_formal
from zlang.ir import (
    Assignment, BitType, Constant, Contract, ContractKind, Fifo, FormalError,
    FormalDesign, FormalProperty, FormalPropertyClassification, FormalStatus,
    Module, NextAssignment, Ownership,
    Port, PortDirection, ProofMode, PropertyKind, FixedType,
    Register, SignalBinding, UIntType,
    TemporalForm,
)
from zlang.ir.csr import CsrAccess, CsrBlock, CsrField, CsrRegister
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.expressions import InputRef
from zlang.ir.module import Rule, RulePriority


class FormalM35Tests(unittest.TestCase):
    def module(self):
        bit = BitType()
        u2 = UIntType(2)
        return Module(
            "FormalSmall",
            ports=(
                Port(PortDirection.INPUT, "rv_in", u2, InterfaceProtocol.READY_VALID),
                Port(PortDirection.OUTPUT, "rv_out", u2, InterfaceProtocol.READY_VALID),
                Port(PortDirection.OUTPUT, "credit", u2, InterfaceProtocol.CREDIT, capacity=2),
            ), assignments=(), clock="clk", reset="rst",
            registers=(Register("counter", u2, Constant(0, u2)),),
            fifos=(Fifo("queue", u2, 2, Constant(0, u2), Constant(0, bit), Constant(0, bit)),),
            csr_blocks=(CsrBlock("csr", 0, (CsrRegister("status", 0, (CsrField("sticky", bit, CsrAccess.WRITE_ONE_TO_CLEAR, 0, 0, 0),)),)),),
            rules=(Rule("high", InputRef("g1", bit), ()), Rule("low", InputRef("g2", bit), ())),
            rule_priorities=(RulePriority("high", "low"),),
        )

    def test_frozen_state_and_protocol_target_families(self):
        design = build_formal_design(self.module())
        families = {item.id.split(".")[1] for item in design.properties}
        self.assertTrue({"register", "fifo", "ready_valid", "credit", "csr"} <= families)
        self.assertNotIn("rules", families)  # independent empty-action rules may fire together
        self.assertEqual(len(design.bindings), len({item.semantic_signal_id for item in design.bindings}))

    def test_fifo_front_stability_uses_existing_front_observation(self):
        properties = [
            item for item in build_formal_design(self.module()).properties
            if item.generated_from == "fifo:queue"
            and item.expression.startswith("previous(queue.count > 0")
        ]
        self.assertEqual(len(properties), 1)
        self.assertEqual(
            set(properties[0].relevant_signals),
            {"reset", "fifo:queue.count", "fifo:queue.pop", "fifo:queue.front"},
        )

    def test_rule_properties_are_limited_to_real_state_conflicts(self):
        module = compile_source("""
            module Rules {
                clock clk
                reset rst
                in a: bit
                in b: bit
                out y: u8
                reg q: u8 = 0
                priority {
                    high: when a { q <- 1 }
                    low: when b { q <- 2 }
                }
                y = q
            }
        """, include_clash=False).ir
        rules = [item for item in build_formal_design(module).properties
                 if item.generated_from and item.generated_from.startswith(("rules:", "priority:"))]
        self.assertEqual(len(rules), 2)
        self.assertTrue(all(item.predicate is not None for item in rules))
        self.assertTrue(all(item.non_executable_reason is None for item in rules))
        design = build_formal_design(module)
        fires = [
            item for item in design.bindings
            if item.semantic_signal_id.startswith("rule:")
        ]
        self.assertEqual(
            [item.semantic_signal_id for item in fires],
            ["rule:high.fire", "rule:low.fire"],
        )
        self.assertTrue(all(item.width == 1 for item in fires))
        self.assertTrue(all(item.source_origin is not None for item in fires))
        # Rule fire is a formal-only observation. The ordinary binding catalog
        # remains the production/public ABI and must not expose scheduler state.
        from zlang.ir.formal import signal_bindings
        self.assertFalse(any(
            item.semantic_signal_id.startswith("rule:")
            for item in signal_bindings(module)
        ))

    def test_fixed_register_uses_signed_raw_storage_bounds(self):
        fixed = FixedType(8, 4)
        module = Module(
            "FixedState", ports=(), assignments=(), clock="clk", reset="rst",
            registers=(Register("sample", fixed, Constant(0, fixed)),),
        )
        properties = build_formal_design(module).properties
        bound = next(item for item in properties if item.generated_from == "register:sample")
        self.assertIn("-(128) <= sample", bound.expression)
        self.assertIn("sample < 128", bound.expression)
        self.assertIs(
            bound.classification,
            FormalPropertyClassification.REPRESENTATION_INVARIANT,
        )

    def test_binding_override_is_explicit(self):
        module = self.module()
        design = build_formal_design(module)
        original = next(item for item in design.bindings
                        if item.semantic_signal_id == "port:rv_in")
        self.assertEqual(original.rtl_name, "rv_in")
        rebound = SignalBinding(original.semantic_signal_id, "OtherTop", "i_data", original.width, original.direction)
        self.assertEqual(rebound.semantic_signal_id, original.semantic_signal_id)

    def test_bounded_pass_never_becomes_proven(self):
        result = run_formal(build_formal_design(self.module()), mode=ProofMode.BMC)
        self.assertTrue(result)
        self.assertTrue(all(item.status is FormalStatus.SKIPPED for item in result))

    def test_harness_and_sby_are_deterministic(self):
        design = build_formal_design(self.module())
        from zlang.ir.formal import emit_harness
        harness = emit_harness(design, mode=ProofMode.BMC, depth=4)
        self.assertIn("non-executable property report", harness)
        self.assertIn("m35.", harness)
        self.assertIn("depth=4", harness)
        with self.assertRaisesRegex(FormalError, "connected backend"):
            emit_sby(design, depth=4)

    def test_invalid_binding_and_result_are_rejected(self):
        with self.assertRaises(FormalError):
            SignalBinding("x", "Top", "x", 0, "internal")
        with self.assertRaises(FormalError):
            from zlang.ir.formal import FormalResult
            FormalResult("p", FormalStatus.BOUNDED_PASS, ProofMode.PROVE, "yosys", "z3", 2)

    def test_duplicate_and_missing_semantic_bindings_are_rejected(self):
        binding = SignalBinding("port:x", "Top", "x", 1, "input")
        with self.assertRaisesRegex(FormalError, "duplicate semantic signal"):
            FormalDesign("Top", (), (binding, binding))
        property_ = FormalProperty(
            "p", PropertyKind.ASSERTION, "clk", "rst", "x == x",
            TemporalForm.SAME_CYCLE, Ownership.IMPLEMENTATION,
            relevant_signals=("port:x",),
        )
        from zlang.ir.formal import emit_harness
        with self.assertRaisesRegex(FormalError, "no signal binding"):
            emit_harness(FormalDesign("Top", (property_,), ()))

    def test_external_solver_timeout_is_unknown_not_failure_or_success(self):
        with patch("zlang.formal.shutil.which", return_value="/tool"), patch(
            "zlang.formal.subprocess.run",
            side_effect=TimeoutExpired(("sby",), 120, output="partial log"),
        ):
            result = run_verilog_formal(
                "module top; endmodule", top="top", property_id="timeout",
                depth=2, timeout_seconds=7,
            )
        self.assertIs(result.status, FormalStatus.UNKNOWN)
        self.assertIn("timed out after 7 seconds", result.reason)

    def test_external_solver_timeout_normalizes_mixed_partial_stream_types(self):
        with patch("zlang.formal.shutil.which", return_value="/tool"), patch(
            "zlang.formal.subprocess.run",
            side_effect=TimeoutExpired(
                ("sby",), 120, output=b"partial stdout", stderr="partial stderr"
            ),
        ):
            result = run_verilog_formal(
                "module top; endmodule", top="top", property_id="timeout.bytes",
            )
        self.assertEqual(result.status, FormalStatus.UNKNOWN)
        self.assertIn("partial stdoutpartial stderr", result.reason)


if __name__ == "__main__":
    unittest.main()
