"""The unnumbered concise atomic-scheduling syntax slice."""

import pytest

from zlang.compiler import compile_source
from zlang.ir.module import RulePriority
from zlang.opt.lowering import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate_cycles


def _counter(prefix: str) -> str:
    return (
        f"module {prefix} {{ clock clk reset rst in clear:bit in increment:bit "
        "out y:u8 reg count:u8=0 "
        "priority { clear_count: when clear { count <- 0 } "
        "increment_count: when increment { count <- truncate<8>(count + 1) } } "
        "y=count }"
    )


def test_labeled_rule_is_exactly_verbose_rule_sugar() -> None:
    concise = analyze(parse(
        "module T { clock c reset r in go:bit out y:u8 reg x:u8=0 "
        "step: when go { x <- 1 } y=x }"
    ))
    verbose = analyze(parse(
        "module T { clock c reset r in go:bit out y:u8 reg x:u8=0 "
        "rule step when go { x <- 1 } y=x }"
    ))
    assert concise.rules == verbose.rules
    assert concise.resolved_transition.semantic_id == verbose.resolved_transition.semantic_id


def test_priority_block_matches_explicit_rules_and_edges() -> None:
    concise = analyze(parse(_counter("Concise")))
    verbose = analyze(parse(
        "module Concise { clock clk reset rst in clear:bit in increment:bit out y:u8 "
        "reg count:u8=0 rule clear_count when clear { count <- 0 } "
        "rule increment_count when increment { count <- truncate<8>(count + 1) } "
        "priority clear_count > increment_count y=count }"
    ))
    assert concise.rules == verbose.rules
    assert concise.rule_priorities == verbose.rule_priorities == (
        RulePriority("clear_count", "increment_count"),
    )
    assert concise.resolved_transition.semantic_id == verbose.resolved_transition.semantic_id
    assert restore(lower(concise)) == concise


def test_three_arm_block_creates_adjacent_transitive_edges() -> None:
    module = analyze(parse(
        "module T { clock c reset r in a:bit in b:bit in d:bit out y:u8 "
        "reg x:u8=0 priority { first: when a { x <- 1 } "
        "second: when b { x <- 2 } third: when d { x <- 3 } } y=x }"
    ))
    assert module.rule_priorities == (
        RulePriority("first", "second"),
        RulePriority("second", "third"),
    )


def test_conflicting_arms_use_earlier_arm_and_reset_suppresses_both() -> None:
    module = compile_source(_counter("PriorityCounter")).ir
    result = simulate_cycles(
        module,
        [
            {"clear": 0, "increment": 1},
            {"clear": 1, "increment": 1},
            {"clear": 0, "increment": 0},
            {"clear": 1, "increment": 1},
        ],
        reset=[False, False, False, True],
    )
    assert [item["y"] for item in result] == [0, 1, 0, 0]


def test_nonconflicting_priority_arms_still_fire_together() -> None:
    module = analyze(parse(
        "module T { clock c reset r in go:bit out a:bit out b:bit "
        "reg ra:bit=0 reg rb:bit=0 priority { "
        "when go { ra <- 1 } when go { rb <- 1 } } a=ra b=rb }"
    ))
    result = simulate_cycles(module, [{"go": 1}, {"go": 0}])
    assert result[-1] == {"a": 1, "b": 1}


def test_priority_block_anonymous_names_are_stable_and_source_attributed() -> None:
    source = (
        "module T { clock c reset r in go:bit out a:bit out b:bit "
        "reg ra:bit=0 reg rb:bit=0 priority { "
        "when go { ra <- 1 } when go { rb <- 1 } } a=ra b=rb }"
    )
    first = analyze(parse(source))
    second = analyze(parse(source))
    assert [rule.name for rule in first.rules] == [rule.name for rule in second.rules]
    assert all(rule.name.startswith("__priority_rule_") for rule in first.rules)
    assert all(rule.guard.origin is not None for rule in first.rules)


def test_compile_time_selection_precedes_priority_normalization() -> None:
    module = analyze(parse(
        "module T<N=1> { clock c reset r in go:bit out y:u8 reg x:u8=0 "
        "if N == 1 { priority { live: when go { x <- 1 } "
        "live2: when go { x <- 2 } } } else { "
        "priority { dead: when go { x <- 3 } dead2: when go { x <- 4 } } } y=x }"
    ))
    assert [rule.name for rule in module.rules] == ["live", "live2"]
    assert all("dead" not in rule.name for rule in module.rules)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "module T { clock c reset r reg x:u8=0 priority { a: when 1 { x <- 0 } } y=x }",
            "at least two arms",
        ),
        (
            "module T { clock c reset r reg x:u8=0 priority { a: when 1 { x <- 0 } a: when 1 { x <- 1 } } y=x }",
            "duplicate priority block rule label",
        ),
        (
            "module T { clock c reset r reg x:u8=0 priority { priority { a: when 1 { x <- 0 } b: when 1 { x <- 1 } } c: when 1 { x <- 2 } } y=x }",
            "nested priority blocks",
        ),
        (
            "module T { clock c reset r in go:u2 reg x:u8=0 priority { a: when go { x <- 0 } b: when 1 { x <- 1 } } y=x }",
            "guard.*must be bit",
        ),
        (
            "module T { clock c reset r in go:bit reg x:u8=0 priority { a: when go { x <- 0 } b: when go { x <- 1 } } priority b > a y=x }",
            "contains a cycle",
        ),
    ],
)
def test_priority_block_diagnostics(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        analyze(parse(source))


def test_outer_else_when_remains_one_atomic_rule() -> None:
    module = analyze(parse(
        "module T { clock c reset r in first,second:bit reg x:u8=0 "
        "when first { x <- 1 } else when second { x <- 2 } y=x }"
    ))
    assert len(module.rules) == 1
    assert len(module.rules[0].actions) == 2
    assert all(action.activation is not None for action in module.rules[0].actions)
    transition = module.resolved_transition
    assert transition is not None
    assert len(transition.action_groups) == 1
