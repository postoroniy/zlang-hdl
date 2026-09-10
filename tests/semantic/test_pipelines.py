from pathlib import Path
import unittest

from zlang.ir.expressions import Pipeline, sequential_stage_count
from zlang.ir.expressions import CostMetric
from zlang.ir.pipelines import (
    MultiplierMapping,
    PipelineConstraint,
    PipelineMetric,
    PipelineRelation,
    RegisterPlacement,
)
from zlang.opt import lower, restore
from zlang.ir.types import UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.pipelines import pipeline_constraint_to_unified


ROOT = Path(__file__).resolve().parents[2]


class PipelineSemanticTests(unittest.TestCase):
    def test_shared_pipeline_constraint_conversion_preserves_exact_relation(self) -> None:
        constraint = pipeline_constraint_to_unified(
            PipelineConstraint(
                PipelineMetric.LATENCY,
                PipelineRelation.EXACT,
                3,
            )
        )
        self.assertIs(constraint.metric, CostMetric.LATENCY)
        self.assertEqual((constraint.minimum, constraint.maximum), (3, 3))

    def test_pipeline_is_typed_and_records_stage_count(self) -> None:
        module = analyze(parse((ROOT / "examples/pipelined_mac.zhl").read_text()))
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, Pipeline)
        self.assertEqual(sequential_stage_count(expression), expression.stages)
        self.assertEqual(expression.stages, 2)
        self.assertEqual(expression.type, UIntType(17))

    def test_pipeline_requires_clock_and_reset(self) -> None:
        source = "module Bad { in a:u8 out y:u8 y=pipeline(1){a} }"
        with self.assertRaisesRegex(SemanticError, "pipeline requires a module clock"):
            analyze(parse(source))

    def test_implement_pipeline_selection_is_preserved(self) -> None:
        module = analyze(parse((ROOT / "examples/implementation_intent.zhl").read_text()))
        exploration = module.pipeline_explorations[0]
        self.assertEqual(exploration.output, "y")
        # The canonical default policy does not opt into finite-width
        # reassociation, so only the exact linear pipeline remains eligible.
        self.assertEqual(exploration.search_bound, 1)
        self.assertEqual(exploration.selected, "linear_output_logic")
        self.assertEqual(exploration.selected_candidate.latency, 1)
        self.assertEqual(exploration.selected_candidate.initiation_interval, 1)
        self.assertIs(exploration.selected_candidate.register_placement,
                      RegisterPlacement.OUTPUT)
        self.assertIs(exploration.selected_candidate.multiplier_mapping,
                      MultiplierMapping.LOGIC)
        # The unified implementation extractor may retain the zero-cycle
        # source/value candidate for the requested LUT objective; the
        # pipeline planner still records its legal timed candidate set.
        self.assertEqual(module.assignments[0].expression.type, UIntType(19))
        self.assertEqual(restore(lower(module)), module)

    def test_implement_without_latency_does_not_search_pipeline(self) -> None:
        module = analyze(parse(
            "module Value { in a:u8 out y:u8 "
            "y=implement { a ^ 0 intent { minimize lut } } }"
        ))
        self.assertEqual(module.pipeline_explorations, ())

    def test_implement_positive_latency_requires_clock(self) -> None:
        source = (
            "module Bad { in a:u8 out y:u8 "
            "y=implement { a intent { latency <= 3 } } }"
        )
        with self.assertRaisesRegex(SemanticError, "module clock and reset"):
            analyze(parse(source))

    def test_implement_positive_latency_searches_legal_candidates(self) -> None:
        source = (
            "module Auto { clock clk reset rst "
            "in a:u8 in b:u8 in c:u8 in d:u8 in e:u8 in f:u8 "
            "in g:u8 in h:u8 out y:u19 "
            "y=implement { a*b+c*d+e*f+g*h "
            "intent { latency <= 3 ii == 1 dsp <= 4 fmax >= 100 } } }"
        )
        module = analyze(parse(source))
        self.assertTrue(module.pipeline_explorations)
        self.assertTrue(any(item.latency > 0
                            for item in module.pipeline_explorations[0].candidates))

    def test_fixed_pipeline_rejects_auto_constraints(self) -> None:
        source = (
            "module Bad { clock c reset r in a:u8 out y:u8 "
            "y=pipeline(1, latency<=2){a} }"
        )
        with self.assertRaisesRegex(SemanticError, "fixed pipeline stages do not accept"):
            analyze(parse(source))

    def test_misaligned_binary_operands_are_rejected(self) -> None:
        source = """
            module Bad { clock c reset r in a:u8 in b:u8 out y:u9
                y = delay<1>(a) + b
            }
        """
        with self.assertRaisesRegex(SemanticError, "latency mismatch"):
            analyze(parse(source))

    def test_misaligned_mux_is_rejected(self) -> None:
        source = """
            module Bad { clock c reset r in pick:bit in a:u8 in b:u8 out y:u8
                y = mux(pick, delay<1>(a), b)
            }
        """
        with self.assertRaisesRegex(SemanticError, "latency mismatch in mux"):
            analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
