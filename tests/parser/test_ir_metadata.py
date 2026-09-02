from dataclasses import replace
from pathlib import Path
import unittest

from zlang.ast.nodes import DelayExpr, DotExpr, NameExpr
from zlang.parser import parse
from zlang.source import SourceSpan


ROOT = Path(__file__).resolve().parents[2]


class SourceMetadataParserTests(unittest.TestCase):
    def test_expression_spans_are_half_open_and_nested(self) -> None:
        module = parse((ROOT / "examples/metadata_datapath.zhl").read_text())
        delay = module.assignments[0].expression
        self.assertIsInstance(delay, DelayExpr)
        self.assertEqual(delay.origin, SourceSpan(9, 9, 9, 45))
        self.assertIsInstance(delay.expression, DotExpr)
        self.assertEqual(delay.expression.origin, SourceSpan(9, 18, 9, 44))
        self.assertEqual(delay.expression.left.origin, SourceSpan(9, 22, 9, 29))

    def test_locations_do_not_change_syntax_structural_equality(self) -> None:
        located = NameExpr("x", origin=SourceSpan(1, 1, 1, 2))
        self.assertEqual(located, NameExpr("x"))
        self.assertEqual(replace(located, origin=SourceSpan(2, 3, 2, 4)), located)

    def test_invalid_source_span_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "end precedes"):
            SourceSpan(2, 1, 1, 1)


if __name__ == "__main__":
    unittest.main()
