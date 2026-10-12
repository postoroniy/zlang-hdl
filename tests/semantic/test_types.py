import unittest

from zlang.ir.expressions import Extend, Truncate
from zlang.ir.types import BitType, BitsType, SIntType, UIntType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


class HardwareTypeTests(unittest.TestCase):
    def test_all_type_spellings_lower_to_canonical_ir(self) -> None:
        source = """
            type Address = u32
            type AddressAgain = Address
            module Types {
                in b: bit
                in u: AddressAgain
                in s: sint<23>
                in v: bits<64>
                out bo: bit
                out uo: uint<32>
                out so: s23
                out vo: bits<64>
                bo = b
                uo = u
                so = s
                vo = v
            }
        """
        module = analyze(parse(source))

        self.assertEqual(
            [port.type for port in module.ports],
            [
                BitType(),
                UIntType(32),
                SIntType(23),
                BitsType(64),
                BitType(),
                UIntType(32),
                SIntType(23),
                BitsType(64),
            ],
        )

    def test_alias_can_refer_to_a_later_alias(self) -> None:
        source = """
            type Address = Word
            type Word = uint<17>
            module Alias { in a: Address out y: u17 y = a }
        """
        module = analyze(parse(source))
        self.assertEqual(module.inputs[0].type, UIntType(17))

    def test_unsigned_addition_uses_widest_operand_plus_carry(self) -> None:
        source = "module Sum { in a: u8 in b: u16 out y: u17 y = a + b }"
        expression = analyze(parse(source)).assignments[0].expression
        self.assertEqual(expression.type, UIntType(17))

    def test_signed_addition_uses_widest_operand_plus_sign_bit(self) -> None:
        source = "module Sum { in a: s8 in b: sint<16> out y: s17 y = a + b }"
        expression = analyze(parse(source)).assignments[0].expression
        self.assertEqual(expression.type, SIntType(17))

    def test_narrow_assignment_requires_explicit_truncate(self) -> None:
        source = "module Bad { in a: u16 out y: u8 y = a }"
        with self.assertRaisesRegex(
            SemanticError, "cannot assign u16 expression to u8 output 'y'"
        ):
            analyze(parse(source))

    def test_truncate_is_typed_explicitly(self) -> None:
        source = "module Narrow { in a: u16 out y: u8 y = truncate<8>(a) }"
        expression = analyze(parse(source)).assignments[0].expression
        self.assertIsInstance(expression, Truncate)
        self.assertEqual(expression.type, UIntType(8))

    def test_extend_preserves_signed_family(self) -> None:
        source = "module Wide { in a: s8 out y: sint<16> y = extend<16>(a) }"
        expression = analyze(parse(source)).assignments[0].expression
        self.assertIsInstance(expression, Extend)
        self.assertEqual(expression.type, SIntType(16))

    def test_mixed_signedness_addition_is_rejected(self) -> None:
        source = "module Bad { in a: u8 in b: s8 out y: u9 y = a + b }"
        with self.assertRaisesRegex(SemanticError, "different type families u8 and s8"):
            analyze(parse(source))

    def test_bit_vector_addition_is_rejected(self) -> None:
        source = "module Bad { in a: bits<8> in b: bits<8> out y: bits<9> y = a + b }"
        with self.assertRaisesRegex(SemanticError, "addition is not defined for bits<8>"):
            analyze(parse(source))

    def test_extend_cannot_narrow(self) -> None:
        source = "module Bad { in a: u16 out y: u8 y = extend<8>(a) }"
        with self.assertRaisesRegex(SemanticError, "cannot extend u16 to width 8"):
            analyze(parse(source))

    def test_truncate_cannot_widen(self) -> None:
        source = "module Bad { in a: u8 out y: u16 y = truncate<16>(a) }"
        with self.assertRaisesRegex(SemanticError, "cannot truncate u8 to width 16"):
            analyze(parse(source))

    def test_bit_resize_is_rejected(self) -> None:
        source = "module Bad { in a: bit out y: bits<8> y = extend<8>(a) }"
        with self.assertRaisesRegex(SemanticError, "not defined for bit"):
            analyze(parse(source))

    def test_duplicate_alias_is_rejected(self) -> None:
        source = """
            type Word = u8
            type Word = u16
            module Bad { in a: Word out y: u8 y = a }
        """
        with self.assertRaisesRegex(SemanticError, "duplicate type alias 'Word'"):
            analyze(parse(source))

    def test_alias_cycle_is_rejected(self) -> None:
        source = """
            type A = B
            type B = A
            module Bad { in a: A out y: u8 y = a }
        """
        with self.assertRaisesRegex(SemanticError, "cyclic type alias: A -> B -> A"):
            analyze(parse(source))

    def test_unknown_alias_target_is_rejected_even_if_unused(self) -> None:
        source = "type Broken = Missing module Bad { in a: u8 out y: u8 y = a }"
        with self.assertRaisesRegex(SemanticError, "unknown type 'Missing'"):
            analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
