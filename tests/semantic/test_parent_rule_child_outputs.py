from __future__ import annotations

import pytest

from zlang.ir import expressions as expr
from zlang.ir.state import StateActionKind
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate_cycles


SOURCE = """
module Child<W=8> {
    in x : uint<W>
    out valid : bit
    out value : uint<W>
    valid = x != 0
    value = x
}

module Parent {
    clock clk
    reset rst
    in x : u8
    in address : u2
    out captured : u8
    out read_data : u8

    reg held : u8 = 0
    fifo queue : fifo<u8,2>
    memory table : mem<u8,4> {
        read_latency 1
        collision write_first
    }

    inst block : Child<8> { x }

    rule capture when block.valid {
        held <- block.value
        queue.push(block.value)
        table.write(address, block.value)
    }
    rule fetch when block.valid == 0 {
        table.read(address)
    }
    rule retire when queue.valid {
        queue.pop()
    }
    priority capture > retire

    captured = held
    read_data = table.read_data
}
"""


def test_child_outputs_are_typed_before_parent_rules_and_state_actions() -> None:
    module = analyze(parse(SOURCE))
    capture = next(item for item in module.rules if item.name == "capture")
    assert isinstance(capture.guard, expr.InstanceOutputRef)
    assert (capture.guard.instance, capture.guard.port) == ("block", "valid")
    assert isinstance(capture.actions[0].expression, expr.InstanceOutputRef)
    assert capture.actions[0].expression.port == "value"

    group = module.resolved_transition.group("capture")
    fifo = next(
        item for item in group.actions if item.kind is StateActionKind.FIFO_PUSH
    )
    memory = next(
        item for item in group.actions if item.kind is StateActionKind.MEMORY_WRITE
    )
    assert isinstance(fifo.operands[0], expr.InstanceOutputRef)
    assert isinstance(memory.operands[1], expr.InstanceOutputRef)
    assert fifo.operands[0].origin is not None
    assert memory.operands[1].origin is not None


def test_specialized_child_output_identity_round_trips_canonical_ir() -> None:
    module = analyze(parse(SOURCE))
    restored = restore(lower(module))
    assert restored == module
    assert restored.rules[0].guard == module.rules[0].guard
    assert restored.elaborated_instances[0].specialization_identity
    assert restored.elaborated_instances[0].instance_identity


@pytest.mark.parametrize(
    ("guard", "diagnostic"),
    (
        ("block.x", "has no field 'x'"),
        ("block.missing", "has no field 'missing'"),
        ("block.value", "guard for rule 'capture' must be bit"),
    ),
)
def test_child_inputs_unknown_outputs_and_wrong_guard_types_are_rejected(
    guard: str, diagnostic: str
) -> None:
    bad = SOURCE.replace("block.valid {", f"{guard} {{", 1)
    with pytest.raises(SemanticError, match=diagnostic):
        analyze(parse(bad))


def test_combinational_child_dependency_cycle_is_rejected() -> None:
    source = """
    module Echo { in x:u8 out y:u8 y=x }
    module Cycle { out y:u8 inst echo:Echo echo.x=echo.y y=echo.y }
    """
    with pytest.raises(SemanticError, match="combinational child dependency cycle"):
        analyze(parse(source))


def test_persistent_hierarchical_simulator_uses_pre_edge_child_output() -> None:
    source = """
    module CounterChild {
        out valid:bit out value:u8
        reg count:u8=0
        rule increment when 1 { count <- truncate<8>(count + 1) }
        valid=count != 0 value=count
    }
    module CaptureParent {
        clock clk reset rst out observed:u8
        reg held:u8=0
        inst counter:CounterChild
        rule capture when counter.valid { held <- counter.value }
        observed=held
    }
    """
    module = analyze(parse(source))
    results = simulate_cycles(
        module,
        [{}, {}, {}, {}, {}],
        [True, False, False, False, True],
    )
    assert [item["observed"] for item in results] == [0, 0, 0, 1, 0]
