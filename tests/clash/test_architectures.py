from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashArchitectureTests(unittest.TestCase):
    def test_selected_architecture_matches_golden_clash(self) -> None:
        result = compile_source(
            (ROOT / "examples/fir_architecture.zhl").read_text()
        )
        expected = (
            ROOT / "examples/generated/FirArchitecture.hs"
        ).read_text()

        self.assertEqual(result.clash, expected)
        self.assertIn(
            "selected=folded_p2 kind=folded parallelism=2 add_depth=2",
            result.clash,
        )
        self.assertIn("samples) !! (2 :: Index 4)", result.clash)


if __name__ == "__main__":
    unittest.main()
