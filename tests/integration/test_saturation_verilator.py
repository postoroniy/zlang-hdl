import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit
from zlang.compiler import compile_source
from zlang.opt import saturate
from zlang.toolchain import generate_verilog


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")


class EqualitySaturationVerilatorTests(unittest.TestCase):
    def test_strength_reduction_is_not_sent_to_verilator(self) -> None:
        compilation = compile_source(
            (ROOT / "examples/shift_multiply.zl").read_text()
        )
        root_id = compilation.optimization_ir.assignments[0].expression
        result = saturate(compilation.optimization_ir, root_id)
        self.assertEqual(result.alternatives, ())


if __name__ == "__main__":
    unittest.main()
