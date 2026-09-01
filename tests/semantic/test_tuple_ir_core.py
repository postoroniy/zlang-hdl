from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.ir.constants import constant_runtime_value
from zlang.ir.expressions import (
    Constant,
    InputRef,
    TupleConstruct,
    TupleProject,
)
from zlang.ir.module import Assignment, Module, Port, PortDirection
from zlang.ir.packing import PackingError, is_bit_packable, pack_runtime, unpack_runtime
from zlang.ir.runtime_values import runtime_value_fits, zero_runtime_value
from zlang.ir.traversal import walk_expression
from zlang.ir.type_codec import (
    TypeCodecError,
    canonical_type_data,
    canonical_type_from_data,
)
from zlang.ir.types import BitType, EnumType, SIntType, TupleType, UIntType
from zlang.opt.ir import ExpressionOp
from zlang.opt.lowering import (
    CanonicalizationError,
    lower_expression_graph,
    restore_expression,
)
from zlang.simulate import simulate


U4 = UIntType(4)
U8 = UIntType(8)


def test_structural_tuple_type_codec_width_and_bounded_arity() -> None:
    nested = TupleType((TupleType((U4, BitType())), SIntType(5)))
    assert nested.width == 10
    assert str(nested) == "((u4,bit),s5)"
    assert canonical_type_data(nested) == {
        "kind": "tuple",
        "elements": [
            {
                "kind": "tuple",
                "elements": [
                    {"kind": "uint", "width": 4},
                    {"kind": "bit", "width": 1},
                ],
            },
            {"kind": "sint", "width": 5},
        ],
    }
    assert canonical_type_from_data(canonical_type_data(nested)) == nested

    for count in (0, 1, 9):
        with pytest.raises(ValueError, match="between 2 and 8"):
            TupleType(tuple(U4 for _ in range(count)))
    with pytest.raises(TypeCodecError, match="tuple elements must be an array"):
        canonical_type_from_data({"kind": "tuple", "elements": {}})
    with pytest.raises(TypeCodecError, match="invalid tuple hardware type"):
        canonical_type_from_data(
            {"kind": "tuple", "elements": [{"kind": "uint", "width": 4}]}
        )


def test_tuple_runtime_domain_zero_and_msb_first_packing_are_exact() -> None:
    type_ = TupleType((U4, SIntType(4), BitType()))
    value = (0xA, -2, 1)
    assert runtime_value_fits(value, type_)
    assert not runtime_value_fits([0xA, -2, 1], type_)
    assert not runtime_value_fits((0xA, 14, 1), type_)
    assert zero_runtime_value(type_) == (0, 0, 0)
    assert is_bit_packable(type_)
    assert pack_runtime(type_, value) == 0b1010_1110_1
    assert unpack_runtime(type_, 0b1010_1110_1) == value

    enum = EnumType("Mode", ("A", "B"), "test::Mode")
    assert not is_bit_packable(TupleType((U4, enum)))
    with pytest.raises(PackingError, match="not bit-packable"):
        pack_runtime(TupleType((U4, enum)), (1, 0))


def test_tuple_expressions_validate_evaluate_and_round_trip_canonically() -> None:
    type_ = TupleType((U8, BitType()))
    constructed = TupleConstruct(
        (Constant(0xA5, U8), Constant(1, BitType())),
        type_,
    )
    projected = TupleProject(constructed, 0, U8)
    assert constant_runtime_value(constructed) == (0xA5, 1)
    assert constant_runtime_value(projected) == 0xA5

    nodes, root = lower_expression_graph(Module("Fixture", (), ()), projected)
    assert tuple(type(item) for item in walk_expression(projected)) == (
        TupleProject,
        TupleConstruct,
        Constant,
        Constant,
    )
    assert tuple(node.op for node in nodes) == (
        ExpressionOp.CONSTANT,
        ExpressionOp.CONSTANT,
        ExpressionOp.TUPLE_CONSTRUCT,
        ExpressionOp.TUPLE_PROJECT,
    )
    assert restore_expression(nodes, root) == projected

    output = Port(PortDirection.OUTPUT, "result", type_)
    first = Port(PortDirection.OUTPUT, "first", U8)
    pair = Port(PortDirection.INPUT, "pair", type_)
    module = Module(
        "TupleRuntime",
        (pair, output, first),
        (
            Assignment(output, constructed),
            Assignment(first, TupleProject(InputRef("pair", type_), 0, U8)),
        ),
    )
    assert simulate(module, pair=(0x3C, 0)) == {
        "result": (0xA5, 1),
        "first": 0x3C,
    }


def test_tuple_expression_and_canonical_validation_fail_closed() -> None:
    type_ = TupleType((U8, BitType()))
    with pytest.raises(ValueError, match="element types"):
        TupleConstruct((Constant(1, U8), Constant(2, U8)), type_)
    with pytest.raises(ValueError, match="requires a tuple"):
        TupleProject(Constant(0, U8), 0, U8)
    with pytest.raises(ValueError, match="out of range"):
        TupleProject(InputRef("pair", type_), 2, U8)
    with pytest.raises(ValueError, match="result type"):
        TupleProject(InputRef("pair", type_), 0, BitType())

    constructed = TupleConstruct(
        (Constant(1, U8), Constant(0, BitType())),
        type_,
    )
    projected = TupleProject(constructed, 0, U8)
    nodes, root = lower_expression_graph(Module("Fixture", (), ()), projected)

    malformed_type = replace(nodes[2], type=TupleType((BitType(), U8)))
    with pytest.raises(CanonicalizationError, match="tuple constructor"):
        restore_expression((*nodes[:2], malformed_type, nodes[3]), root)

    malformed_index = replace(
        nodes[root],
        attributes=(("index", 2),),
    )
    with pytest.raises(CanonicalizationError, match="tuple projection"):
        restore_expression((*nodes[:root], malformed_index), root)
