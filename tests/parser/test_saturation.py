from pathlib import Path
import unittest

from zlang.ast.nodes import Assignment, BinaryExpr, BinaryOperator
from zlang.parser import ParseError, parse


ROOT = Path(__file__).resolve().parents[2]


class SaturationParserBoundaryTests(unittest.TestCase):
    def test_constant_multiply_uses_existing_typed_expression_syntax(self) -> None:
        module = parse((ROOT / "examples/shift_multiply.zl").read_text())
        assignment = next(
            item for item in module.assignments if isinstance(item, Assignment)
        )

        self.assertEqual(module.name, "ShiftMultiply")
        self.assertIsInstance(assignment.expression, BinaryExpr)
        self.assertEqual(assignment.expression.operator, BinaryOperator.MULTIPLY)
        self.assertFalse(hasattr(module, "equality_classes"))

    def test_internal_rewrite_directive_is_not_source_syntax(self) -> None:
        with self.assertRaises(ParseError):
            parse("module Bad { in x:u8 out y:u8 rewrite y=x y=x }")


if __name__ == "__main__":
    unittest.main()
