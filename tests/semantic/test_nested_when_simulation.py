"""Cycle semantics for nested atomic ``when`` effect activation."""

from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.ir.state import select_action_groups
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.simulate import simulate_cycles


def _module(source: str):
    return analyze(parse(source))


def test_selected_illegal_fifo_path_suppresses_group_without_else_fallback() -> None:
    module = _module(
        """
module NestedFifoNoFallback {
  clock clk reset rst
  in fill, act, choose : bit
  in data : u8
  out count : u1
  out front : u8
  out marker : bit
  fifo q : fifo<u8,1>
  reg mark : bit = 0
  priority {
    fill_rule: when fill { q.push(data) }
    choose_rule: when act {
      mark <- 1
      when choose { q.push(data) } else { q.pop() }
    }
  }
  count = q.count
  front = q.front
  marker = mark
}
"""
    )

    trace = simulate_cycles(
        module,
        [
            {"fill": 1, "act": 0, "choose": 0, "data": 7},
            {"fill": 0, "act": 1, "choose": 1, "data": 9},
            {"fill": 0, "act": 0, "choose": 0, "data": 0},
            {"fill": 0, "act": 1, "choose": 0, "data": 0},
            {"fill": 0, "act": 0, "choose": 0, "data": 0},
        ],
    )

    # The true branch requests an illegal push while full.  Readiness cannot
    # select the false branch, so the resident value remains until the later
    # cycle which explicitly selects pop.
    assert trace == [
        {"count": 0, "front": 0, "marker": 0},
        {"count": 1, "front": 7, "marker": 0},
        {"count": 1, "front": 7, "marker": 0},
        {"count": 1, "front": 7, "marker": 0},
        {"count": 0, "front": 0, "marker": 1},
    ]


def test_sibling_conditionals_contribute_to_one_atomic_pre_edge_update() -> None:
    module = _module(
        """
module NestedSiblingActions {
  clock clk reset rst
  in go, left, right : bit
  in x, y : u8
  out a, b : u8
  reg ra : u8 = 0
  reg rb : u8 = 0
  step: when go {
    when left { ra <- x }
    when right { rb <- y }
  }
  a = ra
  b = rb
}
"""
    )

    transition = module.resolved_transition
    assert transition is not None
    group = transition.group("step")
    assert len(group.actions) == 2
    assert all(action.activation is not None for action in group.actions)
    assert select_action_groups(
        transition,
        {"step": True},
        {},
        {action.semantic_id: False for action in group.actions},
    ) == ()

    trace = simulate_cycles(
        module,
        [
            {"go": 1, "left": 1, "right": 1, "x": 5, "y": 9},
            {"go": 1, "left": 0, "right": 1, "x": 6, "y": 10},
            {"go": 1, "left": 0, "right": 0, "x": 7, "y": 11},
            {"go": 0, "left": 0, "right": 0, "x": 0, "y": 0},
        ],
    )
    assert trace == [
        {"a": 0, "b": 0},
        {"a": 5, "b": 9},
        {"a": 5, "b": 10},
        {"a": 5, "b": 10},
    ]


def test_output_only_conflict_suppresses_losing_rules_other_state_effects() -> None:
    module = _module(
        """
module OutputConflictPriority {
  clock clk reset rst
  in high, low : bit
  out y, high_seen, low_seen : bit
  reg rh : bit = 0
  reg rl : bit = 0
  priority {
    higher: when high { y <- 1 rh <- 1 }
    lower: when low { y <- 0 rl <- 1 }
  }
  high_seen = rh
  low_seen = rl
}
"""
    )

    transition = module.resolved_transition
    assert transition is not None
    assert any(resource.name == "y" for resource in transition.resources)

    trace = simulate_cycles(
        module,
        [
            {"high": 1, "low": 1},
            {"high": 0, "low": 0},
        ],
    )
    assert trace == [
        {"y": 1, "high_seen": 0, "low_seen": 0},
        {"y": 0, "high_seen": 1, "low_seen": 0},
    ]


def test_reset_suppresses_conditional_fifo_and_output_effects_immediately() -> None:
    module = _module(
        """
module ResetConditionalFifo {
  clock clk reset rst
  in go, choose : bit
  in data : u8
  out count : u1
  out event : bit
  fifo q : fifo<u8,1>
  operate: when go {
    when choose {
      q.push(data)
      event <- 1
    }
  }
  count = q.count
}
"""
    )

    trace = simulate_cycles(
        module,
        [
            {"go": 1, "choose": 1, "data": 7},
            {"go": 0, "choose": 0, "data": 0},
            {"go": 1, "choose": 1, "data": 9},
            {"go": 0, "choose": 0, "data": 0},
        ],
        reset=[False, False, True, False],
    )

    assert trace == [
        {"count": 0, "event": 1},
        {"count": 1, "event": 0},
        {"count": 0, "event": 0},
        {"count": 0, "event": 0},
    ]


@pytest.mark.parametrize(
    ("guard", "activation"),
    (
        ("delay<1>(fire)", "delay<1>(select)"),
        ("pipeline(1) { fire }", "pipeline(1) { select }"),
    ),
)
def test_temporal_rule_control_advances_once_in_plain_and_hierarchy(
    guard: str,
    activation: str,
) -> None:
    source = f"""
module DelayedNestedRule {{
  clock clk reset rst
  in fire, select : bit
  out observed, event : u8
  reg state : u8 = 0
  update: when {guard} {{
    when {activation} {{
      state <- 9
      event <- 0xa5
    }} else {{
      state <- 3
      event <- 0x3c
    }}
  }}
  observed = state
}}

module DelayedNestedTop {{
  clock clk reset rst
  in fire, select : bit
  out observed, event : u8
  child : DelayedNestedRule {{ fire select }}
  observed = child.observed
  event = child.event
}}
"""
    cycles = [
        {"fire": 1, "select": 0},
        {"fire": 1, "select": 1},
        {"fire": 0, "select": 0},
        {"fire": 0, "select": 0},
        {"fire": 1, "select": 1},
        {"fire": 1, "select": 1},
        {"fire": 0, "select": 0},
    ]
    resets = [False, False, False, False, True, False, False]
    expected = [
        {"observed": 0, "event": 0},
        {"observed": 0, "event": 0x3C},
        {"observed": 3, "event": 0xA5},
        {"observed": 9, "event": 0},
        {"observed": 0, "event": 0},
        {"observed": 0, "event": 0},
        {"observed": 0, "event": 0xA5},
    ]

    plain = simulate_cycles(
        _module(source), cycles, reset=resets
    )
    hierarchy = simulate_cycles(
        compile_source(
            source, top="DelayedNestedTop"
        ).ir,
        cycles,
        reset=resets,
    )
    assert plain == expected
    assert hierarchy == expected
