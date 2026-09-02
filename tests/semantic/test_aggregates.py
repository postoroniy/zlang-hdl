from pathlib import Path
import unittest

from zlang.ir.expressions import Call, FieldAccess, ParameterRef, VectorIndex
from zlang.ir.types import BitType, StructField, StructType, UIntType, VecType
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class AggregateSemanticTests(unittest.TestCase):
    def test_struct_fields_are_canonical_and_access_is_typed(self) -> None:
        module = analyze(parse((ROOT / "examples/packet_data.zhl").read_text()))
        packet_type = StructType(
            "Packet",
            (
                StructField("data", UIntType(64)),
                StructField("last", BitType()),
                StructField("vc", UIntType(2)),
            ),
        )
        self.assertEqual(module.structs, (packet_type,))
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, FieldAccess)
        self.assertEqual(expression.type, UIntType(64))

    def test_nested_vector_index_is_typed(self) -> None:
        source = "module Nested { in x:vec<2,vec<3,u8>> out y:u8 y=x[1][2] }"
        expression = analyze(parse(source)).assignments[0].expression
        self.assertIsInstance(expression, VectorIndex)
        self.assertEqual(expression.type, UIntType(8))

    def test_function_body_and_call_are_typed(self) -> None:
        module = analyze(parse((ROOT / "examples/fir2.zhl").read_text()))
        function = module.functions[0]
        self.assertEqual(function.return_type, UIntType(16))
        self.assertIsInstance(function.body.left, ParameterRef)
        call = module.assignments[0].expression.left
        self.assertIsInstance(call, Call)
        self.assertEqual(call.type, UIntType(16))

    def test_struct_alias_resolves_to_same_canonical_type(self) -> None:
        source = """
            struct Word { value:u8 }
            type Alias = Word
            module AliasUse { in x:Alias out y:u8 y=x.value }
        """
        module = analyze(parse(source))
        self.assertEqual(module.inputs[0].type, module.structs[0])

    def test_duplicate_struct_field_is_rejected(self) -> None:
        source = "struct Bad { x:u8 x:u16 } module Use { out y:u1 y=0 }"
        with self.assertRaisesRegex(SemanticError, "duplicate field 'x'"):
            analyze(parse(source))

    def test_empty_struct_is_rejected(self) -> None:
        source = "struct Empty { } module Use { out y:u1 y=0 }"
        with self.assertRaisesRegex(SemanticError, "must contain at least one field"):
            analyze(parse(source))

    def test_recursive_struct_is_rejected(self) -> None:
        source = "struct Node { next:Node } module Use { out y:u1 y=0 }"
        with self.assertRaisesRegex(SemanticError, "cyclic type alias: Node -> Node"):
            analyze(parse(source))

    def test_missing_field_is_rejected(self) -> None:
        source = "struct P { x:u8 } module Bad { in p:P out y:u8 y=p.y }"
        with self.assertRaisesRegex(SemanticError, "struct 'P' has no field 'y'"):
            analyze(parse(source))

    def test_field_access_on_scalar_is_rejected(self) -> None:
        source = "module Bad { in x:u8 out y:u8 y=x.field }"
        with self.assertRaisesRegex(SemanticError, "field access requires a struct"):
            analyze(parse(source))

    def test_vector_index_bounds_are_checked(self) -> None:
        source = "module Bad { in x:vec<2,u8> out y:u8 y=x[2] }"
        with self.assertRaisesRegex(SemanticError, "index 2 is out of range"):
            analyze(parse(source))

    def test_indexing_scalar_is_rejected(self) -> None:
        source = "module Bad { in x:u8 out y:u8 y=x[0] }"
        with self.assertRaisesRegex(SemanticError, "indexing requires a vector"):
            analyze(parse(source))

    def test_duplicate_function_and_parameter_are_rejected(self) -> None:
        duplicate_function = """
            fn f(x:u8)->u8 { x }
            fn f(x:u8)->u8 { x }
            module Bad { in x:u8 out y:u8 y=f(x) }
        """
        duplicate_parameter = """
            fn f(x:u8,x:u8)->u8 { x }
            module Bad { in x:u8 out y:u8 y=f(x,x) }
        """
        with self.assertRaisesRegex(SemanticError, "duplicate function 'f'"):
            analyze(parse(duplicate_function))
        with self.assertRaisesRegex(SemanticError, "duplicate parameter 'x'"):
            analyze(parse(duplicate_parameter))

    def test_unknown_function_and_wrong_arity_are_rejected(self) -> None:
        unknown = "module Bad { in x:u8 out y:u8 y=missing(x) }"
        wrong_arity = """
            fn f(x:u8)->u8 { x }
            module Bad { in x:u8 out y:u8 y=f(x,x) }
        """
        with self.assertRaisesRegex(SemanticError, "unknown function 'missing'"):
            analyze(parse(unknown))
        with self.assertRaisesRegex(SemanticError, "expects 1 arguments, got 2"):
            analyze(parse(wrong_arity))

    def test_function_argument_and_return_types_are_checked(self) -> None:
        wrong_argument = """
            fn f(x:u8)->u8 { x }
            module Bad { in x:u16 out y:u8 y=f(x) }
        """
        wrong_return = """
            fn f(x:u8)->u8 { x + x }
            module Bad { in x:u8 out y:u8 y=x }
        """
        with self.assertRaisesRegex(SemanticError, "argument 'x'.*has type u16"):
            analyze(parse(wrong_argument))
        with self.assertRaisesRegex(SemanticError, "function 'f' returns u9, expected u8"):
            analyze(parse(wrong_return))

    def test_function_cannot_capture_module_port(self) -> None:
        source = """
            fn f(x:u8)->u8 { external }
            module Bad { in external:u8 in x:u8 out y:u8 y=f(x) }
        """
        with self.assertRaisesRegex(SemanticError, "unknown input 'external'"):
            analyze(parse(source))

    def test_direct_and_indirect_recursion_are_rejected(self) -> None:
        direct = """
            fn f(x:u8)->u8 { f(x) }
            module Bad { in x:u8 out y:u8 y=f(x) }
        """
        indirect = """
            fn f(x:u8)->u8 { g(x) }
            fn g(x:u8)->u8 { f(x) }
            module Bad { in x:u8 out y:u8 y=f(x) }
        """
        with self.assertRaisesRegex(SemanticError, "recursive function call: f -> f"):
            analyze(parse(direct))
        with self.assertRaisesRegex(
            SemanticError, "recursive function call: f -> g -> f"
        ):
            analyze(parse(indirect))


if __name__ == "__main__":
    unittest.main()
