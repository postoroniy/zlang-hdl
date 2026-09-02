from pathlib import Path
import unittest

from zlang.ast.nodes import (
    CallExpr,
    FieldExpr,
    IndexExpr,
    NameExpr,
    TypeName,
    VectorTypeName,
)
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class AggregateParserTests(unittest.TestCase):
    def test_struct_and_field_access_parse(self) -> None:
        module = parse((ROOT / "examples/packet_data.zhl").read_text())
        self.assertEqual(module.structs[0].name, "Packet")
        self.assertEqual(
            [field.name for field in module.structs[0].fields],
            ["data", "last", "vc"],
        )
        self.assertEqual(
            module.assignments[0].expression,
            FieldExpr(NameExpr("packet"), "data"),
        )

    def test_function_calls_and_vector_indexes_parse(self) -> None:
        module = parse((ROOT / "examples/fir2.zhl").read_text())
        self.assertEqual(module.functions[0].name, "tap")
        self.assertEqual(
            module.ports[0].type_name,
            VectorTypeName(2, TypeName("u8")),
        )
        first_call = module.assignments[0].expression.left
        self.assertIsInstance(first_call, CallExpr)
        self.assertIsInstance(first_call.arguments[0], IndexExpr)
        self.assertEqual(first_call.arguments[0].index, 0)

    def test_nested_vector_type_and_index_parse(self) -> None:
        source = "module Nested { in x:vec<2,vec<3,u8>> out y:u8 y=x[1][2] }"
        module = parse(source)
        self.assertEqual(
            module.ports[0].type_name,
            VectorTypeName(2, VectorTypeName(3, TypeName("u8"))),
        )
        self.assertEqual(module.assignments[0].expression.index, 2)

    def test_zero_argument_function_and_call_parse(self) -> None:
        source = "fn zero() -> u1 { 0 } module Zero { out y:u1 y=zero() }"
        module = parse(source)
        self.assertEqual(module.functions[0].parameters, ())
        self.assertEqual(module.assignments[0].expression.arguments, ())


if __name__ == "__main__":
    unittest.main()
