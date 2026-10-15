from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.implementation_regions import canonical_type_data
from zlang.ir.expressions import EnumDecode, EnumEncode, EnumValid, Switch
from zlang.ir.formal import generate_properties
from zlang.ir.types import BitType, BitsType, EnumType, UIntType
from zlang.opt import OptimizationStage, lower, restore
from zlang.opt.identity import canonical_ir_identity
from zlang.opt.ir import ExpressionOp
from zlang.semantic import SemanticError
from zlang.native_simulation import simulate


SOURCE = """
enum WifiRate : bits<3> {
    Continue = 0
    Bpsk6 = 1
    Qpsk12 = 2
    Qam16_24 = 4
}

module EncodedEnum {
    in raw : bits<3>
    out encoded : bits<3>
    out valid : bit
    out selected : u3

    rate : WifiRate = enum_decode<WifiRate>(raw, WifiRate.Continue)
    encoded = enum_encode(rate)
    valid = enum_valid<WifiRate>(raw)
    selected = switch rate {
        WifiRate.Continue => 0
        WifiRate.Bpsk6 => 1
        WifiRate.Qpsk12 => 2
        WifiRate.Qam16_24 => 7
    }
}
"""


def _compile(source: str):
    return compile_source(source).ir


def test_sparse_enum_has_exact_codes_and_safe_intrinsic_ir() -> None:
    module = _compile(SOURCE)
    (enum_type,) = module.enums
    assert isinstance(enum_type, EnumType)
    assert enum_type.width == 3
    assert enum_type.codes == (0, 1, 2, 4)
    assert enum_type.explicit_codes == (0, 1, 2, 4)
    assert isinstance(module.assignments[0].expression, EnumEncode)
    # Immutable locals are deliberately substituted before backend-independent
    # IR publication; the safe decode remains an explicit typed child node.
    assert isinstance(module.assignments[0].expression.expression, EnumDecode)
    assert isinstance(module.assignments[1].expression, EnumValid)
    assert isinstance(module.assignments[2].expression, Switch)
    assert tuple(case.key for case in module.assignments[2].expression.cases) == (
        0, 1, 2, 4,
    )


@pytest.mark.parametrize(
    "raw,expected",
    (
        (0, {"encoded": 0, "valid": 1, "selected": 0}),
        (1, {"encoded": 1, "valid": 1, "selected": 1}),
        (2, {"encoded": 2, "valid": 1, "selected": 2}),
        (4, {"encoded": 4, "valid": 1, "selected": 7}),
        (3, {"encoded": 0, "valid": 0, "selected": 0}),
        (7, {"encoded": 0, "valid": 0, "selected": 0}),
    ),
)
def test_sparse_enum_decode_is_total_and_membership_is_exact(
    raw: int, expected: dict[str, int]
) -> None:
    assert simulate(_compile(SOURCE), raw=raw) == expected


def test_sparse_enum_survives_canonical_and_artifact_round_trip() -> None:
    module = _compile(SOURCE)
    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    assert {
        ExpressionOp.ENUM_ENCODE,
        ExpressionOp.ENUM_VALID,
        ExpressionOp.ENUM_DECODE,
    }.issubset({node.op for node in canonical.expressions})
    assert restore(canonical) == module

    artifact = emit_artifact(module)
    restored = BackendArtifact.from_json(artifact.to_json())
    binding = next(
        item for item in restored.bindings
        if item.semantic_signal_id == "port:encoded"
    )
    assert binding.canonical_type == "bits<3>"
    assert "Qam16_24=4" in str(module.enums[0])

    encoded = next(
        node for node in canonical.expressions
        if node.op is ExpressionOp.ENUM_ENCODE
    )
    malformed = replace(
        encoded,
        type=BitsType(2),
        metadata=replace(encoded.metadata, width=2),
    )
    with pytest.raises(ValueError, match="enum encode.*invalid types"):
        replace(
            canonical,
            expressions=tuple(
                malformed if node.id == encoded.id else node
                for node in canonical.expressions
            ),
        )


def test_ordinal_enum_abi_remains_unchanged() -> None:
    enum_type = _compile(
        "enum Phase { Idle Active Done } module M { out y:Phase y=Phase.Done }"
    ).enums[0]
    assert enum_type.explicit_width is None
    assert enum_type.explicit_codes is None
    assert enum_type.codes == (0, 1, 2)
    assert enum_type.width == 2
    assert str(enum_type).startswith("enum<Phase:Idle,Active,Done@")


def test_bare_nominal_compile_time_condition_selects_without_parentheses() -> None:
    module = _compile(
        "enum E { A B } "
        "fn flag<type T>(x:T) { if T == E { 1 } else { 0 } } "
        "module M { out y:u1 y=flag(E.A) }"
    )
    assert simulate(module) == {"y": 1}


def test_encoded_enum_intrinsics_remain_visible_through_function_traversals() -> None:
    module = _compile(
        "enum E : bits<2> { A=0 B=2 } "
        "fn safe(raw:bits<2>)->bits<2> { "
        "enum_encode(enum_decode<E>(raw,E.A)) } "
        "module M { in x:bits<2> out y:bits<2> y=safe(x) }"
    )
    assert simulate(module, x=2) == {"y": 2}
    assert simulate(module, x=1) == {"y": 0}


@pytest.mark.parametrize(
    "source,message",
    (
        (
            "enum E { A=0 B=1 } module M { out y:E y=E.A }",
            "member codes require an explicit bit, bits<W>, or unsigned integer",
        ),
        (
            "enum E : bits<3> { A=1 B=1 } module M { out y:E y=E.A }",
            "duplicate code 1",
        ),
        (
            "enum E : bits<2> { A=0 B=4 } module M { out y:E y=E.A }",
            "code 4 does not fit bits<2>",
        ),
        (
            "enum E { A B } module M { in x:u2 out y:bits<1> y=enum_encode(x) }",
            "requires a nominal enum input",
        ),
        (
            "enum E { A B } module M { in x:bits<2> out y:bit y=enum_valid<E>(x) }",
            "requires exact bits<1> input",
        ),
        (
            "enum E { A B } module M { in x:bits<1> out y:E y=enum_decode<E>(x) }",
            "enum_decode expects 2 arguments",
        ),
        (
            "enum E { A B } enum F { A B } module M { in x:bits<1> out y:E "
            "y=enum_decode<E>(x,F.A) }",
            "matching nominal enum|expected exact",
        ),
        (
            "enum E { A B } module M { in x:bits<1> out y:E y=x }",
            "has type bits<1>, expected E|expected enum|cannot",
        ),
    ),
)
def test_invalid_encoded_enum_forms_fail_closed(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)


def test_enum_intrinsic_result_types_are_exact() -> None:
    module = _compile(SOURCE)
    assert module.assignments[0].expression.type == BitsType(3)
    assert module.assignments[1].expression.type == BitType()
    assert module.assignments[0].expression.expression.type == module.enums[0]


@pytest.mark.parametrize(
    "backing,expected_type",
    (
        ("bit", BitType()),
        ("bits<8>", BitsType(8)),
        ("u8", UIntType(8)),
        ("u<8>", UIntType(8)),
        ("uint<8>", UIntType(8)),
    ),
)
def test_explicit_enum_backing_controls_intrinsic_type(
    backing: str, expected_type
) -> None:
    module = _compile(
        f"enum E : {backing} {{ A B=10 C=120 D }} "
        f"module M {{ out encoded:{backing} encoded=enum_encode(E.D) }}"
    ) if backing != "bit" else _compile(
        "enum E : bit { A B } module M { out encoded:bit "
        "encoded=enum_encode(E.B) }"
    )
    enum_type = module.enums[0]
    assert enum_type.encoding_type == expected_type
    assert module.assignments[0].expression.type == expected_type
    assert enum_type.codes == (
        (0, 1) if backing == "bit" else (0, 10, 120, 121)
    )


def test_uint_backed_enum_encoding_is_explicit_numeric_boundary() -> None:
    module = _compile(
        "enum E : u8 { A B=10 C=120 D } "
        "module M { out y:u9 y=enum_encode(E.D)+1 }"
    )
    assert module.enums[0].codes == (0, 10, 120, 121)
    assert simulate(module) == {"y": 122}
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module


def test_uint_backed_enum_decode_and_membership_use_exact_backing() -> None:
    module = _compile(
        "enum E : uint<8> { A B=10 C=120 D } "
        "module M { in raw:uint<8> out encoded:uint<8> out valid:bit "
        "value:E=enum_decode<E>(raw,E.A) "
        "encoded=enum_encode(value) valid=enum_valid<E>(raw) }"
    )
    assert simulate(module, raw=121) == {"encoded": 121, "valid": 1}
    assert simulate(module, raw=11) == {"encoded": 0, "valid": 0}


def test_raw_backed_enum_still_requires_explicit_numeric_conversion() -> None:
    with pytest.raises(SemanticError, match="addition is not defined for bits<8>"):
        _compile(
            "enum E : bits<8> { A B=10 } "
            "module M { out y:u9 y=enum_encode(E.B)+1 }"
        )


def test_raw_backed_enum_can_cross_an_explicit_numeric_bitcast() -> None:
    module = _compile(
        "enum E : bits<8> { A B=10 } "
        "module M { out y:u9 y=bitcast<u8>(enum_encode(E.B))+1 }"
    )
    assert simulate(module) == {"y": 11}


def test_implicit_enum_codes_follow_the_previous_resolved_code() -> None:
    enum_type = _compile(
        "enum E : u8 { A B=10 C=120 D } module M { out y:E y=E.D }"
    ).enums[0]
    assert enum_type.codes == (0, 10, 120, 121)
    assert str(enum_type).endswith("A=0,B=10,C=120,D=121:u8@compilation:M::enum::E>")


@pytest.mark.parametrize(
    "source,message",
    (
        (
            "enum E : bit { A B C } module M { out y:E y=E.A }",
            "code 2 does not fit bit",
        ),
        (
            "enum E : u2 { A=3 B } module M { out y:E y=E.A }",
            "code 4 does not fit u2",
        ),
        (
            "enum E : s8 { A B } module M { out y:E y=E.A }",
            "backing type must be bit, bits<W>, or an unsigned integer",
        ),
    ),
)
def test_enum_backing_and_implicit_codes_fail_closed(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)


def test_sparse_code_table_participates_in_canonical_type_identity() -> None:
    enum_type = _compile(SOURCE).enums[0]
    assert canonical_type_data(enum_type) == {
        "kind": "enum",
        "name": "WifiRate",
        "members": ["Continue", "Bpsk6", "Qpsk12", "Qam16_24"],
        "declaration_identity": "compilation:EncodedEnum::enum::WifiRate",
        "explicit_width": 3,
        "codes": [0, 1, 2, 4],
    }
    changed = _compile(SOURCE.replace("Qam16_24 = 4", "Qam16_24 = 3"))
    assert canonical_ir_identity(lower(changed)) != canonical_ir_identity(
        lower(_compile(SOURCE))
    )


def test_existing_register_safety_family_rejects_sparse_encoding_holes() -> None:
    module = _compile(
        "enum E : bits<3> { A=0 B=2 C=5 } "
        "module M { clock clk reset rst in raw:bits<3> out y:bits<3> "
        "reg state:E=E.A state <- enum_decode<E>(raw,E.A) "
        "y=enum_encode(state) }"
    )
    property_ = next(
        item for item in generate_properties(module).properties
        if item.generated_from == "register:state"
    )
    assert property_.expression == "state == 0 || state == 2 || state == 5"
    assert property_.predicate is not None
    assert property_.predicate.render() == (
        "(((register:state == 0) || (register:state == 2)) || "
        "(register:state == 5))"
    )
