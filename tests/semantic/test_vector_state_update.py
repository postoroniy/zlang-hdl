from dataclasses import replace

import pytest

from zlang.ir import expressions as expr
from zlang.opt.ir import ExpressionOp
from zlang.opt.lowering import CanonicalizationError, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate_cycles


SOURCE = """
module VectorState {
  clock clk reset rst
  in fire:bit in index:u2 in value:u8
  out selected:u8 out previous:u8
  reg samples:vec<4,u8>=generate(i in 0..4) 0
  reg seen:u8=0
  rule write when fire {
    samples[index] <- value
    seen <- samples[index]
  }
  selected=samples[index]
  previous=seen
}
"""


STRUCT_INDEX_SOURCE = """
struct Cursor { tag:u8 }

module StructIndexedState {
  clock clk reset rst
  in fire:bit in next_tag:u8 in value:u16
  out selected:u16
  reg state:Cursor=Cursor { tag=0 }
  reg counts:vec<256,u16>=generate(i in 0..256) 0
  when fire {
    counts[state.tag] <- value
    state <- state with { tag=next_tag }
  }
  selected=counts[state.tag]
}
"""


def _compile(source: str = SOURCE):
    return analyze(parse(source))


def test_indexed_write_lowers_to_one_whole_register_vector_update() -> None:
    module = _compile()
    action = module.rules[0].actions[0]
    assert action.target is module.registers[0]
    assert isinstance(action.expression, expr.VectorUpdate)
    update = action.expression
    assert update.type == module.registers[0].type
    assert update.value.type == module.registers[0].type.element_type
    assert update.vector_length == 4
    assert (update.index_range.minimum, update.index_range.maximum) == (0, 3)
    assert isinstance(update.expression, expr.RegisterRef)
    assert update.expression.name == "samples"
    assert update.origin is not None
    assert update.origin.construct == "vector update samples"

    transition = module.resolved_transition
    assert transition is not None
    state_action = transition.group("write").actions[0]
    assert isinstance(state_action.operands[0], expr.VectorUpdate)


def test_indexed_write_reads_and_updates_the_same_pre_edge_snapshot() -> None:
    result = simulate_cycles(
        _compile(),
        (
            {"fire": 0, "index": 0, "value": 0},
            {"fire": 1, "index": 2, "value": 11},
            {"fire": 0, "index": 2, "value": 0},
            {"fire": 1, "index": 2, "value": 17},
            {"fire": 0, "index": 2, "value": 0},
            {"fire": 1, "index": 1, "value": 9},
            {"fire": 0, "index": 2, "value": 0},
        ),
        (True, False, False, False, False, False, False),
    )
    assert result == [
        {"selected": 0, "previous": 0},
        {"selected": 0, "previous": 0},
        {"selected": 11, "previous": 0},
        {"selected": 11, "previous": 0},
        {"selected": 17, "previous": 11},
        {"selected": 0, "previous": 11},
        {"selected": 17, "previous": 0},
    ]


def test_registered_struct_field_retains_range_for_read_and_vector_update() -> None:
    module = _compile(STRUCT_INDEX_SOURCE)
    update = module.rules[0].actions[0].expression
    selected = module.assignments[0].expression

    assert isinstance(update, expr.VectorUpdate)
    assert isinstance(update.index, expr.FieldAccess)
    assert update.index_range == expr.ValueRange(0, 255, "static_type")
    assert isinstance(selected, expr.RuntimeIndex)
    assert isinstance(selected.index, expr.FieldAccess)
    assert selected.index_range == expr.ValueRange(0, 255, "static_type")
    assert restore(lower(module)) == module

    outputs = simulate_cycles(
        module,
        (
            {"fire": 0, "next_tag": 0, "value": 0},
            {"fire": 1, "next_tag": 2, "value": 11},
            {"fire": 0, "next_tag": 0, "value": 0},
            {"fire": 1, "next_tag": 5, "value": 22},
            {"fire": 0, "next_tag": 0, "value": 0},
            {"fire": 1, "next_tag": 2, "value": 33},
            {"fire": 0, "next_tag": 0, "value": 0},
        ),
        (True, False, False, False, False, False, False),
    )
    assert [item["selected"] for item in outputs] == [0, 0, 0, 0, 0, 0, 22]


def test_struct_with_update_field_preserves_a_tighter_constant_range() -> None:
    module = _compile(
        "struct Cursor { tag:u8 } "
        "module ConstantStructIndex { "
        "in values:vec<4,u8> in cursor:Cursor out y:u8 "
        "updated=cursor with { tag=2 } y=values[updated.tag] }"
    )
    selected = module.assignments[0].expression
    assert isinstance(selected, expr.RuntimeIndex)
    assert selected.index_range == expr.ValueRange(2, 2, "constant")


def test_registered_struct_field_still_rejects_a_genuinely_too_wide_index() -> None:
    write_source = STRUCT_INDEX_SOURCE.replace("tag:u8", "tag:u9").replace(
        "next_tag:u8", "next_tag:u9"
    )
    with pytest.raises(
        SemanticError,
        match=r"index range 0\.\.511.*vector length 256",
    ):
        _compile(write_source)

    read_source = (
        "struct Cursor { tag:u9 } "
        "module BadStructIndexRead { clock clk reset rst "
        "in values:vec<256,u16> out y:u16 "
        "reg state:Cursor=Cursor { tag=0 } y=values[state.tag] }"
    )
    with pytest.raises(
        SemanticError,
        match=r"runtime index range 0\.\.511.*vector length 256",
    ):
        _compile(read_source)


@pytest.mark.parametrize(
    ("declaration", "actions", "message"),
    (
        ("reg samples:vec<3,u8>=generate(i in 0..3) 0", "samples[index] <- value", "range 0..3.*vector length 3"),
        ("reg samples:vec<4,u8>=generate(i in 0..4) 0", "samples[sindex] <- value", "must be an unsigned integral"),
        ("reg samples:u8=0", "samples[index] <- value", "one-dimensional vector register"),
        ("reg samples:vec<4,u8>=generate(i in 0..4) 0", "samples[index] <- value samples[0] <- value", "writes register 'samples' twice"),
        ("reg samples:vec<4,u8>=generate(i in 0..4) 0", "samples[index] <- value samples <- generate(i in 0..4) value", "writes register 'samples' twice"),
    ),
)
def test_invalid_indexed_writes_are_rejected(
    declaration: str,
    actions: str,
    message: str,
) -> None:
    source = f"""
      module Bad {{
        clock clk reset rst
        in fire:bit in index:u2 in sindex:s2 in value:u8 out y:u8
        {declaration}
        rule write when fire {{ {actions} }}
        y=0
      }}
    """
    with pytest.raises(SemanticError, match=message):
        _compile(source)


def test_indexed_write_conflicts_with_global_next_state_assignment() -> None:
    source = """
      module Bad {
        clock clk reset rst in fire:bit in index:u2 in value:u8 out y:u8
        reg samples:vec<4,u8>=generate(i in 0..4) 0
        samples <- samples
        rule write when fire { samples[index] <- value }
        y=0
      }
    """
    with pytest.raises(SemanticError, match="both a rule action and next-state"):
        _compile(source)


def test_constant_element_update_and_cross_rule_conflicts_use_register_semantics() -> None:
    static = _compile("""
      module StaticWrite {
        clock clk reset rst in fire:bit in value:u8 out y:u8
        reg samples:vec<4,u8>=generate(i in 0..4) 0
        rule write when fire { samples[2] <- value }
        y=samples[2]
      }
    """)
    update = static.rules[0].actions[0].expression
    assert isinstance(update, expr.VectorUpdate)
    assert (update.index_range.minimum, update.index_range.maximum) == (2, 2)

    conflict = """
      module Conflict {
        clock clk reset rst in a:bit in b:bit in i:u2 in value:u8 out y:u8
        reg samples:vec<4,u8>=generate(k in 0..4) 0
        rule left when a { samples[i] <- value }
        rule right when b { samples[0] <- value }
        y=samples[0]
      }
    """
    with pytest.raises(SemanticError, match="add explicit priority"):
        _compile(conflict)


def test_rule_guard_refines_runtime_vector_index_only_inside_that_rule() -> None:
    module = _compile("""
      module GuardedIndex {
        clock clk reset rst
        in fire:bit in index:u3 in value:u8 out y:u8
        reg samples:vec<5,u8>=generate(i in 0..5) 0
        rule write when (index < 5) & fire { samples[index] <- value }
        y=samples[0]
      }
    """)
    update = module.rules[0].actions[0].expression
    assert isinstance(update, expr.VectorUpdate)
    assert (update.index_range.minimum, update.index_range.maximum) == (0, 4)
    assert update.index_range.provenance == "rule_guard"

    with pytest.raises(SemanticError, match=r"range 0\.\.5.*vector length 5"):
        _compile("""
          module InsufficientGuard {
            clock clk reset rst
            in fire:bit in index:u3 in value:u8 out y:u8
            reg samples:vec<5,u8>=generate(i in 0..5) 0
            rule write when (index <= 5) & fire { samples[index] <- value }
            y=samples[0]
          }
        """)

    with pytest.raises(SemanticError, match="required 0..4"):
        _compile("""
          module NoEscapingFact {
            clock clk reset rst
            in fire:bit in index:u3 in value:u8 out y:u8
            reg samples:vec<5,u8>=generate(i in 0..5) 0
            rule hold when (index < 5) & fire { samples[0] <- value }
            y=samples[index]
          }
        """)


def test_storage_rejection_suppresses_the_entire_vector_update_action_group() -> None:
    module = _compile("""
      module AtomicVectorWrite {
        clock clk reset rst in op:u2 in index:u2 in value:u8
        out selected:u8 out count:u1
        reg samples:vec<4,u8>=generate(i in 0..4) 0
        fifo q:fifo<u8,1>
        rule insert when op==1 { q.push(value) samples[index] <- value }
        rule remove when op==2 { q.pop() }
        selected=samples[index]
        count=truncate<1>(q.count)
      }
    """)
    result = simulate_cycles(
        module,
        (
            {"op": 0, "index": 0, "value": 0},
            {"op": 1, "index": 0, "value": 5},
            {"op": 1, "index": 1, "value": 9},
            {"op": 0, "index": 0, "value": 0},
        ),
        (True, False, False, False),
    )
    assert result == [
        {"selected": 0, "count": 0},
        {"selected": 0, "count": 0},
        {"selected": 0, "count": 1},
        {"selected": 5, "count": 1},
    ]


def test_vector_update_canonical_round_trip_and_malformed_metadata_rejection() -> None:
    module = _compile()
    canonical = lower(module)
    restored = restore(canonical)
    assert restored == module

    update_node = next(
        node for node in canonical.expressions
        if node.op is ExpressionOp.VECTOR_UPDATE
    )
    attributes = dict(update_node.attributes)
    attributes["vector_length"] = 5
    malformed_node = replace(
        update_node,
        attributes=tuple(sorted(attributes.items(), key=lambda item: item[0])),
    )
    malformed = replace(
        canonical,
        expressions=tuple(
            malformed_node if node.id == update_node.id else node
            for node in canonical.expressions
        ),
    )
    with pytest.raises(CanonicalizationError, match="invalid range metadata"):
        restore(malformed)
