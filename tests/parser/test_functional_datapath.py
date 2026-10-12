from pathlib import Path
import unittest

from zlang.ast.nodes import (
    AddExpr,
    CollectionSumExpr,
    DotExpr,
    GenerateExpr,
    IndexedSumExpr,
    MapExpr,
    ReduceExpr,
    ReductionOperator,
)
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


class FunctionalDatapathParserTests(unittest.TestCase):
    def test_indexed_sum_uses_a_half_open_range_binder(self) -> None:
        module = parse((ROOT / "examples/dot_product.zhl").read_text())
        expression = module.assignments[0].expression
        self.assertIsInstance(expression, IndexedSumExpr)
        self.assertEqual((expression.index, expression.start, expression.stop), (
            "i", 0, 8
        ))

    def test_dot_parses_as_a_high_level_expression(self) -> None:
        module = parse((ROOT / "examples/dot_builtin.zhl").read_text())
        self.assertIsInstance(module.assignments[0].expression, DotExpr)

    def test_generate_map_reduce_and_collection_sum_parse(self) -> None:
        generated = parse((ROOT / "examples/generated_reduce.zhl").read_text())
        reduction = generated.assignments[0].expression
        self.assertEqual(
            reduction.operator,
            ReductionOperator.ADD,
        )
        self.assertIsInstance(reduction, ReduceExpr)
        self.assertIsInstance(reduction.collection, GenerateExpr)

        mapped = parse((ROOT / "examples/mapped_sum.zhl").read_text())
        collection_sum = mapped.assignments[0].expression
        self.assertIsInstance(collection_sum, CollectionSumExpr)
        self.assertIsInstance(collection_sum.collection, MapExpr)

    def test_unbraced_body_stops_at_product_precedence(self) -> None:
        source = (
            "module Boundary { in a:vec<2,u8> in b:vec<2,u8> in c:u9 "
            "out y:u11 y=sum(i in 0..2) a[i]*b[i] + c }"
        )
        expression = parse(source).assignments[0].expression
        self.assertIsInstance(expression, AddExpr)
        self.assertIsInstance(expression.left, IndexedSumExpr)

    def test_all_general_reduction_operators_parse(self) -> None:
        for spelling in ("+", "*", "&", "|", "^"):
            with self.subTest(operator=spelling):
                module = parse(
                    f"module R {{ in x:vec<2,u8> out y:u8 "
                    f"y=reduce({spelling},x) }}"
                )
                self.assertEqual(
                    module.assignments[0].expression.operator.value,
                    spelling,
                )


if __name__ == "__main__":
    unittest.main()
