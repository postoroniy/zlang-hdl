"""Small reusable Clash source-rendering primitives."""

from __future__ import annotations


def apply_argument(argument: str) -> str:
    """Parenthesize a non-atomic Haskell expression used as an argument."""

    return f"({argument})" if " " in argument else argument
