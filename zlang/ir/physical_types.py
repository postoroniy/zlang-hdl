"""Backend-independent physical traits of canonical hardware types.

These traits describe an already-typed value's RTL representation.  They do
not decide language-level bitcast eligibility; that stricter responsibility
remains in :mod:`zlang.ir.packing`.
"""

from __future__ import annotations

from enum import Enum

from zlang.ir.types import BitType, BitsType, FixedType, HardwareType, SIntType


class PhysicalSignedness(str, Enum):
    BIT = "bit"
    BITS = "bits"
    SIGNED = "signed"
    UNSIGNED = "unsigned"


def physical_width(type_: HardwareType) -> int:
    """Return the exact flattened RTL width of any canonical hardware type."""

    width = type_.width
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise ValueError(f"hardware type has no positive physical width: {type_!r}")
    return width


def physical_signedness(type_: HardwareType) -> PhysicalSignedness:
    """Return the scalar/packed signedness used at a physical binding."""

    if isinstance(type_, BitType):
        return PhysicalSignedness.BIT
    if isinstance(type_, (SIntType, FixedType)):
        return PhysicalSignedness.SIGNED
    if isinstance(type_, BitsType):
        return PhysicalSignedness.BITS
    return PhysicalSignedness.UNSIGNED


__all__ = ["PhysicalSignedness", "physical_signedness", "physical_width"]
