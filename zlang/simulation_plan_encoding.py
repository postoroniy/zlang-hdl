# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Canonical compiler-owned encoding for native simulation-plan records."""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from zlang.ir import functional_regions
from zlang.ir import packing as ir_packing
from zlang.ir import types as ir_types
from zlang.simulation_plan_policy import (
    JitUnsupportedFeatureError,
    SimulationPlanError,
)


def expression_node_payload(
    node: object,
    *,
    identifier: int | None = None,
    op: str,
    operands: list[int],
    attributes: dict[str, object],
) -> dict[str, object]:
    """Encode the fields shared by every executable expression node."""

    return {
        "id": node.id if identifier is None else identifier,
        "op": op,
        "type": type_payload(node.type),
        "operands": operands,
        "attributes": attributes,
        "origins": [
            encoded
            for origin in node.origins
            if (encoded := origin_payload(origin)) is not None
        ],
    }


def type_payload(type_: ir_types.HardwareType) -> dict[str, Any]:
    if isinstance(type_, ir_types.BitType):
        return {"kind": "bit", "width": 1}
    if isinstance(type_, ir_types.UIntType):
        return {"kind": "uint", "width": type_.width}
    if isinstance(type_, ir_types.SIntType):
        return {"kind": "sint", "width": type_.width}
    if isinstance(type_, ir_types.BitsType):
        return {"kind": "bits", "width": type_.width}
    if isinstance(type_, ir_types.FixedType):
        return {
            "kind": "fixed",
            "width": type_.width,
            "fraction": type_.fraction,
            "overflow": type_.overflow.value,
        }
    if isinstance(type_, ir_types.UFixedType):
        return {
            "kind": "ufixed",
            "width": type_.width,
            "fraction": type_.fraction,
            "overflow": type_.overflow.value,
        }
    if isinstance(type_, ir_types.EnumType):
        return {
            "kind": "enum",
            "width": type_.width,
            "name": type_.name,
            "declaration_identity": type_.declaration_identity,
            "members": list(type_.members),
            "codes": list(type_.codes),
        }
    if isinstance(type_, ir_types.VecType):
        return {
            "kind": "vec",
            "width": type_.width,
            "length": type_.length,
            "element": type_payload(type_.element_type),
        }
    if isinstance(type_, ir_types.TupleType):
        return {
            "kind": "tuple",
            "width": type_.width,
            "elements": [type_payload(item) for item in type_.elements],
        }
    if isinstance(type_, ir_types.StructType):
        return {
            "kind": "struct",
            "width": type_.width,
            "name": type_.name,
            "fields": [
                {"name": field.name, "type": type_payload(field.type)}
                for field in type_.fields
            ],
        }
    if isinstance(type_, ir_types.TaggedUnionType):
        return {
            "kind": "tagged_union",
            "width": type_.width,
            "name": type_.name,
            "declaration_identity": type_.declaration_identity,
            "tag_width": type_.tag_width,
            "payload_width": type_.payload_width,
            "variants": [
                {
                    "name": variant.name,
                    "fields": [
                        {"name": field.name, "type": type_payload(field.type)}
                        for field in variant.fields
                    ],
                }
                for variant in type_.variants
            ],
        }
    raise JitUnsupportedFeatureError(
        f"native simulation does not support hardware type '{type_}'"
    )


def pack_initial(type_: ir_types.HardwareType, value: object) -> int:
    if isinstance(type_, ir_types.EnumType):
        if isinstance(value, str):
            try:
                return type_.member_code(value)
            except ValueError as error:
                raise SimulationPlanError(str(error)) from error
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not type_.is_valid_code(value)
        ):
            raise SimulationPlanError(
                f"initial value {value!r} is invalid for '{type_}'"
            )
        return value
    if isinstance(type_, ir_types.StructType):
        if isinstance(value, int) and not isinstance(value, bool):
            if 0 <= value < 1 << type_.width:
                return value
            raise SimulationPlanError(
                f"initial packed value {value} does not fit '{type_}'"
            )
        if not isinstance(value, dict):
            raise SimulationPlanError(
                f"initial value for struct '{type_}' must be a mapping"
            )
        expected = tuple(field.name for field in type_.fields)
        if set(value) != set(expected):
            raise SimulationPlanError(
                f"initial value for struct '{type_}' must contain exactly {expected}"
            )
        packed = 0
        for field in type_.fields:
            packed = (packed << field.type.width) | pack_initial(
                field.type, value[field.name]
            )
        return packed
    if isinstance(type_, ir_types.TupleType):
        if isinstance(value, int) and not isinstance(value, bool):
            if 0 <= value < 1 << type_.width:
                return value
            raise SimulationPlanError(
                f"initial packed value {value} does not fit '{type_}'"
            )
        if not isinstance(value, tuple) or len(value) != len(type_.elements):
            raise SimulationPlanError(
                f"initial value for '{type_}' must contain exactly "
                f"{len(type_.elements)} elements"
            )
        packed = 0
        offset = 0
        for element_type, element in zip(type_.elements, value, strict=True):
            packed |= pack_initial(element_type, element) << offset
            offset += element_type.width
        return packed
    if isinstance(type_, ir_types.VecType):
        if isinstance(value, int) and not isinstance(value, bool):
            if 0 <= value < 1 << type_.width:
                return value
            raise SimulationPlanError(
                f"initial packed value {value} does not fit '{type_}'"
            )
        if not isinstance(value, (tuple, list)) or len(value) != type_.length:
            raise SimulationPlanError(
                f"initial value for '{type_}' must contain exactly "
                f"{type_.length} elements"
            )
        return sum(
            pack_initial(type_.element_type, element)
            << (index * type_.element_type.width)
            for index, element in enumerate(value)
        )
    if isinstance(type_, ir_types.TaggedUnionType):
        return ir_packing.pack_tagged_union_runtime(value)
    try:
        return ir_packing.pack_runtime(type_, value)
    except ir_packing.PackingError as error:
        raise SimulationPlanError(str(error)) from error


def u64_limbs(value: int, width: int) -> list[int]:
    if value < 0 or value >= 1 << width:
        raise SimulationPlanError(f"packed value does not fit {width} bits")
    return [
        (value >> offset) & ((1 << 64) - 1)
        for offset in range(0, width, 64)
    ]


def json_attribute(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(
        value,
        (
            ir_types.BitType,
            ir_types.UIntType,
            ir_types.SIntType,
            ir_types.BitsType,
            ir_types.FixedType,
            ir_types.UFixedType,
            ir_types.EnumType,
            ir_types.VecType,
            ir_types.TupleType,
            ir_types.StructType,
            ir_types.TaggedUnionType,
        ),
    ):
        return {"hardware_type": type_payload(value)}
    if isinstance(value, tuple):
        return [json_attribute(item) for item in value]
    if isinstance(value, list):
        return [json_attribute(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_attribute(item) for key, item in value.items()}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise JitUnsupportedFeatureError(
        "native simulation cannot serialize expression metadata of type "
        f"'{type(value).__name__}'"
    )


def compile_time_expression_payload(
    value: functional_regions.CompileTimeExpr,
) -> dict[str, object]:
    operands: list[object] = []
    for operand in value.operands:
        if isinstance(operand, functional_regions.CompileTimeExpr):
            operands.append(compile_time_expression_payload(operand))
        elif isinstance(operand, functional_regions.CompileTimeBinderRef):
            operands.append({"binder": operand.identity})
        elif isinstance(operand, int) and not isinstance(operand, bool):
            operands.append({"literal": operand})
        else:
            raise JitUnsupportedFeatureError(
                "simulation cannot serialize a compile-time expression operand"
            )
    return {"operator": value.operator.value, "operands": operands}


def compile_time_expression_binders(
    value: functional_regions.CompileTimeExpr,
) -> list[str]:
    binders: set[str] = set()

    def visit(current: functional_regions.CompileTimeExpr) -> None:
        for operand in current.operands:
            if isinstance(operand, functional_regions.CompileTimeBinderRef):
                binders.add(operand.identity)
            elif isinstance(operand, functional_regions.CompileTimeExpr):
                visit(operand)

    visit(value)
    return sorted(binders)


def origin_payload(origin: object) -> dict[str, object] | None:
    span = getattr(origin, "span", None)
    if span is None:
        return None
    payload = {
        "start_line": span.start_line,
        "start_column": span.start_column,
        "end_line": span.end_line,
        "end_column": span.end_column,
        "construct": getattr(origin, "construct", ""),
    }
    source_unit = getattr(origin, "source_unit", None)
    if isinstance(source_unit, str) and not PurePosixPath(source_unit).is_absolute():
        payload["source_unit"] = source_unit
    digest = getattr(origin, "digest", None)
    if isinstance(digest, str):
        payload["digest"] = digest
    return payload
