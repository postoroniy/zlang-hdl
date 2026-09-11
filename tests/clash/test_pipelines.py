from pathlib import Path
import unittest

from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]


class ClashPipelineTests(unittest.TestCase):
    def test_pipelined_mac_golden_matches_emitter(self) -> None:
        source = (ROOT / "examples/pipelined_mac.zhl").read_text()
        expected = (ROOT / "examples/generated/PipelinedMAC.hs").read_text()
        self.assertEqual(compile_source(source).clash, expected)

    def test_retired_clash_compatibility_keeps_exact_visible_latency(self) -> None:
        generated = compile_source(
            (ROOT / "examples/pipelined_mac.zhl").read_text()
        ).clash
        self.assertEqual(generated.count("y_pipe_s1 = register"), 1)
        self.assertEqual(generated.count("y_pipe_s2 = register"), 1)
        self.assertNotIn("y_pipe_s3", generated)
        self.assertIn("register (0 :: Unsigned 17)", generated)
        self.assertIn("<$> a <*> b <*> c", generated)
        self.assertNotIn("zlang_pipe_", generated)

    def test_auto_pipeline_golden_and_architecture_annotation_match(self) -> None:
        source = (ROOT / "examples/implementation_intent.zhl").read_text()
        result = compile_source(source)
        expected = (
            ROOT / "examples/generated/AutoPipelineProducts.hs"
        ).read_text()

        self.assertEqual(result.clash, expected)
        # The unified implementation selected the zero-cycle expression;
        # retained pipeline metadata is catalog-only and must not claim that
        # the emitted Clash circuit contains a one-cycle register.
        self.assertNotIn("ZLang implement pipeline candidate", result.clash)
        self.assertEqual(result.clash.count(" = register "), 0)

    def test_auto_pipeline_comment_is_emitted_only_for_selected_pipeline(self) -> None:
        source = """
        module TimedImplement {
          clock clk
          reset rst
          in a:u2
          in b:u2
          in c:u2
          in d:u2
          in e:u2
          in f:u2
          in g:u2
          in h:u2
          out y:u7
          y = implement {
            a*b+c*d+e*f+g*h
            intent { latency >= 1 ii == 1 dsp <= 4 fmax >= 100 minimize lut }
          }
        }
        """
        result = compile_source(source)
        self.assertIn(
            "ZLang implement pipeline candidate: output=y selected=linear_output_logic",
            result.clash,
        )


if __name__ == "__main__":
    unittest.main()
