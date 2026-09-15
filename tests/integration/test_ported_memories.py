from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_experimental,
    emit_target,
)
from zlang.compiler import compile_source
from zlang.ir.storage import MemoryCollision, MemoryPortKind
from zlang.opt.lowering import CanonicalizationError, lower, restore
from zlang.opt.capabilities import RewriteBarrier, module_rewrite_barriers
from zlang.semantic.errors import SemanticError
from zlang.simulate import simulate_multiclock_steps, simulate_storage_cycles
from zlang.targets import select_implementation_graph
from zlang.toolchain import lint_with_verilator


def _dual_port_source(collision: str = "new") -> str:
    return f"""
module DualPort {{
  clock clk reset rst
  in a_re:bit in a_we:bit in a_addr:u2 in a_data:u8
  in b_re:bit in b_we:bit in b_addr:u2 in b_data:u8
  out a_q:u8 out b_q:u8
  memory table:mem<u8,4> {{
    read_write_port a
    read_write_port b
    read_latency 1
    collision {collision}
    write_priority a > b
  }}
  table.a.read_enable = a_re
  table.a.write_enable = a_we
  table.a.address = a_addr
  table.a.write_data = a_data
  table.b.read_enable = b_re
  table.b.write_enable = b_we
  table.b.address = b_addr
  table.b.write_data = b_data
  a_q = table.a.read_data
  b_q = table.b.data
}}
"""


def _async_memory_source(collision: str = "old") -> str:
    return f"""
module AsyncMemory {{
  clock write_clk reset write_rst @write_clk
  clock read_clk reset read_rst @read_clk
  in write_enable:bit @write_clk
  in write_address:u2 @write_clk
  in write_data:u8 @write_clk
  in read_address:u2 @read_clk
  out read_data:u8 @read_clk
  memory table:async_mem<u8,4> {{
    write_port wr @write_clk
    read_port rd @read_clk
    read_latency 1
    collision {collision}
    reset {{ contents preserve read_data clear }}
  }}
  table.wr.enable = write_enable
  table.wr.address = write_address
  table.wr.data = write_data
  table.rd.address = read_address
  read_data = table.rd.data
}}
"""


def test_named_ports_lower_to_one_strict_canonical_memory() -> None:
    module = compile_source(_dual_port_source()).ir
    memory = module.memories[0]

    assert [port.kind for port in memory.ports] == [
        MemoryPortKind.READ_WRITE,
        MemoryPortKind.READ_WRITE,
    ]
    assert memory.write_priority == ("a", "b")
    assert memory.collision is MemoryCollision.WRITE_FIRST
    assert restore(lower(module)) == module

    canonical = lower(module)
    broken = replace(
        canonical,
        memories=(replace(canonical.memories[0], write_priority=("a",)),),
    )
    with pytest.raises(CanonicalizationError, match="complete priority"):
        restore(broken)


def test_same_clock_priority_and_different_address_writes() -> None:
    module = compile_source(_dual_port_source()).ir
    zero = {"a_re": 1, "a_we": 0, "a_addr": 0, "a_data": 0,
            "b_re": 1, "b_we": 0, "b_addr": 0, "b_data": 0}
    results = simulate_storage_cycles(
        module,
        [
            {**zero, "a_we": 1, "a_addr": 1, "a_data": 17,
             "b_we": 1, "b_addr": 1, "b_data": 99},
            {**zero, "a_addr": 1, "b_addr": 1},
            {**zero, "a_we": 1, "a_addr": 2, "a_data": 22,
             "b_we": 1, "b_addr": 3, "b_data": 33},
            {**zero, "a_addr": 2, "b_addr": 3},
            zero,
        ],
    )

    assert results[1] == {"a_q": 17, "b_q": 17}
    assert results[4] == {"a_q": 22, "b_q": 33}


@pytest.mark.parametrize(
    ("collision", "expected"),
    [("old", 5), ("new", 9), ("no_change", 5)],
)
def test_same_clock_collision_contract(collision: str, expected: int) -> None:
    module = compile_source(_dual_port_source(collision)).ir
    idle = {"a_re": 1, "a_we": 0, "a_addr": 1, "a_data": 0,
            "b_re": 1, "b_we": 0, "b_addr": 1, "b_data": 0}
    results = simulate_storage_cycles(
        module,
        [
            {**idle, "a_we": 1, "a_data": 5},
            idle,
            {**idle, "a_we": 1, "a_data": 9},
            idle,
        ],
    )
    assert results[-1]["a_q"] == expected


def test_named_latency_zero_read_and_reset_are_executable() -> None:
    source = """
module CombinationalRead {
  clock clk reset rst
  in we:bit in wa:u2 in wd:u8 in ra:u2 out q:u8
  memory table:mem<u8,4> {
    write_port wr read_port rd read_latency 0 collision new
  }
  table.wr.enable=we table.wr.address=wa table.wr.data=wd
  table.rd.address=ra q=table.rd.data
}
"""
    module = compile_source(source).ir
    results = simulate_storage_cycles(
        module,
        [
            {"we": 1, "wa": 1, "wd": 7, "ra": 1},
            {"we": 0, "wa": 0, "wd": 0, "ra": 1},
            {"we": 0, "wa": 0, "wd": 0, "ra": 1},
        ],
        reset=[False, False, True],
    )
    assert [item["q"] for item in results] == [7, 7, 0]
    text = emit_experimental(module)
    assert "assign zlang_table_rd_read_data" in text
    assert "? wd" in text


def test_memory_uniform_init_is_used_at_power_up_and_runtime_reset() -> None:
    source = """
module InitializedMemory<INIT=0x5a> {
  clock clk reset rst
  in we:bit in wa:u2 in wd:u8 in ra:u2 out q:u8
  memory table:mem<u8,4> {
    write_port wr read_port rd
    init INIT
    read_latency 1 collision old
    reset { contents clear read_data clear }
  }
  table.wr.enable=we table.wr.address=wa table.wr.data=wd
  table.rd.address=ra q=table.rd.data
}
"""
    module = compile_source(source).ir
    assert module.memories[0].initial_value is not None
    assert restore(lower(module)) == module
    results = simulate_storage_cycles(
        module,
        [
            {"we": 0, "wa": 0, "wd": 0, "ra": 2},
            {"we": 0, "wa": 0, "wd": 0, "ra": 2},
            {"we": 1, "wa": 2, "wd": 3, "ra": 2},
            {"we": 0, "wa": 0, "wd": 0, "ra": 2},
            {"we": 0, "wa": 0, "wd": 0, "ra": 2},
        ],
        reset=[False, False, False, False, True],
    )
    assert [item["q"] for item in results] == [0, 0x5A, 0x5A, 0x5A, 0]
    after_reset = simulate_storage_cycles(
        module,
        [
            {"we": 0, "wa": 0, "wd": 0, "ra": 2},
            {"we": 0, "wa": 0, "wd": 0, "ra": 2},
            {"we": 0, "wa": 0, "wd": 0, "ra": 2},
        ],
        reset=[True, False, False],
    )
    assert after_reset[-1]["q"] == 0x5A
    text = emit_experimental(module)
    assert "zlang_table_cells[zlang_table_reset_index] = 8'd90" in text
    assert "zlang_table_cells[zlang_table_reset_index] <= 8'd90" in text


def test_memory_init_must_be_exact_compile_time_value() -> None:
    source = """
module BadInit {
  clock clk reset rst
  in dynamic:u8 in we:bit in wa:u1 in ra:u1 out q:u8
  memory table:mem<u8,2> {
    init dynamic read_latency 1 collision old
  }
  table.read_address=ra table.write_enable=we
  table.write_address=wa table.write_data=dynamic q=table.read_data
}
"""
    with pytest.raises(SemanticError, match="init value must be a compile-time"):
        compile_source(source)


def test_rule_owned_memory_uses_the_same_uniform_init_contract() -> None:
    source = """
module ScheduledInit<INIT=7> {
  clock clk reset rst
  in address:u2 in read_enable:bit in write_enable:bit in value:u8
  out q:u8
  memory table:mem<u8,4> {
    init INIT read_latency 1 collision old
    reset { contents clear read_data clear }
  }
  read: when read_enable { table.read(address) }
  write: when write_enable { table.write(address,value) }
  q=table.read_data
}
"""
    module = compile_source(source).ir
    text = emit_experimental(module)
    assert "zlang_table_cells[zlang_table_reset_index] = 8'd7" in text
    assert "zlang_table_cells[zlang_table_reset_index] <= 8'd7" in text


def test_async_memory_uses_unrelated_edges_and_destination_latency() -> None:
    module = compile_source(_async_memory_source()).ir
    inputs = [
        {"write_enable": 1, "write_address": 1, "write_data": 42, "read_address": 1},
        {"write_enable": 0, "write_address": 0, "write_data": 0, "read_address": 1},
        {"write_enable": 0, "write_address": 0, "write_data": 0, "read_address": 1},
        {"write_enable": 0, "write_address": 0, "write_data": 0, "read_address": 1},
    ]
    results = simulate_multiclock_steps(
        module,
        inputs,
        [{"write_clk"}, set(), {"read_clk"}, set()],
    )
    assert [item["read_data"] for item in results] == [0, 0, 0, 42]


def test_async_memory_resets_are_owned_by_their_domains() -> None:
    module = compile_source(_async_memory_source()).ir
    idle = {
        "write_enable": 0,
        "write_address": 0,
        "write_data": 0,
        "read_address": 1,
    }
    results = simulate_multiclock_steps(
        module,
        [
            {**idle, "write_enable": 1, "write_address": 1, "write_data": 55},
            idle,
            idle,
            {**idle, "write_enable": 1, "write_address": 1, "write_data": 99},
            idle,
            idle,
        ],
        [
            {"write_clk"},
            {"read_clk"},
            {"read_clk"},
            {"write_clk"},
            {"read_clk"},
            set(),
        ],
        resets=[
            set(),
            set(),
            {"read_clk"},
            {"write_clk"},
            set(),
            set(),
        ],
    )
    # Reader reset clears only its result. Writer reset suppresses the attempted
    # update while the preserve policy keeps the previously committed cell.
    assert [item["read_data"] for item in results] == [0, 0, 55, 0, 0, 55]


def test_named_port_mask_keeps_arbitrary_width_lane_semantics() -> None:
    source = """
module Masked {
  clock clk reset rst
  in we:bit in wa:u1 in wd:bits<13> in wm:bits<2> in ra:u1 out q:bits<13>
  memory table:mem<bits<13>,2> {
    write_port wr read_port rd read_latency 1 collision old
  }
  table.wr.enable=we table.wr.address=wa table.wr.data=wd table.wr.mask=wm
  table.rd.address=ra q=table.rd.data
}
"""
    module = compile_source(source).ir
    results = simulate_storage_cycles(
        module,
        [
            {"we": 1, "wa": 0, "wd": 0x1ABC, "wm": 0b11, "ra": 0},
            {"we": 1, "wa": 0, "wd": 0x0123, "wm": 0b01, "ra": 0},
            {"we": 0, "wa": 0, "wd": 0, "wm": 0, "ra": 0},
            {"we": 0, "wa": 0, "wd": 0, "wm": 0, "ra": 0},
        ],
    )
    assert results[-1]["q"] == 0x1A23
    text = emit_experimental(module)
    assert "write_mask_expanded" in text


@pytest.mark.parametrize(("collision", "expected"), [("old", 0), ("new", 7)])
def test_async_coincident_edge_collision_model(collision: str, expected: int) -> None:
    module = compile_source(_async_memory_source(collision)).ir
    inputs = [
        {"write_enable": 1, "write_address": 2, "write_data": 7, "read_address": 2},
        {"write_enable": 0, "write_address": 0, "write_data": 0, "read_address": 2},
    ]
    results = simulate_multiclock_steps(
        module,
        inputs,
        [{"write_clk", "read_clk"}, set()],
    )
    assert results[1]["read_data"] == expected


def test_async_memory_emits_one_writer_and_one_reader_process() -> None:
    module = compile_source(_async_memory_source()).ir
    text = emit_experimental(module)
    assert text.count("always_ff @(posedge write_clk)") == 1
    assert text.count("always_ff @(posedge read_clk)") == 1
    assert text.count("table_cells[write_address] <= write_data") == 1
    assert text.count("table_cells[read_address]") == 1
    assert "table_rd_read_data" in text
    assert module_rewrite_barriers(module) == frozenset(
        {RewriteBarrier.CLOCK_DOMAIN_CROSSING}
    )


def test_async_new_collision_requires_exact_physical_binding() -> None:
    module = compile_source(_async_memory_source("new")).ir
    with pytest.raises(
        SystemVerilogEmissionError, match="exact target physical binding"
    ):
        emit_experimental(module)


def test_same_clock_memory_emits_one_process_and_replication_is_physical() -> None:
    source = """
module Replicated {
  clock clk reset rst
  in we:bit in wa:u2 in wd:u8 in a0:u2 in a1:u2 in a2:u2
  out q0:u8 out q1:u8 out q2:u8
  memory table:mem<u8,4> {
    write_port w read_port r0 read_port r1 read_port r2
    read_latency 1 collision old
  }
  table.w.enable=we table.w.address=wa table.w.data=wd
  table.r0.address=a0 table.r1.address=a1 table.r2.address=a2
  q0=table.r0.data q1=table.r1.data q2=table.r2.data
}
"""
    text = emit_experimental(compile_source(source).ir)
    assert text.count("always_ff @(posedge clk)") == 1
    assert "memory_plan=replicated_1r1w" in text
    for port in ("r0", "r1", "r2"):
        assert f"table_cells_{port}" in text
        assert f"table_cells_{port}[wa] <= wd" in text
        assert (
            f"table_{port}_read_data <= zlang_table_cells_{port}[a" in text
        )


def test_xilinx_same_clock_true_dual_mapping_is_exact() -> None:
    source = """
module NativeTdp {
  clock clk reset rst
  in ae:bit in awe:bit in aa:u2 in ad:bits<9>
  in be:bit in bwe:bit in ba:u2 in bd:bits<9>
  out aq:bits<9> out bq:bits<9>
  memory table:mem<bits<9>,4> {
    read_write_port a read_write_port b
    init 0x12
    read_latency 1 collision old write_priority a > b
    reset { contents preserve read_data clear }
  }
  table.a.read_enable=ae table.a.write_enable=awe
  table.a.address=aa table.a.write_data=ad
  table.b.read_enable=be table.b.write_enable=bwe
  table.b.address=ba table.b.write_data=bd
  aq=table.a.data bq=table.b.data
}
"""
    module = compile_source(source).ir
    graph = select_implementation_graph(
        module,
        target="xc7z030ffg676-1",
        architecture="Xilinx7BRAM36SimpleDualPort",
        mode="required",
    )
    assert dict(graph.resources[0].configuration)["width"] == 9
    assert "true_dual synchronous memory" in graph.legality_evidence[0]
    text = emit_target(module, graph)
    assert text.count("always_ff @(posedge clk)") == 2
    assert "zlang_table_cells[zlang_table_reset_index] = 9'd18" in text
    assert "zlang_table_cells[zlang_table_reset_index] <=" not in text


def test_ported_memory_generic_and_selected_sv_lint(
    tmp_path: Path,
) -> None:
    async_module = compile_source(_async_memory_source()).ir
    async_rtl = tmp_path / "AsyncMemory.sv"
    async_rtl.write_text(emit_experimental(async_module))
    lint_with_verilator((async_rtl,), "AsyncMemory")

    compilation = compile_source(
        """
module NativeTdpLint {
  clock clk reset rst
  in ae:bit in awe:bit in aa:u2 in ad:bits<9>
  in be:bit in bwe:bit in ba:u2 in bd:bits<9>
  out aq:bits<9> out bq:bits<9>
  memory table:mem<bits<9>,4> {
    read_write_port a read_write_port b
    read_latency 1 collision old write_priority a > b
    reset { contents preserve read_data clear }
  }
  table.a.read_enable=ae table.a.write_enable=awe
  table.a.address=aa table.a.write_data=ad
  table.b.read_enable=be table.b.write_enable=bwe
  table.b.address=ba table.b.write_data=bd
  aq=table.a.data bq=table.b.data
}
""",
        target="xc7z030ffg676-1",
        architecture="Xilinx7BRAM36SimpleDualPort",
        architecture_mode="required",
    )
    selected_rtl = tmp_path / "NativeTdpLint.sv"
    selected_rtl.write_text(
        emit_target(compilation.ir, compilation.implementation_graph)
    )
    lint_with_verilator((selected_rtl,), "NativeTdpLint")


@pytest.mark.parametrize(
    "source,child_name",
    [
        (
            """import std.storage module Top {
            clock write_clk reset write_rst @write_clk
            clock read_clk reset read_rst @read_clk
            in source:rv<u8>@write_clk out sink:rv<u8>@read_clk
            inst bridge:StorageAsyncFifo<T=u8,D=4>
            source -> bridge.source bridge.sink -> sink }""",
            "StorageAsyncFifo",
        ),
        (
            """import std.storage module Top {
            clock write_clk reset write_rst @write_clk
            clock read_clk reset read_rst @read_clk
            in we:bit@write_clk in wa:u2@write_clk in wd:u8@write_clk
            in ra:u2@read_clk out q:u8@read_clk
            inst storage:StorageAsyncMemory1W1R<T=u8,N=4,AW=2>
            storage.write=we storage.write_address=wa storage.write_data=wd
            storage.read_address=ra q=storage.read_data }""",
            "StorageAsyncMemory1W1R",
        ),
    ],
)
def test_multidomain_stdlib_wrappers_are_ordinary_hierarchy(
    source: str,
    child_name: str,
) -> None:
    module = compile_source(source, top="Top").ir
    assert module.children[0].name == child_name
    text = emit_experimental(module)
    assert ".write_clk(write_clk)" in text
    assert ".read_clk(read_clk)" in text


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        (
            "read_write_port a read_write_port b read_latency 1 collision old",
            "complete write_priority",
        ),
        (
            "write_port wr @a read_port rd @a read_latency 1 collision old",
            "different clock domains",
        ),
        (
            "write_port wr @a read_port rd @b read_latency 0 collision old",
            "read_latency 1",
        ),
    ],
)
def test_invalid_port_shapes_fail_closed(declaration: str, message: str) -> None:
    kind = "mem" if "read_write_port" in declaration else "async_mem"
    source = f"""
module Bad {{
  clock a reset ar @a
  clock b reset br @b
  memory table:{kind}<u8,4>{' @a' if kind == 'mem' else ''} {{ {declaration} }}
}}
"""
    with pytest.raises(SemanticError, match=message):
        compile_source(source)


def test_foreign_domain_port_control_fails_closed() -> None:
    source = """
module BadControl {
  clock w reset wr @w clock r reset rr @r
  in foreign_enable:bit @r in wa:u2 @w in wd:u8 @w
  in ra:u2 @r out q:u8 @r
  memory table:async_mem<u8,4> {
    write_port wp @w read_port rp @r read_latency 1 collision old
  }
  table.wp.enable=foreign_enable table.wp.address=wa table.wp.data=wd
  table.rp.address=ra q=table.rp.data
}
"""
    with pytest.raises(SemanticError, match="clock-domain mismatch"):
        compile_source(source)
