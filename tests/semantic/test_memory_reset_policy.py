from dataclasses import replace

import pytest

from zlang.compiler import compile_source
from zlang.ir.storage import MemoryResetPolicy
from zlang.opt import canonical_ir_identity
from zlang.opt.lowering import CanonicalizationError, lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze
from zlang.simulate import simulate_cycles
from zlang.targets import TargetArchitectureError


def source(
    *,
    latency: int = 0,
    contents: str = "preserve",
    read_data: str = "preserve",
) -> str:
    return f"""
module MemoryPolicy {{
  clock clk reset rst
  in address:u2 in write_enable:bit in data:u8
  out q:u8
  memory table:mem<u8,4> {{
    read_latency {latency}
    collision read_first
    reset {{
      contents {contents}
      read_data {read_data}
    }}
  }}
  table.read_address=address
  table.write_enable=write_enable
  table.write_address=address
  table.write_data=data
  q=table.read_data
}}
"""


@pytest.mark.parametrize(
    ("contents", "read_data"),
    (
        (MemoryResetPolicy.CLEAR, MemoryResetPolicy.CLEAR),
        (MemoryResetPolicy.CLEAR, MemoryResetPolicy.PRESERVE),
        (MemoryResetPolicy.PRESERVE, MemoryResetPolicy.CLEAR),
        (MemoryResetPolicy.PRESERVE, MemoryResetPolicy.PRESERVE),
    ),
)
def test_global_memory_reset_policies_reach_typed_and_canonical_ir(
    contents: MemoryResetPolicy,
    read_data: MemoryResetPolicy,
) -> None:
    module = analyze(parse(source(contents=contents.value, read_data=read_data.value)))
    memory = module.memories[0]
    assert memory.read_latency == 0
    assert memory.contents_reset is contents
    assert memory.read_data_reset is read_data

    canonical = lower(module)
    canonical_memory = canonical.memories[0]
    assert canonical_memory.contents_reset is contents
    assert canonical_memory.read_data_reset is read_data
    assert restore(canonical) == module


def test_absent_reset_block_is_exactly_explicit_clear_clear() -> None:
    explicit = source(latency=1, contents="clear", read_data="clear")
    implicit = explicit.replace(
        "reset {\n      contents clear\n      read_data clear\n    }", ""
    )
    explicit_ir = analyze(parse(explicit))
    implicit_ir = analyze(parse(implicit))
    assert implicit_ir.memories[0].contents_reset is MemoryResetPolicy.CLEAR
    assert implicit_ir.memories[0].read_data_reset is MemoryResetPolicy.CLEAR
    assert canonical_ir_identity(lower(explicit_ir)) == canonical_ir_identity(
        lower(implicit_ir)
    )


def test_latency_and_reset_policy_participate_in_canonical_identity() -> None:
    baseline = lower(analyze(parse(source(latency=1))))
    changed_latency = lower(analyze(parse(source(latency=0))))
    changed_contents = lower(
        analyze(parse(source(latency=1, contents="clear")))
    )
    changed_read_data = lower(
        analyze(parse(source(latency=1, read_data="clear")))
    )
    identities = {
        canonical_ir_identity(item)
        for item in (
            baseline, changed_latency, changed_contents, changed_read_data
        )
    }
    assert len(identities) == 4


@pytest.mark.parametrize("latency", (-1, 2, 3))
def test_memory_read_latency_is_exactly_zero_or_one(latency: int) -> None:
    # Negative spelling is not part of the storage syntax; exercise positive
    # malformed values semantically and retain the parser rejection for -1.
    if latency < 0:
        from zlang.parser import ParseError

        with pytest.raises(ParseError):
            parse(source(latency=latency))
        return
    with pytest.raises(
        SemanticError,
        match="memory 'table' read_latency must be 0 or 1",
    ):
        analyze(parse(source(latency=latency)))


def test_rule_owned_zero_latency_memory_is_rejected_precisely() -> None:
    scheduled = """
module ScheduledZero {
  clock clk reset rst
  in address:u2 in read_enable:bit
  out q:u8
  memory table:mem<u8,4> {
    read_latency 0
    collision read_first
    reset { contents preserve read_data preserve }
  }
  read: when read_enable { table.read(address) }
  q=table.read_data
}
"""
    with pytest.raises(
        SemanticError,
        match=(
            "rule-owned memory 'table' requires read_latency 1; "
            "read_latency 0 is supported only for globally controlled memories"
        ),
    ):
        analyze(parse(scheduled))


def test_canonical_memory_rejects_malformed_latency_policy_and_scheduled_profile() -> None:
    canonical = lower(analyze(parse(source(latency=1))))
    memory = canonical.memories[0]

    with pytest.raises(CanonicalizationError, match="read latency must be zero or one"):
        restore(replace(canonical, memories=(replace(memory, read_latency=2),)))
    with pytest.raises(CanonicalizationError, match="contents reset policy is invalid"):
        restore(
            replace(
                canonical,
                memories=(replace(memory, contents_reset="invalid"),),
            )
        )
    with pytest.raises(CanonicalizationError, match="read data reset policy is invalid"):
        restore(
            replace(
                canonical,
                memories=(replace(memory, read_data_reset="invalid"),),
            )
        )

    scheduled_source = """
module Scheduled {
  clock clk reset rst
  in address:u2 in enable:bit out q:u8
  memory table:mem<u8,4>{read_latency 1 collision read_first}
  read: when enable { table.read(address) }
  q=table.read_data
}
"""
    scheduled = lower(analyze(parse(scheduled_source)))
    with pytest.raises(
        CanonicalizationError,
        match="scheduled memory requires read latency one",
    ):
        restore(
            replace(
                scheduled,
                memories=(replace(scheduled.memories[0], read_latency=0),),
            )
        )


def test_registered_cell_and_read_result_reset_policies_are_independent() -> None:
    cycles = [
        {"address": 1, "write_enable": 0, "data": 0},
        {"address": 1, "write_enable": 1, "data": 0x5A},
        {"address": 1, "write_enable": 0, "data": 0},
        {"address": 1, "write_enable": 0, "data": 0},
        {"address": 1, "write_enable": 0, "data": 0},
        {"address": 1, "write_enable": 0, "data": 0},
        {"address": 1, "write_enable": 0, "data": 0},
    ]
    expected = {
        ("clear", "clear"): [0, 0, 0, 0x5A, 0, 0, 0],
        ("clear", "preserve"): [0, 0, 0, 0x5A, 0x5A, 0x5A, 0],
        ("preserve", "clear"): [0, 0, 0, 0x5A, 0, 0, 0x5A],
        ("preserve", "preserve"): [0, 0, 0, 0x5A, 0x5A, 0x5A, 0x5A],
    }
    for policies, trace in expected.items():
        contents, read_data = policies
        module = analyze(parse(source(
            latency=1,
            contents=contents,
            read_data=read_data,
        )))
        assert [
            item["q"]
            for item in simulate_cycles(
                module,
                cycles,
                reset=[True, False, False, False, True, False, False],
            )
        ] == trace


@pytest.mark.parametrize(
    ("latency", "contents", "read_data", "diagnostic"),
    (
        (0, "clear", "clear", "one-cycle synchronous reads"),
        (1, "preserve", "clear", "does not advertise reset-preserved"),
        (1, "clear", "preserve", "does not advertise reset-preserved"),
    ),
)
def test_target_memory_mapping_rejects_unadvertised_profiles(
    latency: int,
    contents: str,
    read_data: str,
    diagnostic: str,
) -> None:
    with pytest.raises(TargetArchitectureError, match=diagnostic):
        compile_source(
            source(
                latency=latency,
                contents=contents,
                read_data=read_data,
            ),
            target="xc7z030ffg676-1",
            architecture="Xilinx7BRAM36SimpleDualPort",
            architecture_mode="required",
        )


def test_zero_latency_memory_rejects_combinational_feedback_before_emission() -> None:
    def feedback(collision: str, read_address: str) -> str:
        return f"""
module MemoryFeedback {{
  clock clk reset rst
  in address:u2 in write_enable:bit out q:u8
  memory table:mem<u8,4> {{
    read_latency 0
    collision {collision}
    reset {{ contents preserve read_data preserve }}
  }}
  table.read_address={read_address}
  table.write_enable=write_enable
  table.write_address=address
  table.write_data=truncate<8>(table.read_data + 1)
  q=table.read_data
}}
"""

    with pytest.raises(
        SemanticError,
        match=(
            "combinational memory dependency cycle: "
            "table.read_data -> table.read_data"
        ),
    ):
        analyze(parse(feedback("write_first", "address")))

    with pytest.raises(
        SemanticError,
        match="combinational memory dependency cycle",
    ):
        analyze(parse(feedback("read_first", "truncate<2>(table.read_data)")))

    # Under read-first semantics the write word affects only next-edge cell
    # state, so this feedback is an ordinary synchronous recurrence.
    module = analyze(parse(feedback("read_first", "address")))
    assert module.memories[0].read_latency == 0
