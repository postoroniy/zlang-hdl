import unittest
from pathlib import Path
import tempfile

from zlang.cli import main
from zlang.compiler import compile_source
from zlang.parser import ParseError, parse
from zlang.semantic.analyze import SemanticError


class ExplorationM34Tests(unittest.TestCase):
    def test_cli_writes_unified_implementation_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "example.zhl"
            output = root / "Example.hs"
            report = root / "Example.explore"
            source.write_text(
                "module Example { in a:u8 out y:u8 "
                "y=implement { a intent { minimize lut } } }"
            )
            self.assertEqual(
                main([str(source), "-o", str(output), "--exploration-report", str(report)]),
                0,
            )
            self.assertIn("Implementation selection", report.read_text())

    def test_implement_clauses_compile_to_one_request(self):
        source = """
        module ImplementDemo {
          in a: u8
          out y: u8
          y = implement { a intent { lut <= 4 dsp <= 1 minimize lut } }
        }
        """
        syntax = parse(source)
        intent = syntax.assignments[0].expression
        self.assertEqual(
            tuple((item.metric.value, item.relation.value, item.value)
                  for item in intent.constraints),
            (("lut", "<=", 4), ("dsp", "<=", 1)),
        )
        result = compile_source(source)
        self.assertEqual(result.ir.assignments[0].expression.type.width, 8)
        self.assertEqual(len(result.exploration_results), 1)

    def test_removed_scalar_forms_have_migration_diagnostics(self):
        for spelling in ("explore { a }", "pipeline(auto){a}", "architecture(auto){a}"):
            with self.assertRaisesRegex(ParseError, "removed"):
                parse(f"module Removed {{ in a:u8 out y:u8 y={spelling} }}")

    def test_defaults_use_exact_value_rewrites_without_pipeline(self):
        result = compile_source(
            "module ImplementValue { in a:u8 out y:u8 "
            "y=implement { a ^ 0 intent { minimize lut } } }"
        )
        self.assertEqual(result.ir.assignments[0].expression.name, "a")
        request = result.exploration_results[0].request
        self.assertNotIn("pipeline", {item.value for item in request.allowed})

    def test_dsp_permission_is_default_for_implement(self):
        result = compile_source(
            "module ImplementMac { in a:u4 in b:u4 in c:u8 out y:u9 "
            "y=implement { a*b+c intent { dsp <= 1 minimize lut } } }"
        )
        candidates = result.exploration_results[0].generated_candidates
        self.assertTrue(any("dsp_mac" in item.stages for item in candidates))

    def test_reduction_depth_is_not_cycle_latency(self):
        result = compile_source(
            "module ImplementDot { in a:vec<4,u3> in b:vec<4,u3> out y:u8 "
            "y=implement { dot(a,b) intent { minimize lut } } }"
        )
        reduction_candidates = tuple(
            item for item in result.exploration_results[0].generated_candidates
            if any(stage.startswith("reduction:") for stage in item.stages)
        )
        self.assertTrue(reduction_candidates)
        self.assertEqual({item.cost.latency.value for item in reduction_candidates}, {0})

    def test_positive_latency_requires_clock_and_materializes_pipeline(self):
        expression = "a*b+c*d+e*f+g*h"
        without_clock = (
            "module P { in a:u2 in b:u2 in c:u2 in d:u2 in e:u2 in f:u2 "
            "in g:u2 in h:u2 out y:u7 y=implement { "
            + expression + " intent { latency >= 1 minimize ff } } }"
        )
        with self.assertRaisesRegex(SemanticError, "module clock and reset"):
            compile_source(without_clock)
        with_clock = without_clock.replace("module P {", "module P { clock clk reset rst")
        result = compile_source(with_clock)
        self.assertTrue(any(item.timing_relation is not None
                            for item in result.exploration_results[0].generated_candidates))

    def test_non_reduction_implement_reuses_general_dag_scheduler(self):
        result = compile_source(
            "module GeneralDag { clock clk reset rst "
            "in a:u8 in b:u8 in c:u8 in d:u8 in e:u8 in f:u25 out y:u26 "
            "y=implement { (a*b+c*d)*e+f intent { "
            "latency == 3 ii == 1 maximize fmax } } }",
            include_clash=False,
        )
        selected = result.exploration_results[0].selected_candidate
        self.assertIn("pipeline:dag_partition_3", selected.stages)
        self.assertEqual(selected.cost.latency.value, 3)
        self.assertEqual(selected.architecture.tree.value, "dag")
        self.assertEqual(
            selected.architecture.pipeline_plan.scheduler,
            "dag_partition_v1",
        )
        self.assertIn("reason=emitted_selected_candidate", result.pipeline_report)

    def test_general_dag_latency_search_is_bounded_and_keeps_endpoints(self):
        result = compile_source(
            "module GeneralRange { clock clk reset rst "
            "in a:u8 in b:u8 in c:u8 out y:u17 "
            "y=implement { (a+b)*c intent { "
            "latency >= 2 ii == 1 minimize ff } } }",
            include_clash=False,
        )
        latencies = {
            item.architecture.latency
            for item in result.exploration_results[0].generated_candidates
            if getattr(item.architecture, "tree", None) is not None
            and item.architecture.tree.value == "dag"
        }
        self.assertEqual(latencies, {2, 3, 4, 5})

    def test_general_dag_timing_failure_is_bounded_and_evidence_honest(self):
        source = (
            "module TooFast { clock clk reset rst in a,b,c:u32 out y:u96 "
            "y=implement { (a*b)*c intent { "
            "latency <= 2 ii == 1 fmax >= 5000 maximize fmax } } }"
        )
        with self.assertRaises(SemanticError) as raised:
            compile_source(source, include_clash=False)
        message = str(raised.exception)
        self.assertIn("expression critical stage", message)
        self.assertIn("requested Fmax 5000 MHz", message)
        self.assertIn("structural_estimate", message)
        self.assertIn("not a synthesis or routed measurement", message)
        self.assertLess(len(message), 2_000)

    def test_combined_reduction_dsp_pipeline_is_bounded(self):
        result = compile_source(
            "module Combined { clock clk reset rst "
            "in a:vec<4,u3> in b:vec<4,u3> out y:u8 "
            "y=implement { dot(a,b) intent { latency >= 1 dsp <= 4 minimize lut } } }"
        )
        exploration = result.exploration_results[0]
        self.assertIn("reduction", dict(exploration.stage_counts))
        self.assertIn("pipeline", dict(exploration.stage_counts))
        self.assertLessEqual(len(exploration.generated_candidates), exploration.request.bounds.max_candidates)

    def test_implement_rejects_nested_selection_and_bad_objective(self):
        with self.assertRaisesRegex(ParseError, "removed"):
            parse("module N { in a:u8 out y:u8 y=explore { explore { a } } }")
        with self.assertRaisesRegex(SemanticError, "maximize currently supports"):
            compile_source("module Max { in a:u8 out y:u8 y=implement { a intent { maximize lut } } }")


if __name__ == "__main__":
    unittest.main()
