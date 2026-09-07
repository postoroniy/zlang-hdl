"""Semantic normalization for nested sequential ``when`` action trees."""

from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir import state
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


def _analyze(source: str):
    return analyze(parse(source))


def test_outer_chain_and_nested_actions_remain_one_atomic_rule() -> None:
    module = _analyze("""
      module FirstMatch {
        clock clk reset rst
        in fault, clear, valid : bit
        out event : bit
        reg status : u2 = 0
        choose: when fault {
          when valid { status <- 3 event <- 1 }
          else { status <- 2 event <- 0 }
        } else when clear {
          status <- 0
          event <- 0
        } else {
          status <- 1
          event <- 1
        }
      }
    """)

    assert len(module.rules) == 1
    rule = module.rules[0]
    assert rule.name == "choose"
    assert isinstance(rule.guard, expr.Constant) and rule.guard.value == 1
    assert len(rule.actions) == 8
    assert all(action.activation is not None for action in rule.actions)

    transition = module.resolved_transition
    assert transition is not None
    assert len(transition.action_groups) == 1
    group = transition.group("choose")
    assert len(group.actions) == len(rule.actions)
    assert all(action.activation is not None for action in group.actions)
    assert {
        resource.kind for resource in transition.resources
    } == {state.StateResourceKind.REGISTER, state.StateResourceKind.OUTPUT}
    assert any(
        action.kind is state.StateActionKind.OUTPUT_WRITE
        for action in group.actions
    )


def test_opposite_branches_may_write_one_target_but_overlapping_paths_may_not() -> None:
    accepted = _analyze("""
      module Exclusive {
        clock clk reset rst in fire,select:bit out y:u8 reg x:u8=0
        when fire {
          when select { x <- 1 y <- 1 }
          else { x <- 2 y <- 2 }
        }
      }
    """)
    assert [action.target.name for action in accepted.rules[0].actions] == [
        "x", "y", "x", "y",
    ]

    rejected = (
        "module Overlap { clock clk reset rst in fire,a,b:bit out y:u8 "
        "reg x:u8=0 when fire { when a { x <- 1 } when b { x <- 2 } } y=x }"
    )
    with pytest.raises(SemanticError) as caught:
        _analyze(rejected)
    assert caught.value.code == "ZL-SEMANTIC-CONDITIONAL-ACTION-CONFLICT"
    assert "writes register 'x' twice" in str(caught.value)
    assert caught.value.primary is not None

    unconditional = (
        "module Common { clock clk reset rst in fire,a:bit out y:u8 "
        "reg x:u8=0 when fire { x <- 0 when a { x <- 1 } } y=x }"
    )
    with pytest.raises(
        SemanticError, match="writes register 'x' twice.*overlapping"
    ):
        _analyze(unconditional)


def test_nested_guard_refines_dynamic_vector_write_on_each_exact_path() -> None:
    true_path = _analyze("""
      module TrueRange {
        clock clk reset rst in fire:bit in index:u3 in value:u8 out y:u8
        reg samples:vec<5,u8>=repeat<5>(0)
        when fire { when index < 5 { samples[index] <- value } }
        y=samples[0]
      }
    """)
    update = true_path.rules[0].actions[0].expression
    assert isinstance(update, expr.VectorUpdate)
    assert (update.index_range.minimum, update.index_range.maximum) == (0, 4)

    false_path = _analyze("""
      module FalseRange {
        clock clk reset rst in fire:bit in index:u3 in value:u8 out y:u8
        reg samples:vec<5,u8>=repeat<5>(0)
        when fire { when index >= 5 {} else { samples[index] <- value } }
        y=samples[0]
      }
    """)
    update = false_path.rules[0].actions[0].expression
    assert isinstance(update, expr.VectorUpdate)
    assert (update.index_range.minimum, update.index_range.maximum) == (0, 4)


def test_nested_guard_requires_bit_with_source_attribution() -> None:
    with pytest.raises(SemanticError) as caught:
        _analyze(
            "module BadGuard { clock clk reset rst in fire:bit in select:u2 "
            "out y:u8 reg x:u8=0 when fire { when select { x <- 1 } } y=x }"
        )
    assert caught.value.code == "ZL-SEMANTIC-CONDITIONAL-GUARD"
    assert "nested when guard" in str(caught.value)
    assert caught.value.primary is not None


def test_priority_and_fsm_keep_their_existing_rule_identities() -> None:
    priority = _analyze("""
      module NestedPriority {
        clock clk reset rst in hi,lo,select:bit out y:u8
        reg a:u8=0 reg b:u8=0
        priority {
          high: when hi { when select { a <- 1 } else { a <- 2 } }
          low: when lo { when select { b <- 1 } else { b <- 2 } }
        }
        y=a
      }
    """)
    assert [rule.name for rule in priority.rules] == ["high", "low"]
    assert [len(rule.actions) for rule in priority.rules] == [2, 2]
    assert all(
        action.activation is not None
        for rule in priority.rules
        for action in rule.actions
    )

    fsm = _analyze("""
      enum Phase { Idle Done }
      module NestedFsm {
        clock clk reset rst in go,select:bit out y:u8 reg x:u8=0
        fsm phase:Phase=Idle {
          Idle { when go -> Done {
            when select { x <- 1 } else { x <- 2 }
          } }
          Done { hold }
        }
        y=x
      }
    """)
    assert len(fsm.rules) == 1
    assert fsm.rules[0].name.startswith("__fsm_")
    assert [action.activation is not None for action in fsm.rules[0].actions] == [
        False, True, True,
    ]


def test_nested_resource_actions_are_discovered_before_storage_ownership() -> None:
    module = _analyze("""
      module NestedFifo {
        clock clk reset rst in fire,select:bit in a,b:u8 out count:u2
        fifo q:fifo<u8,2>
        when fire {
          when select { q.push(a) }
          else { q.push(b) }
        }
        count=truncate<2>(q.count)
      }
    """)
    assert module.fifos[0].scheduled
    transition = module.resolved_transition
    assert transition is not None
    actions = transition.action_groups[0].actions
    assert [action.kind for action in actions] == [
        state.StateActionKind.FIFO_PUSH,
        state.StateActionKind.FIFO_PUSH,
    ]
    assert all(action.activation is not None for action in actions)

    memory = _analyze("""
      module NestedMemory {
        clock clk reset rst in fire,write:bit in address:u2 in data:u8 out q:u8
        memory table:mem<u8,4> { read_latency 1 collision read_first }
        when fire {
          when write { table.write(address,data) }
          else { table.read(address) }
        }
        q=table.read_data
      }
    """)
    assert memory.memories[0].scheduled
    transition = memory.resolved_transition
    assert transition is not None
    assert [action.kind for action in transition.action_groups[0].actions] == [
        state.StateActionKind.MEMORY_WRITE,
        state.StateActionKind.MEMORY_READ_REQUEST,
    ]


def test_empty_conditional_tree_is_not_a_scheduled_rule() -> None:
    with pytest.raises(SemanticError, match="has no state or output effects"):
        _analyze(
            "module Empty { clock clk reset rst in a:bit out y:u1 "
            "when a { when a {} else {} } y=0 }"
        )


def test_child_output_dependency_includes_nested_activation_predicates() -> None:
    source = """
      module ConditionalEcho {
        clock clk reset rst in x:bit out y:bit
        when 1 { when x { y <- 1 } else { y <- 0 } }
      }
      module ConditionalLoop {
        clock clk reset rst out y:bit
        child:ConditionalEcho { x=child.y }
        y=child.y
      }
    """
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: child\.y -> child\.y",
    ):
        compile_source(
            source, top="ConditionalLoop", include_clash=False
        )
