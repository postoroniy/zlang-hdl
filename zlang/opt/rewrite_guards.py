"""Typed semantic guard evaluation for source-authored exact rewrites."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ir.module import EquivalenceGuardKind, EquivalenceGuardPredicate
from zlang.ir.types import BitType, BitsType, HardwareType, SIntType, UIntType
from zlang.opt.rewrite_model import Term, term_constant_value


class RewriteGuardError(ValueError):
    """A serialized or constructed guard is outside the frozen guard set."""


@dataclass(frozen=True)
class PatternValue:
    type: HardwareType
    constant: int | None = None


def pattern_options(
    variable: str,
    type_: HardwareType,
    guards: tuple[EquivalenceGuardPredicate, ...],
    terms: tuple[Term, ...],
) -> tuple[PatternValue, ...]:
    """Enumerate deterministic typed bindings required by a guard set."""

    requires_constant = any(
        predicate.kind
        in {EquivalenceGuardKind.CONSTANT, EquivalenceGuardKind.POWER_OF_TWO}
        and variable in predicate.arguments
        for predicate in guards
    )
    if not requires_constant:
        return (PatternValue(type_),)
    values = {
        value
        for item in terms
        if item.type == type_
        if (value := term_constant_value(item)) is not None
    }
    return tuple(PatternValue(type_, value) for value in sorted(values))


def guard_holds(
    predicate: EquivalenceGuardPredicate,
    bindings: dict[str, PatternValue],
) -> bool:
    """Evaluate one frozen guard against exact typed pattern bindings."""

    values = tuple(bindings[name] for name in predicate.arguments)
    first = values[0]
    if predicate.kind is EquivalenceGuardKind.UNSIGNED:
        return isinstance(first.type, UIntType)
    if predicate.kind is EquivalenceGuardKind.SIGNED:
        return isinstance(first.type, SIntType)
    if predicate.kind is EquivalenceGuardKind.BITS:
        return isinstance(first.type, BitsType)
    if predicate.kind is EquivalenceGuardKind.BIT:
        return isinstance(first.type, BitType)
    if predicate.kind is EquivalenceGuardKind.WIDTH:
        return first.type.width == predicate.value
    if predicate.kind is EquivalenceGuardKind.SAME_TYPE:
        return values[0].type == values[1].type
    if predicate.kind is EquivalenceGuardKind.CONSTANT:
        return first.constant is not None
    if predicate.kind is EquivalenceGuardKind.POWER_OF_TWO:
        value = first.constant
        return value is not None and value > 0 and value & (value - 1) == 0
    raise RewriteGuardError(
        f"unsupported equiv guard kind '{predicate.kind.value}'"
    )


__all__ = [
    "PatternValue",
    "RewriteGuardError",
    "guard_holds",
    "pattern_options",
]
