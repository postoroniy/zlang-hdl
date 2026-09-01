"""Backend-independent runtime values for canonical hardware types.

These helpers describe value domains and logical vector sequence order.  They
do not define packed bit layout or endianness; exact representation conversion
remains exclusively in :mod:`zlang.ir.packing`.
"""

from __future__ import annotations

from dataclasses import dataclass

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


class RuntimeValueError(ValueError):
    """A runtime value does not satisfy its canonical hardware type."""


def minimum_unsigned_width(value: int) -> int:
    """Return the minimum positive hardware width for an unsigned value."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("minimum unsigned width requires a non-negative integer")
    return max(1, value.bit_length())


def minimum_signed_width(value: int) -> int:
    """Return the minimum two's-complement width for one signed value."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("minimum signed width requires an integer")
    if value >= 0:
        return max(1, value.bit_length() + 1)
    # ``~value`` is the magnitude below the negative power-of-two boundary:
    # -1 therefore needs one bit, -2 two bits, -3/-4 three bits, and so on.
    return max(1, (~value).bit_length() + 1)


@dataclass(frozen=True)
class TaggedUnionValue:
    """Immutable nominal runtime value for one tagged-union constructor."""

    type: TaggedUnionType
    variant: str
    fields: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        declaration = self.type.variant(self.variant)
        if declaration is None:
            raise RuntimeValueError(
                f"tagged union '{self.type.name}' has no variant '{self.variant}'"
            )
        names = tuple(name for name, _ in self.fields)
        expected = tuple(field.name for field in declaration.fields)
        if names != expected:
            raise RuntimeValueError(
                f"tagged-union value '{self.type.name}.{self.variant}' fields "
                f"must be {expected}, got {names}"
            )
        for field, (_, value) in zip(declaration.fields, self.fields, strict=True):
            if not runtime_value_fits(value, field.type):
                raise RuntimeValueError(
                    f"tagged-union field '{field.name}' value does not fit {field.type}"
                )

    def field(self, name: str) -> object:
        try:
            return dict(self.fields)[name]
        except KeyError as error:
            raise RuntimeValueError(
                f"variant '{self.type.name}.{self.variant}' has no field '{name}'"
            ) from error


def scalar_fits(value: object, type_: HardwareType) -> bool:
    """Return whether one scalar value belongs to the exact hardware domain."""

    if isinstance(type_, BitType):
        return isinstance(value, int) and value in (0, 1)
    if isinstance(type_, (SIntType, FixedType)):
        return isinstance(value, int) and (
            -(1 << (type_.width - 1))
            <= value
            < (1 << (type_.width - 1))
        )
    if isinstance(type_, (UIntType, BitsType, UFixedType)):
        return isinstance(value, int) and 0 <= value < (1 << type_.width)
    if isinstance(type_, EnumType):
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and type_.is_valid_code(value)
        )
    return False


def runtime_value_fits(value: object, type_: HardwareType) -> bool:
    """Return whether a scalar or aggregate value exactly fits its type."""

    if isinstance(
        type_,
        (
            BitType,
            UIntType,
            SIntType,
            BitsType,
            FixedType,
            UFixedType,
            EnumType,
        ),
    ):
        return scalar_fits(value, type_)
    if isinstance(type_, StructType):
        return (
            isinstance(value, dict)
            and set(value) == {field.name for field in type_.fields}
            and all(
                runtime_value_fits(value[field.name], field.type)
                for field in type_.fields
            )
        )
    if isinstance(type_, TaggedUnionType):
        return isinstance(value, TaggedUnionValue) and value.type == type_
    if isinstance(type_, TupleType):
        return (
            isinstance(value, tuple)
            and len(value) == len(type_.elements)
            and all(
                runtime_value_fits(element, element_type)
                for element, element_type in zip(
                    value, type_.elements, strict=True
                )
            )
        )
    if isinstance(type_, VecType):
        return (
            isinstance(value, (list, tuple))
            and len(value) == type_.length
            and all(
                runtime_value_fits(element, type_.element_type)
                for element in value
            )
        )
    return False


def normalize_scalar(value: int, type_: HardwareType) -> int:
    """Normalize one integral scalar to its exact finite-width representation."""

    if isinstance(type_, BitType):
        return int(bool(value))
    if isinstance(type_, EnumType):
        if not scalar_fits(value, type_):
            raise RuntimeValueError(
                f"enum value {value!r} is not a legal code of {type_}"
            )
        return value
    if not isinstance(
        type_,
        (UIntType, SIntType, BitsType, FixedType, UFixedType),
    ):
        raise RuntimeValueError(f"cannot normalize non-scalar type {type_}")
    mask = (1 << type_.width) - 1
    raw = value & mask
    if isinstance(type_, (SIntType, FixedType)) and raw >= (
        1 << (type_.width - 1)
    ):
        return raw - (1 << type_.width)
    return raw


def zero_runtime_value(type_: HardwareType) -> object:
    """Construct the deterministic reset-zero value for a canonical type."""

    if isinstance(type_, EnumType):
        return type_.codes[0]
    if isinstance(
        type_,
        (BitType, UIntType, SIntType, BitsType, FixedType, UFixedType),
    ):
        return 0
    if isinstance(type_, StructType):
        return {
            field.name: zero_runtime_value(field.type) for field in type_.fields
        }
    if isinstance(type_, TaggedUnionType):
        first = type_.variants[0]
        return TaggedUnionValue(
            type_,
            first.name,
            tuple(
                (field.name, zero_runtime_value(field.type))
                for field in first.fields
            ),
        )
    if isinstance(type_, TupleType):
        return tuple(zero_runtime_value(element) for element in type_.elements)
    if isinstance(type_, VecType):
        return [
            zero_runtime_value(type_.element_type) for _ in range(type_.length)
        ]
    raise RuntimeValueError(f"no zero runtime value for {type_}")


def flatten_vector_value(type_: VecType, value: object) -> list[object]:
    """Flatten nested vectors in outer-to-inner logical sequence order."""

    if not isinstance(value, (list, tuple)) or len(value) != type_.length:
        raise RuntimeValueError(
            f"reshape source value does not contain exactly {type_.length} elements"
        )
    if not isinstance(type_.element_type, VecType):
        return list(value)
    leaves: list[object] = []
    for element in value:
        leaves.extend(flatten_vector_value(type_.element_type, element))
    return leaves


def rebuild_vector_value(
    type_: VecType,
    leaves: list[object],
    offset: int = 0,
) -> tuple[list[object], int]:
    """Rebuild one vector shape from an outer-to-inner logical leaf stream."""

    result: list[object] = []
    if isinstance(type_.element_type, VecType):
        for _ in range(type_.length):
            element, offset = rebuild_vector_value(
                type_.element_type,
                leaves,
                offset,
            )
            result.append(element)
        return result, offset
    end = offset + type_.length
    if end > len(leaves):
        raise RuntimeValueError(
            "reshape target requires more leaves than its source"
        )
    return list(leaves[offset:end]), end


__all__ = [
    "RuntimeValueError",
    "TaggedUnionValue",
    "flatten_vector_value",
    "minimum_signed_width",
    "minimum_unsigned_width",
    "normalize_scalar",
    "rebuild_vector_value",
    "runtime_value_fits",
    "scalar_fits",
    "zero_runtime_value",
]
