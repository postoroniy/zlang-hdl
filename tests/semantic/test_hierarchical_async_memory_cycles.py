from __future__ import annotations

from pathlib import Path

import pytest

from zlang.compiler import compile_source
from zlang.semantic import SemanticError


ROOT = Path(__file__).resolve().parents[2]


def _memory_child(*, latency: int, collision: str) -> str:
    return f"""
module AsyncMem {{
    clock clk reset rst
    in ra:u2 in we:bit in wa:u2 in wd:u8 out q:u8
    memory m:mem<u8,4> {{
        read_latency {latency}
        collision {collision}
        reset {{ contents preserve read_data preserve }}
    }}
    m.read_address=ra
    m.write_enable=we
    m.write_address=wa
    m.write_data=wd
    q=m.read_data
}}
"""


def test_latency_zero_child_dependency_cycles_are_rejected_exactly() -> None:
    self_address = _memory_child(latency=0, collision="read_first") + """
module Loop {
    clock clk reset rst out q:u8
    c:AsyncMem { ra=truncate<2>(c.q) we=0 wa=0 wd=0 }
    q=c.q
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: c\.q -> c\.q",
    ):
        compile_source(self_address, top="Loop", include_clash=False)

    two_children = _memory_child(latency=0, collision="read_first") + """
module PairLoop {
    clock clk reset rst out q:u8
    a:AsyncMem { ra=truncate<2>(b.q) we=0 wa=0 wd=0 }
    b:AsyncMem { ra=truncate<2>(a.q) we=0 wa=0 wd=0 }
    q=a.q
}
"""
    with pytest.raises(SemanticError, match="combinational child dependency cycle"):
        compile_source(two_children, top="PairLoop", include_clash=False)

    write_first = _memory_child(latency=0, collision="write_first") + """
module WriteLoop {
    clock clk reset rst out q:u8
    c:AsyncMem { ra=0 we=1 wa=0 wd=c.q }
    q=c.q
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: c\.q -> c\.q",
    ):
        compile_source(write_first, top="WriteLoop", include_clash=False)


def test_sequential_and_read_first_next_edge_feedback_remain_legal() -> None:
    read_first = _memory_child(latency=0, collision="read_first") + """
module ReadFirstFeedback {
    clock clk reset rst out q:u8
    c:AsyncMem { ra=0 we=1 wa=0 wd=c.q }
    q=c.q
}
"""
    compile_source(read_first, top="ReadFirstFeedback", include_clash=False)

    latency_one = _memory_child(latency=1, collision="write_first") + """
module RegisteredReadFeedback {
    clock clk reset rst out q:u8
    c:AsyncMem { ra=truncate<2>(c.q) we=0 wa=0 wd=c.q }
    q=c.q
}
"""
    compile_source(latency_one, top="RegisteredReadFeedback", include_clash=False)

    banked = (ROOT / "examples" / "ztpu_banked_memory.zhl").read_text()
    compile_source(banked, top="ZtpuBankedMemory", include_clash=False)


def test_latency_zero_dependencies_are_transitive_through_nested_children() -> None:
    source = _memory_child(latency=0, collision="read_first") + """
module Mid {
    clock clk reset rst in address:u2 out q:u8
    leaf:AsyncMem { ra=address we=0 wa=0 wd=0 }
    q=leaf.q
}
module NestedLoop {
    clock clk reset rst out q:u8
    mid:Mid { address=truncate<2>(mid.q) }
    q=mid.q
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: mid\.q -> mid\.q",
    ):
        compile_source(source, top="NestedLoop", include_clash=False)


def test_rule_driven_child_output_dependencies_are_not_hidden() -> None:
    source = """
module RuleEcho {
    clock clk reset rst in x:bit out y:bit
    rule emit when x { y <- x }
}
module RuleLoop {
    clock clk reset rst out y:bit
    child:RuleEcho { x=child.y }
    y=child.y
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: child\.y -> child\.y",
    ):
        compile_source(source, top="RuleLoop", include_clash=False)


def test_rule_output_dependencies_include_the_exact_resolved_schedule() -> None:
    conflicting = """
module ScheduledOutput {
    clock clk reset rst in x:bit out y:bit
    reg shared:bit=0
    rule high when x { shared <- 0 }
    rule emit when 1 { shared <- 1 y <- 1 }
    priority high > emit
}
module ScheduledLoop {
    clock clk reset rst out y:bit
    child:ScheduledOutput { x=child.y }
    y=child.y
}
"""
    with pytest.raises(
        SemanticError,
        match=r"combinational child dependency cycle: child\.y -> child\.y",
    ):
        compile_source(conflicting, top="ScheduledLoop", include_clash=False)

    unrelated = """
module IndependentOutput {
    clock clk reset rst in x:bit out y:bit
    reg left:bit=0 reg right:bit=0
    rule high when x { left <- 1 }
    rule emit when 1 { right <- 1 y <- 1 }
    priority high > emit
}
module IndependentFeedback {
    clock clk reset rst out y:bit
    child:IndependentOutput { x=child.y }
    y=child.y
}
"""
    compile_source(unrelated, top="IndependentFeedback", include_clash=False)


@pytest.mark.parametrize(
    ("signal", "push", "pop"),
    (
        ("ready", "1", "drive"),
        ("overflow", "drive", "0"),
        ("overflow", "1", "drive"),
        ("underflow", "0", "drive"),
    ),
)
def test_legacy_fifo_combinational_observation_dependencies_are_not_hidden(
    signal: str,
    push: str,
    pop: str,
) -> None:
    source = f"""
module LegacyFifoObservation {{
    clock clk reset rst in drive:bit out observed:bit
    fifo q:fifo<u8,1>
    q.data=0
    q.push={push}
    q.pop={pop}
    observed=q.{signal}
}}
module FifoLoop {{
    clock clk reset rst out ready:bit
    child:LegacyFifoObservation {{ drive=child.observed }}
    ready=child.observed
}}
"""
    with pytest.raises(
        SemanticError,
        match=(
            r"combinational child dependency cycle: "
            r"child\.observed -> child\.observed"
        ),
    ):
        compile_source(source, top="FifoLoop", include_clash=False)


def test_fifo_state_and_scheduled_observations_remain_cycle_cuts() -> None:
    legacy_state = """
module LegacyFifoValid {
    clock clk reset rst in pop:bit out valid:bit
    fifo q:fifo<u8,1>
    q.data=0
    q.push=1
    q.pop=pop
    valid=q.valid
}
module LegacyStateFeedback {
    clock clk reset rst out valid:bit
    child:LegacyFifoValid { pop=child.valid }
    valid=child.valid
}
"""
    compile_source(legacy_state, top="LegacyStateFeedback", include_clash=False)

    scheduled = """
module ScheduledFifoReady {
    clock clk reset rst in push:bit out ready:bit
    fifo q:fifo<u8,1>
    rule fill when push { q.push(0) }
    ready=q.ready
}
module ScheduledStateFeedback {
    clock clk reset rst out ready:bit
    child:ScheduledFifoReady { push=child.ready }
    ready=child.ready
}
"""
    compile_source(scheduled, top="ScheduledStateFeedback", include_clash=False)
