from pathlib import Path
import unittest

from zlang.ast.nodes import PipelineExpr
from zlang.parser import ParseError, parse


ROOT = Path(__file__).resolve().parents[2]


class PipelineParserTests(unittest.TestCase):
    def test_fixed_pipeline_parses(self) -> None:
        module = parse((ROOT / "examples/pipelined_mac.zhl").read_text())
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, PipelineExpr)
        self.assertEqual(expression.stages, 2)

    def test_scalar_auto_pipeline_has_migration_diagnostic(self) -> None:
        source = (
            "module Auto { clock c reset r in a:u8 out y:u8 "
            "y=pipeline(auto, latency<=3, throughput==1, dsp<=4, fmax>=400){a} }"
        )
        with self.assertRaisesRegex(ParseError, "removed"):
            parse(source)


if __name__ == "__main__":
    unittest.main()
