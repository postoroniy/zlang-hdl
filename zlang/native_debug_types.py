# SPDX-License-Identifier: Apache-2.0
"""Immutable debugger layouts derived from the compiler's packing contract.

These are presentation facts, never runtime instructions. Indexed aggregates
retain one element template, so even large memories/vectors have bounded metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

from zlang.common.serialization import stable_json

from zlang.ir.packing import (
    struct_field_lsb, tagged_union_field_slice, tuple_element_lsb,
    vector_element_lsb,
)
from zlang.ir.type_codec import canonical_type_data
from zlang.ir.types import (
    BitsType, EnumType, HardwareType, StructType, TaggedUnionType, TupleType, UIntType,
    VecType,
)

TYPED_CATALOG_SCHEMA = "zlang-debug-typed-catalog-v1"


@dataclass(frozen=True)
class DebugMember:
    name: str
    layout: DebugType
    lsb: int = 0
    binding: str | None = None
    direction: str | None = None
    domain: str | None = None
    # (tag LSB relative to this member's parent, tag width, exact code).
    guard: tuple[int, int, int] | None = None

    def to_data(self) -> dict[str, object]:
        return {
            "name": self.name, "layout": self.layout.to_data(), "lsb": self.lsb,
            "binding": self.binding, "direction": self.direction,
            "domain": self.domain, "guard": self.guard,
        }


@dataclass(frozen=True)
class DebugType:
    kind: str
    label: str
    width: int
    canonical: str = "null"
    members: tuple[DebugMember, ...] = ()
    element: DebugType | None = None
    length: int = 0
    stride: int = 0
    enum_values: tuple[tuple[str, str], ...] = ()

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind, "label": self.label, "width": self.width,
            "canonical": json.loads(self.canonical),
            "members": [item.to_data() for item in self.members],
            "element": None if self.element is None else self.element.to_data(),
            "length": self.length, "stride": self.stride,
            "enum_values": [{"bits": bits, "name": name} for bits, name in self.enum_values],
        }


def debug_type(type_: HardwareType) -> DebugType:
    """Describe exact types; all offsets come from the existing packing owner."""
    canonical = canonical_type_data(type_)
    members: tuple[DebugMember, ...] = ()
    element = None
    length = stride = 0
    if isinstance(type_, StructType):
        members = tuple(DebugMember(
            field.name, debug_type(field.type), struct_field_lsb(type_, field.name),
        ) for field in type_.fields)
    elif isinstance(type_, TupleType):
        members = tuple(DebugMember(
            f"[{index}]", debug_type(item), tuple_element_lsb(type_, index),
        ) for index, item in enumerate(type_.elements))
    elif isinstance(type_, VecType):
        element = debug_type(type_.element_type)
        length = type_.length
        stride = (vector_element_lsb(type_, 1) if length > 1 else element.width)
    elif isinstance(type_, TaggedUnionType):
        variants = []
        for variant in type_.variants:
            fields = tuple(DebugMember(
                field.name, debug_type(field.type),
                tagged_union_field_slice(type_, variant.name, field.name)[1],
            ) for field in variant.fields)
            variants.append(DebugMember(
                variant.name,
                DebugType("variant", variant.name, type_.width, members=fields),
                guard=(type_.payload_width, type_.tag_width, type_.tag(variant.name)),
            ))
        members = (
            DebugMember("tag", debug_type(UIntType(type_.tag_width)), type_.payload_width),
            *variants,
        )
    return DebugType(
        str(canonical["kind"]), str(type_), type_.width,
        stable_json(canonical),
        members, element, length, stride,
        tuple((format(type_.member_code(name), f"0{type_.width}b"), name)
              for name in type_.members) if isinstance(type_, EnumType) else (),
    )


@dataclass(frozen=True)
class DebugSignal:
    name: str
    kind: str
    width: int
    type_data: str
    direction: str | None = None
    depth: int | None = None
    layout: DebugType | None = None
    binding: str | None = None
    domain: str | None = None

    def to_data(self) -> dict[str, object]:
        layout = self.layout or debug_type(BitsType(self.width))
        return {
            "direction": self.direction, "depth": self.depth, "kind": self.kind,
            "name": self.name, "type": self.type_data, "width": self.width,
            "layout": layout.to_data(), "binding": self.binding or self.name,
            "domain": self.domain,
        }
