"""Backend-independent bit packing and slicing conventions.

This module is the single definition of ZLang aggregate bit order.  Struct
field zero and vector element zero occupy the most-significant portion of the
packed value.  Nominal enums are deliberately excluded from this first
packing slice; accepting their ordinal representation requires a separate
language decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    StructType,
    TaggedUnionType,
    TupleType,
    UFixedType,
    UIntType,
    VecType,
)
from zlang.ir.runtime_values import TaggedUnionValue


class PackingError(ValueError):
    """A type, runtime value, or bit range violates the packing contract."""


_PACKABLE_SCALARS = (
    BitType,
    BitsType,
    FixedType,
    SIntType,
    UFixedType,
    UIntType,
)


def is_bit_packable(type_: HardwareType) -> bool:
    """Return whether ``type_`` belongs to the frozen non-enum packing set."""

    if isinstance(type_, EnumType):
        return False
    if isinstance(type_, _PACKABLE_SCALARS):
        return True
    if isinstance(type_, StructType):
        return bool(type_.fields) and all(
            is_bit_packable(field.type) for field in type_.fields
        )
    if isinstance(type_, TupleType):
        return all(is_bit_packable(element) for element in type_.elements)
    if isinstance(type_, VecType):
        return is_bit_packable(type_.element_type)
    return False


def require_bit_packable(type_: HardwareType) -> None:
    if not is_bit_packable(type_):
        raise PackingError(f"type '{type_}' is not bit-packable")


def packed_width(type_: HardwareType) -> int:
    require_bit_packable(type_)
    width = type_.width
    if width < 1:
        raise PackingError("a packed type must have positive width")
    return width


def bit_mask(width: int) -> int:
    if width < 1:
        raise PackingError("a bit width must be positive")
    return (1 << width) - 1


def slice_width(msb: int, lsb: int) -> int:
    if lsb < 0:
        raise PackingError("a bit-slice lower bound must not be negative")
    if msb < lsb:
        raise PackingError("a bit-slice MSB must not be below its LSB")
    return msb - lsb + 1


def slice_runtime(value: int, source_width: int, msb: int, lsb: int) -> int:
    """Evaluate an inclusive bit slice after exact source-range validation."""

    width = slice_width(msb, lsb)
    if source_width < 1:
        raise PackingError("a sliced source must have positive width")
    if msb >= source_width:
        raise PackingError(
            f"bit slice [{msb}:{lsb}] exceeds source width {source_width}"
        )
    if not isinstance(value, int):
        raise PackingError("a sliced runtime value must be integral")
    return (value >> lsb) & bit_mask(width)


def concat_runtime(parts: Iterable[tuple[int, int]]) -> int:
    """Concatenate ``(value, width)`` parts, first part at the MSB."""

    result = 0
    count = 0
    for value, width in parts:
        if not isinstance(value, int):
            raise PackingError("a concatenated runtime value must be integral")
        result = (result << width) | (value & bit_mask(width))
        count += 1
    if count < 2:
        raise PackingError("concat requires at least two operands")
    return result


def pack_runtime(type_: HardwareType, value: object) -> int:
    """Pack one typed runtime value into an unsigned raw-bit integer."""

    require_bit_packable(type_)
    if isinstance(type_, _PACKABLE_SCALARS):
        if not isinstance(value, int):
            raise PackingError(f"value for '{type_}' must be integral")
        if isinstance(type_, BitType):
            fits = value in (0, 1)
        elif isinstance(type_, (SIntType, FixedType)):
            fits = -(1 << (type_.width - 1)) <= value < (1 << (type_.width - 1))
        else:
            fits = 0 <= value <= bit_mask(type_.width)
        if not fits:
            raise PackingError(f"value {value} does not fit exact type '{type_}'")
        return value & bit_mask(type_.width)
    if isinstance(type_, StructType):
        if not isinstance(value, Mapping):
            raise PackingError(f"value for struct '{type_}' must be a mapping")
        expected = tuple(field.name for field in type_.fields)
        if set(value) != set(expected):
            raise PackingError(
                f"value for struct '{type_}' must contain exactly {expected}"
            )
        packed = 0
        for field in type_.fields:
            packed = (
                packed << packed_width(field.type)
            ) | pack_runtime(field.type, value[field.name])
        return packed
    if isinstance(type_, TupleType):
        if not isinstance(value, tuple) or len(value) != len(type_.elements):
            raise PackingError(
                f"value for '{type_}' must contain exactly "
                f"{len(type_.elements)} elements"
            )
        packed = 0
        for element_type, element in zip(type_.elements, value, strict=True):
            packed = (
                packed << packed_width(element_type)
            ) | pack_runtime(element_type, element)
        return packed
    if isinstance(type_, VecType):
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes, bytearray))
            or len(value) != type_.length
        ):
            raise PackingError(
                f"value for '{type_}' must contain exactly {type_.length} elements"
            )
        packed = 0
        element_width = packed_width(type_.element_type)
        for element in value:
            packed = (packed << element_width) | pack_runtime(
                type_.element_type, element
            )
        return packed
    raise PackingError(f"type '{type_}' is not bit-packable")


def unpack_runtime(type_: HardwareType, value: int) -> object:
    """Unpack a raw-bit integer using the frozen aggregate bit order."""

    require_bit_packable(type_)
    if not isinstance(value, int):
        raise PackingError("an unpacked runtime value must be integral")
    width = packed_width(type_)
    if value < 0 or value > bit_mask(width):
        raise PackingError(
            f"raw value {value} does not fit exact unpack width {width}"
        )
    if isinstance(type_, _PACKABLE_SCALARS):
        raw = value & bit_mask(type_.width)
        if isinstance(type_, (SIntType, FixedType)) and raw >= 1 << (type_.width - 1):
            return raw - (1 << type_.width)
        return raw
    if isinstance(type_, StructType):
        remaining = value
        result: dict[str, object] = {}
        shift = width
        for field in type_.fields:
            field_width = packed_width(field.type)
            shift -= field_width
            field_raw = (remaining >> shift) & bit_mask(field_width)
            result[field.name] = unpack_runtime(field.type, field_raw)
        return result
    if isinstance(type_, TupleType):
        result: list[object] = []
        shift = width
        for element_type in type_.elements:
            element_width = packed_width(element_type)
            shift -= element_width
            element_raw = (value >> shift) & bit_mask(element_width)
            result.append(unpack_runtime(element_type, element_raw))
        return tuple(result)
    if isinstance(type_, VecType):
        element_width = packed_width(type_.element_type)
        return [
            unpack_runtime(
                type_.element_type,
                (value >> ((type_.length - index - 1) * element_width))
                & bit_mask(element_width),
            )
            for index in range(type_.length)
        ]
    raise PackingError(f"type '{type_}' is not bit-packable")


def tagged_union_field_slice(
    type_: TaggedUnionType, variant_name: str, field_name: str
) -> tuple[int, int]:
    """Return the exact inclusive packed slice of one union payload field."""

    variant = type_.variant(variant_name)
    if variant is None:
        raise PackingError(
            f"tagged union '{type_.name}' has no variant '{variant_name}'"
        )
    offset = type_.payload_width
    for field in variant.fields:
        msb = offset - 1
        lsb = offset - field.type.width
        if field.name == field_name:
            return msb, lsb
        offset = lsb
    raise PackingError(
        f"variant '{type_.name}.{variant_name}' has no field '{field_name}'"
    )


def pack_tagged_union_runtime(value: TaggedUnionValue) -> int:
    """Pack one nominal union using tag-MSB and source-order payload layout."""

    type_ = value.type
    variant = type_.variant(value.variant)
    assert variant is not None
    payload = 0
    fields = dict(value.fields)
    for field in variant.fields:
        payload = (
            payload << field.type.width
        ) | pack_runtime(field.type, fields[field.name])
    padding = type_.payload_width - variant.payload_width
    payload <<= padding
    return (type_.tag(variant.name) << type_.payload_width) | payload
