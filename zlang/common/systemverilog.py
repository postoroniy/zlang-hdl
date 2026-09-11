"""Dependency-free SystemVerilog expression spelling shared by compiler paths.

Callers remain responsible for typed-IR validation and exact-width casts.  The
helpers here only make type-dependent SystemVerilog operator spelling explicit
so production, reference, and verification emitters cannot drift apart.
"""

from __future__ import annotations


def render_typed_resize(
    rendered: str,
    *,
    source_width: int,
    target_width: int,
    signed: bool,
) -> str:
    """Render the exact typed operand width used by ZLang arithmetic.

    This helper is shared by production and semantic-reference emitters so an
    enclosing SystemVerilog expression cannot silently resize multiplication
    or addition operands according to Verilog context rules.
    """

    if source_width >= target_width:
        return f"({rendered})"
    if signed:
        return f"{target_width}'($signed({rendered}))"
    extension = target_width - source_width
    return f"{{{{{extension}{{1'b0}}}}, {rendered}}}"


def render_right_shift(left: str, amount: str, *, signed: bool) -> str:
    """Render one typed right shift without relying on SV context signedness.

    SystemVerilog ``>>`` is logical even for a signed value.  Arithmetic shift
    therefore requires both ``>>>`` and an explicitly signed left operand.
    The casts below change interpretation only; they do not resize either
    operand.
    """

    cast = "$signed" if signed else "$unsigned"
    operator = ">>>" if signed else ">>"
    return f"({cast}({left}) {operator} ({amount}))"


def render_ordered_comparison(
    left: str,
    operator: str,
    right: str,
    *,
    signed: bool,
) -> str:
    """Render a typed ordered comparison with explicit SV interpretation.

    A declaration, slice, concatenation, conditional, or other surrounding
    expression can change SystemVerilog's self-determined signedness.  The
    compiler has already proved one exact operand type, so preserve that type
    at both comparison operands rather than relying on declaration context.
    These casts reinterpret existing bits and never resize them.
    """

    if operator not in {"<", "<=", ">", ">="}:
        raise ValueError(f"unsupported ordered comparison operator: {operator!r}")
    cast = "$signed" if signed else "$unsigned"
    return f"({cast}({left}) {operator} {cast}({right}))"


__all__ = [
    "render_ordered_comparison",
    "render_right_shift",
    "render_typed_resize",
]
