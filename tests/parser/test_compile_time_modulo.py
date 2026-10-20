"""Modulo shares multiplication precedence without adding a hardware opcode."""

from zlang.ast.nodes import AddExpr, BinaryOperator
from zlang.parser import parse


def test_modulo_multiplication_and_addition_precedence():
    root = parse('module W{out y:u8 y=1+8%3*2}').assignments[0].expression
    assert isinstance(root, AddExpr)
    assert root.right.operator is BinaryOperator.MULTIPLY
    assert root.right.left.operator is BinaryOperator.MODULO


def test_modulo_product_chain_is_left_associative():
    root = parse('module W{out y:u8 y=12%5%2}').assignments[0].expression
    assert root.operator is BinaryOperator.MODULO
    assert root.left.operator is BinaryOperator.MODULO


def test_generic_arguments_accept_parenthesized_negative_modulo_operands():
    module = parse('fn f<N>(){N} module W{out y:u8 y=f<N=(-5)%(-3)>()}')
    assert module.assignments[0].expression.specializations[0].value == '(-5)%(-3)'
