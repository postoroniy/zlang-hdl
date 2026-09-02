from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class HardwareCsrClashTests(unittest.TestCase):
    def test_engine_csr_golden_and_artifacts_match(self) -> None:
        result = compile_source((ROOT / "examples/engine_csr.zhl").read_text())
        self.assertEqual(
            result.clash,
            (ROOT / "examples/generated/EngineCsr.hs").read_text(),
        )
        self.assertEqual(
            result.csr_json,
            (ROOT / "examples/generated/EngineCsr.json").read_text(),
        )
        self.assertEqual(
            result.csr_markdown,
            (ROOT / "examples/generated/EngineCsr.md").read_text(),
        )
        self.assertIn("pack <$> engine_busy", result.clash)
        self.assertIn(".|. hardwareSet", result.clash)
        self.assertIn("engine_start = unpack", result.clash)


if __name__ == "__main__":
    unittest.main()
