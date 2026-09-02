from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashTypeTests(unittest.TestCase):
    def test_extended_add_golden_file_matches_emitter(self) -> None:
        generated = compile_source((ROOT / "examples/extended_add.zhl").read_text()).clash
        expected = (ROOT / "examples/generated/ExtendedAdd.hs").read_text()
        self.assertEqual(generated, expected)

    def test_signed_types_and_extension_lower_to_clash(self) -> None:
        source = "module SignedWide { in a: s8 out y: s16 y = extend<16>(a) }"
        generated = compile_source(source).clash
        self.assertIn("topEntity :: Signed 8 -> Signed 16", generated)
        self.assertIn("resize (a) :: Signed 16", generated)

    def test_bits_lower_to_bit_vector(self) -> None:
        source = "module Vector { in a: bits<64> out y: bits<8> y = truncate<8>(a) }"
        generated = compile_source(source).clash
        self.assertIn("topEntity :: BitVector 64 -> BitVector 8", generated)
        self.assertIn("resize (a) :: BitVector 8", generated)

    def test_bit_lowers_to_clash_bit(self) -> None:
        generated = compile_source(
            "module SingleBit { in a: bit out y: bit y = a }"
        ).clash
        self.assertIn("topEntity :: Bit -> Bit", generated)


if __name__ == "__main__":
    unittest.main()
