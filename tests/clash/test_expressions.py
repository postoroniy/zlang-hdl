from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashExpressionTests(unittest.TestCase):
    def test_alu_golden_file_matches_emitter(self) -> None:
        generated = compile_source((ROOT / "examples/alu.zl").read_text()).clash
        self.assertEqual(generated, (ROOT / "examples/generated/ALU.hs").read_text())

    def test_comparison_and_mux_lower_to_clash(self) -> None:
        source = "module Min { in a:u8 in b:u8 out y:u8 y=mux(a < b,a,b) }"
        generated = compile_source(source).clash
        self.assertIn("if (boolToBit", generated)
        self.assertIn("== high then", generated)
        self.assertIn("<", generated)

    def test_shift_uses_explicit_shift_amount_conversion(self) -> None:
        source = "module Shift { in a:u8 in amount:u3 out y:u8 y=a << amount }"
        generated = compile_source(source).clash
        self.assertIn("shiftL", generated)
        self.assertIn("fromIntegral", generated)

    def test_constant_folding_reaches_backend(self) -> None:
        generated = compile_source("module Fold { out y:u5 y=3*4 }").clash
        self.assertIn("topEntity  = (12 :: Unsigned 5)", generated)
        self.assertNotIn(" * ", generated)


if __name__ == "__main__":
    unittest.main()
