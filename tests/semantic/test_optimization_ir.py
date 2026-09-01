from dataclasses import replace
from pathlib import Path
import unittest

from zlang.opt import (
    CanonicalizationError,
    EquivalenceMode,
    NodeCategory,
    equivalence_definition,
    lower,
    restore,
)
from zlang.opt.ir import ExpressionOp, Observation
from zlang.parser import parse
from zlang.semantic import analyze


ROOT = Path(__file__).resolve().parents[2]


class CanonicalOptimizationSemanticTests(unittest.TestCase):
    def test_exact_typed_subexpressions_are_interned_deterministically(self) -> None:
        semantic = analyze(
            parse(
                "module Shared { in a:u8 out y:u9 out z:u9 "
                "y=a+a z=a+a }"
            )
        )
        first = lower(semantic)
        second = lower(semantic)

        self.assertEqual(first, second)
        self.assertEqual([node.op for node in first.expressions], [
            ExpressionOp.INPUT,
            ExpressionOp.ADD,
        ])
        self.assertEqual(first.assignments[0].expression, first.assignments[1].expression)
        self.assertEqual(restore(first), semantic)

    def test_node_categories_are_explicit_across_completed_features(self) -> None:
        sources = (
            ROOT / "examples/add.zl",
            ROOT / "examples/counter.zl",
            ROOT / "examples/rv_passthrough.zl",
            ROOT / "examples/fifo_bridge.zl",
            ROOT / "examples/request_client.zl",
        )
        categories = {
            category
            for path in sources
            for category in NodeCategory
            if lower(analyze(parse(path.read_text()))).nodes(category)
        }
        self.assertEqual(categories, set(NodeCategory))

    def test_equivalence_modes_have_distinct_observation_boundaries(self) -> None:
        mathematical = equivalence_definition(EquivalenceMode.MATHEMATICAL)
        cycle = equivalence_definition(EquivalenceMode.CYCLE_ACCURATE)
        observational = equivalence_definition(EquivalenceMode.OBSERVATIONAL)

        self.assertEqual(mathematical.observations, (Observation.TYPED_VALUE,))
        self.assertIn(Observation.CLOCK_EDGE, cycle.observations)
        self.assertIn(Observation.RESET, cycle.observations)
        self.assertIn(Observation.PORT, observational.observations)
        self.assertIn(Observation.PROTOCOL_EVENT, observational.observations)
        self.assertFalse(cycle.permits_internal_architecture_change)
        self.assertTrue(observational.permits_internal_architecture_change)

    def test_malformed_canonical_ids_and_targets_fail_explicitly(self) -> None:
        semantic = analyze(parse("module A { in x:u8 out y:u8 y=x }"))
        canonical = lower(semantic)
        bad_node = replace(canonical.expressions[0], id=1)
        with self.assertRaisesRegex(ValueError, "IDs must be contiguous"):
            replace(canonical, expressions=(bad_node,))

        bad_assignment = replace(canonical.assignments[0], target_name="missing")
        malformed = replace(canonical, assignments=(bad_assignment,))
        with self.assertRaisesRegex(
            CanonicalizationError, "missing port 'missing'"
        ):
            restore(malformed)

    def test_all_expression_families_have_lossless_canonical_forms(self) -> None:
        sources = (
            "module M { in c:bit in a:u8 in b:u8 out y:u8 y=mux(c,a,b) }",
            "module C { clock clk reset rst in p:u8 in request:bit "
            "out tx:credit<u8,2> tx.payload=p tx.send=request "
            "guarantee g @ clk disable iff rst "
            "{ tx.transfer == tx.send } }",
            "module P { clock clk reset rst in a:packet<u8> in b:packet<u8> "
            "out tx:packet<u8> arbiter [a,b] -> tx "
            "{ policy fixed_priority grant packet } "
            "guarantee g @ clk disable iff rst "
            "{ tx.transfer == (tx.valid & tx.ready) } }",
            "module V { clock clk reset rst in p:u8 in vc:u1 in request:bit "
            "out tx:vc_credit<u8,2,2> tx.payload=p tx.vc=vc tx.send=request "
            "guarantee g @ clk disable iff rst "
            "{ tx.transfer == tx.send } }",
        )
        seen: set[ExpressionOp] = set()
        for source in sources:
            semantic = analyze(parse(source))
            canonical = lower(semantic)
            self.assertEqual(restore(canonical), semantic)
            seen.update(node.op for node in canonical.expressions)
        self.assertTrue(
            {
                ExpressionOp.MUX,
                ExpressionOp.CREDIT_REF,
                ExpressionOp.PACKET_REF,
                ExpressionOp.VC_CREDIT_REF,
            }.issubset(seen)
        )


if __name__ == "__main__":
    unittest.main()
