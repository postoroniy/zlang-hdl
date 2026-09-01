from pathlib import Path
import unittest

from zlang.ast.nodes import (
    AddExpr,
    BinaryExpr,
    BinaryOperator,
    MuxExpr,
    NumberExpr,
    SwitchExpr,
)
from zlang.parser import ParseError, parse


ROOT = Path(__file__).resolve().parents[2]


class ExpressionParserTests(unittest.TestCase):
    def test_alu_switch_parses(self) -> None:
        module = parse((ROOT / "examples/alu.zl").read_text())
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, SwitchExpr)
        self.assertEqual([arm.key for arm in expression.arms], [0, 1, 2, 3])
        self.assertEqual(expression.default, NumberExpr(0))

    def test_operator_precedence(self) -> None:
        source = """
            module Precedence {
                in a: u8 in b: u8 in c: u8
                out y: u17
                y = a + b * c
            }
        """
        expression = parse(source).assignments[0].expression
        self.assertIsInstance(expression, AddExpr)
        self.assertIsInstance(expression.right, BinaryExpr)
        self.assertEqual(expression.right.operator, BinaryOperator.MULTIPLY)

    def test_bitwise_precedence(self) -> None:
        source = "module Bits { in a:u8 in b:u8 in c:u8 out y:u8 y = a | b ^ c & a }"
        expression = parse(source).assignments[0].expression
        self.assertEqual(expression.operator, BinaryOperator.BIT_OR)
        self.assertEqual(expression.right.operator, BinaryOperator.BIT_XOR)
        self.assertEqual(expression.right.right.operator, BinaryOperator.BIT_AND)

    def test_mux_and_comparison_parse(self) -> None:
        source = "module Pick { in a:u8 in b:u8 out y:u8 y = mux(a < b, a, b) }"
        expression = parse(source).assignments[0].expression
        self.assertIsInstance(expression, MuxExpr)
        self.assertEqual(expression.condition.operator, BinaryOperator.LESS)

    def test_hex_and_underscored_constants_parse(self) -> None:
        source = "module Literal { out y:u16 y = 0x12_34 }"
        expression = parse(source).assignments[0].expression
        self.assertEqual(expression, NumberExpr(0x1234))

    def test_numeric_switch_without_else_is_deferred_to_semantics(self) -> None:
        source = "module Bad { in op:u1 out y:u1 y = switch op { 0 => 0 } }"
        parsed = parse(source)
        self.assertIsNone(parsed.assignments[0].expression.default)


if __name__ == "__main__":
    unittest.main()
