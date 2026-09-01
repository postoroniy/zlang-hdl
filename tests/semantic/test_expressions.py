import unittest

from zlang.ir.expressions import Binary, BinaryOperator, Constant, Mux, Switch
from zlang.ir.types import BitType, BitsType, SIntType, UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


class ExpressionSemanticTests(unittest.TestCase):
    def analyze_expression(self, source: str):
        return analyze(parse(source)).assignments[0].expression

    def test_unsigned_subtraction_is_modular_at_widest_width(self) -> None:
        expression = self.analyze_expression(
            "module Sub { in a:u8 in b:u16 out y:u16 y = a - b }"
        )
        self.assertEqual(expression.type, UIntType(16))

    def test_signed_subtraction_preserves_full_range(self) -> None:
        expression = self.analyze_expression(
            "module Sub { in a:s8 in b:s16 out y:s17 y = a - b }"
        )
        self.assertEqual(expression.type, SIntType(17))

    def test_multiplication_width_is_sum_of_operand_widths(self) -> None:
        expression = self.analyze_expression(
            "module Mul { in a:u8 in b:u16 out y:u24 y = a * b }"
        )
        self.assertEqual(expression.type, UIntType(24))

    def test_bitwise_result_uses_widest_width(self) -> None:
        expression = self.analyze_expression(
            "module Mask { in a:bits<8> in b:bits<16> out y:bits<16> y = a ^ b }"
        )
        self.assertEqual(expression.type, BitsType(16))

    def test_shift_preserves_left_type(self) -> None:
        expression = self.analyze_expression(
            "module Shift { in a:s16 in amount:u4 out y:s16 y = a >> amount }"
        )
        self.assertEqual(expression.type, SIntType(16))

    def test_literal_shift_amount_is_unsigned(self) -> None:
        expression = self.analyze_expression(
            "module Shift { in a:s16 out y:s16 y = a << 1 }"
        )
        self.assertEqual(expression.right.type, UIntType(1))

    def test_comparison_produces_bit(self) -> None:
        expression = self.analyze_expression(
            "module Compare { in a:u8 in b:u16 out y:bit y = a <= b }"
        )
        self.assertEqual(expression.type, BitType())
        self.assertEqual(expression.operand_type, UIntType(16))

    def test_runtime_logical_not_is_a_bit_comparison(self) -> None:
        expression = self.analyze_expression(
            "module Invert { in x:bit out y:bit y = !x }"
        )
        self.assertIsInstance(expression, Binary)
        self.assertEqual(expression.operator, BinaryOperator.EQUAL)
        self.assertEqual(expression.type, BitType())
        self.assertEqual(expression.right, Constant(0, BitType()))

    def test_runtime_logical_not_rejects_non_bit_values(self) -> None:
        source = "module Bad { in x:u2 out y:bit y = !x }"
        with self.assertRaisesRegex(
            SemanticError, "logical not requires bit, got u2"
        ):
            analyze(parse(source))

    def test_mux_requires_bit_condition(self) -> None:
        source = "module Bad { in c:u2 in a:u8 in b:u8 out y:u8 y=mux(c,a,b) }"
        with self.assertRaisesRegex(SemanticError, "mux condition must be bit"):
            analyze(parse(source))

    def test_mux_branches_must_match(self) -> None:
        source = "module Bad { in c:bit in a:u8 in b:u16 out y:u8 y=mux(c,a,b) }"
        with self.assertRaisesRegex(SemanticError, "mux branch has type u16, expected u8"):
            analyze(parse(source))

    def test_switch_rejects_duplicate_cases(self) -> None:
        source = """
            module Bad { in op:u2 out y:u8
                y=switch op { 1=>0 1=>1 else=>2 }
            }
        """
        with self.assertRaisesRegex(SemanticError, "duplicate switch case 1"):
            analyze(parse(source))

    def test_switch_case_must_fit_selector(self) -> None:
        source = "module Bad { in op:u2 out y:u8 y=switch op { 4=>0 else=>1 } }"
        with self.assertRaisesRegex(SemanticError, "case 4 does not fit u2"):
            analyze(parse(source))

    def test_switch_selector_must_be_unsigned_or_bits(self) -> None:
        source = "module Bad { in op:s2 out y:u8 y=switch op { 0=>0 else=>1 } }"
        with self.assertRaisesRegex(SemanticError, "switch selector must be unsigned"):
            analyze(parse(source))

    def test_switch_branch_must_match_output_context(self) -> None:
        source = """
            module Bad { in op:u1 in a:u8 in b:u8 out y:u8
                y=switch op { 0=>a + b else=>0 }
            }
        """
        with self.assertRaisesRegex(SemanticError, "switch branch has type u9, expected u8"):
            analyze(parse(source))

    def test_ordered_bit_vector_comparison_is_rejected(self) -> None:
        source = "module Bad { in a:bits<8> in b:bits<8> out y:bit y=a < b }"
        with self.assertRaisesRegex(SemanticError, "ordered comparison is not defined"):
            analyze(parse(source))

    def test_mixed_family_bitwise_operation_is_rejected(self) -> None:
        source = "module Bad { in a:u8 in b:bits<8> out y:u8 y=a & b }"
        with self.assertRaisesRegex(SemanticError, "matching type families"):
            analyze(parse(source))

    def test_signed_shift_amount_is_rejected(self) -> None:
        source = "module Bad { in a:u8 in amount:s4 out y:u8 y=a << amount }"
        with self.assertRaisesRegex(SemanticError, "shift amount must be unsigned"):
            analyze(parse(source))

    def test_trivial_arithmetic_constants_are_folded(self) -> None:
        expression = self.analyze_expression("module Fold { out y:u5 y=3 * 4 }")
        self.assertEqual(expression, Constant(12, UIntType(5)))

    def test_constant_mux_is_folded(self) -> None:
        expression = self.analyze_expression("module Fold { out y:u8 y=mux(1,5,7) }")
        self.assertEqual(expression, Constant(5, UIntType(8)))

    def test_constant_switch_is_folded(self) -> None:
        expression = self.analyze_expression(
            "module Fold { out y:u8 y=switch 2 { 1=>5 2=>7 else=>9 } }"
        )
        self.assertEqual(expression, Constant(7, UIntType(8)))

    def test_literal_must_fit_context(self) -> None:
        source = "module Bad { out y:u8 y=256 }"
        with self.assertRaisesRegex(SemanticError, "constant 256 does not fit u8"):
            analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
