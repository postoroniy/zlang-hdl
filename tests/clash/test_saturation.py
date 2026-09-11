from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.opt import RewriteRule, render_saturation, saturate


ROOT = Path(__file__).resolve().parents[2]


class EqualitySaturationClashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compilation = compile_source(
            (ROOT / "examples/shift_multiply.zhl").read_text()
        )
        root = self.compilation.optimization_ir.assignments[0].expression
        self.saturation = saturate(self.compilation.optimization_ir, root)

    def test_unoptimized_clash_and_saturation_report_match_goldens(self) -> None:
        self.assertEqual(
            self.compilation.clash,
            (ROOT / "examples/generated/ShiftMultiply.hs").read_text(),
        )
        self.assertEqual(
            render_saturation(self.saturation),
            (ROOT / "examples/generated/ShiftMultiply.saturation").read_text(),
        )

    def test_exact_strength_reduction_is_reported_independently_of_clash(self) -> None:
        self.assertGreaterEqual(len(self.saturation.alternatives), 1)
        self.assertIn(RewriteRule.MULTIPLY_POWER_OF_TWO, self.saturation.rules)


if __name__ == "__main__":
    unittest.main()
