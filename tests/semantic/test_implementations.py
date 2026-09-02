from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.opt import NodeCategory, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class ImplementationChoiceSemanticTests(unittest.TestCase):
    def test_applicability_timing_and_equivalence_are_typed_ir(self) -> None:
        compilation = compile_source((ROOT / "examples/mac_choice.zhl").read_text())
        choice = compilation.ir.assignments[0].expression

        self.assertIsInstance(choice, expr.ImplementationChoice)
        self.assertEqual(choice.selected, expr.ImplementationKind.DSP_MAC)
        self.assertEqual(
            choice.proven_equivalences,
            (
                expr.ImplementationEquivalence.MATHEMATICAL,
                expr.ImplementationEquivalence.CYCLE_ACCURATE,
            ),
        )
        self.assertNotIn(
            expr.ImplementationEquivalence.PROTOCOL_OBSERVATIONAL,
            choice.proven_equivalences,
        )
        self.assertEqual(
            [item.applicability.resource_hint for item in choice.alternatives],
            [expr.ImplementationResource.LOGIC, expr.ImplementationResource.DSP],
        )
        for alternative in choice.alternatives:
            self.assertEqual(alternative.applicability.operation, "multiply_add")
            self.assertEqual(alternative.semantics.latency, 1)
            self.assertEqual(alternative.semantics.initiation_interval, 1)
            self.assertEqual(alternative.semantics.protocol_events, ())

    def test_choice_is_architectural_canonical_ir_and_round_trips_losslessly(self) -> None:
        semantic = analyze(parse((ROOT / "examples/mac_choice.zhl").read_text()))
        canonical = lower(semantic)
        root = canonical.assignments[0].expression

        self.assertEqual(
            canonical.expressions[root].category,
            NodeCategory.ARCHITECTURE,
        )
        self.assertEqual(restore(canonical), semantic)

    def test_duplicate_wrong_shape_and_mathematical_mismatch_are_rejected(self) -> None:
        prefix = "module Bad { in a:u8 in b:u8 in c:u16 in d:u16 out y:u17 "
        cases = (
            (
                "repeats 'mul_add'",
                "y=choice(mul_add){mul_add=>a*b+c mul_add=>a*b+c} }",
            ),
            (
                "requires one multiply-add",
                "y=choice(mul_add){mul_add=>extend<17>(c) dsp_mac=>a*b+c} }",
            ),
            (
                "not mathematically equivalent",
                "y=choice(mul_add){mul_add=>a*b+c dsp_mac=>a*b+d} }",
            ),
        )
        for message, body in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(SemanticError, message):
                    analyze(parse(prefix + body))

    def test_latency_mismatch_is_not_accepted_as_cycle_accurate(self) -> None:
        source = (
            "module BadLatency { clock clk reset rst "
            "in a:u8 in b:u8 in c:u16 out y:u17 "
            "y=choice(mul_add){"
            "mul_add=>a*b+c "
            "dsp_mac=>pipeline(1){a*b+c}} }"
        )
        with self.assertRaisesRegex(
            SemanticError,
            r"latency mismatch: dsp_mac=1, mul_add=0.*not cycle-accurate",
        ):
            analyze(parse(source))

    def test_protocol_state_and_storage_cannot_be_blindly_merged(self) -> None:
        cases = (
            (
                "protocol/observational alternatives are not supported",
                "module ProtocolBad { in rx:rv<u8> in b:u8 in c:u16 out y:u17 "
                "y=choice(mul_add){mul_add=>rx.payload*b+c "
                "dsp_mac=>rx.payload*b+c} }",
            ),
            (
                "depends on sequential state",
                "module StateBad { clock clk reset rst reg a:u8=0 "
                "in b:u8 in c:u16 out y:u17 "
                "y=choice(mul_add){mul_add=>a*b+c dsp_mac=>a*b+c} }",
            ),
            (
                "architectural storage value",
                "module StorageBad { clock clk reset rst in a:u8 in b:u8 "
                "in c:u16 out y:u17 fifo q:fifo<u8,2> "
                "q.data=a q.push=0 q.pop=0 "
                "y=choice(mul_add){mul_add=>q.front*b+c "
                "dsp_mac=>q.front*b+c} }",
            ),
        )
        for message, source in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(SemanticError, message):
                    analyze(parse(source))

    def test_choice_is_only_a_complete_wire_output_assignment(self) -> None:
        nested = (
            "module Nested { in a:u8 in b:u8 in c:u16 out y:u18 "
            "y=choice(mul_add){mul_add=>a*b+c dsp_mac=>a*b+c}+0 }"
        )
        with self.assertRaisesRegex(SemanticError, "complete wire-output assignment"):
            analyze(parse(nested))

        protocol_target = (
            "module Target { in a:u8 in b:u8 in c:u16 out tx:rv<u17> "
            "tx.payload=choice(mul_add){mul_add=>a*b+c dsp_mac=>a*b+c} "
            "tx.valid=0 }"
        )
        with self.assertRaisesRegex(SemanticError, "require a wire output"):
            analyze(parse(protocol_target))


if __name__ == "__main__":
    unittest.main()
