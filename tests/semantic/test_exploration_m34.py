import unittest
from pathlib import Path
import tempfile

from zlang.cli import main
from zlang.compiler import compile_source
from zlang.parser import ParseError, parse
from zlang.semantic.analyze import SemanticError


class ExplorationM34Tests(unittest.TestCase):
    def test_cli_writes_unified_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "example.zl"
            output = root / "Example.hs"
            report = root / "Example.explore"
            source.write_text(
                "module Example { in a:u8 out y:u8 "
                "y=explore { a minimize lut } }"
            )
            self.assertEqual(
                main(
                    [
                        str(source),
                        "-o",
                        str(output),
                        "--exploration-report",
                        str(report),
                    ]
                ),
                0,
            )
            self.assertIn("Exploration: result", report.read_text())

    def test_clauses_parse_and_compile_to_one_request(self):
        source = """
        module ExploreDemo {
          in a: u8
          out y: u8
          y = explore { a allow { reduction, dsp } require { lut <= 4, dsp <= 1 } minimize lut }
        }
        """
        syntax = parse(source)
        clause = syntax.assignments[0].expression
        self.assertEqual(
            tuple(item.value for item in clause.allowed),
            ("reduction", "dsp"),
        )
        result = compile_source(source)
        self.assertEqual(result.ir.assignments[0].expression.type.width, 8)
        self.assertEqual(len(result.exploration_results), 1)
        self.assertIn("best candidate in explored bounded search", result.exploration_report)

    def test_allow_avoid_conflict_is_diagnostic(self):
        source = """
        module ExploreConflict {
          in a: u8
          out y: u8
          y = explore { a allow reduction avoid reduction }
        }
        """
        with self.assertRaisesRegex(SemanticError, "both allowed and avoided"):
            compile_source(source)

    def test_defaults_use_m26_value_rewrites_without_architecture_change(self):
        result = compile_source(
            "module ExploreValue { in a:u8 out y:u8 "
            "y=explore { a ^ 0 minimize lut } }"
        )
        self.assertEqual(result.ir.assignments[0].expression.name, "a")
        selected = result.exploration_results[0].selected_candidate
        self.assertIn("M26/M27", selected.value_relation)
        self.assertEqual(selected.cost.latency.value, 0)

    def test_dsp_permission_generates_m29_candidates(self):
        result = compile_source(
            "module ExploreMac { in a:u4 in b:u4 in c:u8 out y:u9 "
            "y=explore { a*b+c allow dsp require dsp <= 1 minimize lut } }"
        )
        candidates = result.exploration_results[0].generated_candidates
        self.assertTrue(any("dsp_mac" in item.stages for item in candidates))
        self.assertTrue(
            any(item.architecture is not None for item in candidates)
        )

    def test_reduction_depth_is_not_misreported_as_cycle_latency(self):
        result = compile_source(
            "module ExploreDot { in a:vec<4,u3> in b:vec<4,u3> out y:u8 "
            "y=explore { dot(a,b) allow reduction minimize lut } }"
        )
        reduction_candidates = tuple(
            item for item in result.exploration_results[0].generated_candidates
            if any(stage.startswith("reduction:") for stage in item.stages)
        )
        self.assertTrue(reduction_candidates)
        self.assertEqual(
            {item.cost.latency.value for item in reduction_candidates}, {0}
        )

    def test_pipeline_requires_clock_and_materializes_selected_timing(self):
        expression = "a*b+c*d+e*f+g*h"
        without_clock = (
            "module P { in a:u2 in b:u2 in c:u2 in d:u2 in e:u2 in f:u2 "
            "in g:u2 in h:u2 out y:u7 y=explore { " + expression
            + " allow pipeline minimize ff } }"
        )
        with self.assertRaisesRegex(SemanticError, "module clock and reset"):
            compile_source(without_clock)

        with_clock = without_clock.replace(
            "module P {", "module P { clock clk reset rst"
        ).replace(
            "allow pipeline minimize ff",
            "allow { pipeline, reassociate } require latency >= 1 minimize ff",
        )
        result = compile_source(with_clock)
        candidates = result.exploration_results[0].generated_candidates
        self.assertTrue(any(item.timing_relation is not None for item in candidates))
        self.assertGreater(
            result.exploration_results[0].selected_candidate.cost.latency.value,
            0,
        )

    def test_combined_reduction_dsp_pipeline_is_staged_and_bounded(self):
        result = compile_source(
            "module Combined { clock clk reset rst "
            "in a:vec<4,u3> in b:vec<4,u3> out y:u8 "
            "y=explore { dot(a,b) allow { reduction dsp pipeline reassociate } "
            "require { latency >= 1 dsp <= 4 } minimize lut } }"
        )
        exploration = result.exploration_results[0]
        stages = dict(exploration.stage_counts)
        self.assertIn("reduction", stages)
        self.assertIn("pipeline", stages)
        self.assertLessEqual(
            len(exploration.generated_candidates),
            exploration.request.bounds.max_candidates,
        )
        self.assertGreater(exploration.selected_candidate.cost.latency.value, 0)

    def test_nested_explore_and_duplicate_options_are_rejected(self):
        with self.assertRaisesRegex(SemanticError, "nested explore"):
            compile_source(
                "module N { in a:u8 out y:u8 "
                "y=explore { explore { a } } }"
            )
        with self.assertRaisesRegex(ParseError, "repeats 'lut' constraint"):
            parse(
                "module D { in a:u8 out y:u8 "
                "y=explore { a require lut <= 2 require lut <= 3 } }"
            )
        with self.assertRaisesRegex(ParseError, "exactly one objective"):
            parse(
                "module O { in a:u8 out y:u8 "
                "y=explore { a minimize lut minimize ff } }"
            )

    def test_adapter_is_not_silently_applied_to_value_exploration(self):
        with self.assertRaisesRegex(SemanticError, "protocol-connection"):
            compile_source(
                "module A { in a:u8 out y:u8 "
                "y=explore { a allow adapter } }"
            )

    def test_maximize_non_fmax_is_rejected(self):
        with self.assertRaisesRegex(SemanticError, "maximize currently supports"):
            compile_source(
                "module ExploreMax { in a:u8 out y:u8 "
                "y=explore { a maximize lut } }"
            )


if __name__ == "__main__":
    unittest.main()
