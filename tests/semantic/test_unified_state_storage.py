from pathlib import Path
from itertools import product

import pytest

from zlang.ir.state import (
    FifoOccupancy,
    StateActionKind,
    groups_conflict,
    ordered_groups,
    select_action_groups,
    selection_cubes,
    selection_regions,
)
from zlang.opt.lowering import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate_cycles


ROOT = Path(__file__).resolve().parents[2]


ATOMIC = """
module AtomicFifo {
  clock clk reset rst
  in x:u8 in op:uint<3>
  out front:u8 out count:uint<2> out a:u8 out b:u8
  fifo q:fifo<u8,2>
  reg ra:u8=0 reg rb:u8=0
  rule push when op==1 { q.push(x) ra <- x rb <- x }
  rule pop when op==2 { q.pop() ra <- q.front rb <- extend<8>(q.count) }
  rule swap when op==3 { q.pop() q.push(x) ra <- q.front rb <- x }
  priority push > pop
  priority push > swap
  priority pop > swap
  front=q.front count=q.count a=ra b=rb
}
"""


def test_parser_and_typed_transition_retain_atomic_fifo_actions() -> None:
    syntax = parse(ATOMIC)
    assert syntax.rules[2].actions[0].operation == "pop"
    module = analyze(syntax)
    transition = module.resolved_transition
    assert transition is not None
    assert {item.kind.value for item in transition.resources} == {"register", "fifo"}
    assert next(item for item in transition.resources if item.name == "q").source_origin is not None
    swap = transition.group("swap")
    assert [item.kind for item in swap.actions] == [
        StateActionKind.REGISTER_WRITE,
        StateActionKind.REGISTER_WRITE,
        StateActionKind.FIFO_POP,
        StateActionKind.FIFO_PUSH,
    ]
    assert swap.actions[-1].source_origin is not None


def test_canonical_round_trip_preserves_resources_actions_and_identity() -> None:
    module = analyze(parse(ATOMIC))
    canonical = lower(module)
    restored = restore(canonical)
    assert restored == module
    assert canonical.resolved_transition is not None
    assert canonical.resolved_transition.semantic_id == module.resolved_transition.semantic_id
    assert canonical.resolved_transition.action_groups[2].actions[-1].source_origin is not None

    same = analyze(parse(ATOMIC)).resolved_transition.semantic_id
    changed = analyze(parse(ATOMIC.replace("q.push(x) ra <- x", "q.push(7) ra <- x", 1)))
    assert same != changed.resolved_transition.semantic_id


def test_push_blocked_push_pop_swap_and_current_front_are_atomic() -> None:
    module = analyze(parse(ATOMIC))
    result = simulate_cycles(module, [
        {"x": 10, "op": 1},  # push 10
        {"x": 20, "op": 1},  # push 20, now full
        {"x": 99, "op": 1},  # blocked push: neither register changes
        {"x": 30, "op": 3},  # full pop+push, registers see old front=10
        {"x": 0, "op": 2},   # pop 20
        {"x": 0, "op": 2},   # pop 30, now empty
        {"x": 40, "op": 3},  # empty pop+push: whole group suppressed
        {"x": 0, "op": 0},
    ])
    assert result[2] == {"front": 10, "count": 2, "a": 20, "b": 20}
    assert result[3] == {"front": 10, "count": 2, "a": 20, "b": 20}
    assert result[4] == {"front": 20, "count": 2, "a": 10, "b": 30}
    assert result[7] == {"front": 0, "count": 0, "a": 30, "b": 1}


def test_reset_atomically_clears_fifo_and_registers() -> None:
    module = analyze(parse(ATOMIC))
    result = simulate_cycles(
        module,
        [{"x": 10, "op": 1}, {"x": 20, "op": 1}, {"x": 99, "op": 3}, {"x": 0, "op": 0}],
        [False, False, True, False],
    )
    assert result[2] == {"front": 0, "count": 0, "a": 0, "b": 0}
    assert result[3] == {"front": 0, "count": 0, "a": 0, "b": 0}


def test_cross_rule_push_pop_are_compatible_and_capacity_is_joint() -> None:
    source = """
module Pair { clock clk reset rst in x:u8 in fire:bit
 out count:uint<2> out a:bit out b:bit fifo q:fifo<u8,2>
 reg ra:bit=0 reg rb:bit=0
 rule consume when fire { q.pop() ra <- 1 }
 rule produce when fire { q.push(x) rb <- 1 }
 count=q.count a=ra b=rb }
"""
    module = analyze(parse(source))
    groups = module.resolved_transition.action_groups
    assert not groups_conflict(groups[0], groups[1])
    result = simulate_cycles(module, [
        {"x": 7, "fire": 1},  # empty: pop group suppressed, push group fires
        {"x": 8, "fire": 1},  # both fire, count remains one
        {"x": 0, "fire": 0},
    ])
    assert result[-1] == {"count": 1, "a": 1, "b": 1}


def test_unready_higher_group_does_not_block_legal_lower_group() -> None:
    source = """module ReadyPriority { clock clk reset rst in fire:bit out y:bit
      fifo q:fifo<u8,2> reg r:bit=0
      rule high when fire { q.pop() r <- 1 }
      rule low when fire { r <- 0 }
      priority high > low y=r }"""
    module = analyze(parse(source))
    result = simulate_cycles(module, [{"fire": 0}, {"fire": 1}, {"fire": 0}])
    assert result[-1] == {"y": 0}


def test_priority_selects_one_whole_conflicting_fifo_group() -> None:
    source = """module PushPriority { clock clk reset rst in fire:bit out front:u8 out a:bit out b:bit
      fifo q:fifo<u8,2> reg ra:bit=0 reg rb:bit=0
      rule high when fire { q.push(7) ra <- 1 }
      rule low when fire { q.push(9) rb <- 1 }
      priority high > low front=q.front a=ra b=rb }"""
    module = analyze(parse(source))
    result = simulate_cycles(module, [{"fire": 1}, {"fire": 0}])
    assert result[-1] == {"front": 7, "a": 1, "b": 0}


def test_pop_push_operands_and_register_writes_see_current_front() -> None:
    source = """module Rotate { clock clk reset rst in op:uint<2> in x:u8
      out front:u8 out count:uint<2> out seen:u8 fifo q:fifo<u8,2> reg r:u8=0
      rule fill when op==1 { q.push(x) }
      rule rotate when op==2 { q.pop() q.push(q.front) r <- q.front }
      priority fill > rotate front=q.front count=q.count seen=r }"""
    module = analyze(parse(source))
    result = simulate_cycles(module, [
        {"op": 1, "x": 10}, {"op": 1, "x": 20}, {"op": 2, "x": 0},
        {"op": 0, "x": 0},
    ])
    assert result[-1] == {"front": 20, "count": 2, "seen": 10}


@pytest.mark.parametrize("operation", ("push", "pop"))
def test_same_fifo_action_conflicts_require_existing_priority(operation: str) -> None:
    operand = "(x)" if operation == "push" else "()"
    source = f"""module Conflict {{ clock clk reset rst in x:u8 in fire:bit out y:u8
      fifo q:fifo<u8,2> rule a when fire {{ q.{operation}{operand} }}
      rule b when fire {{ q.{operation}{operand} }} y=q.front }}"""
    with pytest.raises(SemanticError, match="conflicting state actions; add explicit priority"):
        analyze(parse(source))
    prioritized = source.replace("y=q.front", "priority a > b y=q.front")
    module = analyze(parse(prioritized))
    cubes = selection_cubes(module.resolved_transition, "a")
    assert cubes


def test_depth_independent_scheduler_regions_match_exact_selection() -> None:
    module = analyze(parse("""module DeepScheduler {
      clock clk reset rst in push:bit in pop:bit in value:u8 out count:u6
      fifo q:fifo<u8,32>
      rule enqueue when push { q.push(value) }
      rule dequeue when pop { q.pop() }
      count=q.count
    }"""))
    transition = module.resolved_transition
    assert transition is not None
    groups = ordered_groups(transition)
    regions = {
        group.rule_name: selection_regions(transition, group.rule_name)
        for group in groups
    }
    # Region size is independent of the 33 possible numerical count values.
    assert all(len(items) <= 4 for items in regions.values())

    def occupancy(count: int) -> FifoOccupancy:
        if count == 0:
            return FifoOccupancy.EMPTY
        if count == 32:
            return FifoOccupancy.FULL
        return FifoOccupancy.MIDDLE

    for count, guard_bits in product(range(33), product((False, True), repeat=2)):
        guards = {
            group.rule_name: value
            for group, value in zip(groups, guard_bits, strict=True)
        }
        expected = set(select_action_groups(transition, guards, {"q": count}))
        values = (occupancy(count), *guard_bits)
        actual = {
            name
            for name, items in regions.items()
            if any(
                all(want is None or want == got for want, got in zip(item, values, strict=True))
                for item in items
            )
        }
        assert actual == expected


def test_global_and_scheduled_fifo_ownership_cannot_mix() -> None:
    source = """module Bad { clock clk reset rst in x:u8 out y:u8 fifo q:fifo<u8,2>
      q.data=x q.push=0 q.pop=0 rule r when 1 { q.push(x) } y=q.front }"""
    with pytest.raises(SemanticError, match="cannot mix global controls"):
        analyze(parse(source))


def test_rule_local_memory_write_requires_address_and_data() -> None:
    source = """module BadMemory { clock clk reset rst in x:u8 out y:u8
      memory m:mem<u8,2>{read_latency 1 collision read_first}
      rule r when 1 { m.write(x) } y=x }"""
    with pytest.raises(SemanticError, match="write requires exactly address and data"):
        analyze(parse(source))


def test_sdf_shaped_acceptance_fixture_simulates_stall_and_reset() -> None:
    module = analyze(parse((ROOT / "examples/fft_sdf_stage_atomic_transition.zl").read_text()))
    result = simulate_cycles(module, [
        {"input": {"payload": 10, "valid": 1}, "output": {"ready": 1}},
        {"input": {"payload": 20, "valid": 1}, "output": {"ready": 1}},
        {"input": {"payload": 30, "valid": 1}, "output": {"ready": 0}},
        {"input": {"payload": 30, "valid": 1}, "output": {"ready": 1}},
        {"input": {"payload": 0, "valid": 0}, "output": {"ready": 1}},
    ])
    assert result[2]["input"]["transfer"] == 0
    assert result[2]["output"] == {"payload": 10, "valid": 1, "transfer": 0}
    assert result[2]["phase_debug"] == result[3]["phase_debug"]
    assert result[2]["count_debug"] == result[3]["count_debug"]
    assert result[3]["output"] == {"payload": 10, "valid": 1, "transfer": 1}
    assert result[4]["output"]["payload"] == 20

    reset = simulate_cycles(
        module,
        [
            {"input": {"payload": 10, "valid": 1}, "output": {"ready": 1}},
            {"input": {"payload": 20, "valid": 1}, "output": {"ready": 1}},
            {"input": {"payload": 0, "valid": 0}, "output": {"ready": 0}},
            {"input": {"payload": 0, "valid": 0}, "output": {"ready": 1}},
        ],
        [False, False, True, False],
    )
    assert reset[2]["output"]["valid"] == 0
    assert reset[2]["count_debug"] == 0
    assert reset[2]["phase_debug"] == 0
