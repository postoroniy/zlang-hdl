from pathlib import Path
import unittest

from zlang.ir import expressions as expr
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class CostPolicySemanticTests(unittest.TestCase):
    def test_auto_policy_is_typed_and_round_trips_before_extraction(self) -> None:
        semantic = analyze(parse((ROOT / "examples/cost_mac.zhl").read_text()))
        choice = semantic.assignments[0].expression

        self.assertIsInstance(choice, expr.ImplementationChoice)
        self.assertIsNone(choice.selected)
        self.assertIsNotNone(choice.cost_policy)
        assert choice.cost_policy is not None
        self.assertEqual(choice.cost_policy.goal, expr.CostMetric.LUT)
        self.assertEqual(
            choice.cost_policy.feedback,
            expr.SynthesisFeedback.OPTIONAL_YOSYS,
        )
        self.assertTrue(all(item.estimate is None for item in choice.alternatives))
        self.assertEqual(restore(lower(semantic)), semantic)

    def test_duplicate_hard_constraint_is_rejected(self) -> None:
        source = (
            "module Duplicate { in a:u8 in b:u8 in c:u16 out y:u17 "
            "y=choice(auto,minimize=lut,dsp<=1,dsp<=0){"
            "mul_add=>a*b+c dsp_mac=>a*b+c} }"
        )
        with self.assertRaisesRegex(SemanticError, "repeats 'dsp' constraint"):
            analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
