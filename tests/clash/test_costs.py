from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.costs import CostExtractionError
from zlang.ir import expressions as expr


ROOT = Path(__file__).resolve().parents[2]


class EstimatedCostExtractionTests(unittest.TestCase):
    def test_minimum_lut_candidate_is_selected_and_matches_golden(self) -> None:
        compilation = compile_source((ROOT / "examples/cost_mac.zl").read_text())
        choice = compilation.ir.assignments[0].expression

        self.assertIsInstance(choice, expr.ImplementationChoice)
        self.assertEqual(choice.selected, expr.ImplementationKind.DSP_MAC)
        estimates = {
            alternative.kind: alternative.estimate
            for alternative in choice.alternatives
        }
        self.assertEqual(
            estimates[expr.ImplementationKind.MUL_ADD],
            expr.ImplementationCostEstimate(81, 17, 0, 0, 1, 1),
        )
        self.assertEqual(
            estimates[expr.ImplementationKind.DSP_MAC],
            expr.ImplementationCostEstimate(17, 17, 1, 0, 1, 1),
        )
        self.assertEqual(
            compilation.clash,
            (ROOT / "examples/generated/CostMac.hs").read_text(),
        )
        self.assertIn("let zlangDspMac", compilation.clash)

    def test_no_dsp_constraint_selects_logic_and_matches_golden(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/cost_mac_no_dsp.zl").read_text()
        )
        choice = compilation.ir.assignments[0].expression

        self.assertIsInstance(choice, expr.ImplementationChoice)
        self.assertEqual(choice.selected, expr.ImplementationKind.MUL_ADD)
        self.assertEqual(
            compilation.clash,
            (ROOT / "examples/generated/CostMacNoDsp.hs").read_text(),
        )
        self.assertNotIn("zlangDspMac", compilation.clash)

    def test_ties_use_unified_m28_resource_tie_break(self) -> None:
        source = (
            "module Tie { in a:u8 in b:u8 in c:u16 out y:u17 "
            "y=choice(auto,minimize=ff){"
            "mul_add=>a*b+c dsp_mac=>a*b+c} }"
        )
        choice = compile_source(source).ir.assignments[0].expression
        self.assertIsInstance(choice, expr.ImplementationChoice)
        self.assertEqual(choice.selected, expr.ImplementationKind.MUL_ADD)

    def test_constraint_failure_explains_every_candidate(self) -> None:
        source = (
            "module Impossible { in a:u8 in b:u8 in c:u16 out y:u17 "
            "y=choice(auto,minimize=lut,lut<=10,dsp<=0){"
            "mul_add=>a*b+c dsp_mac=>a*b+c} }"
        )
        with self.assertRaisesRegex(
            CostExtractionError,
            r"no legal implementation.*dsp_mac violates lut=17 > 10, dsp=1 > 0; "
            r"mul_add violates lut=81 > 10",
        ):
            compile_source(source)


if __name__ == "__main__":
    unittest.main()
