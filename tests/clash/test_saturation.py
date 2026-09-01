from pathlib import Path
import unittest

from zlang.compiler import compile_source
from zlang.opt import render_saturation, saturate


ROOT = Path(__file__).resolve().parents[2]


class EqualitySaturationClashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compilation = compile_source(
            (ROOT / "examples/shift_multiply.zl").read_text()
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

    def test_strength_reduction_is_excluded_from_the_m26_report(self) -> None:
        self.assertEqual(self.saturation.alternatives, ())


if __name__ == "__main__":
    unittest.main()
