# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Typed public-value packing for the native simulation boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from zlang.ir import packing
from zlang.ir.runtime_values import TaggedUnionValue
from zlang.ir.types import (
    EnumType,
    HardwareType,
    StructType,
    TaggedUnionType,
    TupleType,
    VecType,
)


class SimulationRuntimeError(RuntimeError):
    """The persistent simulation instance rejected an operation."""


def pack_value(type_: HardwareType, value: object) -> int:
    """Pack one public Python value according to its compiler-owned type."""

    if isinstance(type_, EnumType):
        if isinstance(value, str):
            try:
                return type_.member_code(value)
            except ValueError as error:
                raise SimulationRuntimeError(str(error)) from error
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not type_.is_valid_code(value)
        ):
            raise SimulationRuntimeError(
                f"value {value!r} is not a valid member/code of '{type_.name}'"
            )
        return value
    if isinstance(type_, StructType):
        if not isinstance(value, Mapping):
            raise SimulationRuntimeError(
                f"value for struct '{type_}' must be a mapping"
            )
        expected = tuple(field.name for field in type_.fields)
        if set(value) != set(expected):
            raise SimulationRuntimeError(
                f"value does not fit {type_}: struct value must contain "
                f"exactly {expected}"
            )
        packed = 0
        try:
            for field in type_.fields:
                packed = (packed << field.type.width) | pack_value(
                    field.type, value[field.name]
                )
        except SimulationRuntimeError as error:
            raise SimulationRuntimeError(
                f"value does not fit {type_}: {error}"
            ) from error
        return packed
    if isinstance(type_, TupleType):
        if not isinstance(value, tuple) or len(value) != len(type_.elements):
            raise SimulationRuntimeError(
                f"value for '{type_}' must contain exactly "
                f"{len(type_.elements)} elements"
            )
        packed = 0
        offset = 0
        for element_type, element in zip(type_.elements, value, strict=True):
            packed |= pack_value(element_type, element) << offset
            offset += element_type.width
        return packed
    if isinstance(type_, VecType):
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes, bytearray))
            or len(value) != type_.length
        ):
            raise SimulationRuntimeError(
                f"value does not fit {type_}: vector value must contain "
                f"exactly {type_.length} elements"
            )
        try:
            return sum(
                pack_value(type_.element_type, element)
                << (index * type_.element_type.width)
                for index, element in enumerate(value)
            )
        except SimulationRuntimeError as error:
            raise SimulationRuntimeError(
                f"value does not fit {type_}: {error}"
            ) from error
    if isinstance(type_, TaggedUnionType):
        if not isinstance(value, TaggedUnionValue):
            raise SimulationRuntimeError(
                f"value for tagged union '{type_.name}' must be TaggedUnionValue"
            )
        return packing.pack_tagged_union_runtime(value)
    try:
        return packing.pack_runtime(type_, value)
    except packing.PackingError as error:
        raise SimulationRuntimeError(
            f"value {value!r} does not fit {type_}"
        ) from error


def unpack_value(type_: HardwareType, value: int) -> object:
    """Restore one typed public Python value from its packed representation."""

    if isinstance(type_, EnumType):
        if not type_.is_valid_code(value):
            raise SimulationRuntimeError(
                f"native value {value} is not a valid code of '{type_.name}'"
            )
        return value
    if isinstance(type_, StructType):
        result: dict[str, object] = {}
        shift = type_.width
        for field in type_.fields:
            shift -= field.type.width
            raw = (value >> shift) & ((1 << field.type.width) - 1)
            result[field.name] = unpack_value(field.type, raw)
        return result
    if isinstance(type_, TupleType):
        result = []
        offset = 0
        for element_type in type_.elements:
            raw = (value >> offset) & ((1 << element_type.width) - 1)
            result.append(unpack_value(element_type, raw))
            offset += element_type.width
        return tuple(result)
    if isinstance(type_, VecType):
        return [
            unpack_value(
                type_.element_type,
                (value >> (index * type_.element_type.width))
                & ((1 << type_.element_type.width) - 1),
            )
            for index in range(type_.length)
        ]
    if isinstance(type_, TaggedUnionType):
        # Public tagged-union reconstruction requires an active-variant proof.
        # Keep the exact packed value rather than guessing a variant.
        return value
    try:
        return packing.unpack_runtime(type_, value)
    except packing.PackingError as error:
        raise SimulationRuntimeError(str(error)) from error


__all__ = ["SimulationRuntimeError", "pack_value", "unpack_value"]
