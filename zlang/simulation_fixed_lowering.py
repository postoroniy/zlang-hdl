# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Fixed-point conversion lowering for the primitive simulation VM."""

from __future__ import annotations

from typing import Any

from zlang.simulation_primitive_model import PrimitiveLowerer, PrimitiveLoweringError


class FixedPointPrimitiveLowerer(PrimitiveLowerer):
    """Own exact rescale, rounding, overflow, and signedness lowering."""

    def lower(self, node: dict[str, Any], source: int) -> int:  # noqa: C901
        builder = self._builder
        attrs = node["attributes"]
        width = int(node["type"]["width"])
        if attrs["conversion_kind"] != "rescale":
            return builder.resize(source, width)
        source_type = builder.semantic_nodes[node["operands"][0]]["type"]
        source_fraction = int(source_type["fraction"])
        target_fraction = int(node["type"]["fraction"])
        source_signed = builder._signed(source_type)
        target_signed = builder._signed(node["type"])
        left_shift = max(0, target_fraction - source_fraction)
        work_width = max(builder.width(source) + left_shift + 2, width + 2)
        extended = builder.resize(source, work_width, signed=source_signed)
        zero = builder.constant(0, work_width)
        negative = (
            builder.binary("slt", extended, zero, 1)
            if source_signed
            else builder.constant(0, 1)
        )
        if target_fraction >= source_fraction:
            converted = builder.binary(
                "shl", extended, builder.constant(left_shift, 32), work_width
            )
        else:
            shift = source_fraction - target_fraction
            magnitude = builder.select(
                negative,
                builder.binary("sub", zero, extended, work_width),
                extended,
                work_width,
            )
            quotient = builder.binary(
                "lshr", magnitude, builder.constant(shift, 32), work_width
            )
            remainder = builder.binary(
                "and",
                magnitude,
                builder.constant((1 << shift) - 1, work_width),
                work_width,
            )
            discarded = builder.truthy(remainder)
            rounding = attrs["rounding"]
            if rounding == "toward_zero":
                increment = builder.constant(0, 1)
            elif rounding == "floor":
                increment = builder.binary("and", negative, discarded, 1)
            elif rounding == "away_zero":
                increment = discarded
            elif rounding == "nearest_even":
                half = builder.constant(1 << (shift - 1), work_width)
                greater = builder.unary("not", builder.binary("ule", remainder, half, 1), 1)
                equal = builder.binary("eq", remainder, half, 1)
                odd = builder.truthy(
                    builder.binary(
                        "and", quotient, builder.constant(1, work_width), work_width
                    )
                )
                increment = builder.binary(
                    "or", greater, builder.binary("and", equal, odd, 1), 1
                )
            else:
                raise PrimitiveLoweringError(f"unknown rounding policy '{rounding}'")
            rounded = builder.binary(
                "add", quotient, builder.resize(increment, work_width), work_width
            )
            converted = builder.select(
                negative,
                builder.binary("sub", zero, rounded, work_width),
                rounded,
                work_width,
            )
        if attrs["overflow"] == "wrap":
            return builder.resize(converted, width)
        minimum = builder.constant(-(1 << (width - 1)) if target_signed else 0, work_width)
        maximum = builder.constant(
            (1 << (width - 1)) - 1 if target_signed else (1 << width) - 1, work_width
        )
        below = builder.binary("slt", converted, minimum, 1)
        above = builder.unary("not", builder.binary("sle", converted, maximum, 1), 1)
        return builder.resize(
            builder.select(
                below,
                minimum,
                builder.select(above, maximum, converted, work_width),
                work_width,
            ),
            width,
        )
