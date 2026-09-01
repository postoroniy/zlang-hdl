from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashCsrTests(unittest.TestCase):
    def test_csr_golden_matches_emitter(self) -> None:
        result = compile_source((ROOT / "examples/control_csr.zl").read_text())
        expected = (ROOT / "examples/generated/ControlCsr.hs").read_text()
        self.assertEqual(result.clash, expected)
        self.assertIn("csr_control_control_enable", result.clash)
        self.assertIn("complement incoming", result.clash)
        self.assertIn("1073741828 -> word1", result.clash)
        self.assertEqual(
            result.csr_json,
            (ROOT / "examples/generated/ControlCsr.json").read_text(),
        )
        self.assertEqual(
            result.csr_markdown,
            (ROOT / "examples/generated/ControlCsr.md").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
