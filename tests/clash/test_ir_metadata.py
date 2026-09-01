from pathlib import Path
import unittest

from zlang.backend.clash import emit
from zlang.compiler import compile_source
from zlang.opt import restore


ROOT = Path(__file__).resolve().parents[2]


class CanonicalMetadataClashTests(unittest.TestCase):
    def test_metadata_example_matches_golden_after_selected_ir_restore(self) -> None:
        source = (ROOT / "examples/metadata_datapath.zl").read_text()
        compilation = compile_source(source)
        self.assertEqual(
            compilation.clash,
            (ROOT / "examples/generated/MetadataDatapath.hs").read_text(),
        )
        self.assertEqual(compilation.clash, emit(restore(compilation.optimization_ir)))


if __name__ == "__main__":
    unittest.main()
