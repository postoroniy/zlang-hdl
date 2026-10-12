"""Declarative identities and provenance for exact typed rewrite rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RewriteRule(str, Enum):
    ADD_ZERO = "add_zero"
    SUBTRACT_ZERO = "subtract_zero"
    MULTIPLY_ZERO = "multiply_zero"
    MULTIPLY_ONE = "multiply_one"
    BIT_OR_ZERO = "bit_or_zero"
    BIT_XOR_ZERO = "bit_xor_zero"
    SHIFT_ZERO = "shift_zero"
    MUX_IDENTITY = "mux_identity"
    MUX_CONSTANT = "mux_constant"
    RESIZE_IDENTITY = "resize_identity"
    MULTIPLY_POWER_OF_TWO = "multiply_power_of_two"
    ADD_COMMUTE = "add_commute"
    MULTIPLY_COMMUTE = "multiply_commute"


@dataclass(frozen=True)
class TypedRewriteSpec:
    """One logical rewrite independent of its egglog rule objects."""

    identity: str
    rule: RewriteRule
    family: tuple[str, str | None]
    direction: str
    provenance: tuple[str, ...]
    guards: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.identity:
            raise ValueError("rewrite identity must not be empty")
        if self.direction != "equality":
            raise ValueError("the exact-value subsystem accepts equality rules only")
        if not self.provenance:
            raise ValueError("rewrite provenance must not be empty")
        if self.provenance != tuple(sorted(set(self.provenance))):
            raise ValueError("rewrite provenance must be sorted and unique")
        if len(self.guards) != len(set(self.guards)):
            raise ValueError("rewrite guards must be unique")

    @classmethod
    def builtin(
        cls,
        identity: str,
        rule: RewriteRule,
        family: tuple[str, str | None],
    ) -> TypedRewriteSpec:
        return cls(
            identity,
            rule,
            family,
            "equality",
            (f"builtin:{identity}",),
        )


@dataclass(frozen=True)
class RewriteRegistration:
    """Deterministic provenance and execution status for one engine rule."""

    identity: str
    rule: RewriteRule
    direction: str
    provenance: tuple[str, ...]
    guards: tuple[str, ...]
    enabled: bool
    fired: bool = False
    reason: str | None = None

    @classmethod
    def from_spec(
        cls,
        spec: TypedRewriteSpec,
        *,
        enabled: bool,
        fired: bool = False,
        reason: str | None = None,
    ) -> RewriteRegistration:
        return cls(
            spec.identity,
            spec.rule,
            spec.direction,
            spec.provenance,
            spec.guards,
            enabled,
            fired,
            reason,
        )


BUILTIN_REWRITE_SPECS = tuple(
    TypedRewriteSpec.builtin(identity, rule, family)
    for identity, rule, family in (
        ("bit_or_zero", RewriteRule.BIT_OR_ZERO, ("or_zero", "|")),
        ("bit_xor_zero", RewriteRule.BIT_XOR_ZERO, ("xor_zero", "^")),
        ("shift_left_zero", RewriteRule.SHIFT_ZERO, ("shift_zero", "<<")),
        ("shift_right_zero", RewriteRule.SHIFT_ZERO, ("shift_zero", ">>")),
        ("mux_identity", RewriteRule.MUX_IDENTITY, ("mux_identity", None)),
        (
            "mux_constant_false",
            RewriteRule.MUX_CONSTANT,
            ("mux_constant", "0"),
        ),
        (
            "mux_constant_true",
            RewriteRule.MUX_CONSTANT,
            ("mux_constant", "1"),
        ),
        (
            "extend_identity",
            RewriteRule.RESIZE_IDENTITY,
            ("resize_identity", "extend"),
        ),
        (
            "truncate_identity",
            RewriteRule.RESIZE_IDENTITY,
            ("resize_identity", "truncate"),
        ),
    )
)
_BUILTIN_REWRITE_SPECS_BY_ID = {
    item.identity: item for item in BUILTIN_REWRITE_SPECS
}


def builtin_rewrite_spec(identity: str) -> TypedRewriteSpec:
    """Resolve one frozen built-in specification by stable identity."""

    try:
        return _BUILTIN_REWRITE_SPECS_BY_ID[identity]
    except KeyError as error:
        raise ValueError(f"unknown built-in rewrite '{identity}'") from error


__all__ = [
    "BUILTIN_REWRITE_SPECS",
    "RewriteRegistration",
    "RewriteRule",
    "TypedRewriteSpec",
    "builtin_rewrite_spec",
]
