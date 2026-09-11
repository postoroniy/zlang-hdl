from __future__ import annotations

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.packing import pack_runtime, unpack_runtime
from zlang.ir.types import BitsType, FixedType, SIntType, StructType, VecType
from zlang.opt import OptimizationStage, lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate


PACKING_SOURCE = """
struct Pair { hi:u4 lo:u4 }
struct Nested { pair:Pair samples:vec<2,u4> }
module Packing {
    in a:u4
    in b:u4
    in pair_in:Pair
    in samples:vec<2,u4>
    in signed_value:s8
    in fixed_value:fixed<8,4>
    out joined:bits<8>
    out upper:bits<4>
    out pair_bits:bits<8>
    out pair_out:Pair
    out sample_bits:bits<8>
    out samples_out:vec<2,u4>
    out signed_bits:bits<8>
    out signed_out:s8
    out fixed_bits:bits<8>
    out fixed_out:fixed<8,4>
    local:bits<8> = concat(a,b)
    joined = local
    upper = local[7:4]
    pair_bits = pack(pair_in)
    pair_out = unpack<Pair>(pack(pair_in))
    sample_bits = pack(samples)
    samples_out = unpack<vec<2,u4>>(pack(samples))
    signed_bits = pack(signed_value)
    signed_out = unpack<s8>(pack(signed_value))
    fixed_bits = pack(fixed_value)
    fixed_out = unpack<fixed<8,4>>(pack(fixed_value))
}
"""


def _compile(source: str):
    return compile_source(source).ir


def test_packing_types_nodes_origins_and_runtime_layout_are_exact() -> None:
    module = _compile(PACKING_SOURCE)
    expressions = {item.target.name: item.expression for item in module.assignments}
    assert isinstance(expressions["joined"], expr.Concat)
    assert isinstance(expressions["upper"], expr.Slice)
    assert isinstance(expressions["pair_bits"], expr.Bitcast)
    # Adjacent equal-width pack/unpack compatibility forms erase to the
    # original concrete value instead of retaining a redundant cast pair.
    assert isinstance(expressions["pair_out"], expr.InputRef)
    assert expressions["pair_out"].name == "pair_in"
    assert expressions["joined"].type == BitsType(8)
    assert expressions["upper"].type == BitsType(4)
    assert (expressions["upper"].msb, expressions["upper"].lsb) == (7, 4)
    assert all(value.origin is not None for value in expressions.values())

    result = simulate(
        module,
        a=0xA,
        b=0x5,
        pair_in={"hi": 0xC, "lo": 0x2},
        samples=[0x3, 0xD],
        signed_value=-1,
        fixed_value=-2,
    )
    assert result == {
        "joined": 0xA5,
        "upper": 0xA,
        "pair_bits": 0xC2,
        "pair_out": {"hi": 0xC, "lo": 0x2},
        "sample_bits": 0x3D,
        "samples_out": [0x3, 0xD],
        "signed_bits": 0xFF,
        "signed_out": -1,
        "fixed_bits": 0xFE,
        "fixed_out": -2,
    }


def test_shared_runtime_layout_is_field_zero_and_element_zero_at_msb() -> None:
    module = _compile(PACKING_SOURCE)
    pair = next(type_ for type_ in module.structs if type_.name == "Pair")
    samples = VecType(2, next(field.type for field in pair.fields if field.name == "hi"))
    assert pack_runtime(pair, {"hi": 0xA, "lo": 0x5}) == 0xA5
    assert unpack_runtime(pair, 0xA5) == {"hi": 0xA, "lo": 0x5}
    assert pack_runtime(samples, [0x1, 0xE]) == 0x1E
    assert unpack_runtime(samples, 0x1E) == [0x1, 0xE]
    assert unpack_runtime(SIntType(8), 0xFF) == -1
    assert unpack_runtime(FixedType(8, 4), 0x80) == -128


def test_packing_survives_canonical_and_artifact_round_trips() -> None:
    module = _compile(PACKING_SOURCE)
    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    restored = restore(canonical)
    assert restored == module
    operations = {node.op.value for node in canonical.expressions}
    assert {"slice", "concat", "bitcast"} <= operations
    assert "pack" not in operations
    assert "unpack" not in operations

    artifact = emit_artifact(module)
    restored_artifact = BackendArtifact.from_json(artifact.to_json())
    assert restored_artifact.artifact_hash == artifact.artifact_hash
    for signal, type_name in (
        ("port:pair_out", "Pair"),
        ("port:samples_out", "vec<2,u4>"),
        ("port:signed_out", "s8"),
        ("port:fixed_out", "fixed<8,4>"),
    ):
        binding = next(
            item for item in restored_artifact.bindings
            if item.semantic_signal_id == signal
        )
        assert binding.canonical_type == type_name


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module M { in x:u8 out y:bits<4> y=x[2:5] }",
            "bit slice [2:5] is reversed",
        ),
        (
            "module M { in x:u8 out y:bits<2> y=x[8:7] }",
            "bit slice [8:7] is out of range",
        ),
        (
            "module M { in x:u8 out y:bits<2> y=x[-1:0] }",
            "bit slice [-1:0] is reversed",
        ),
        (
            "module M { in x:u8 in i:u3 out y:bits<2> y=x[i:0] }",
            "unresolved compile-time parameter 'i' in bit-slice MSB",
        ),
        (
            "module M { in x:u8 out y:bits<8> y=concat(x) }",
            "concat requires at least two operands",
        ),
        (
            "module M { in x:u8 out y:bits<7> y=pack(x) }",
            "pack produces bits<8>, expected exact bits<7>",
        ),
        (
            "module M { in x:u8 out y:u8 y=unpack<u8>(x) }",
            "requires exact bits<8> source",
        ),
        (
            "module M { in x:bits<7> out y:u8 y=unpack<u8>(x) }",
            "requires exact bits<8> source, got bits<7>",
        ),
        (
            "enum E { A B } module M { out y:bits<1> y=pack(E.A) }",
            "pack requires a recursively bit-packable non-enum value",
        ),
        (
            "enum E { A B } struct S { e:E } module M { "
            "out y:bits<1> s:S=S{e=E.A} y=pack(s) }",
            "pack requires a recursively bit-packable non-enum value",
        ),
        (
            "enum E { A B } module M { in x:bits<1> out y:E y=unpack<E>(x) }",
            "unpack target must be recursively bit-packable and non-enum",
        ),
    ),
)
def test_invalid_packing_forms_fail_closed(source: str, message: str) -> None:
    with pytest.raises(SemanticError) as caught:
        _compile(source)
    assert message in str(caught.value)


def test_parameterized_slice_bounds_fold_before_typed_ir() -> None:
    module = _compile(
        "module Low<N=8> { in x:bits<N> out y:bits<N> y=x[N-1:0] }"
    )
    expression = module.assignments[0].expression
    assert isinstance(expression, expr.Slice)
    assert (expression.msb, expression.lsb, expression.type) == (7, 0, BitsType(8))
