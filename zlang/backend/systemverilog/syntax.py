"""Small, shared SystemVerilog lexical rendering primitives.

These helpers own spelling rules only.  The production emitter and the
conservative contract emitter deliberately retain independent expression
lowering and supported-feature policies.
"""

from __future__ import annotations


def sized_decimal(width: int, value: int, *, signed: bool) -> str:
    """Render one legal, exact-width SystemVerilog decimal literal."""

    if width < 1:
        raise ValueError("a sized SystemVerilog literal requires positive width")
    if signed and value < 0:
        # IEEE 1800 applies unary minus to the sized literal.  A sign between
        # ``d`` and the digits (for example ``8'sd-1``) is not legal syntax.
        return f"-{width}'sd{-value}"
    sign = "s" if signed else ""
    return f"{width}'{sign}d{value}"


__all__ = ["sized_decimal"]
