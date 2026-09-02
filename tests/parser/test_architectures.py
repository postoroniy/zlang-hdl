from pathlib import Path
import unittest

from zlang.ast.nodes import (
    ArchitectureExpr,
    ArchitectureMetric,
)
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class ArchitectureParserTests(unittest.TestCase):
    def test_auto_architecture_parses_bounded_dimensions(self) -> None:
        expression = parse(
            (ROOT / "examples/fir_architecture.zhl").read_text()
        ).assignments[0].expression

        self.assertIsInstance(expression, ArchitectureExpr)
        self.assertEqual(
            tuple((item.metric, item.maximum) for item in expression.constraints),
            (
                (ArchitectureMetric.PARALLELISM, 2),
                (ArchitectureMetric.DEPTH, 2),
                (ArchitectureMetric.CANDIDATES, 3),
            ),
        )


if __name__ == "__main__":
    unittest.main()
