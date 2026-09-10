"""Small reusable Clash source-rendering primitives."""

from __future__ import annotations


def apply_argument(argument: str) -> str:
    """Parenthesize a non-atomic Haskell expression used as an argument."""

    return f"({argument})" if " " in argument else argument


def or_signal_expressions(items: list[str]) -> str:
    """Combine Clash ``Signal Bit`` expressions without applicative drift."""

    if not items:
        return "pure low"
    value = items[0]
    for item in items[1:]:
        # The accumulated applicative expression must remain one operand.
        value = f"((.|.) <$> ({value}) <*> {item})"
    return value


__all__ = ["apply_argument", "or_signal_expressions"]
