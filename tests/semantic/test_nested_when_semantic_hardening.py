"""Fail-closed semantic and canonical checks for nested atomic ``when``."""

from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.module import RulePriority
from zlang.ir.traversal import walk_expression
from zlang.opt.lowering import CanonicalizationError, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


def _analyze(source: str):
    return analyze(parse(source))


@pytest.mark.parametrize(
    "rule_source",
    (
        "when fire { when selector {} x <- 1 }",
        "step: when fire { when selector {} x <- 1 }",
        (
            "priority { high: when fire { when selector {} x <- 1 } "
            "low: when 0 { x <- 0 } }"
        ),
    ),
)
def test_empty_nested_branch_guard_still_requires_bit(rule_source: str) -> None:
    with pytest.raises(SemanticError) as caught:
        _analyze(
            "module Bad { clock clk reset rst in fire:bit in selector:u2 "
            f"out y:bit reg x:bit=0 {rule_source} y=x }}"
        )

    assert caught.value.code == "ZL-SEMANTIC-CONDITIONAL-GUARD"
    assert "nested when guard" in str(caught.value)


def test_empty_nested_branch_unknown_guard_is_not_erased() -> None:
    with pytest.raises(SemanticError, match="unknown input 'missing'"):
        _analyze(
            "module Bad { clock clk reset rst in fire:bit out y:bit "
            "reg x:bit=0 when fire { when missing {} x <- 1 } y=x }"
        )


def test_fsm_transition_empty_nested_branch_guard_is_checked() -> None:
    with pytest.raises(SemanticError) as caught:
        _analyze(
            "enum Phase { Idle Done } "
            "module Bad { clock clk reset rst in go:bit in selector:u2 "
            "out y:bit fsm phase:Phase=Idle { "
            "Idle { when go -> Done { when selector {} } } "
            "Done { hold } } y=phase == Phase.Done }"
        )

    assert caught.value.code == "ZL-SEMANTIC-CONDITIONAL-GUARD"


def test_explicit_empty_arm_remains_legal() -> None:
    true_only = _analyze(
        "module TrueOnly { clock clk reset rst in fire,select:bit out y:bit "
        "reg x:bit=0 when fire { when select { x <- 1 } else {} } y=x }"
    )
    false_only = _analyze(
        "module FalseOnly { clock clk reset rst in fire,select:bit out y:bit "
        "reg x:bit=0 when fire { when select {} else { x <- 1 } } y=x }"
    )

    assert len(true_only.rules[0].actions) == 1
    assert len(false_only.rules[0].actions) == 1


def test_one_source_guard_allocates_one_delay_for_both_arms() -> None:
    module = _analyze(
        "module SharedGuard { clock clk reset rst in fire,select:bit out y:bit "
        "reg x:bit=0 when fire { when delay<1>(select) { x <- 1 } "
        "else { x <- 0 } } y=x }"
    )

    instances = {
        node.instance
        for action in module.rules[0].actions
        for node in walk_expression(action.activation)
        if isinstance(node, expr.Delay)
    }
    assert instances == {0}


def test_output_dependency_keeps_activation_when_group_has_other_effects() -> None:
    source = """
      module Child {
        clock clk reset rst in x:bit out y:bit reg seen:bit=0
        step: when 1 {
          seen <- 1
          when x { y <- 1 } else { y <- 0 }
        }
      }
      module Top {
        clock clk reset rst out y:bit
        child:Child { x=child.y }
        y=child.y
      }
    """

    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: child\.y -> child\.y",
    ):
        compile_source(source, top="Top")


def _conditional_canonical():
    return lower(_analyze(
        "module Conditional { clock clk reset rst in fire,select:bit "
        "out y:u8 reg x:u8=0 when fire { when select { x <- 1 } "
        "else { x <- 0 } } y=x }"
    ))


def _priority_canonical():
    return lower(_analyze(
        "module Prioritized { clock clk reset rst in hi,lo:bit out y:u8 "
        "reg x:u8=0 priority { high: when hi { x <- 1 } "
        "low: when lo { x <- 0 } } y=x }"
    ))


def test_canonical_restore_requires_bit_rule_and_group_guards() -> None:
    canonical = _conditional_canonical()
    non_bit = canonical.registers[0].initial
    rule = replace(canonical.rules[0], guard=non_bit)
    transition = canonical.resolved_transition
    assert transition is not None
    group = replace(transition.action_groups[0], guard=non_bit)

    with pytest.raises(
        CanonicalizationError,
        match="canonical rule '.*' guard must have type bit",
    ):
        restore(replace(canonical, rules=(rule,)))
    with pytest.raises(
        CanonicalizationError,
        match="canonical action group '.*' guard must have type bit",
    ):
        restore(replace(
            canonical,
            resolved_transition=replace(transition, action_groups=(group,)),
        ))


def test_canonical_restore_rejects_overlapping_conflicting_branch_effects() -> None:
    canonical = _conditional_canonical()
    transition = canonical.resolved_transition
    assert transition is not None
    rule = canonical.rules[0]
    group = transition.action_groups[0]
    shared_activation = rule.actions[0].activation
    assert shared_activation is not None

    corrupted_rule = replace(
        rule,
        actions=(
            rule.actions[0],
            replace(rule.actions[1], activation=shared_activation),
        ),
    )
    corrupted_group = replace(
        group,
        actions=(
            group.actions[0],
            replace(group.actions[1], activation=shared_activation),
        ),
    )

    with pytest.raises(
        CanonicalizationError,
        match="overlapping conflicting effects",
    ):
        restore(replace(
            canonical,
            rules=(corrupted_rule,),
            resolved_transition=replace(
                transition,
                action_groups=(corrupted_group,),
            ),
        ))


def test_canonical_restore_requires_exact_acyclic_priority_graph() -> None:
    canonical = _priority_canonical()
    transition = canonical.resolved_transition
    assert transition is not None

    with pytest.raises(
        CanonicalizationError,
        match="priorities do not match typed rule priorities",
    ):
        restore(replace(
            canonical,
            resolved_transition=replace(transition, priorities=()),
        ))

    cyclic = (RulePriority("high", "low"), RulePriority("low", "high"))
    with pytest.raises(
        CanonicalizationError,
        match="priority graph contains a cycle",
    ):
        restore(replace(
            canonical,
            rule_priorities=cyclic,
            resolved_transition=replace(
                transition,
                priorities=(("high", "low"), ("low", "high")),
            ),
        ))

    unknown = (RulePriority("missing", "low"),)
    with pytest.raises(
        CanonicalizationError,
        match="priority references an unknown rule",
    ):
        restore(replace(
            canonical,
            rule_priorities=unknown,
            resolved_transition=replace(
                transition,
                priorities=(("missing", "low"),),
            ),
        ))
