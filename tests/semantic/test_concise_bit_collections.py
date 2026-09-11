from __future__ import annotations

from dataclasses import replace
from itertools import product

import pytest

from zlang import exploration as exploration_module
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.types import BitType, BitsType, UIntType, VecType
from zlang.opt import CanonicalizationError, OptimizationStage, lower, restore
from zlang.opt.ir import ExpressionOp, pure_metadata
from zlang.semantic import SemanticError
from zlang.simulate import simulate


def _compile(source: str):
    return compile_source(source).ir


def _assignments(module):
    return {assignment.target.name: assignment.expression for assignment in module.assignments}


def test_scalar_and_homogeneous_vector_concat_have_distinct_typed_ir() -> None:
    module = _compile(
        "module ConcatKinds { in a:u4 in b:bits<3> in x:vec<2,u8> "
        "in y:vec<3,u8> out raw:bits<7> out values:vec<5,u8> "
        "raw=concat(a,b) values=concat(x,y) }"
    )
    values = _assignments(module)
    assert isinstance(values["raw"], expr.Concat)
    assert values["raw"].type == BitsType(7)
    assert isinstance(values["values"], expr.VectorConcat)
    assert values["values"].type == VecType(5, UIntType(8))
    assert simulate(module, a=0xA, b=0x5, x=[1, 2], y=[3, 4, 5]) == {
        "raw": 0x55,
        "values": [1, 2, 3, 4, 5],
    }


def test_nested_vector_concat_preserves_elements_without_flattening() -> None:
    module = _compile(
        "module NestedConcat { in a:vec<1,vec<2,u4>> in b:vec<2,vec<2,u4>> "
        "out y:vec<3,vec<2,u4>> y=concat(a,b) }"
    )
    expression = module.assignments[0].expression
    assert isinstance(expression, expr.VectorConcat)
    assert expression.type == VecType(3, VecType(2, UIntType(4)))
    assert simulate(module, a=[[1, 2]], b=[[3, 4], [5, 6]])["y"] == [
        [1, 2],
        [3, 4],
        [5, 6],
    ]


def test_contextual_and_explicit_reshape_preserve_outer_to_inner_order() -> None:
    module = _compile(
        "module Shape { in matrix:vec<2,vec<4,u8>> out flat:vec<8,u8> "
        "out regrouped:vec<4,vec<2,u8>> flat_value:vec<8,u8>=reshape(matrix) "
        "flat=flat_value regrouped=reshape<vec<4,vec<2,u8>>>(flat_value) }"
    )
    values = _assignments(module)
    assert isinstance(values["flat"], expr.Reshape)
    assert isinstance(values["regrouped"], expr.Reshape)
    source = [[0x10, 0x11, 0x12, 0x13], [0x20, 0x21, 0x22, 0x23]]
    assert simulate(module, matrix=source) == {
        "flat": [0x10, 0x11, 0x12, 0x13, 0x20, 0x21, 0x22, 0x23],
        "regrouped": [
            [0x10, 0x11],
            [0x12, 0x13],
            [0x20, 0x21],
            [0x22, 0x23],
        ],
    }


def test_reshape_preserves_nominal_struct_and_enum_leaves() -> None:
    module = _compile(
        "enum E { A B C } struct Pair { value:u4 flag:bit } "
        "module NominalShape { in pairs:vec<2,vec<2,Pair>> "
        "out pair_flat:vec<4,Pair> out states:vec<2,vec<2,E>> "
        "pair_flat=reshape(pairs) "
        "states=reshape<vec<2,vec<2,E>>>(generate(i in 0..4) E.A) }"
    )
    values = _assignments(module)
    assert isinstance(values["pair_flat"], expr.Reshape)
    assert isinstance(values["states"], expr.Reshape)
    pairs = [
        [{"value": 1, "flag": 0}, {"value": 2, "flag": 1}],
        [{"value": 3, "flag": 0}, {"value": 4, "flag": 1}],
    ]
    result = simulate(module, pairs=pairs)
    assert result["pair_flat"] == [item for row in pairs for item in row]
    assert result["states"] == [[0, 0], [0, 0]]


def test_bitcast_legacy_spelling_and_raw_boundary_normalize_to_bitcast() -> None:
    module = _compile(
        "struct Pair { hi:u4 lo:u4 } module Casts { in x:s8 in raw:bits<8> "
        "in pair:Pair in fixed_value:fixed<8,4> "
        "out a:bits<8> out b:bits<8> out c:s8 out d:s8 "
        "out e:bits<8> out bitvec:vec<8,bit> "
        "out pair_raw:bits<8> out pair_again:Pair "
        "out fixed_bits:bits<8> out fixed_again:fixed<8,4> "
        "a=bitcast<bits<8>>(x) b=pack(x) c=bitcast<s8>(raw) "
        "d=unpack<s8>(raw) e=pair bitvec=raw "
        "pair_raw=bitcast<bits<8>>(pair) pair_again=bitcast<Pair>(raw) "
        "fixed_bits=bitcast<bits<8>>(fixed_value) "
        "fixed_again=bitcast<fixed<8,4>>(raw) }"
    )
    values = _assignments(module)
    assert all(isinstance(values[name], expr.Bitcast) for name in values)
    assert values["a"] == values["b"]
    assert values["c"] == values["d"]
    assert simulate(
        module,
        x=-2,
        raw=0x80,
        pair={"hi": 0xA, "lo": 0x5},
        fixed_value=-2,
    ) == {
        "a": 0xFE,
        "b": 0xFE,
        "c": -128,
        "d": -128,
        "e": 0xA5,
        "bitvec": [1, 0, 0, 0, 0, 0, 0, 0],
        "pair_raw": 0xA5,
        "pair_again": {"hi": 0x8, "lo": 0x0},
        "fixed_bits": 0xFE,
        "fixed_again": -128,
    }


def test_bitcast_vector_representation_uses_element_zero_at_msb() -> None:
    module = _compile(
        "module CastVector { in values:vec<2,u4> out raw:bits<8> "
        "out again:vec<2,u4> raw_value=bitcast<bits<8>>(values) "
        "raw=raw_value again=bitcast<vec<2,u4>>(raw_value) }"
    )
    assert simulate(module, values=[0xA, 0x5]) == {
        "raw": 0xA5,
        "again": [0xA, 0x5],
    }


def test_packed_bits_use_lsb_zero_single_bit_indexing() -> None:
    module = _compile(
        "module PackedIndex { in raw:bits<8> out lsb:bit out msb:bit "
        "out reversed:bits<8> lsb=raw[0] msb=raw[7] "
        "reversed=bitcast<bits<8>>(generate(i in 0..8) raw[i]) }"
    )
    values = _assignments(module)
    assert isinstance(values["lsb"], expr.Bitcast)
    assert isinstance(values["lsb"].expression, expr.Slice)
    assert (values["lsb"].expression.msb, values["lsb"].expression.lsb) == (0, 0)
    assert values["lsb"].type == BitType()
    assert simulate(module, raw=0x96) == {
        "lsb": 0,
        "msb": 1,
        "reversed": 0x69,
    }
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module M { in x:bits<8> out y:bit y=x[8] }",
            "packed bit index 8 is out of range for bits<8>",
        ),
        (
            "module M { in x:bits<8> in i:u3 out y:bit y=x[i] }",
            "packed bit indexing requires a compile-time-proven selector",
        ),
    ),
)
def test_invalid_packed_bit_indices_fail_closed(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)


@pytest.mark.parametrize("width", range(1, 7))
def test_scalar_representation_reductions_are_exhaustive(width: int) -> None:
    source = (
        f"module R {{ in u:u{width} in s:s{width} in raw:bits<{width}> "
        "out ux:bit out ua:bit out uo:bit out sx:bit out p:bit "
        "ux=reduce(^,u) ua=reduce(&,u) uo=reduce(|,u) "
        "sx=reduce(^,s) p=parity(raw) }"
    )
    module = _compile(source)
    mask = (1 << width) - 1
    for raw in range(1 << width):
        signed = raw if raw < (1 << (width - 1)) else raw - (1 << width)
        result = simulate(module, u=raw, s=signed, raw=raw)
        parity = raw.bit_count() & 1
        assert result == {
            "ux": parity,
            "ua": int(raw == mask),
            "uo": int(raw != 0),
            "sx": parity,
            "p": parity,
        }


def test_parity_accepts_bit_and_flat_bit_vector() -> None:
    module = _compile(
        "module P { in b:bit in v:vec<5,bit> out pb:bit out pv:bit "
        "pb=parity(b) pv=parity(v) }"
    )
    for b, bits in product(range(2), repeat=2):
        vector = [b, bits, 1, 0, 1]
        assert simulate(module, b=b, v=vector) == {
            "pb": b,
            "pv": sum(vector) & 1,
        }


def test_parity_is_a_protected_compiler_intrinsic() -> None:
    with pytest.raises(
        SemanticError, match="function name 'parity' is reserved for a compiler intrinsic"
    ):
        _compile(
            "fn parity(x:u8)->bit{0} "
            "module M { in x:u8 out y:bit y=parity(x) }"
        )


def test_concise_nodes_round_trip_canonical_with_origins() -> None:
    module = _compile(
        "module C { in a:vec<2,u4> in b:vec<2,u4> in raw:bits<8> "
        "out joined:vec<4,u4> out shaped:vec<2,vec<2,u4>> out n:u8 "
        "joined_value:vec<4,u4>=concat(a,b) "
        "joined=joined_value shaped=reshape(joined_value) n=bitcast<u8>(raw) }"
    )
    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    assert restore(canonical) == module
    assert {node.op.value for node in canonical.expressions} >= {
        "vector_concat",
        "reshape",
        "bitcast",
    }
    assert all(assignment.expression.origin is not None for assignment in module.assignments)


def test_exploration_traverses_scalar_concat_and_legacy_representation_nodes() -> None:
    module = _compile(
        "module Inputs { in a:u4 in b:u4 out y:bits<8> y=concat(a,b) }"
    )
    root = module.assignments[0].expression
    assert set(exploration_module._input_refs(root)) == {"a", "b"}

    # Pack/Unpack no longer originate from new source, but remain public typed
    # compatibility records and must not become traversal dead ends.
    raw = expr.InputRef("raw", BitsType(8))
    legacy = expr.Pack(
        expr.Unpack(raw, UIntType(8)),
        BitsType(8),
    )
    sliced = expr.Slice(legacy, 7, 4, BitsType(4))
    assert set(exploration_module._input_refs(sliced)) == {"raw"}


def test_malformed_concise_canonical_nodes_fail_closed() -> None:
    concat_module = _compile(
        "module C { in a:u4 in b:u4 out y:bits<8> y=concat(a,b) }"
    )
    concat_canonical = lower(concat_module, stage=OptimizationStage.HIGH_LEVEL)
    concat_index, concat_node = next(
        (index, node)
        for index, node in enumerate(concat_canonical.expressions)
        if node.op is ExpressionOp.CONCAT
    )
    short_concat = replace(concat_node, operands=concat_node.operands[:1])
    expressions = list(concat_canonical.expressions)
    expressions[concat_index] = short_concat
    with pytest.raises(CanonicalizationError, match="at least two operands"):
        restore(replace(concat_canonical, expressions=tuple(expressions)))

    wrong_type = BitsType(7)
    narrow_concat = replace(
        concat_node,
        type=wrong_type,
        metadata=pure_metadata(wrong_type),
    )
    expressions[concat_index] = narrow_concat
    with pytest.raises(CanonicalizationError, match="result width"):
        restore(replace(concat_canonical, expressions=tuple(expressions)))

    cast_module = _compile(
        "module B { in raw:bits<8> out y:u8 y=bitcast<u8>(raw) }"
    )
    cast_canonical = lower(cast_module, stage=OptimizationStage.HIGH_LEVEL)
    cast_index, cast_node = next(
        (index, node)
        for index, node in enumerate(cast_canonical.expressions)
        if node.op is ExpressionOp.BITCAST
    )
    expressions = list(cast_canonical.expressions)
    expressions[cast_index] = replace(
        cast_node, operands=(cast_node.operands[0], cast_node.operands[0])
    )
    with pytest.raises(CanonicalizationError, match="exactly one operand"):
        restore(replace(cast_canonical, expressions=tuple(expressions)))

    reshape_module = _compile(
        "module R { in x:vec<2,vec<2,u8>> out y:vec<4,u8> "
        "y=reshape<vec<4,u8>>(x) }"
    )
    reshape_canonical = lower(reshape_module, stage=OptimizationStage.HIGH_LEVEL)
    reshape_index, reshape_node = next(
        (index, node)
        for index, node in enumerate(reshape_canonical.expressions)
        if node.op is ExpressionOp.RESHAPE
    )
    expressions = list(reshape_canonical.expressions)
    expressions[reshape_index] = replace(reshape_node, operands=())
    with pytest.raises(CanonicalizationError, match="exactly one operand"):
        restore(replace(reshape_canonical, expressions=tuple(expressions)))


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module M { in a:vec<2,u8> in b:u8 out y:bits<24> y=concat(a,b) }",
            "concat cannot mix vector and non-vector operands",
        ),
        (
            "module M { in a:vec<2,u8> in b:vec<2,u9> out y:vec<4,u8> y=concat(a,b) }",
            "vector concat requires exact common element type",
        ),
        (
            "module M { in a:vec<2,u8> out y:vec<4,u8> y=reshape(a) }",
            "reshape requires equal leaf count",
        ),
        (
            "module M { in a:vec<2,u8> out y:vec<2,u9> y=reshape(a) }",
            "reshape requires exact common leaf type",
        ),
        (
            "module M { in a:vec<2,u8> out y:vec<2,u8> y=reshape<u16>(a) }",
            "reshape target must be a vector",
        ),
        (
            "module M { in a:vec<2,u8> out y:vec<2,u8> tmp=reshape(a) y=tmp }",
            "contextual reshape requires an unambiguous vector target",
        ),
        (
            "module M { in x:u8 out y:bits<7> y=bitcast<bits<7>>(x) }",
            "requires equal packed widths",
        ),
        (
            "enum E { A B } module M { out y:bits<1> y=bitcast<bits<1>>(E.A) }",
            "must be recursively bit-packable and non-enum",
        ),
        (
            "module M { in x:s8 out y:u8 y=x }",
            "cannot assign s8 expression to u8 output",
        ),
        (
            "module M { in x:fixed<8,4> out y:u8 y=x }",
            "cannot assign fixed<8,4> expression to u8 output",
        ),
        (
            "module M { in x:bits<8> out y:u7 y=x }",
            "cannot assign bits<8> expression to u7 output",
        ),
        (
            "struct S { x:u8 } module M { in x:S out y:u8 y=x }",
            "cannot assign S expression to u8 output",
        ),
        (
            "struct S { x:u8 } module M { in x:S out y:bits<8> y=reduce(^,x) }",
            "scalar reduce(^, ...) requires",
        ),
        (
            "module M { in x:fixed<8,4> out y:bit y=parity(x) }",
            "parity requires",
        ),
    ),
)
def test_invalid_concise_bit_collection_forms_fail_closed(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError) as caught:
        _compile(source)
    assert message in str(caught.value)
