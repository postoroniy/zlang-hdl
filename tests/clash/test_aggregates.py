from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashAggregateTests(unittest.TestCase):
    def test_fir_golden_matches_emitter(self) -> None:
        source = (ROOT / "examples/fir2.zl").read_text()
        expected = (ROOT / "examples/generated/FIR2.hs").read_text()
        self.assertEqual(compile_source(source).clash, expected)

    def test_packet_golden_matches_emitter(self) -> None:
        source = (ROOT / "examples/packet_data.zl").read_text()
        expected = (ROOT / "examples/generated/PacketData.hs").read_text()
        self.assertEqual(compile_source(source).clash, expected)

    def test_vector_and_function_lowering(self) -> None:
        generated = compile_source((ROOT / "examples/fir2.zl").read_text()).clash
        self.assertIn("Vec 2 (Unsigned 8)", generated)
        self.assertIn("tap :: Unsigned 8 -> Unsigned 8 -> Unsigned 16", generated)
        self.assertIn("!! (0 :: Index 2)", generated)

    def test_struct_and_field_lowering(self) -> None:
        generated = compile_source(
            (ROOT / "examples/packet_data.zl").read_text()
        ).clash
        self.assertIn("data Packet = Packet", generated)
        self.assertIn("deriving (Generic, NFDataX, Show, Eq)", generated)
        self.assertIn("packet_data (packet)", generated)


if __name__ == "__main__":
    unittest.main()
