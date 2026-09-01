from __future__ import annotations

from zlang.ast import (
    BitcastExpr,
    ConcatExpr,
    PackExpr,
    ReshapeExpr,
    SliceExpr,
    UnpackExpr,
)
from zlang.parser import parse


def test_slice_concat_pack_and_unpack_have_dedicated_ast_nodes() -> None:
    syntax = parse(
        "struct Pair { hi:u4 lo:u4 } module Packing { "
        "in x:u8 out upper:bits<4> out joined:bits<8> out raw:bits<8> "
        "out pair:Pair upper=x[7:4] joined=concat(x[7:4],x[3:0]) "
        "raw=pack(pair) pair=unpack<Pair>(joined) }"
    )
    upper, joined, raw, pair = (
        assignment.expression for assignment in syntax.assignments
    )
    assert isinstance(upper, SliceExpr)
    assert (upper.msb, upper.lsb) == (7, 4)
    assert isinstance(joined, ConcatExpr)
    assert len(joined.arguments) == 2
    assert all(isinstance(argument, SliceExpr) for argument in joined.arguments)
    assert isinstance(raw, PackExpr)
    assert isinstance(pair, UnpackExpr)
    assert pair.target_type.text == "Pair"
    for expression in (upper, joined, raw, pair):
        assert expression.origin is not None


def test_slice_bounds_keep_compile_time_parameter_expressions() -> None:
    syntax = parse(
        "module Low<N=8> { in x:bits<N> out y:bits<N> y=x[N-1:0] }"
    )
    expression = syntax.assignments[0].expression
    assert isinstance(expression, SliceExpr)
    assert expression.msb == "N-1"
    assert expression.lsb == 0


def test_bitcast_and_both_reshape_spellings_have_dedicated_ast_nodes() -> None:
    syntax = parse(
        "module Shapes { in x:bits<8> in nested:vec<2,vec<2,u8>> "
        "out numeric:u8 out flat:vec<4,u8> out nested_again:vec<2,vec<2,u8>> "
        "numeric=bitcast<u8>(x) flat=reshape(nested) "
        "nested_again=reshape<vec<2,vec<2,u8>>>(flat) }"
    )
    numeric, flat, nested_again = (
        assignment.expression for assignment in syntax.assignments
    )
    assert isinstance(numeric, BitcastExpr)
    assert numeric.target_type.text == "u8"
    assert isinstance(flat, ReshapeExpr)
    assert flat.target_type is None
    assert isinstance(nested_again, ReshapeExpr)
    assert nested_again.target_type is not None
    assert nested_again.target_type.length == 2
    assert nested_again.target_type.element_type.length == 2
    assert nested_again.target_type.element_type.element_type.text == "u8"
    assert all(
        expression.origin is not None
        for expression in (numeric, flat, nested_again)
    )
