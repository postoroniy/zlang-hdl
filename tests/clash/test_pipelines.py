from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashPipelineTests(unittest.TestCase):
    def test_pipelined_mac_golden_matches_emitter(self) -> None:
        source = (ROOT / "examples/pipelined_mac.zl").read_text()
        expected = (ROOT / "examples/generated/PipelinedMAC.hs").read_text()
        self.assertEqual(compile_source(source).clash, expected)

    def test_fixed_pipeline_emits_exact_stage_count(self) -> None:
        generated = compile_source(
            (ROOT / "examples/pipelined_mac.zl").read_text()
        ).clash
        self.assertEqual(generated.count("pipeline_0_s1 = register"), 1)
        self.assertEqual(generated.count("pipeline_0_s2 = register"), 1)
        self.assertNotIn("pipeline_0_s3", generated)

    def test_auto_pipeline_golden_and_architecture_annotation_match(self) -> None:
        source = (ROOT / "examples/auto_pipeline_products.zl").read_text()
        result = compile_source(source)
        expected = (
            ROOT / "examples/generated/AutoPipelineProducts.hs"
        ).read_text()

        self.assertEqual(result.clash, expected)
        self.assertIn(
            "selected=balanced_levels_dsp tree=balanced ",
            result.clash,
        )
        self.assertEqual(result.clash.count(" = register "), 7)


if __name__ == "__main__":
    unittest.main()
