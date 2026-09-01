from pathlib import Path
import unittest

from zlang.ir.expressions import Dot, Generate, Map, Reduce, ReductionOperator
from zlang.ir.types import BitsType, UIntType, VecType
from zlang.opt import lower, render, restore
from zlang.opt import OptimizationStage
from zlang.opt.ir import ExpressionOp
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]


class FunctionalDatapathSemanticTests(unittest.TestCase):
    def test_indexed_sum_is_a_typed_generate_plus_reduction(self) -> None:
        module = analyze(parse((ROOT / "examples/dot_product.zl").read_text()))
        reduction = module.assignments[0].expression
        self.assertIsInstance(reduction, Reduce)
        self.assertEqual(reduction.operator, ReductionOperator.ADD)
        self.assertEqual(reduction.type, UIntType(19))
        self.assertIsInstance(reduction.collection, Generate)
        self.assertEqual(reduction.collection.type, VecType(8, UIntType(16)))
        self.assertEqual(
            [term.left.index for term in reduction.collection.elements],
            list(range(8)),
        )

    def test_dot_preserves_products_then_uses_the_same_reduction_model(self) -> None:
        module = analyze(parse((ROOT / "examples/dot_builtin.zl").read_text()))
        reduction = module.assignments[0].expression
        self.assertIsInstance(reduction, Reduce)
        self.assertEqual(reduction.operator, ReductionOperator.ADD)
        self.assertEqual(reduction.type, UIntType(19))
        self.assertIsInstance(reduction.collection, Dot)
        self.assertEqual(len(reduction.collection.products), 8)

    def test_canonical_ir_preserves_functional_intent_losslessly(self) -> None:
        cases = (
            ("dot_product.zl", "DotProduct.opt", ExpressionOp.GENERATE),
            ("dot_builtin.zl", "DotBuiltin.opt", ExpressionOp.DOT),
        )
        for source_name, golden_name, producer_op in cases:
            with self.subTest(source=source_name):
                semantic = analyze(parse((ROOT / "examples" / source_name).read_text()))
                canonical = lower(
                    semantic,
                    stage=OptimizationStage.SELECTED_ARCHITECTURE,
                )
                assignment_root = canonical.expressions[
                    canonical.assignments[0].expression
                ]
                producer = canonical.expressions[assignment_root.operands[0]]
                self.assertEqual(assignment_root.op, ExpressionOp.REDUCE)
                self.assertEqual(producer.op, producer_op)
                self.assertEqual(restore(canonical), semantic)
                self.assertEqual(
                    render(canonical),
                    (ROOT / "examples/generated" / golden_name).read_text(),
                )

    def test_generate_and_map_create_exact_vector_types(self) -> None:
        generated = analyze(
            parse(
                "module G { in x:vec<4,u8> out y:vec<3,u8> "
                "y=generate(i in 1..4) x[i] }"
            )
        ).assignments[0].expression
        mapped = analyze(
            parse(
                "module M { in x:vec<4,u8> out y:vec<4,u9> "
                "y=map(i in 0..4) { x[i]+x[i] } }"
            )
        ).assignments[0].expression
        self.assertIsInstance(generated, Generate)
        self.assertEqual(generated.type, VecType(3, UIntType(8)))
        self.assertIsInstance(mapped, Map)
        self.assertEqual(mapped.type, VecType(4, UIntType(9)))

    def test_balanced_reduction_widths_are_deterministic(self) -> None:
        cases = (
            ("+", "vec<8,u16>", "u19", UIntType(19)),
            ("*", "vec<3,u2>", "u6", UIntType(6)),
            ("&", "vec<5,bits<7>>", "bits<7>", BitsType(7)),
        )
        for operator, vector_type, output_type, expected in cases:
            with self.subTest(operator=operator):
                module = analyze(
                    parse(
                        f"module R {{ in x:{vector_type} out y:{output_type} "
                        f"y=reduce({operator},x) }}"
                    )
                )
                self.assertEqual(module.assignments[0].expression.type, expected)

    def test_invalid_ranges_and_binders_are_rejected(self) -> None:
        cases = (
            (
                "module E { in x:vec<2,u8> out y:vec<1,u8> "
                "y=generate(i in 1..1) x[i] }",
                "range 1\\.\\.1 is empty",
            ),
            (
                "module E { in x:vec<2,u8> out y:vec<4097,u8> "
                "y=generate(i in 0..4097) x[0] }",
                "limit is 4096",
            ),
            (
                "module E { in x:vec<2,u8> in i:u1 out y:vec<2,u8> "
                "y=generate(i in 0..2) x[i] }",
                "shadows an existing name",
            ),
            (
                "module E { in x:vec<3,u8> in index:u2 out y:u8 y=x[index] }",
                "runtime index range 0\\.\\.3 is not provably within vector length 3",
            ),
            (
                "module E { in x:vec<2,u8> out y:vec<2,u8> "
                "y=generate(i in 1..3) x[i] }",
                "index 2 is out of range",
            ),
        )
        for source, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                SemanticError, message
            ):
                analyze(parse(source))

    def test_invalid_reductions_and_dot_shapes_are_rejected(self) -> None:
        cases = (
            (
                "module E { in x:u8 out y:u8 y=reduce(+,x) }",
                "requires a vector collection",
            ),
            (
                "module E { in x:vec<2,bit> out y:bit y=sum(x) }",
                "addition reduction requires one integer signedness family",
            ),
            (
                "module E { in a:vec<2,u8> in b:vec<3,u8> out y:u18 "
                "y=dot(a,b) }",
                "dot vector lengths must match",
            ),
            (
                "module E { in a:u8 in b:u8 out y:u16 y=dot(a,b) }",
                "dot requires two vectors",
            ),
            (
                "module E { in a:vec<2,u8> in b:vec<2,s8> out y:u17 "
                "y=dot(a,b) }",
                "multiplication requires matching integer families",
            ),
        )
        for source, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                SemanticError, message
            ):
                analyze(parse(source))


if __name__ == "__main__":
    unittest.main()
