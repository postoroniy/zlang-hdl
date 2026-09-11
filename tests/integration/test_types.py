from pathlib import Path
import subprocess
import tempfile
import unittest

from zlang.compiler import compile_source
from zlang.simulate import SimulationError, simulate


ROOT = Path(__file__).resolve().parents[2]


class HardwareTypeIntegrationTests(unittest.TestCase):
    def test_extended_add_example_behavior(self) -> None:
        source = (ROOT / "examples/extended_add.zhl").read_text()
        module = compile_source(source).ir
        self.assertEqual(simulate(module, a=255, b=255), {"y": 510})

    def test_signed_range_and_truncation_behavior(self) -> None:
        source = "module SignedNarrow { in a: s16 out y: s8 y = truncate<8>(a) }"
        module = compile_source(source).ir
        self.assertEqual(simulate(module, a=-129), {"y": 127})
        self.assertEqual(simulate(module, a=255), {"y": -1})
        with self.assertRaisesRegex(SimulationError, "does not fit s16"):
            simulate(module, a=32768)

    def test_bit_and_bit_vector_input_ranges(self) -> None:
        bit_module = compile_source(
            "module SingleBit { in a: bit out y: bit y = a }"
        ).ir
        vector_module = compile_source(
            "module Vector { in a: bits<3> out y: bits<3> y = a }"
        ).ir
        with self.assertRaisesRegex(SimulationError, "does not fit bit"):
            simulate(bit_module, a=2)
        with self.assertRaisesRegex(SimulationError, "does not fit bits<3>"):
            simulate(vector_module, a=8)



if __name__ == "__main__":
    unittest.main()
