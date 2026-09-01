from dataclasses import replace

import pytest

from zlang.compiler import compile_source
from zlang.ir.state import StateActionKind, StateResourceKind, groups_conflict
from zlang.ir.types import UIntType
from zlang.opt.lowering import CanonicalizationError, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate_cycles


def source(collision: str = "write_first") -> str:
    return f"""
module ScheduledMemory {{
  clock clk reset rst
  in rd:bit in wr:bit in address:u2 in data:u8
  out q:u8 out touched:bit
  memory table:mem<u8,4> {{ read_latency 1 collision {collision} }}
  reg seen:bit=0
  rule fetch when rd {{ table.read(address) seen <- 1 }}
  rule store when wr {{ table.write(address,data) }}
  q=table.read_data touched=seen
}}
"""


def test_parser_accepts_comma_separated_resource_operands() -> None:
    syntax = parse(source())
    assert len(syntax.rules[0].actions[0].operands) == 1
    assert len(syntax.rules[1].actions[0].operands) == 2
    for action, count in (("table.read()", 0), ("table.write(0,1,2)", 3)):
        parsed = parse(source().replace("table.read(address)", action))
        assert len(parsed.rules[0].actions[0].operands) == count


def test_memory_actions_are_typed_and_linked_to_authoritative_memory() -> None:
    module = analyze(parse(source()))
    memory = module.memories[0]
    assert memory.scheduled
    assert memory.source_origin is not None
    transition = module.resolved_transition
    assert transition is not None
    resource = next(item for item in transition.resources if item.kind is StateResourceKind.MEMORY)
    assert resource.semantic_id == memory.semantic_id
    assert resource.type == memory.element_type == UIntType(8)
    assert resource.depth == memory.depth == 4
    assert [item.kind for item in transition.group("fetch").actions] == [
        StateActionKind.REGISTER_WRITE,
        StateActionKind.MEMORY_READ_REQUEST,
    ]
    write = transition.group("store").actions[0]
    assert write.kind is StateActionKind.MEMORY_WRITE
    assert tuple(item.type for item in write.operands) == (UIntType(2), UIntType(8))
    assert write.source_origin is not None


@pytest.mark.parametrize(
    ("action", "message"),
    (
        ("table.read()", "read requires exactly one"),
        ("table.read(address,data)", "read requires exactly one"),
        ("table.write(address)", "write requires exactly address and data"),
        ("table.write(address,data,0,0)", "write requires exactly address and data"),
        ("table.read(data)", "read address has type u8, expected u2"),
        ("table.write(data,data)", "write address has type u8, expected u2"),
        ("table.write(address,address)", "write data has type u2, expected u8"),
    ),
)
def test_memory_action_arity_and_exact_types_are_diagnostics(
    action: str, message: str
) -> None:
    bad = source().replace("table.read(address)", action)
    with pytest.raises(SemanticError, match=message):
        analyze(parse(bad))


def test_memory_ownership_is_whole_resource_and_all_or_none() -> None:
    mixed = source().replace(
        "reg seen:bit=0",
        "table.read_address=address table.write_enable=wr "
        "table.write_address=address table.write_data=data reg seen:bit=0",
    )
    with pytest.raises(SemanticError, match="cannot mix global controls"):
        analyze(parse(mixed))

    partial = """module Partial { clock clk reset rst in address:u2 out q:u8
      memory table:mem<u8,4>{read_latency 1 collision read_first}
      table.read_address=address q=table.read_data }"""
    with pytest.raises(SemanticError, match="has no .* control assignment"):
        analyze(parse(partial))


def test_same_direction_conflicts_and_read_write_is_legal() -> None:
    module = analyze(parse(source()))
    assert not groups_conflict(
        module.resolved_transition.group("fetch"),
        module.resolved_transition.group("store"),
    )
    duplicate_writer = source().replace(
        "q=table.read_data",
        "rule store2 when wr { table.write(address,7) } q=table.read_data",
    )
    with pytest.raises(SemanticError, match="conflicting state actions"):
        analyze(parse(duplicate_writer))
    prioritized = duplicate_writer.replace(
        "q=table.read_data", "priority store > store2 q=table.read_data"
    )
    analyze(parse(prioritized))


def test_scheduled_memory_coexists_with_register_and_one_scheduled_fifo() -> None:
    combined = """module Combined { clock clk reset rst
      in op:u2 in address:u2 in data:u8 out q:u8 out count:u2
      memory table:mem<u8,4>{read_latency 1 collision read_first}
      fifo queue:fifo<u8,2> reg seen:u8=0
      rule fetch when op==1 { table.read(address) seen <- table.read_data }
      rule store when op==2 { table.write(address,data) queue.push(data) }
      rule drain when op==3 { queue.pop() }
      priority store > drain q=table.read_data count=queue.count }"""
    module = analyze(parse(combined))
    assert {item.kind for item in module.resolved_transition.resources} == {
        StateResourceKind.REGISTER,
        StateResourceKind.FIFO,
        StateResourceKind.MEMORY,
    }
    result = simulate_cycles(module, [
        {"op": 2, "address": 1, "data": 7},
        {"op": 1, "address": 1, "data": 0},
        {"op": 3, "address": 0, "data": 0},
    ])
    assert result[-1] == {"q": 7, "count": 1}


def test_snapshot_collision_hold_reset_and_atomic_register_effect() -> None:
    cycles = [
        {"rd": 0, "wr": 1, "address": 1, "data": 7},
        {"rd": 1, "wr": 0, "address": 1, "data": 0},
        {"rd": 0, "wr": 0, "address": 0, "data": 0},
        {"rd": 1, "wr": 1, "address": 1, "data": 9},
        {"rd": 0, "wr": 0, "address": 0, "data": 0},
    ]
    read_first = simulate_cycles(analyze(parse(source("read_first"))), cycles)
    write_first = simulate_cycles(analyze(parse(source("write_first"))), cycles)
    assert [item["q"] for item in read_first] == [0, 0, 7, 7, 7]
    assert [item["q"] for item in write_first] == [0, 0, 7, 7, 9]
    assert read_first[-1]["touched"] == write_first[-1]["touched"] == 1

    reset = simulate_cycles(
        analyze(parse(source())), cycles[:4], [False, False, True, False]
    )
    assert reset[2] == {"q": 0, "touched": 0}
    assert reset[3] == {"q": 0, "touched": 0}


def test_canonical_round_trip_and_malformed_link_rejection() -> None:
    module = analyze(parse(source()))
    canonical = lower(module)
    assert restore(canonical) == module
    assert lower(analyze(parse(source()))).memories == canonical.memories

    transition = canonical.resolved_transition
    assert transition is not None
    without_memory_resource = replace(
        transition,
        resources=tuple(
            item for item in transition.resources
            if item.kind is not StateResourceKind.MEMORY
        ),
    )
    with pytest.raises(CanonicalizationError, match="missing resource"):
        restore(replace(canonical, resolved_transition=without_memory_resource))

    memory_resource = next(
        item for item in transition.resources
        if item.kind is StateResourceKind.MEMORY
    )
    duplicate_resource = replace(
        transition,
        resources=(*transition.resources, memory_resource),
    )
    with pytest.raises(CanonicalizationError, match="duplicate state-resource"):
        restore(replace(canonical, resolved_transition=duplicate_resource))

    inconsistent_resource = replace(
        transition,
        resources=tuple(
            replace(item, depth=8) if item is memory_resource else item
            for item in transition.resources
        ),
    )
    with pytest.raises(CanonicalizationError, match="resource metadata disagrees"):
        restore(replace(canonical, resolved_transition=inconsistent_resource))

    partial_memory = replace(
        canonical.memories[0],
        read_address=canonical.assignments[0].expression,
    )
    with pytest.raises(CanonicalizationError, match="controls must be all present"):
        restore(replace(canonical, memories=(partial_memory,)))


def test_no_memory_formal_property_family_is_fabricated() -> None:
    result = compile_source(source())
    assert not any(
        (item.generated_from or "").startswith("memory:")
        for item in result.formal_design.properties
    )
