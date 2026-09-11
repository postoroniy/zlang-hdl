import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from zlang.compiler import compile_source
from zlang.opt import saturate
from zlang.opt import RewriteRule


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")


class EqualitySaturationVerilatorTests(unittest.TestCase):
    def test_exact_strength_reduction_is_available_to_implementation_planning(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/shift_multiply.zhl").read_text()
        )
        root_id = compilation.optimization_ir.assignments[0].expression
        result = saturate(compilation.optimization_ir, root_id)
        self.assertGreaterEqual(len(result.alternatives), 1)
        self.assertIn(RewriteRule.MULTIPLY_POWER_OF_TWO, result.rules)


if __name__ == "__main__":
    unittest.main()
