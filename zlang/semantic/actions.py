# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned traversal of conditional semantic action trees."""

from __future__ import annotations

from dataclasses import dataclass

from zlang.ast import nodes as ast
from zlang.source import SourceSpan


@dataclass(frozen=True)
class ConditionalActionLeaf:
    """One source effect and its exact path inside one atomic action tree."""

    action: ast.NextAssignment | ast.OutputDrive | ast.ResourceAction
    activation: ast.Expression | None
    branch_path: tuple[tuple[tuple[int, ...], bool], ...]
    conditions: tuple[tuple[ast.Expression, bool], ...] = ()


def _combine_action_activation(
    left: ast.Expression | None,
    right: ast.Expression,
    origin: SourceSpan | None,
) -> ast.Expression:
    if left is None:
        return right
    return ast.BinaryExpr(
        ast.BinaryOperator.BIT_AND,
        left,
        right,
        origin=origin,
    )


def conditional_action_leaves(
    actions: tuple[
        ast.NextAssignment
        | ast.OutputDrive
        | ast.ResourceAction
        | ast.ConditionalAction,
        ...,
    ],
    *,
    activation: ast.Expression | None = None,
    branch_path: tuple[tuple[tuple[int, ...], bool], ...] = (),
    conditions: tuple[tuple[ast.Expression, bool], ...] = (),
    structural_path: tuple[int, ...] = (),
) -> tuple[ConditionalActionLeaf, ...]:
    """Flatten effects while preserving exact first-match branch paths."""

    leaves: list[ConditionalActionLeaf] = []
    for ordinal, action in enumerate(actions):
        if not isinstance(action, ast.ConditionalAction):
            leaves.append(ConditionalActionLeaf(
                action, activation, branch_path, conditions
            ))
            continue
        decision = (*structural_path, ordinal)
        true_activation = _combine_action_activation(
            activation, action.guard, action.origin
        )
        leaves.extend(conditional_action_leaves(
            action.when_true,
            activation=true_activation,
            branch_path=(*branch_path, (decision, True)),
            conditions=(*conditions, (action.guard, True)),
            structural_path=(*decision, 0),
        ))
        if action.when_false is not None:
            false_guard = ast.UnaryExpr(
                ast.BinaryOperator.LOGIC_NOT,
                action.guard,
                origin=action.origin,
            )
            false_activation = _combine_action_activation(
                activation, false_guard, action.origin
            )
            leaves.extend(conditional_action_leaves(
                action.when_false,
                activation=false_activation,
                branch_path=(*branch_path, (decision, False)),
                conditions=(*conditions, (action.guard, False)),
                structural_path=(*decision, 1),
            ))
    return tuple(leaves)


def action_paths_are_exclusive(
    left: tuple[tuple[tuple[int, ...], bool], ...],
    right: tuple[tuple[tuple[int, ...], bool], ...],
) -> bool:
    """Prove exclusivity only from opposite arms of one source conditional."""

    left_decisions = dict(left)
    right_decisions = dict(right)
    return any(
        left_decisions[key] is not right_decisions[key]
        for key in left_decisions.keys() & right_decisions.keys()
    )
