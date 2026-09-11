from pathlib import Path
import shutil
import tempfile
import unittest

from zlang.compiler import compile_source
from zlang.simulate import simulate
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]


class FunctionalDatapathIntegrationTests(unittest.TestCase):
    def test_indexed_sum_and_dot_are_behaviorally_identical(self) -> None:
        indexed = compile_source((ROOT / "examples/dot_product.zhl").read_text()).ir
        builtin = compile_source((ROOT / "examples/dot_builtin.zhl").read_text()).ir
        vectors = (
            ([0] * 8, [0] * 8, 0),
            ([1, 2, 3, 4, 5, 6, 7, 8], [8, 7, 6, 5, 4, 3, 2, 1], 120),
            ([255] * 8, [255] * 8, 520200),
        )
        for a, b, expected in vectors:
            with self.subTest(expected=expected):
                self.assertEqual(simulate(indexed, a=a, b=b), {"y": expected})
                self.assertEqual(simulate(builtin, a=a, b=b), {"y": expected})

    def test_generate_map_and_general_reduce_behavior(self) -> None:
        generated = compile_source(
            (ROOT / "examples/generated_reduce.zhl").read_text()
        ).ir
        mapped = compile_source((ROOT / "examples/mapped_sum.zhl").read_text()).ir
        self.assertEqual(
            simulate(generated, a=[1, 2, 3, 4], b=[5, 6, 7, 8]),
            {"y": 70},
        )
        self.assertEqual(simulate(mapped, values=[1, 2, 3, 4]), {"y": 20})



if __name__ == "__main__":
    unittest.main()
