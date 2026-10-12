# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned validation of exact implementation rewrite declarations."""

from __future__ import annotations

import re

from zlang.ast import nodes as ast
from zlang.ir import module as ir_module

from .errors import SemanticError


def analyze_equivalences(
    declarations: tuple[ast.EquivDecl, ...],
) -> tuple[ir_module.EquivalenceRule, ...]:
    """Validate the deliberately small guarded exact rewrite rule language."""

    result: list[ir_module.EquivalenceRule] = []
    names: set[str] = set()
    allowed = {"or_zero", "xor_zero", "shift_zero", "mux_identity"}
    atom_re = re.compile(
        r"^(unsigned|signed|bits|bit|width|same_type|constant|power_of_two)"
        r"\(\s*([^()]*)\s*\)(?:\s*==\s*([0-9]+))?$"
    )
    identifier_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def vars_in(expr: ast.Expression) -> set[str]:
        if isinstance(expr, ast.NameExpr):
            return {expr.name}
        if isinstance(expr, ast.PatternConstantExpr):
            return {expr.witness}
        if isinstance(expr, (ast.AddExpr, ast.BinaryExpr)):
            return vars_in(expr.left) | vars_in(expr.right)
        if isinstance(expr, ast.MuxExpr):
            return vars_in(expr.condition) | vars_in(expr.when_true) | vars_in(expr.when_false)
        if isinstance(expr, ast.ResizeExpr):
            return vars_in(expr.expression)
        if isinstance(expr, ast.NumberExpr):
            return set()
        raise SemanticError(
            "equiv rules may contain only pure scalar value expressions"
        )

    def pattern_kind(
        left: ast.Expression,
        right: ast.Expression,
    ) -> tuple[str, tuple[tuple[str, str], ...], str | None] | None:
        binary_kinds = {
            ast.BinaryOperator.BIT_OR: "or_zero",
            ast.BinaryOperator.BIT_XOR: "xor_zero",
            ast.BinaryOperator.SHIFT_LEFT: "shift_zero",
            ast.BinaryOperator.SHIFT_RIGHT: "shift_zero",
        }
        for candidate, result_expression in ((left, right), (right, left)):
            if isinstance(result_expression, ast.NameExpr) and isinstance(
                candidate, ast.BinaryExpr
            ):
                if (
                    candidate.operator in binary_kinds
                    and isinstance(candidate.left, ast.NameExpr)
                    and candidate.left.name == result_expression.name
                    and isinstance(candidate.right, ast.PatternConstantExpr)
                    and candidate.right.kind is ast.PatternConstantKind.ZERO
                    and candidate.right.witness == result_expression.name
                ):
                    return (
                        binary_kinds[candidate.operator],
                        (("value", result_expression.name),),
                        candidate.operator.value,
                    )
            if (
                isinstance(candidate, ast.MuxExpr)
                and isinstance(candidate.condition, ast.NameExpr)
                and isinstance(candidate.when_true, ast.NameExpr)
                and isinstance(candidate.when_false, ast.NameExpr)
                and candidate.when_true.name == candidate.when_false.name
                and isinstance(result_expression, ast.NameExpr)
                and result_expression.name == candidate.when_true.name
            ):
                return (
                    "mux_identity",
                    (
                        ("condition", candidate.condition.name),
                        ("value", result_expression.name),
                    ),
                    None,
                )
            if (
                isinstance(candidate, ast.ResizeExpr)
                and isinstance(candidate.expression, ast.NameExpr)
                and isinstance(result_expression, ast.NameExpr)
                and result_expression.name == candidate.expression.name
            ):
                return "resize_identity", (("value", result_expression.name),), None
        return None

    def analyze_guard(
        text: str,
        variables: set[str],
    ) -> ir_module.EquivalenceGuardPredicate:
        match = atom_re.fullmatch(text.strip())
        if match is None:
            raise SemanticError(f"unsupported equiv guard '{text}'")
        kind = ir_module.EquivalenceGuardKind(match.group(1))
        arguments = tuple(
            item.strip() for item in match.group(2).split(",") if item.strip()
        )
        expected_arity = (
            2 if kind is ir_module.EquivalenceGuardKind.SAME_TYPE else 1
        )
        if len(arguments) != expected_arity or any(
            identifier_re.fullmatch(item) is None for item in arguments
        ):
            raise SemanticError(
                f"equiv guard '{text}' expects {expected_arity} bound identifier"
                f"{'s' if expected_arity != 1 else ''}"
            )
        unbound = tuple(item for item in arguments if item not in variables)
        if unbound:
            raise SemanticError(
                f"equiv guard '{text}' references unbound pattern variable "
                f"'{unbound[0]}'"
            )
        value = int(match.group(3)) if match.group(3) is not None else None
        if kind is ir_module.EquivalenceGuardKind.WIDTH:
            if value is None:
                raise SemanticError(
                    f"equiv guard '{text}' must compare width to an integer"
                )
        elif value is not None:
            raise SemanticError(
                f"equiv guard '{text}' does not accept an integer comparison"
            )
        return ir_module.EquivalenceGuardPredicate(kind, arguments, value)

    for declaration in declarations:
        if declaration.name in names:
            raise SemanticError(f"duplicate equiv declaration '{declaration.name}'")
        names.add(declaration.name)
        variables = vars_in(declaration.left) | vars_in(declaration.right)
        pattern = pattern_kind(declaration.left, declaration.right)
        if pattern is None or pattern[0] not in allowed:
            raise SemanticError(
                f"equiv '{declaration.name}' is not an approved pure, exact-width "
                "e-graph optimization rewrite; arithmetic identities are not accepted"
            )
        kind, bindings, operator = pattern
        if set(dict(bindings).values()) != variables:
            raise SemanticError(
                f"equiv '{declaration.name}' has a pattern constant or variable "
                "that is not bound by the approved rule shape"
            )
        predicates = declaration.guard.predicates if declaration.guard is not None else ()
        guards = tuple(sorted(
            (analyze_guard(predicate, variables) for predicate in predicates),
            key=lambda item: item.render(),
        ))
        result.append(ir_module.EquivalenceRule(
            declaration.name, kind, tuple(sorted(variables)), guards, bindings, operator
        ))
    return tuple(result)
