import pytest

from zlang.ir.type_codec import (
    TypeCodecError,
    canonical_type_data,
    canonical_type_from_data,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedOverflowPolicy,
    FixedType,
    SIntType,
    StructField,
    StructType,
    UFixedType,
    UIntType,
    VecType,
)


@pytest.mark.parametrize(
    "type_",
    (
        BitType(),
        UIntType(7),
        SIntType(9),
        BitsType(5),
        FixedType(12, 7),
        UFixedType(10, 4, FixedOverflowPolicy.SATURATE),
        EnumType("State", ("Idle", "Run"), "enum.state"),
        EnumType(
            "Sparse", ("A", "B"), "enum.sparse", explicit_width=3,
            explicit_codes=(1, 6),
        ),
        StructType(
            "Pair",
            (StructField("left", UIntType(8)), StructField("right", BitsType(3))),
        ),
        VecType(4, StructType("Lane", (StructField("x", SIntType(6)),))),
    ),
)
def test_canonical_hardware_type_codec_round_trip(type_) -> None:
    assert canonical_type_from_data(canonical_type_data(type_)) == type_


def test_canonical_hardware_type_codec_rejects_unknown_fields() -> None:
    with pytest.raises(TypeCodecError, match="unknown surprise"):
        canonical_type_from_data({"kind": "uint", "width": 8, "surprise": 1})


def test_ordinal_enum_codec_rejects_nonordinal_codes() -> None:
    with pytest.raises(TypeCodecError, match="ordinal enum codes"):
        canonical_type_from_data(
            {
                "kind": "enum",
                "name": "State",
                "members": ["Idle", "Run"],
                "declaration_identity": "enum.state",
                "explicit_width": None,
                "codes": [1, 2],
            }
        )


@pytest.mark.parametrize(
    "payload, detail",
    (
        ({"kind": "uint", "width": 0}, "invalid uint hardware type"),
        (
            {
                "kind": "fixed",
                "width": 8,
                "fraction": 8,
                "overflow": "wrap",
            },
            "invalid fixed hardware type",
        ),
        (
            {
                "kind": "fixed",
                "width": 8,
                "fraction": 4,
                "overflow": "clamp-ish",
            },
            "invalid fixed hardware type",
        ),
        (
            {
                "kind": "vec",
                "length": 0,
                "element_type": {"kind": "bit", "width": 1},
            },
            "invalid vector hardware type",
        ),
    ),
)
def test_codec_normalizes_constructor_failures(payload, detail) -> None:
    with pytest.raises(TypeCodecError, match=detail):
        canonical_type_from_data(payload)
