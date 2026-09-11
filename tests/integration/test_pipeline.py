from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from zlang.compiler import compile_source
from zlang.simulate import SimulationError, simulate


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/add.zhl").read_text()


class PipelineTests(unittest.TestCase):
    def test_representative_add_values_including_carry(self) -> None:
        module = compile_source(SOURCE).ir
        vectors = [
            (0, 0, 0),
            (1, 2, 3),
            (255, 1, 256),
            (255, 255, 510),
        ]
        for a, b, expected in vectors:
            with self.subTest(a=a, b=b):
                self.assertEqual(simulate(module, a=a, b=b), {"y": expected})

    def test_out_of_range_input_is_rejected(self) -> None:
        module = compile_source(SOURCE).ir
        with self.assertRaisesRegex(SimulationError, "does not fit u8"):
            simulate(module, a=256, b=0)




if __name__ == "__main__":
    unittest.main()
