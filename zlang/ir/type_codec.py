"""Strict canonical data codec for every backend-independent hardware type."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Callable
from typing import TypeVar

from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedOverflowPolicy,
    FixedType,
    HardwareType,
    SIntType,
    StructField,
    StructType,
    TaggedUnionField,
    TaggedUnionType,
    TaggedUnionVariant,
    TupleType,
    UFixedType,
    UIntType,
    VecType,
)


class TypeCodecError(ValueError):
    """A canonical hardware-type payload is malformed."""


T = TypeVar("T")


def _construct(description: str, constructor: Callable[[], T]) -> T:
    """Normalize canonical-constructor failures to the codec boundary."""

    try:
        return constructor()
    except (TypeError, ValueError) as error:
        raise TypeCodecError(f"invalid {description}: {error}") from error


def canonical_type_data(type_: HardwareType) -> dict[str, object]:
    """Return an exact deterministic representation of ``type_``."""

    if isinstance(type_, BitType):
        return {"kind": "bit", "width": 1}
    if isinstance(type_, UIntType):
        return {"kind": "uint", "width": type_.width}
    if isinstance(type_, SIntType):
        return {"kind": "sint", "width": type_.width}
    if isinstance(type_, BitsType):
        return {"kind": "bits", "width": type_.width}
    if isinstance(type_, FixedType):
        return {
            "kind": "fixed",
            "width": type_.width,
            "fraction": type_.fraction,
            "overflow": type_.overflow.value,
        }
    if isinstance(type_, UFixedType):
        return {
            "kind": "ufixed",
            "width": type_.width,
            "fraction": type_.fraction,
            "overflow": type_.overflow.value,
        }
    if isinstance(type_, EnumType):
        return {
            "kind": "enum",
            "name": type_.name,
            "members": list(type_.members),
            "declaration_identity": type_.declaration_identity,
            "explicit_width": type_.explicit_width,
            "codes": list(type_.codes),
        }
    if isinstance(type_, StructType):
        return {
            "kind": "struct",
            "name": type_.name,
            "fields": [
                {"name": field.name, "type": canonical_type_data(field.type)}
                for field in type_.fields
            ],
        }
    if isinstance(type_, TupleType):
        return {
            "kind": "tuple",
            "elements": [canonical_type_data(element) for element in type_.elements],
        }
    if isinstance(type_, TaggedUnionType):
        return {
            "kind": "tagged_union",
            "name": type_.name,
            "declaration_identity": type_.declaration_identity,
            "variants": [
                {
                    "name": variant.name,
                    "fields": [
                        {
                            "name": field.name,
                            "type": canonical_type_data(field.type),
                        }
                        for field in variant.fields
                    ],
                }
                for variant in type_.variants
            ],
        }
    if isinstance(type_, VecType):
        return {
            "kind": "vec",
            "length": type_.length,
            "element_type": canonical_type_data(type_.element_type),
        }
    raise TypeCodecError(f"unsupported canonical hardware type: {type_!r}")


def canonical_type_from_data(value: object) -> HardwareType:
    """Validate and restore one canonical hardware-type payload."""

    data = _mapping(value, "hardware type")
    kind = _string(data.get("kind"), "hardware type kind")
    if kind == "bit":
        _keys(data, {"kind", "width"})
        if _integer(data["width"], "bit width") != 1:
            raise TypeCodecError("bit width must be exactly 1")
        return BitType()
    if kind in {"uint", "sint", "bits"}:
        _keys(data, {"kind", "width"})
        width = _integer(data["width"], f"{kind} width")
        return _construct(
            f"{kind} hardware type",
            lambda: {"uint": UIntType, "sint": SIntType, "bits": BitsType}[kind](
                width
            ),
        )
    if kind in {"fixed", "ufixed"}:
        _keys(data, {"kind", "width", "fraction", "overflow"})
        type_class = FixedType if kind == "fixed" else UFixedType
        width = _integer(data["width"], f"{kind} width")
        fraction = _integer(data["fraction"], f"{kind} fraction")
        overflow = _string(data["overflow"], f"{kind} overflow")
        return _construct(
            f"{kind} hardware type",
            lambda: type_class(
                width,
                fraction,
                FixedOverflowPolicy(overflow),
            ),
        )
    if kind == "enum":
        _keys(
            data,
            {
                "kind",
                "name",
                "members",
                "declaration_identity",
                "explicit_width",
                "codes",
            },
        )
        members = _string_list(data["members"], "enum members")
        codes = _integer_list(data["codes"], "enum codes")
        explicit_width = data["explicit_width"]
        if explicit_width is not None:
            explicit_width = _integer(explicit_width, "enum explicit width")
        elif codes != tuple(range(len(members))):
            raise TypeCodecError("ordinal enum codes must match declaration order")
        name = _string(data["name"], "enum name")
        declaration_identity = _string(
            data["declaration_identity"], "enum declaration identity"
        )
        return _construct(
            "enum hardware type",
            lambda: EnumType(
                name,
                members,
                declaration_identity,
                explicit_width,
                codes if explicit_width is not None else None,
            ),
        )
    if kind == "struct":
        _keys(data, {"kind", "name", "fields"})
        raw_fields = data["fields"]
        if not isinstance(raw_fields, list):
            raise TypeCodecError("struct fields must be an array")
        fields: list[StructField] = []
        for index, raw_field in enumerate(raw_fields):
            field = _mapping(raw_field, f"struct field {index}")
            _keys(field, {"name", "type"})
            fields.append(
                StructField(
                    _string(field["name"], f"struct field {index} name"),
                    canonical_type_from_data(field["type"]),
                )
            )
        return _construct(
            "struct hardware type",
            lambda: StructType(
                _string(data["name"], "struct name"), tuple(fields)
            ),
        )
    if kind == "tuple":
        _keys(data, {"kind", "elements"})
        raw_elements = data["elements"]
        if not isinstance(raw_elements, list):
            raise TypeCodecError("tuple elements must be an array")
        elements = tuple(canonical_type_from_data(item) for item in raw_elements)
        return _construct(
            "tuple hardware type",
            lambda: TupleType(elements),
        )
    if kind == "tagged_union":
        _keys(data, {"kind", "name", "declaration_identity", "variants"})
        raw_variants = data["variants"]
        if not isinstance(raw_variants, list):
            raise TypeCodecError("tagged-union variants must be an array")
        variants: list[TaggedUnionVariant] = []
        for variant_index, raw_variant in enumerate(raw_variants):
            variant_data = _mapping(
                raw_variant, f"tagged-union variant {variant_index}"
            )
            _keys(variant_data, {"name", "fields"})
            raw_fields = variant_data["fields"]
            if not isinstance(raw_fields, list):
                raise TypeCodecError(
                    f"tagged-union variant {variant_index} fields must be an array"
                )
            fields: list[TaggedUnionField] = []
            for field_index, raw_field in enumerate(raw_fields):
                field_data = _mapping(
                    raw_field,
                    f"tagged-union variant {variant_index} field {field_index}",
                )
                _keys(field_data, {"name", "type"})
                fields.append(
                    TaggedUnionField(
                        _string(
                            field_data["name"],
                            f"tagged-union variant {variant_index} field name",
                        ),
                        canonical_type_from_data(field_data["type"]),
                    )
                )
            variants.append(
                TaggedUnionVariant(
                    _string(
                        variant_data["name"],
                        f"tagged-union variant {variant_index} name",
                    ),
                    tuple(fields),
                )
            )
        return _construct(
            "tagged-union hardware type",
            lambda: TaggedUnionType(
                _string(data["name"], "tagged-union name"),
                tuple(variants),
                _string(
                    data["declaration_identity"],
                    "tagged-union declaration identity",
                ),
            ),
        )
    if kind == "vec":
        _keys(data, {"kind", "length", "element_type"})
        length = _integer(data["length"], "vector length")
        element_type = canonical_type_from_data(data["element_type"])
        return _construct(
            "vector hardware type",
            lambda: VecType(length, element_type),
        )
    raise TypeCodecError(f"unknown canonical hardware type kind '{kind}'")


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeCodecError(f"{description} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TypeCodecError(f"{description} keys must be strings")
    return value


def _keys(data: Mapping[str, object], expected: set[str]) -> None:
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unknown " + ", ".join(unknown))
        raise TypeCodecError("hardware type has " + "; ".join(detail))


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeCodecError(f"{description} must be a non-empty string")
    return value


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeCodecError(f"{description} must be an integer")
    return value


def _string_list(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeCodecError(f"{description} must be an array")
    return tuple(_string(item, description) for item in value)


def _integer_list(value: object, description: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise TypeCodecError(f"{description} must be an array")
    return tuple(_integer(item, description) for item in value)


__all__ = ["TypeCodecError", "canonical_type_data", "canonical_type_from_data"]
