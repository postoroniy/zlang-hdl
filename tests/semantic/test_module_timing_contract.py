from __future__ import annotations

import pytest

from zlang.ir.timing import TimingKnowledge
from zlang.opt.lowering import lower
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.source import SourceOrigin, SourceSpan


def test_zero_latency_contract_records_known_combinational_output() -> None:
    module = analyze(
        parse(
            "module Pass { in x:u8 out y:u8 y=x "
            "timing { latency 0 ii 1 } }"
        )
    )

    assert module.timing_contract is not None
    assert module.timing_contract.latency == 0
    assert module.timing_contract.initiation_interval == 1
    assert module.timing_contract.clock_domain is None
    assert module.output_timings[0].port == "y"
    assert module.output_timings[0].timing.knowledge is TimingKnowledge.KNOWN
    assert module.output_timings[0].timing.latency == 0
    assert module.timing_contract.source_origin is not None
    assert module.timing_contract.source_origin.construct == "module timing contract Pass"


def test_timing_origin_and_canonical_ir_are_preserved_exactly() -> None:
    source = "module Pass { in x:u8 out y:u8 y=x timing { latency 0 ii 1 } }"
    digest = "a" * 64

    module = analyze(
        parse(source),
        source_unit="project/pass.zl",
        source_digest=digest,
    )

    expected_origin = SourceOrigin(
        SourceSpan(1, 36, 1, 61),
        "module timing contract Pass",
        "project/pass.zl",
        digest,
    )
    assert module.timing_contract is not None
    assert module.timing_contract.source_origin == expected_origin
    canonical = lower(module)
    assert canonical.timing_contract == module.timing_contract
    assert canonical.output_timings == module.output_timings
    assert canonical.instance_output_timings == module.instance_output_timings


def test_timing_mismatch_keeps_exact_structured_diagnostic() -> None:
    source = (
        "module Bad { clock clk reset rst in x:u8 out y:u8 "
        "y=pipeline(1) { x } timing { latency 2 ii 1 } }"
    )

    with pytest.raises(SemanticError) as raised:
        analyze(parse(source))

    assert str(raised.value) == (
        "output 'y' has exact latency 1, but module timing contract declares "
        "latency 2"
    )
    assert raised.value.code == "ZL-TIMING-CONTRACT"
    assert raised.value.primary is None
    assert raised.value.notes == ()
    assert raised.value.fixes == (
        "make the declared and derived exact latencies equal",
    )


def test_uncontracted_legacy_module_does_not_publish_inferred_public_timing() -> None:
    module = analyze(parse("module Legacy { in x:u8 out y:u8 y=x }"))

    assert module.timing_contract is None
    assert module.output_timings == ()
    assert module.instance_output_timings == ()


def test_fixed_delay_adds_exact_contract_latency() -> None:
    module = analyze(
        parse(
            "module Delayed { clock c reset r in x:u8 out y:u8 "
            "y=delay<2>(x) timing { latency 2 ii 1 } }"
        )
    )

    assert module.output_timings[0].timing.latency == 2


def test_nested_conversions_and_staged_locals_retain_exact_latency() -> None:
    module = analyze(
        parse(
            """
            module Staged {
                clock clk
                reset rst
                in x:u8
                out y:u16
                staged:u8 = pipeline(3) { x }
                widened:u16 = extend<16>(staged)
                y = unpack<u16>(pack(widened))
                timing { latency 3 ii 1 }
            }
            """
        )
    )

    assert module.output_timings[0].timing.knowledge is TimingKnowledge.KNOWN
    assert module.output_timings[0].timing.latency == 3


def test_timeless_outputs_satisfy_declared_latency_without_becoming_known() -> None:
    module = analyze(
        parse(
            """
            module Constants {
                clock clk
                reset rst
                out a:u8
                out b:u8
                a = 1
                b = 2
                timing { latency 7 ii 1 }
            }
            """
        )
    )

    assert [item.port for item in module.output_timings] == ["a", "b"]
    assert all(
        item.timing.knowledge is TimingKnowledge.TIMELESS
        for item in module.output_timings
    )


def test_multiple_outputs_must_each_match_the_exact_contract() -> None:
    source = """
        module Mixed {
            clock clk
            reset rst
            in x:u8
            out a:u8
            out b:u8
            a = pipeline(2) { x }
            b = pipeline(1) { x }
            timing { latency 2 ii 1 }
        }
    """
    with pytest.raises(SemanticError, match="output 'b'.*exact latency 1.*latency 2"):
        analyze(parse(source))


def test_state_dependent_output_is_unknown_not_same_cycle() -> None:
    source = """
        module Stateful {
            clock clk
            reset rst
            out y:u8
            reg value:u8=0
            y=value
            timing { latency 0 ii 1 }
        }
    """
    with pytest.raises(SemanticError, match="unknown timing: register 'value' is state-dependent"):
        analyze(parse(source))


@pytest.mark.parametrize(
    ("source", "reason"),
    (
        (
            """
            module FifoState {
                clock clk reset rst
                in data:u8 out y:u8
                fifo queue:fifo<u8,2>
                queue.data=data queue.push=0 queue.pop=0
                y=queue.front
                timing { latency 0 ii 1 }
            }
            """,
            "FIFO 'queue' is state-dependent",
        ),
        (
            """
            module MemoryState {
                clock clk reset rst
                in address:u1 in data:u8 in write:bit out y:u8
                memory table:mem<u8,2> {
                    read_latency 1 collision read_first
                }
                table.read_address=address
                table.write_enable=write
                table.write_address=address
                table.write_data=data
                y=table.read_data
                timing { latency 1 ii 1 }
            }
            """,
            "memory 'table' is state-dependent",
        ),
        (
            """
            module RomState {
                clock clk reset rst
                in address:u1 out y:u8
                rom table:rom<u8,2> {
                    read_latency 1
                    init generate(i in 0..2) i
                }
                table.read_address=address
                y=table.read_data
                timing { latency 1 ii 1 }
            }
            """,
            "ROM 'table' is state-dependent",
        ),
    ),
)
def test_storage_values_are_unknown_for_public_module_timing(
    source: str,
    reason: str,
) -> None:
    with pytest.raises(SemanticError, match=reason):
        analyze(parse(source))


@pytest.mark.parametrize(
    ("source", "diagnostic"),
    (
        (
            "module Bad { in x:u8 out y:u8 y=x timing { latency 0 ii 2 } }",
            "require ii 1",
        ),
        (
            "module Bad { in x:u8 out y:u8 y=x timing { latency 1 ii 1 } }",
            "positive module timing latency requires exactly one clock/reset domain",
        ),
        (
            """
            module Bad {
                clock a reset ar @a
                clock b reset br @b
                in x:u8 @a
                out y:u8 @a
                y=x
                timing { latency 0 ii 1 }
            }
            """,
            "at most one clock/reset domain",
        ),
        (
            """
            module Bad {
                in rx:rv<u8>
                out tx:rv<u8>
                tx.payload=rx.payload
                tx.valid=rx.valid
                rx.ready=tx.ready
                timing { latency 0 ii 1 }
            }
            """,
            "only scalar wire ports, not protocols",
        ),
        (
            """
            module Bad {
                in x:vec<2,u8>
                out y:vec<2,u8>
                y=x
                timing { latency 0 ii 1 }
            }
            """,
            "output 'y' must be a scalar wire value",
        ),
    ),
)
def test_contract_scope_errors_are_fail_closed(source: str, diagnostic: str) -> None:
    with pytest.raises(SemanticError, match=diagnostic):
        analyze(parse(source))


def test_child_contract_adds_bound_input_latency_at_instance_boundary() -> None:
    module = analyze(
        parse(
            """
            module Child {
                clock clk reset rst
                in x:u8
                out y:u8
                y=pipeline(2) { x }
                timing { latency 2 ii 1 }
            }
            module Parent {
                clock clk reset rst
                in x:u8
                out y:u8
                inst child:Child
                child.x=pipeline(3) { x }
                y=child.y
                timing { latency 5 ii 1 }
            }
            """
        )
    )

    assert module.output_timings[0].timing.latency == 5
    assert len(module.instance_output_timings) == 1
    child_timing = module.instance_output_timings[0]
    assert (child_timing.instance, child_timing.port) == ("child", "y")
    assert child_timing.timing.knowledge is TimingKnowledge.KNOWN
    assert child_timing.timing.latency == 5


def test_unaligned_child_bindings_are_rejected_without_alignment_insertion() -> None:
    source = """
        module Child {
            clock clk reset rst
            in a:u8 in b:u8
            out y:u9
            y=a+b
            timing { latency 0 ii 1 }
        }
        module Parent {
            clock clk reset rst
            in x:u8
            out y:u9
            inst child:Child
            child.a=x
            child.b=pipeline(1) { x }
            y=child.y
            timing { latency 1 ii 1 }
        }
    """
    with pytest.raises(
        SemanticError,
        match="latency mismatch in inputs bound to instance 'child'.*0, 1",
    ):
        analyze(parse(source))


def test_current_parent_input_cannot_join_delayed_child_output() -> None:
    source = """
        module Child {
            clock clk reset rst
            in x:u8 out y:u8
            y=pipeline(2) { x }
            timing { latency 2 ii 1 }
        }
        module Parent {
            clock clk reset rst
            in x:u8 out y:u9
            inst child:Child
            child.x=x
            y=child.y+x
            timing { latency 2 ii 1 }
        }
    """
    with pytest.raises(SemanticError, match=r"latency mismatch in \+:.*0, 2"):
        analyze(parse(source))


def test_uncontracted_child_is_unknown_at_hierarchy_boundary() -> None:
    source = """
        module Child { in x:u8 out y:u8 y=x }
        module Parent {
            in x:u8 out y:u8
            inst child:Child
            child.x=x
            y=child.y
            timing { latency 0 ii 1 }
        }
    """
    with pytest.raises(
        SemanticError,
        match="unknown timing: child module 'Child' has no timing contract",
    ):
        analyze(parse(source))


def test_recursive_module_hierarchy_is_rejected_before_python_recursion() -> None:
    source = """
        module A {
            out y:u8
            inst b:B
            y=b.y
            timing { latency 0 ii 1 }
        }
        module B {
            out y:u8
            inst a:A
            y=a.y
            timing { latency 0 ii 1 }
        }
        module Top {
            out y:u8
            inst a:A
            y=a.y
            timing { latency 0 ii 1 }
        }
    """
    with pytest.raises(
        SemanticError,
        match="cyclic module hierarchy is not allowed: Top -> A -> B -> A",
    ):
        analyze(parse(source))
