from pathlib import Path
import unittest
from dataclasses import replace

from zlang.compiler import compile_source
from zlang.ir.types import UIntType
from zlang.opt import RewriteRule, SaturationError, saturate, term_to_expression
from zlang.opt.ir import pure_metadata
from zlang.simulate import simulate
from zlang.opt import (
    EGraphAdapterError,
    canonical_to_egraph,
    deserialize_egraph,
    egraph_to_canonical,
    egraph_to_expression,
    render_egraph,
    serialize_egraph,
)
from zlang.source import SourceOrigin


ROOT = Path(__file__).resolve().parents[2]


class EGraphAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compilation = compile_source((ROOT / "examples/add.zl").read_text())
        self.root = self.compilation.optimization_ir.assignments[0].expression

    def test_pure_scalar_graph_round_trips_with_metadata_and_origin(self) -> None:
        program = canonical_to_egraph(self.compilation.optimization_ir, self.root)
        restored_nodes, restored_root = egraph_to_canonical(program)
        original = self.compilation.optimization_ir.expressions
        source = original[self.root]
        restored = restored_nodes[restored_root]
        self.assertEqual(restored.type, source.type)
        self.assertEqual(restored.metadata, source.metadata)
        self.assertEqual(restored.origins, source.origins)
        self.assertEqual(egraph_to_expression(program), self.compilation.ir.assignments[0].expression)

    def test_serialization_and_debug_dump_are_deterministic(self) -> None:
        program = canonical_to_egraph(self.compilation.optimization_ir, self.root)
        encoded = serialize_egraph(program)
        self.assertEqual(encoded, serialize_egraph(deserialize_egraph(encoded)))
        self.assertIn('"schema": "zlang-egraph-v1"', encoded)
        self.assertIn("egraph root=", render_egraph(program))

    def test_serialization_retains_logical_source_identity_and_digest(self) -> None:
        program = canonical_to_egraph(self.compilation.optimization_ir, self.root)
        source_origin = program.nodes[-1].origins[0]
        qualified = SourceOrigin(
            source_origin.span,
            source_origin.construct,
            "examples/add.zl",
            "d" * 64,
        )
        nodes = list(program.nodes)
        nodes[-1] = replace(nodes[-1], origins=(qualified,))
        qualified_program = replace(program, nodes=tuple(nodes))

        encoded = serialize_egraph(qualified_program)
        restored = deserialize_egraph(encoded)
        self.assertEqual(restored.nodes[-1].origins, (qualified,))
        self.assertIn('"source_unit": "examples/add.zl"', encoded)
        self.assertIn(f'"digest": "{"d" * 64}"', encoded)

    def test_binary_operator_attributes_survive_serialization(self) -> None:
        compilation = compile_source(
            "module Binary { in x:u8 in y:u8 out z:u16 z = x * y }"
        )
        root = compilation.optimization_ir.assignments[0].expression
        program = canonical_to_egraph(compilation.optimization_ir, root)
        restored = deserialize_egraph(serialize_egraph(program))
        self.assertEqual(egraph_to_expression(restored), compilation.ir.assignments[0].expression)

    def test_state_and_aggregate_roots_are_rejected(self) -> None:
        delayed = compile_source((ROOT / "examples/delayed_mul.zl").read_text())
        delayed_root = delayed.optimization_ir.assignments[0].expression
        with self.assertRaises(EGraphAdapterError):
            canonical_to_egraph(delayed.optimization_ir, delayed_root)

        aggregate = compile_source((ROOT / "examples/dot_product.zl").read_text())
        aggregate_root = aggregate.optimization_ir.assignments[0].expression
        with self.assertRaises(EGraphAdapterError):
            canonical_to_egraph(aggregate.optimization_ir, aggregate_root)

    def test_malformed_serialization_is_rejected(self) -> None:
        with self.assertRaises(EGraphAdapterError):
            deserialize_egraph('{"schema":"unknown"}')

    def test_frozen_rewrites_are_exhaustively_equivalent_at_small_width(self) -> None:
        compilation = compile_source(
            "module Bitwise { in x:u4 out y:u4 y=x|0 }"
        )
        root = compilation.optimization_ir.assignments[0].expression
        result = saturate(compilation.optimization_ir, root)
        self.assertEqual(result.rules, (RewriteRule.BIT_OR_ZERO,))
        alternative = replace(
            compilation.ir,
            assignments=(replace(
                compilation.ir.assignments[0],
                expression=term_to_expression(result.alternatives[0]),
            ),),
        )
        for x in range(16):
            self.assertEqual(simulate(compilation.ir, x=x), simulate(alternative, x=x))

    def test_unsafe_arithmetic_and_incompatible_bitwise_types_are_not_merged(self) -> None:
        arithmetic = compile_source("module Arithmetic { in x:u4 out y:u5 y=x+0 }")
        root = arithmetic.optimization_ir.assignments[0].expression
        self.assertEqual(saturate(arithmetic.optimization_ir, root).alternatives, ())

        bitwise = compile_source("module Bitwise { in x:u4 out y:u4 y=x|0 }")
        root = bitwise.optimization_ir.assignments[0].expression
        constant_id = bitwise.optimization_ir.expressions[root].operands[1]
        expressions = list(bitwise.optimization_ir.expressions)
        expressions[constant_id] = replace(
            expressions[constant_id],
            type=UIntType(5),
            metadata=pure_metadata(UIntType(5)),
        )
        malformed = replace(bitwise.optimization_ir, expressions=tuple(expressions))
        with self.assertRaises(SaturationError):
            saturate(malformed, root)


if __name__ == "__main__":
    unittest.main()
