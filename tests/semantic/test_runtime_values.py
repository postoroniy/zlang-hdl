from __future__ import annotations

import pytest

from zlang.ir.runtime_values import (
    RuntimeValueError,
    flatten_vector_value,
    normalize_scalar,
    rebuild_vector_value,
    runtime_value_fits,
    scalar_fits,
    zero_runtime_value,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    SIntType,
    StructField,
    StructType,
    UFixedType,
    UIntType,
    VecType,
)


@pytest.mark.parametrize("width", range(1, 7))
def test_scalar_domains_and_normalization_match_finite_width_arithmetic(
    width: int,
) -> None:
    mask = (1 << width) - 1
    for value in range(-(1 << (width + 1)), 1 << (width + 1)):
        assert scalar_fits(value, UIntType(width)) == (0 <= value <= mask)
        assert scalar_fits(value, BitsType(width)) == (0 <= value <= mask)
        assert scalar_fits(value, UFixedType(width, 0)) == (0 <= value <= mask)

        signed_minimum = -(1 << (width - 1))
        signed_maximum = (1 << (width - 1)) - 1
        signed_fits = signed_minimum <= value <= signed_maximum
        assert scalar_fits(value, SIntType(width)) == signed_fits
        assert scalar_fits(value, FixedType(width, 0)) == signed_fits

        assert normalize_scalar(value, UIntType(width)) == (value & mask)
        signed_raw = value & mask
        signed_value = (
            signed_raw - (1 << width)
            if signed_raw >= (1 << (width - 1))
            else signed_raw
        )
        assert normalize_scalar(value, SIntType(width)) == signed_value
        assert normalize_scalar(value, FixedType(width, 0)) == signed_value


def test_enum_and_nested_zero_values_preserve_nominal_domains() -> None:
    enum = EnumType(
        "Sparse",
        ("Idle", "Run"),
        "test::Sparse",
        explicit_width=3,
        explicit_codes=(2, 5),
    )
    assert scalar_fits(2, enum)
    assert scalar_fits(5, enum)
    assert not scalar_fits(0, enum)
    assert not scalar_fits(True, enum)
    assert normalize_scalar(5, enum) == 5
    with pytest.raises(RuntimeValueError, match="not a legal code"):
        normalize_scalar(0, enum)

    aggregate = StructType(
        "State",
        (
            StructField("phase", enum),
            StructField("samples", VecType(2, UIntType(4))),
        ),
    )
    zero = {"phase": 2, "samples": [0, 0]}
    assert zero_runtime_value(aggregate) == zero
    assert runtime_value_fits(zero, aggregate)
    assert not runtime_value_fits({"phase": 0, "samples": [0, 0]}, aggregate)


def test_vector_reshape_helpers_use_sequence_order_not_packed_layout() -> None:
    source_type = VecType(2, VecType(3, UIntType(4)))
    target_type = VecType(3, VecType(2, UIntType(4)))
    source = [[0, 1, 2], [3, 4, 5]]
    leaves = flatten_vector_value(source_type, source)
    assert leaves == [0, 1, 2, 3, 4, 5]
    rebuilt, consumed = rebuild_vector_value(target_type, leaves)
    assert consumed == 6
    assert rebuilt == [[0, 1], [2, 3], [4, 5]]

    with pytest.raises(RuntimeValueError, match="exactly 2 elements"):
        flatten_vector_value(source_type, [[0, 1, 2]])
    with pytest.raises(RuntimeValueError, match="requires more leaves"):
        rebuild_vector_value(target_type, [0, 1, 2])


def test_bit_zero_and_normalization_remain_boolean() -> None:
    assert zero_runtime_value(BitType()) == 0
    assert normalize_scalar(0, BitType()) == 0
    assert normalize_scalar(17, BitType()) == 1
