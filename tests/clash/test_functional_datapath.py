from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class FunctionalDatapathClashTests(unittest.TestCase):
    def test_functional_examples_match_clash_goldens(self) -> None:
        for source_name, golden_name in (
            ("dot_product.zhl", "DotProduct.hs"),
            ("dot_builtin.zhl", "DotBuiltin.hs"),
            ("generated_reduce.zhl", "GeneratedReduce.hs"),
            ("mapped_sum.zhl", "MappedSum.hs"),
        ):
            with self.subTest(source=source_name):
                generated = compile_source(
                    (ROOT / "examples" / source_name).read_text()
                ).clash
                expected = (ROOT / "examples/generated" / golden_name).read_text()
                self.assertEqual(generated, expected)

    def test_backend_materializes_balanced_tree_after_high_level_ir(self) -> None:
        generated = compile_source(
            (ROOT / "examples/dot_builtin.zhl").read_text()
        ).clash
        self.assertIn("Vec 8 (Unsigned 8)", generated)
        self.assertIn("Unsigned 19", generated)
        self.assertEqual(generated.count(":: Index 8"), 16)


if __name__ == "__main__":
    unittest.main()
