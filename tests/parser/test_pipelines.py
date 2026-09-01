from pathlib import Path
import unittest

from zlang.ast.nodes import (
    PipelineExpr,
    PipelineMetric,
    PipelineRelation,
)
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class PipelineParserTests(unittest.TestCase):
    def test_fixed_pipeline_parses(self) -> None:
        module = parse((ROOT / "examples/pipelined_mac.zl").read_text())
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, PipelineExpr)
        self.assertEqual(expression.stages, 2)

    def test_auto_pipeline_parses_constraints(self) -> None:
        source = (
            "module Auto { clock c reset r in a:u8 out y:u8 "
            "y=pipeline(auto, latency<=3, throughput==1, dsp<=4, fmax>=400){a} }"
        )
        expression = parse(source).assignments[0].expression
        self.assertIsNone(expression.stages)
        self.assertEqual(
            tuple((item.metric, item.relation, item.value) for item in expression.constraints),
            (
                (PipelineMetric.LATENCY, PipelineRelation.MAXIMUM, 3),
                (PipelineMetric.THROUGHPUT, PipelineRelation.EXACT, 1),
                (PipelineMetric.DSP, PipelineRelation.MAXIMUM, 4),
                (PipelineMetric.FMAX, PipelineRelation.MINIMUM, 400),
            ),
        )


if __name__ == "__main__":
    unittest.main()
