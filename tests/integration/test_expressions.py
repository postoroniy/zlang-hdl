from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from zlang.compiler import compile_source
from zlang.simulate import simulate


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/alu.zhl").read_text()


class ExpressionIntegrationTests(unittest.TestCase):
    def test_alu_behavior(self) -> None:
        module = compile_source(SOURCE).ir
        vectors = [
            (7, 5, 0, 12),
            (0xFFFF_FFFF, 1, 0, 0),
            (3, 5, 1, 0xFFFF_FFFE),
            (0b1100, 0b1010, 2, 0b1000),
            (0b1100, 0b1010, 3, 0b1110),
            (1, 2, 7, 0),
        ]
        for a, b, op, expected in vectors:
            with self.subTest(a=a, b=b, op=op):
                self.assertEqual(simulate(module, a=a, b=b, op=op), {"y": expected})

    def test_mux_comparison_multiply_and_shifts(self) -> None:
        minimum = compile_source(
            "module Min { in a:u8 in b:u8 out y:u8 y=mux(a < b,a,b) }"
        ).ir
        multiply = compile_source(
            "module Mul { in a:u8 in b:u8 out y:u16 y=a*b }"
        ).ir
        left_shift = compile_source(
            "module Shift { in a:u8 in n:u3 out y:u8 y=a << n }"
        ).ir
        right_shift = compile_source(
            "module Shift { in a:s8 in n:u3 out y:s8 y=a >> n }"
        ).ir
        self.assertEqual(simulate(minimum, a=9, b=4), {"y": 4})
        self.assertEqual(simulate(multiply, a=255, b=255), {"y": 65025})
        self.assertEqual(simulate(left_shift, a=0x81, n=1), {"y": 2})
        self.assertEqual(simulate(right_shift, a=-8, n=2), {"y": -2})



if __name__ == "__main__":
    unittest.main()
