"""Shared exact fixed-point arithmetic helpers.

These helpers operate only on integers/rationals.  They are the production
compile-time and simulator oracle for policy semantics; RTL emitters render the
same operations structurally from typed IR.
"""

from __future__ import annotations


def round_ratio(numerator: int, denominator: int, mode: object) -> int:
    """Round one exact rational to an integer under a named ZLang policy."""

    if denominator <= 0:
        raise ValueError("fixed-point rational denominator must be positive")
    name = getattr(mode, "value", mode)
    magnitude, remainder = divmod(abs(numerator), denominator)
    negative = numerator < 0
    if name == "toward_zero":
        rounded = magnitude
    elif name == "floor":
        rounded = magnitude + int(negative and remainder != 0)
    elif name == "away_zero":
        rounded = magnitude + int(remainder != 0)
    elif name == "nearest_even":
        doubled = remainder * 2
        rounded = magnitude + int(
            doubled > denominator
            or (doubled == denominator and bool(magnitude & 1))
        )
    else:
        raise ValueError(f"unknown fixed-point rounding mode: {name}")
    return -rounded if negative else rounded


def apply_overflow(value: int, *, width: int, signed: bool, policy: object) -> int:
    """Apply one target-width overflow policy to a rounded raw integer."""

    name = getattr(policy, "value", policy)
    minimum = -(1 << (width - 1)) if signed else 0
    maximum = (1 << (width - 1)) - 1 if signed else (1 << width) - 1
    if name == "saturate":
        return min(max(value, minimum), maximum)
    if name != "wrap":
        raise ValueError(f"unknown fixed-point overflow mode: {name}")
    raw = value & ((1 << width) - 1)
    if signed and raw >= (1 << (width - 1)):
        raw -= 1 << width
    return raw


def quantize_rational(
    numerator: int,
    denominator: int,
    *,
    fraction: int,
    width: int,
    signed: bool,
    rounding: object,
    overflow: object,
) -> int:
    """Quantize an exact mathematical rational into one fixed target."""

    rounded = round_ratio(numerator << fraction, denominator, rounding)
    return apply_overflow(
        rounded, width=width, signed=signed, policy=overflow
    )
