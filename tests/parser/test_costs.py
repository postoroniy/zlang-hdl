from pathlib import Path
import unittest

from zlang.ast.nodes import (
    CostMetric,
    ImplementationChoiceExpr,
    SynthesisFeedback,
)
from zlang.parser import ParseError, parse


ROOT = Path(__file__).resolve().parents[2]


class CostPolicyParserTests(unittest.TestCase):
    def test_auto_goal_and_every_constraint_are_syntax_ast(self) -> None:
        module = parse((ROOT / "examples/cost_mac.zhl").read_text())
        choice = module.assignments[0].expression

        self.assertIsInstance(choice, ImplementationChoiceExpr)
        self.assertIsNone(choice.selected)
        self.assertIsNotNone(choice.cost_policy)
        assert choice.cost_policy is not None
        self.assertEqual(choice.cost_policy.goal, CostMetric.LUT)
        self.assertEqual(
            choice.cost_policy.feedback,
            SynthesisFeedback.OPTIONAL_YOSYS,
        )
        self.assertEqual(
            [constraint.metric for constraint in choice.cost_policy.constraints],
            [
                CostMetric.LUT,
                CostMetric.FF,
                CostMetric.DSP,
                CostMetric.BRAM,
                CostMetric.LATENCY,
                CostMetric.INITIATION_INTERVAL,
            ],
        )

    def test_auto_requires_a_known_minimization_metric(self) -> None:
        cases = (
            "choice(auto){mul_add=>a*b+c dsp_mac=>a*b+c}",
            "choice(auto,minimize=power){mul_add=>a*b+c dsp_mac=>a*b+c}",
        )
        for choice in cases:
            with self.subTest(choice=choice):
                with self.assertRaises(ParseError):
                    parse(
                        "module Bad { in a:u8 in b:u8 in c:u16 out y:u17 "
                        f"y={choice} }}"
                    )

    def test_every_cost_metric_is_accepted_as_a_goal(self) -> None:
        for metric in CostMetric:
            with self.subTest(metric=metric.value):
                module = parse(
                    "module Goals { in a:u8 in b:u8 in c:u16 out y:u17 "
                    f"y=choice(auto,minimize={metric.value}){{"
                    "mul_add=>a*b+c dsp_mac=>a*b+c} }"
                )
                choice = module.assignments[0].expression
                self.assertIsInstance(choice, ImplementationChoiceExpr)
                assert isinstance(choice, ImplementationChoiceExpr)
                assert choice.cost_policy is not None
                self.assertEqual(choice.cost_policy.goal, metric)

    def test_unknown_synthesis_feedback_policy_is_rejected(self) -> None:
        with self.assertRaises(ParseError):
            parse(
                "module BadFeedback { in a:u8 in b:u8 in c:u16 out y:u17 "
                "y=choice(auto,minimize=lut,feedback=vendor){"
                "mul_add=>a*b+c dsp_mac=>a*b+c} }"
            )


if __name__ == "__main__":
    unittest.main()
