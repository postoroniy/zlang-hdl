from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.ir.expressions import BinaryOperator
from zlang.ir.types import BitType, UIntType
from zlang.opt import (
    EquivalenceMode,
    RewriteRule,
    SaturationError,
    lower,
    render_saturation,
    saturate,
    term_to_expression,
)
from zlang.opt.ir import ExpressionOp, Observation
from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[2]


class EqualitySaturationSemanticTests(unittest.TestCase):
    def test_power_of_two_multiply_is_not_strength_reduced(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/shift_multiply.zhl").read_text()
        )
        root = compilation.optimization_ir.assignments[0].expression
        result = saturate(compilation.optimization_ir, root)

        self.assertTrue(result.saturated)
        self.assertFalse(result.truncated)
        self.assertEqual(result.observations, (Observation.TYPED_VALUE,))
        self.assertEqual(result.equivalence_class.type, UIntType(16))
        self.assertNotIn(RewriteRule.MULTIPLY_POWER_OF_TWO, result.rules)
        self.assertEqual(result.alternatives, ())

    def test_signed_multiply_is_not_strength_reduced(self) -> None:
        compilation = compile_source(
            "module SignedShift { in x:s8 out y:s16 y=x*8 }"
        )
        root = compilation.optimization_ir.assignments[0].expression
        result = saturate(compilation.optimization_ir, root)
        self.assertEqual(result.alternatives, ())

    def test_identity_and_zero_rewrites_keep_the_declared_result_type(self) -> None:
        source = (
            "module Identities { in x:u8 "
            "out bor:u8 out bxor:u8 out shift:u8 "
            "bor=x|0 bxor=x^0 shift=x<<0 }"
        )
        canonical = lower(analyze(parse(source)))
        expected_rules = (
            RewriteRule.BIT_OR_ZERO,
            RewriteRule.BIT_XOR_ZERO,
            RewriteRule.SHIFT_ZERO,
        )
        for assignment, expected_rule in zip(
            canonical.assignments,
            expected_rules,
            strict=True,
        ):
            with self.subTest(output=assignment.target_name):
                result = saturate(
                    canonical,
                    assignment.expression,
                )
                self.assertIn(expected_rule, result.rules)
                self.assertTrue(
                    all(
                        term.type == result.original.type
                        for term in result.equivalence_class.terms
                    )
                )

    def test_retained_generic_call_is_boundedly_expanded_before_m26(self) -> None:
        compilation = compile_source("""
            fn identity<type T>(x : T) { x }
            module ThroughCall {
                in x : u8
                out y : u8
                y = identity(x) | 0
            }
        """)
        root = compilation.optimization_ir.assignments[0].expression

        result = saturate(compilation.optimization_ir, root)

        self.assertIn(RewriteRule.BIT_OR_ZERO, result.rules)
        self.assertTrue(
            any(term.op is ExpressionOp.INPUT for term in result.alternatives)
        )

    def test_nested_identities_retain_the_typed_original_and_explore_candidates(self) -> None:
        source = "module Nested { in x:u8 out y:u8 y=(x|0)^0 }"
        compilation = compile_source(source)
        root = compilation.optimization_ir.assignments[0].expression

        first = saturate(compilation.optimization_ir, root)
        second = saturate(compilation.optimization_ir, root)

        self.assertEqual(first.original.op, ExpressionOp.BINARY)
        self.assertEqual(
            first.original.attribute("operator"),
            BinaryOperator.BIT_XOR,
        )
        self.assertEqual(first.equivalence_class.type, UIntType(8))
        self.assertEqual(
            {RewriteRule.BIT_OR_ZERO, RewriteRule.BIT_XOR_ZERO},
            set(first.rules),
        )
        self.assertTrue(any(
            term.op is ExpressionOp.INPUT for term in first.alternatives
        ))
        for term in first.equivalence_class.terms:
            materialized = term_to_expression(term)
            self.assertEqual(materialized.type, UIntType(8))
        self.assertEqual(first.eclass_count, 2)
        self.assertEqual(first.eclass_count, second.eclass_count)
        self.assertEqual(render_saturation(first), render_saturation(second))

        explored = compile_source(
            "module Nested { in x:u8 out y:u8 "
            "y=explore { (x|0)^0 } }"
        )
        self.assertIn("source=1 value=4 legal=4 rejected=0", explored.exploration_report)

    def test_source_equiv_is_bidirectional_from_the_plain_value_root(self) -> None:
        compilation = compile_source(
            "equiv expand { x | zero<x> <=> x "
            "when unsigned(x) && width(x) == 8 } "
            "module M { in x:u8 out y:u8 y=x }"
        )
        root = compilation.optimization_ir.assignments[0].expression

        result = saturate(compilation.optimization_ir, root)

        expanded = tuple(
            term for term in result.alternatives
            if term.op is ExpressionOp.BINARY
            and term.attribute("operator") is BinaryOperator.BIT_OR
        )
        self.assertEqual(len(expanded), 1)
        registration = next(
            item
            for item in result.registrations
            if item.enabled and "source:expand" in item.provenance
        )
        self.assertEqual(registration.direction, "equality")
        self.assertTrue(registration.fired)
        self.assertEqual(registration.provenance, ("source:expand",))
        self.assertGreater(result.eclass_count, 1)

    def test_comparisons_and_nested_muxes_preserve_operand_types(self) -> None:
        sources = (
            "module Compare { in x:u8 out y:bit y=x==0 }",
            "module Nested { in c:bit in x:u8 out y:bit "
            "y=mux(c,x==0,x==1) }",
        )
        for source in sources:
            with self.subTest(source=source):
                compilation = compile_source(source)
                root = compilation.optimization_ir.assignments[0].expression
                result = saturate(compilation.optimization_ir, root)
                self.assertEqual(result.original.type, BitType())
                # Materialization validates the retained comparison operand
                # type rather than reconstructing it from the result bit.
                term_to_expression(result.original)

    def test_comparison_roots_are_total_through_unified_explore(self) -> None:
        sources = (
            "module Compare { in x:u8 out y:bit "
            "y=explore { x==0 } }",
            "module Nested { in c:bit in x:u8 out y:bit "
            "y=explore { mux(c,x==0,x==1) } }",
        )
        for source in sources:
            with self.subTest(source=source):
                compilation = compile_source(source)
                self.assertTrue(compilation.exploration_report)
                self.assertEqual(compilation.ir.outputs[0].type, BitType())

    def test_resize_identity_requires_one_exact_canonical_type(self) -> None:
        cases = (
            ("module E { in x:u4 out y:u8 y=extend<8>(x) }", False),
            ("module T { in x:u8 out y:u4 y=truncate<4>(x) }", False),
            ("module I { in x:u4 out y:u4 y=extend<4>(x) }", True),
        )
        for source, identity in cases:
            with self.subTest(source=source):
                compilation = compile_source(source)
                root = compilation.optimization_ir.assignments[0].expression
                result = saturate(compilation.optimization_ir, root)
                self.assertEqual(
                    RewriteRule.RESIZE_IDENTITY in result.rules,
                    identity,
                )
                self.assertTrue(all(
                    term.type == result.original.type
                    for term in result.equivalence_class.terms
                ))

    def test_source_equiv_guard_controls_registration_and_reports_provenance(self) -> None:
        declaration = (
            "equiv only_u8 { value | zero<value> <=> value "
            "when unsigned(value) && width(value) == 8 } "
        )
        accepted = compile_source(
            declaration + "module M { in x:u8 out y:u8 y=x|0 }"
        )
        accepted_result = saturate(
            accepted.optimization_ir,
            accepted.optimization_ir.assignments[0].expression,
        )
        self.assertIn(RewriteRule.BIT_OR_ZERO, accepted_result.rules)
        self.assertTrue(any(
            registration.fired
            and registration.provenance == ("source:only_u8",)
            for registration in accepted_result.registrations
        ))

        rejected = compile_source(
            declaration + "module M { in x:u4 out y:u4 y=x|0 }"
        )
        rejected_result = saturate(
            rejected.optimization_ir,
            rejected.optimization_ir.assignments[0].expression,
        )
        self.assertNotIn(RewriteRule.BIT_OR_ZERO, rejected_result.rules)
        self.assertEqual(rejected_result.alternatives, ())
        self.assertIn(
            "guard has no satisfying typed binding in this root",
            rejected_result.rejection_reasons,
        )

    def test_duplicate_source_rules_are_registered_once_with_all_provenance(self) -> None:
        compilation = compile_source(
            "equiv a { x | zero<x> <=> x when width(x) == 8 } "
            "equiv b { y | zero<y> <=> y when width(y) == 8 } "
            "module M { in x:u8 out y:u8 y=x|0 }"
        )
        root = compilation.optimization_ir.assignments[0].expression
        first = saturate(compilation.optimization_ir, root)
        second = saturate(compilation.optimization_ir, root)
        source_rules = tuple(
            registration
            for registration in first.registrations
            if registration.enabled
            and any(item.startswith("source:") for item in registration.provenance)
        )
        self.assertEqual(len(source_rules), 1)
        self.assertEqual(source_rules[0].provenance, ("source:a", "source:b"))
        self.assertEqual(render_saturation(first), render_saturation(second))

    def test_each_frozen_source_rule_family_reaches_egglog(self) -> None:
        cases = (
            (
                "equiv e { x ^ zero<x> <=> x when bits(x) } "
                "module M { in x:bits<8> out y:bits<8> y=x^0 }",
                RewriteRule.BIT_XOR_ZERO,
            ),
            (
                "equiv e { x >> zero<x> <=> x when signed(x) } "
                "module M { in x:s8 out y:s8 y=x>>0 }",
                RewriteRule.SHIFT_ZERO,
            ),
            (
                "equiv e { mux(c,x,x) <=> x "
                "when bit(c) && unsigned(x) && same_type(x,x) } "
                "module M { in c:bit in x:u8 out y:u8 y=mux(c,x,x) }",
                RewriteRule.MUX_IDENTITY,
            ),
        )
        for source, expected in cases:
            with self.subTest(rule=expected.value):
                compilation = compile_source(source)
                root = compilation.optimization_ir.assignments[0].expression
                result = saturate(compilation.optimization_ir, root)
                self.assertIn(expected, result.rules)
                self.assertTrue(any(
                    registration.fired
                    and registration.provenance == ("source:e",)
                    for registration in result.registrations
                ))

    def test_non_power_of_two_constant_is_not_given_a_shift_alternative(self) -> None:
        compilation = compile_source(
            "module MulThree { in x:u8 out y:u16 y=x*3 }"
        )
        root = compilation.optimization_ir.assignments[0].expression
        result = saturate(compilation.optimization_ir, root)

        self.assertTrue(result.saturated)
        self.assertEqual(result.rules, ())
        self.assertEqual(result.alternatives, ())

    def test_state_protocol_and_architectural_dependencies_are_rejected(self) -> None:
        cases = []
        for filename, category in (
            ("counter.zhl", "state"),
            ("rv_passthrough.zhl", "protocol"),
            ("request_client.zhl", "transaction"),
            ("mac_choice.zhl", "architecture"),
        ):
            compilation = compile_source((ROOT / "examples" / filename).read_text())
            node = next(
                node
                for node in compilation.optimization_ir.expressions
                if node.category.value == category
            )
            cases.append((compilation, node.id, category))

        for compilation, root, category in cases:
            with self.subTest(category=category):
                with self.assertRaisesRegex(
                    SaturationError,
                    rf"not a pure mathematical value.*{category}",
                ):
                    saturate(compilation.optimization_ir, root)

    def test_nonmathematical_modes_invalid_roots_and_bounds_fail_explicitly(self) -> None:
        compilation = compile_source("module Plain { in x:u8 out y:u8 y=x }")
        root = compilation.optimization_ir.assignments[0].expression

        with self.assertRaisesRegex(SaturationError, "only mathematical"):
            saturate(
                compilation.optimization_ir,
                root,
                mode=EquivalenceMode.CYCLE_ACCURATE,
            )
        with self.assertRaisesRegex(SaturationError, "root %-1 does not exist"):
            saturate(compilation.optimization_ir, -1)
        with self.assertRaisesRegex(SaturationError, "max_iterations"):
            saturate(compilation.optimization_ir, root, max_iterations=0)
        with self.assertRaisesRegex(SaturationError, "max_terms"):
            saturate(compilation.optimization_ir, root, max_terms=0)

    def test_candidate_bound_is_reported_instead_of_silently_dropping_work(self) -> None:
        compilation = compile_source("module Mux { in c:bit in x:u8 out y:u8 y=mux(c,x,x) }")
        root = compilation.optimization_ir.assignments[0].expression
        result = saturate(compilation.optimization_ir, root, max_terms=1)

        self.assertFalse(result.saturated)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.equivalence_class.terms), 1)
        self.assertEqual(result.original.op, ExpressionOp.MUX)


if __name__ == "__main__":
    unittest.main()
