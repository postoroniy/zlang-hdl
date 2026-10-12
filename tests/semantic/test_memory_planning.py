from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.memory_planning import (
    MemoryImplementationKind,
    MemoryPlanningError,
    MemoryTargetPolicy,
    plan_memory_implementation,
    render_memory_implementation_report,
)


def _ported_memory(declarations: str, controls: str, *, depth: int = 16):
    address_width = max(1, (depth - 1).bit_length())
    source = f"""
module Planned {{
  clock clk reset rst
  in re0:bit in re1:bit in re2:bit
  in we0:bit in we1:bit
  in a0:uint<{address_width}> in a1:uint<{address_width}>
  in a2:uint<{address_width}> in a3:uint<{address_width}>
  in d0:u8 in d1:u8
  out q0:u8 out q1:u8 out q2:u8
  memory table:mem<u8,{depth}> {{
    {declarations}
    read_latency 1 collision old
    {"write_priority w0 > w1" if "write_port w1" in declarations else ""}
  }}
  {controls}
}}
"""
    return compile_source(source).ir.memories[0]


def test_three_read_one_write_selects_coherent_replication() -> None:
    memory = _ported_memory(
        "read_port r0 read_port r1 read_port r2 write_port w0",
        """
        table.r0.address=a0 table.r1.address=a1 table.r2.address=a2
        table.w0.address=a3 table.w0.enable=we0 table.w0.data=d0
        q0=table.r0.data q1=table.r1.data q2=table.r2.data
        """,
    )
    plan = plan_memory_implementation(memory)
    assert plan.implementation is MemoryImplementationKind.REPLICATED_1R1W
    assert plan.logical_shape == "3R1W"
    assert plan.physical_copies == 3
    assert plan.storage_bits == 3 * 16 * 8
    assert "cost_source=structural_estimate" in render_memory_implementation_report(plan)


def test_bounded_two_read_two_write_uses_register_mux_fallback() -> None:
    memory = _ported_memory(
        "read_port r0 read_port r1 write_port w0 write_port w1",
        """
        table.r0.address=a0 table.r1.address=a1
        table.w0.address=a2 table.w0.enable=we0 table.w0.data=d0
        table.w1.address=a3 table.w1.enable=we1 table.w1.data=d1
        q0=table.r0.data q1=table.r1.data q2=0
        """,
    )
    plan = plan_memory_implementation(memory)
    assert plan.implementation is MemoryImplementationKind.REGISTER_ARRAY
    assert plan.priority_gates == 1


def test_oversized_multiwrite_and_required_target_fail_closed() -> None:
    memory = _ported_memory(
        "read_port r0 read_port r1 write_port w0 write_port w1",
        """
        table.r0.address=a0 table.r1.address=a1
        table.w0.address=a2 table.w0.enable=we0 table.w0.data=d0
        table.w1.address=a3 table.w1.enable=we1 table.w1.data=d1
        q0=table.r0.data q1=table.r1.data q2=0
        """,
        depth=1024,
    )
    with pytest.raises(MemoryPlanningError, match="maximum is 4096"):
        plan_memory_implementation(memory)
    with pytest.raises(MemoryPlanningError, match="required target policy"):
        plan_memory_implementation(
            memory,
            target_policy=MemoryTargetPolicy.REQUIRED,
        )


def test_native_capability_requires_exact_collision_and_clock_mode() -> None:
    source = """
module NativeAsync {
  clock w reset wr @w clock r reset rr @r
  in we:bit @w in wa:u2 @w in wd:u8 @w in ra:u2 @r out q:u8 @r
  memory m:async_mem<u8,4> {
    write_port wp @w read_port rp @r read_latency 1 collision old
  }
  m.wp.enable=we m.wp.address=wa m.wp.data=wd m.rp.address=ra q=m.rp.data
}
"""
    memory = compile_source(source).ir.memories[0]
    capabilities = {
        "port_modes": "simple_dual.independent_1w1r",
        "clock_modes": "common.independent",
        "read_latencies": "1",
        "cross_clock_collision": "read_first",
    }
    plan = plan_memory_implementation(
        memory,
        target_resource_identity="test.ram",
        target_capabilities=capabilities,
        target_policy=MemoryTargetPolicy.REQUIRED,
        target_inventory=1,
    )
    assert plan.implementation is MemoryImplementationKind.NATIVE
    assert plan.collision_guarantee == "target_capability"

    with pytest.raises(MemoryPlanningError, match="no exact target resource"):
        plan_memory_implementation(
            memory,
            target_resource_identity="test.ram",
            target_capabilities={**capabilities, "cross_clock_collision": "write_first"},
            target_policy=MemoryTargetPolicy.REQUIRED,
        )


def test_bram_never_claims_zero_cycle_read_or_unadvertised_extra_latency() -> None:
    source = """
module NativeLatency {
  clock clk reset rst
  in we:bit in wa:u2 in wd:u8 in ra:u2 out q:u8
  memory m:mem<u8,4> { read_latency LAT collision old }
  m.write_enable=we m.write_address=wa m.write_data=wd
  m.read_address=ra q=m.read_data
}
"""
    capabilities = {
        "port_modes": "simple_dual",
        "clock_modes": "common",
        "synchronous_read": "true",
        "read_latencies": "0.1.2",
        "same_clock_collision": "read_first",
    }
    zero = compile_source(source.replace("LAT", "0")).ir.memories[0]
    two = compile_source(source.replace("LAT", "2")).ir.memories[0]
    for memory in (zero, two):
        with pytest.raises(MemoryPlanningError, match="no exact target resource"):
            plan_memory_implementation(
                memory,
                target_capabilities=capabilities,
                target_policy=MemoryTargetPolicy.REQUIRED,
            )
        plan = plan_memory_implementation(
            memory,
            target_capabilities=capabilities,
            target_policy=MemoryTargetPolicy.PREFERRED,
        )
        assert plan.implementation is MemoryImplementationKind.LEGACY_1R1W
        assert plan.cost_source == "structural_estimate"


def test_plan_identity_is_domain_sensitive_and_deterministic() -> None:
    first = plan_memory_implementation(
        compile_source(
            """module A { clock w reset wr @w clock r reset rr @r
            in we:bit @w in wa:u2 @w in wd:u8 @w in ra:u2 @r out q:u8 @r
            memory m:async_mem<u8,4>{write_port wp @w read_port rp @r read_latency 1 collision old}
            m.wp.enable=we m.wp.address=wa m.wp.data=wd m.rp.address=ra q=m.rp.data }"""
        ).ir.memories[0]
    )
    second = plan_memory_implementation(
        compile_source(
            """module A { clock r reset rr @r clock w reset wr @w
            in we:bit @r in wa:u2 @r in wd:u8 @r in ra:u2 @w out q:u8 @w
            memory m:async_mem<u8,4>{write_port wp @r read_port rp @w read_latency 1 collision old}
            m.wp.enable=we m.wp.address=wa m.wp.data=wd m.rp.address=ra q=m.rp.data }"""
        ).ir.memories[0]
    )
    assert first.identity == plan_memory_implementation(
        compile_source(
            """module A { clock w reset wr @w clock r reset rr @r
            in we:bit @w in wa:u2 @w in wd:u8 @w in ra:u2 @r out q:u8 @r
            memory m:async_mem<u8,4>{write_port wp @w read_port rp @r read_latency 1 collision old}
            m.wp.enable=we m.wp.address=wa m.wp.data=wd m.rp.address=ra q=m.rp.data }"""
        ).ir.memories[0]
    ).identity
    assert first.identity != second.identity
