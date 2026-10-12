import unittest

from zlang.ast.nodes import NameExpr, ResizeExpr, ResizeKind, TypeAlias, TypeName
from zlang.parser import ParseError, parse


class TypeParserTests(unittest.TestCase):
    def test_aliases_generic_types_and_resize_parse(self) -> None:
        source = """
            type Address = u32
            type Sample = sint<16>
            module Types {
                in address: Address
                in sample: Sample
                in mask: bits<64>
                out narrowed: uint<8>
                narrowed = truncate<8>(address)
            }
        """
        module = parse(source)

        self.assertEqual(
            module.type_aliases,
            (
                TypeAlias("Address", TypeName("u32")),
                TypeAlias("Sample", TypeName("sint<16>")),
            ),
        )
        self.assertEqual(
            [port.type_name.text for port in module.ports],
            ["Address", "Sample", "bits<64>", "uint<8>"],
        )
        self.assertEqual(
            module.assignments[0].expression,
            ResizeExpr(ResizeKind.TRUNCATE, 8, NameExpr("address")),
        )

    def test_extend_parses_as_an_expression(self) -> None:
        module = parse(
            "module Extend { in a: u8 out y: uint<16> y = extend<16>(a) }"
        )
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, ResizeExpr)
        self.assertEqual(expression.kind, ResizeKind.EXTEND)
        self.assertEqual(expression.width, 16)

    def test_addition_is_left_associative(self) -> None:
        module = parse(
            "module Sum { in a: u8 in b: u8 in c: u8 out y: u10 y = a + b + c }"
        )
        expression = module.assignments[0].expression
        self.assertEqual(expression.left.left.name, "a")
        self.assertEqual(expression.left.right.name, "b")
        self.assertEqual(expression.right.name, "c")

    def test_zero_width_generic_type_is_rejected(self) -> None:
        with self.assertRaises(ParseError):
            parse("module Bad { in a: uint<0> out y: u1 y = a }")


if __name__ == "__main__":
    unittest.main()
