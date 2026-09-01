"""Deterministic compiler-only real arithmetic for compile-time intrinsics."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Decimal,
    InvalidOperation,
    localcontext,
)
import decimal
from fractions import Fraction
from functools import lru_cache
import sys
from typing import Callable

from zlang.fixed_point import apply_overflow, quantize_rational


SCHEMA_VERSION = "zlang-ct-real-v2"
DECIMAL_VERSION = getattr(decimal, "__version__", "stdlib")
PYTHON_DECIMAL_IDENTITY = (
    "python",
    f"{sys.version_info.major}.{sys.version_info.minor}",
    "decimal",
    DECIMAL_VERSION,
)
PRECISION_STEPS = (64, 96, 128, 192, 256, 384)
EVALUATION_GUARD_DIGITS = 24


class CompileTimeRealError(ValueError):
    """The compile-time real evaluator cannot produce a legal value."""


EvalFn = Callable[[int], Decimal]


@dataclass(frozen=True)
class CompileTimeReal:
    """A compiler-only real value.

    ``rational`` and ``linear_pi`` are exact side channels used for integer and
    cardinal detection. ``linear_pi`` is ``a*pi + b`` when that form is still
    exact; otherwise it is ``None`` and decimal evaluation is used only at an
    explicit quantization boundary.
    """

    identity: tuple[object, ...]
    evaluator: EvalFn
    rational: Fraction | None = None
    linear_pi: tuple[Fraction, Fraction] | None = None

    @staticmethod
    def rational_value(value: Fraction) -> "CompileTimeReal":
        return CompileTimeReal(
            ("rational", value.numerator, value.denominator),
            lambda precision: _decimal_from_fraction(value, precision),
            rational=value,
            linear_pi=(Fraction(0), value),
        )

    @staticmethod
    def pi() -> "CompileTimeReal":
        return CompileTimeReal(
            ("pi",),
            lambda precision: _pi_decimal(precision),
            linear_pi=(Fraction(1), Fraction(0)),
        )

    def exact_integer(self) -> int | None:
        if self.rational is None or self.rational.denominator != 1:
            return None
        return self.rational.numerator

    def evaluate(self, precision: int) -> Decimal:
        return self.evaluator(precision)


def dependency_identity() -> tuple[str, str, tuple[str, str, str, str]]:
    return (SCHEMA_VERSION, "stdlib-decimal", PYTHON_DECIMAL_IDENTITY)


def negate(value: CompileTimeReal) -> CompileTimeReal:
    rational = -value.rational if value.rational is not None else None
    linear = None
    if value.linear_pi is not None:
        coeff, offset = value.linear_pi
        linear = (-coeff, -offset)
    return CompileTimeReal(
        ("neg", value.identity),
        lambda precision: -value.evaluate(precision),
        rational=rational,
        linear_pi=linear,
    )


def add(left: CompileTimeReal, right: CompileTimeReal) -> CompileTimeReal:
    rational = (
        left.rational + right.rational
        if left.rational is not None and right.rational is not None
        else None
    )
    linear = None
    if left.linear_pi is not None and right.linear_pi is not None:
        left_coeff, left_offset = left.linear_pi
        right_coeff, right_offset = right.linear_pi
        linear = (left_coeff + right_coeff, left_offset + right_offset)
    return CompileTimeReal(
        ("add", left.identity, right.identity),
        lambda precision: left.evaluate(precision) + right.evaluate(precision),
        rational=rational,
        linear_pi=linear,
    )


def subtract(left: CompileTimeReal, right: CompileTimeReal) -> CompileTimeReal:
    return add(left, negate(right))


def multiply(left: CompileTimeReal, right: CompileTimeReal) -> CompileTimeReal:
    rational = (
        left.rational * right.rational
        if left.rational is not None and right.rational is not None
        else None
    )
    linear = None
    if left.rational is not None and right.linear_pi is not None:
        coeff, offset = right.linear_pi
        linear = (coeff * left.rational, offset * left.rational)
    elif right.rational is not None and left.linear_pi is not None:
        coeff, offset = left.linear_pi
        linear = (coeff * right.rational, offset * right.rational)
    return CompileTimeReal(
        ("mul", left.identity, right.identity),
        lambda precision: left.evaluate(precision) * right.evaluate(precision),
        rational=rational,
        linear_pi=linear,
    )


def divide(left: CompileTimeReal, right: CompileTimeReal) -> CompileTimeReal:
    if right.rational == 0:
        raise CompileTimeRealError("compile-time real division by zero")
    rational = (
        left.rational / right.rational
        if left.rational is not None and right.rational is not None
        else None
    )
    linear = None
    if right.rational is not None and left.linear_pi is not None:
        coeff, offset = left.linear_pi
        linear = (coeff / right.rational, offset / right.rational)

    def evaluator(precision: int) -> Decimal:
        denominator = right.evaluate(precision + EVALUATION_GUARD_DIGITS)
        if denominator == 0:
            raise CompileTimeRealError("compile-time real division by zero")
        return left.evaluate(precision + EVALUATION_GUARD_DIGITS) / denominator

    return CompileTimeReal(
        ("div", left.identity, right.identity),
        evaluator,
        rational=rational,
        linear_pi=linear,
    )


def sin(value: CompileTimeReal) -> CompileTimeReal:
    cardinal = _cardinal_sin_cos(value, sine=True)
    if cardinal is not None:
        return CompileTimeReal.rational_value(cardinal)
    periodic = _periodic_linear_pi(value)
    if periodic is not None:
        numerator, denominator = periodic.numerator, periodic.denominator
        return CompileTimeReal(
            ("sin-pi-mod-2", numerator, denominator),
            lambda precision: _sin_decimal(
                _decimal_from_fraction(periodic, precision + EVALUATION_GUARD_DIGITS)
                * _pi_decimal(precision + EVALUATION_GUARD_DIGITS),
                precision,
            ),
            linear_pi=(periodic, Fraction(0)),
        )
    return CompileTimeReal(
        ("sin", value.identity),
        lambda precision: _sin_decimal(value.evaluate(precision + EVALUATION_GUARD_DIGITS), precision),
    )


def cos(value: CompileTimeReal) -> CompileTimeReal:
    cardinal = _cardinal_sin_cos(value, sine=False)
    if cardinal is not None:
        return CompileTimeReal.rational_value(cardinal)
    periodic = _periodic_linear_pi(value)
    if periodic is not None:
        numerator, denominator = periodic.numerator, periodic.denominator
        return CompileTimeReal(
            ("cos-pi-mod-2", numerator, denominator),
            lambda precision: _cos_decimal(
                _decimal_from_fraction(periodic, precision + EVALUATION_GUARD_DIGITS)
                * _pi_decimal(precision + EVALUATION_GUARD_DIGITS),
                precision,
            ),
            linear_pi=(periodic, Fraction(0)),
        )
    return CompileTimeReal(
        ("cos", value.identity),
        lambda precision: _cos_decimal(value.evaluate(precision + EVALUATION_GUARD_DIGITS), precision),
    )


def log2(value: CompileTimeReal) -> CompileTimeReal:
    _reject_non_positive(value, "log2")
    if value.rational is not None:
        exact = _exact_integer_log(Fraction(2), value.rational)
        if exact is not None:
            return CompileTimeReal.rational_value(Fraction(exact))
    return CompileTimeReal(
        ("log2", value.identity),
        lambda precision: _log_decimal(value.evaluate(precision + EVALUATION_GUARD_DIGITS), precision)
        / _log_decimal(Decimal(2), precision),
    )


def log(base: CompileTimeReal, value: CompileTimeReal) -> CompileTimeReal:
    _reject_invalid_base(base)
    _reject_non_positive(value, "log")
    if base.rational is not None and value.rational is not None:
        exact = _exact_integer_log(base.rational, value.rational)
        if exact is not None:
            return CompileTimeReal.rational_value(Fraction(exact))
    return CompileTimeReal(
        ("log", base.identity, value.identity),
        lambda precision: _log_decimal(value.evaluate(precision + EVALUATION_GUARD_DIGITS), precision)
        / _log_decimal(base.evaluate(precision + EVALUATION_GUARD_DIGITS), precision),
    )


def quantize_to_raw(
    value: CompileTimeReal,
    *,
    width: int,
    fraction: int,
    signed: bool,
    rounding: object,
    overflow: object,
    budget_step: Callable[[int], None] | None = None,
) -> tuple[int, int]:
    """Quantize a compiler-only real to one fixed raw code.

    Exact rationals use the shared fixed-point rational helper. Irrationals are
    evaluated at increasing deterministic decimal precision until the selected
    raw code is stable between a precision and a guarded retry.
    """

    if value.rational is not None:
        raw = quantize_rational(
            value.rational.numerator,
            value.rational.denominator,
            fraction=fraction,
            width=width,
            signed=signed,
            rounding=rounding,
            overflow=overflow,
        )
        return raw, 0

    previous: int | None = None
    for precision in PRECISION_STEPS:
        if budget_step is not None:
            budget_step(2)
        first = _quantize_decimal(
            value.evaluate(precision),
            width=width,
            fraction=fraction,
            signed=signed,
            rounding=rounding,
            overflow=overflow,
        )
        guarded = _quantize_decimal(
            value.evaluate(precision + EVALUATION_GUARD_DIGITS),
            width=width,
            fraction=fraction,
            signed=signed,
            rounding=rounding,
            overflow=overflow,
        )
        if first == guarded and (previous is None or previous == first):
            return first, precision
        previous = guarded
    raise CompileTimeRealError(
        "compile-time real quantization did not converge within "
        f"{PRECISION_STEPS[-1]} decimal digits"
    )


def _reject_non_positive(value: CompileTimeReal, name: str) -> None:
    if value.rational is not None and value.rational <= 0:
        raise CompileTimeRealError(f"{name} requires a positive argument")


def _reject_invalid_base(base: CompileTimeReal) -> None:
    if base.rational is not None and (base.rational <= 0 or base.rational == 1):
        raise CompileTimeRealError("log base must be positive and not equal to one")


def _cardinal_sin_cos(value: CompileTimeReal, *, sine: bool) -> Fraction | None:
    if value.linear_pi is None:
        return None
    coefficient, offset = value.linear_pi
    if offset != 0:
        return None
    quarter_turns = coefficient * 2
    if quarter_turns.denominator != 1:
        return None
    index = quarter_turns.numerator % 4
    if sine:
        return (Fraction(0), Fraction(1), Fraction(0), Fraction(-1))[index]
    return (Fraction(1), Fraction(0), Fraction(-1), Fraction(0))[index]


def _periodic_linear_pi(value: CompileTimeReal) -> Fraction | None:
    """Return an exact ``pi`` coefficient reduced to one two-pi period.

    This is deliberately restricted to values whose ``a*pi`` form is retained
    exactly.  Decimal approximations and non-zero rational offsets never enter
    this identity path, so periodic cache reuse cannot change numerical
    semantics.
    """

    if value.linear_pi is None:
        return None
    coefficient, offset = value.linear_pi
    if offset != 0:
        return None
    return coefficient % 2


def _exact_integer_log(base: Fraction, value: Fraction) -> int | None:
    if base <= 0 or base == 1 or value <= 0:
        return None
    if base < 1:
        return _exact_integer_log(1 / base, 1 / value)
    if value == 1:
        return 0
    current = Fraction(1)
    if value > 1:
        for exponent in range(1, 4097):
            current *= base
            if current == value:
                return exponent
            if current > value:
                return None
    else:
        for exponent in range(1, 4097):
            current /= base
            if current == value:
                return -exponent
            if current < value:
                return None
    return None


def _decimal_from_fraction(value: Fraction, precision: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = precision + EVALUATION_GUARD_DIGITS
        return Decimal(value.numerator) / Decimal(value.denominator)


@lru_cache(maxsize=32)
def _pi_decimal(precision: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = precision + EVALUATION_GUARD_DIGITS
        return +(
            Decimal(16) * _atan_inverse_decimal(5, ctx.prec)
            - Decimal(4) * _atan_inverse_decimal(239, ctx.prec)
        )


def _atan_inverse_decimal(denominator: int, precision: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = precision + EVALUATION_GUARD_DIGITS
        x = Decimal(1) / Decimal(denominator)
        x_squared = x * x
        term = x
        total = x
        threshold = Decimal(1).scaleb(-(precision + EVALUATION_GUARD_DIGITS))
        n = 1
        sign = -1
        while True:
            term *= x_squared
            addend = term / Decimal(2 * n + 1)
            if abs(addend) < threshold:
                break
            total = total + addend if sign > 0 else total - addend
            sign *= -1
            n += 1
        return +total


def _reduce_radians(value: Decimal, precision: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = precision + EVALUATION_GUARD_DIGITS
        pi = _pi_decimal(ctx.prec)
        two_pi = pi * 2
        reduced = value % two_pi
        if reduced > pi:
            reduced -= two_pi
        if reduced <= -pi:
            reduced += two_pi
        return +reduced


def _sin_decimal(value: Decimal, precision: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = precision + EVALUATION_GUARD_DIGITS
        x = _reduce_radians(value, ctx.prec)
        sign = 1
        half_pi = _pi_decimal(ctx.prec) / 2
        pi = _pi_decimal(ctx.prec)
        if x > half_pi:
            x = pi - x
        elif x < -half_pi:
            x = pi + x
            sign = -1
        result = _sin_series(x, ctx.prec)
        return +result if sign > 0 else +(-result)


def _cos_decimal(value: Decimal, precision: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = precision + EVALUATION_GUARD_DIGITS
        x = _reduce_radians(value, ctx.prec)
        sign = 1
        half_pi = _pi_decimal(ctx.prec) / 2
        pi = _pi_decimal(ctx.prec)
        if x > half_pi:
            x = pi - x
            sign = -1
        elif x < -half_pi:
            x = -pi - x
            sign = -1
        result = _cos_series(x, ctx.prec)
        return +result if sign > 0 else +(-result)


def _sin_series(x: Decimal, precision: int) -> Decimal:
    term = x
    total = x
    x_squared = x * x
    threshold = Decimal(1).scaleb(-(precision + EVALUATION_GUARD_DIGITS))
    n = 1
    while True:
        term = -(term * x_squared) / Decimal((2 * n) * (2 * n + 1))
        if abs(term) < threshold:
            break
        total += term
        n += 1
    return +total


def _cos_series(x: Decimal, precision: int) -> Decimal:
    term = Decimal(1)
    total = Decimal(1)
    x_squared = x * x
    threshold = Decimal(1).scaleb(-(precision + EVALUATION_GUARD_DIGITS))
    n = 1
    while True:
        term = -(term * x_squared) / Decimal((2 * n - 1) * (2 * n))
        if abs(term) < threshold:
            break
        total += term
        n += 1
    return +total


def _log_decimal(value: Decimal, precision: int) -> Decimal:
    if value <= 0:
        raise CompileTimeRealError("log requires a positive argument")
    with localcontext() as ctx:
        ctx.prec = precision + EVALUATION_GUARD_DIGITS
        try:
            return +value.ln()
        except InvalidOperation as error:
            raise CompileTimeRealError("log requires a positive argument") from error


def _decimal_floor_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _quantize_decimal(
    value: Decimal,
    *,
    width: int,
    fraction: int,
    signed: bool,
    rounding: object,
    overflow: object,
) -> int:
    scale = Decimal(1 << fraction)
    scaled = value * scale
    mode = getattr(rounding, "value", rounding)
    if mode == "toward_zero":
        rounded = _decimal_floor_int(abs(scaled))
        if scaled < 0:
            rounded = -rounded
    elif mode == "floor":
        rounded = _decimal_floor_int(scaled)
    elif mode == "away_zero":
        magnitude = abs(scaled)
        floor = _decimal_floor_int(magnitude)
        rounded = floor + int(magnitude != Decimal(floor))
        if scaled < 0:
            rounded = -rounded
    elif mode == "nearest_even":
        rounded = int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))
    else:
        raise CompileTimeRealError(f"unknown fixed-point rounding mode '{mode}'")
    return apply_overflow(rounded, width=width, signed=signed, policy=overflow)
