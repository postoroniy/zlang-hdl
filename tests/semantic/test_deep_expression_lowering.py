"""Canonical lowering is stack-safe, ordered and sharing/provenance invariant."""

from dataclasses import replace

import pytest

from zlang.ir.expressions import Binary, BinaryOperator, Constant, InputRef
from zlang.ir.module import Module
from zlang.ir.types import BitsType
from zlang.opt.ir import ExpressionOp
from zlang.opt.lowering import CanonicalizationError, lower_expression_graph


TYPE = BitsType(8)
MODULE = Module("Top", (), ())


@pytest.mark.parametrize("depth", (1, 400, 4000))
def test_deep_chain_preserves_all_typed_operations(depth):
    leaf, constant = InputRef("a", TYPE), Constant(3, TYPE)
    root = leaf
    for _ in range(depth):
        root = Binary(BinaryOperator.BIT_XOR, root, constant, TYPE, TYPE)
    nodes, result = lower_expression_graph(MODULE, root)
    assert len(nodes) == depth+2 and result == depth+1
    assert nodes[0].op is ExpressionOp.INPUT
    assert nodes[1].op is ExpressionOp.CONSTANT
    for node in nodes[2:]:
        assert node.op is ExpressionOp.BINARY and node.type == TYPE
        assert node.operands == (0 if node.id == 2 else node.id-1, 1)


def test_shared_and_copied_diamonds_preserve_numbering():
    def graph(copied):
        left = InputRef("a", TYPE)
        for _ in range(8):
            right = replace(left) if copied else left
            left = Binary(BinaryOperator.BIT_XOR, left, right, TYPE, TYPE)
        return lower_expression_graph(MODULE, left)
    assert graph(True) == graph(False)


def test_invalid_cyclic_ir_fails_without_recursive_diagnostic():
    root = Binary(BinaryOperator.BIT_XOR, InputRef("a", TYPE), Constant(3, TYPE), TYPE, TYPE)
    object.__setattr__(root, "left", root)
    with pytest.raises(CanonicalizationError, match="cyclic"):
        lower_expression_graph(MODULE, root)
