from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.compiler import compile_source
from zlang.opt.lowering import CanonicalizationError, lower, restore
from zlang.opt.capabilities import RewriteBarrier, module_rewrite_barriers
from zlang.ir.module import default_selected_ir_identity
from zlang.semantic.errors import SemanticError
from zlang.simulate import simulate_multiclock_steps


DUAL_COUNTER = """
module DualClockCounter {
    clock clk_a
    reset rst_a @clk_a

    clock clk_b
    reset rst_b @clk_b

    out count_a : u8 @clk_a
    out count_b : u8 @clk_b

    reg a : u8 @clk_a = 0
    reg b : u8 @clk_b = 0

    rule TickA @clk_a when 1 {
        a <- truncate<8>(a + 1)
    }

    rule TickB @clk_b when 1 {
        b <- truncate<8>(b + 2)
    }

    count_a = a
    count_b = b
}
"""


def test_domain_ownership_round_trips_and_emits_independent_processes() -> None:
    module = compile_source(DUAL_COUNTER).ir

    assert module.clock is None
    assert module.reset is None
    assert [item.domain for item in module.registers] == ["clk_a", "clk_b"]
    assert [item.domain for item in module.rules] == ["clk_a", "clk_b"]
    assert module.resolved_transition is not None
    assert [
        item.domain for item in module.resolved_transition.action_groups
    ] == ["clk_a", "clk_b"]
    assert restore(lower(module)) == module

    text = emit_experimental(module)
    assert text.count("always_ff @(posedge clk_a)") == 1
    assert text.count("always_ff @(posedge clk_b)") == 1
    assert "if (rst_a) begin\n      a <= 8'd0;" in text
    assert "if (rst_b) begin\n      b <= 8'd0;" in text
    assert len(emit_artifact(module).physical_domains) == 2
    port_domains = {
        binding.semantic_signal_id: (
            binding.clock_domain,
            binding.reset_domain,
        )
        for binding in emit_artifact(module).bindings
    }
    assert port_domains["port:count_a"] == ("clk_a", "rst_a")
    assert port_domains["port:count_b"] == ("clk_b", "rst_b")


def test_multiclock_simulation_commits_only_the_active_domain() -> None:
    module = compile_source(DUAL_COUNTER).ir
    outputs = simulate_multiclock_steps(
        module,
        [{}, {}, {}, {}, {}, {}],
        [
            {"clk_a"},
            {"clk_b"},
            {"clk_a", "clk_b"},
            set(),
            {"clk_a"},
            {"clk_b"},
        ],
        [set(), set(), set(), set(), {"clk_a"}, set()],
    )

    assert [(item["count_a"], item["count_b"]) for item in outputs] == [
        (0, 0),
        (1, 0),
        (1, 2),
        (2, 4),
        (2, 4),
        (0, 4),
    ]


@pytest.mark.parametrize(
    "declaration, message",
    [
        ("reg value : u8 = 0", "ambiguous clock domain for register 'value'"),
        (
            "reg value : u8 @missing = 0",
            "register 'value' references unknown clock domain 'missing'",
        ),
    ],
)
def test_multiclock_state_requires_one_exact_domain(
    declaration: str,
    message: str,
) -> None:
    source = f"""
module Ambiguous {{
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    {declaration}
}}
"""
    with pytest.raises(SemanticError, match=message):
        compile_source(source)


def test_cross_domain_read_cannot_be_hidden_behind_an_inferred_local() -> None:
    source = """
module IllegalCrossing {
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    reg a : u8 @clk_a = 0
    reg b : u8 @clk_b = 0
    forwarded = a
    rule UpdateB @clk_b when 1 { b <- forwarded }
}
"""
    with pytest.raises(SemanticError, match="clock-domain mismatch in rule 'UpdateB'"):
        compile_source(source)


@pytest.mark.parametrize(
    ("body", "message"),
    (
        (
            "in a : u8 @clk_a out y : u8 @clk_b y = a",
            "implicit clock-domain crossing in assignment to 'y'",
        ),
        (
            "in a : u8 @clk_a in b : u8 @clk_b out y : u9 @clk_b y = a + b",
            "implicit clock-domain crossing in assignment to 'y'",
        ),
        (
            "in go : bit @clk_a "
            "fsm phase : S @clk_b = Idle { "
            "Idle { when go -> Run {} } Run { hold } }",
            "clock-domain mismatch in rule",
        ),
        (
            "in data : u8 @clk_a fifo q : fifo<u8,2> @clk_b "
            "q.data = data q.push = 0 q.pop = 0",
            "clock-domain mismatch in FIFO 'q'",
        ),
    ),
)
def test_ordinary_constructs_never_infer_a_cdc(
    body: str,
    message: str,
) -> None:
    source = f"""
enum S {{ Idle Run }}
module IllegalImplicitCrossing {{
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    {body}
}}
"""
    with pytest.raises(SemanticError, match=message):
        compile_source(source)


def test_cross_domain_priorities_are_rejected_not_globalized() -> None:
    source = """
module IllegalPriority {
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    reg a : u8 @clk_a = 0
    reg b : u8 @clk_b = 0
    rule A @clk_a when 1 { a <- 1 }
    rule B @clk_b when 1 { b <- 1 }
    priority A > B
}
"""
    with pytest.raises(SemanticError, match="different clock domains"):
        compile_source(source)


def test_exact_pipelines_are_clocked_and_simulated_in_their_own_domains() -> None:
    source = """
module DualPipeline {
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    in a : u8 @clk_a
    in b : u8 @clk_a
    in c : u8 @clk_b
    out product : u16 @clk_a
    out delayed : u8 @clk_b
    product = pipeline(2) { a * b }
    delayed = pipeline(1) @clk_b { c }
}
"""
    module = compile_source(source).ir
    pipelines = [assignment.expression for assignment in module.assignments]
    assert [item.domain for item in pipelines] == ["clk_a", "clk_b"]
    assert [
        item.pipeline_plan.scheduled_value_graph.clock_domain
        for item in pipelines
    ] == ["clk_a", "clk_b"]

    text = emit_experimental(module)
    assert "always_ff @(posedge clk_a)" in text
    assert "always_ff @(posedge clk_b)" in text
    assert "product_pipe_s2 <= product_pipe_s1;" in text
    assert "delayed_pipe_s1 <= c;" in text

    outputs = simulate_multiclock_steps(
        module,
        [{"a": 2, "b": 3, "c": 7}] * 4,
        [{"clk_a"}, {"clk_b"}, {"clk_a", "clk_b"}, {"clk_a"}],
    )
    assert [(item["product"], item["delayed"]) for item in outputs] == [
        (0, 0), (0, 0), (0, 7), (6, 7),
    ]


def test_pipeline_is_not_an_implicit_cdc() -> None:
    source = """
module IllegalPipelineCrossing {
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    in a : u8 @clk_a
    out b : u8 @clk_b
    b = pipeline(2) @clk_b { a }
}
"""
    with pytest.raises(SemanticError, match="pipeline cut is not a CDC crossing"):
        compile_source(source)


def test_canonical_action_group_domain_is_validated() -> None:
    module = compile_source(DUAL_COUNTER).ir
    canonical = lower(module)
    assert canonical.resolved_transition is not None
    first, *remaining = canonical.resolved_transition.action_groups
    broken = replace(
        canonical.resolved_transition,
        action_groups=(replace(first, domain="clk_b"), *remaining),
    )
    with pytest.raises(CanonicalizationError, match="does not match its typed rule"):
        restore(replace(canonical, resolved_transition=broken))


def test_fsm_state_and_generated_rules_inherit_explicit_domain() -> None:
    source = """
enum Phase { Idle Run }
module DualDomainFsm {
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    out state : bits<1> @clk_b
    fsm phase : Phase @clk_b = Idle {
        Idle { -> Run {} }
        Run { -> Idle {} }
    }
    state = enum_encode(phase)
}
"""
    module = compile_source(source).ir
    assert module.registers[0].domain == "clk_b"
    assert {rule.domain for rule in module.rules} == {"clk_b"}
    text = emit_experimental(module)
    assert "always_ff @(posedge clk_b)" in text
    assert "always_ff @(posedge clk_a)" not in text


def test_scheduled_fifos_are_owned_and_emitted_per_domain() -> None:
    source = """
module DualQueue {
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    in a : u8 @clk_a
    in b : u8 @clk_b
    out count_a : u3 @clk_a
    out count_b : u3 @clk_b
    fifo qa : fifo<u8,4> @clk_a
    fifo qb : fifo<u8,4> @clk_b
    rule PushA @clk_a when 1 { qa.push(a) }
    rule PushB @clk_b when 1 { qb.push(b) }
    count_a = qa.count
    count_b = qb.count
}
"""
    module = compile_source(source).ir
    assert [fifo.domain for fifo in module.fifos] == ["clk_a", "clk_b"]
    text = emit_experimental(module)
    assert text.count("always_ff @(posedge clk_a)") == 1
    assert text.count("always_ff @(posedge clk_b)") == 1
    assert "if (rst_a) begin" in text
    assert "if (rst_b) begin" in text


def test_storage_controls_cannot_hide_a_cross_domain_read() -> None:
    source = """
module BadMemoryDomain {
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    in address : u3 @clk_b
    out data : u8 @clk_a
    memory table : mem<u8,8> @clk_a {
        read_latency 1
        collision read_first
    }
    table.read_address = address
    table.write_enable = 0
    table.write_address = 0
    table.write_data = 0
    data = table.read_data
}
"""
    with pytest.raises(SemanticError, match="clock-domain mismatch"):
        compile_source(source)


def test_csr_and_ordinary_state_can_live_in_different_domains() -> None:
    source = """
module DualDomainCsr {
    clock apb_clk reset apb_rst @apb_clk
    clock dsp_clk reset dsp_rst @dsp_clk
    out count : u8 @dsp_clk
    reg counter : u8 @dsp_clk = 0
    rule Tick @dsp_clk when 1 {
        counter <- truncate<8>(counter + 1)
    }
    count = counter
    csr control @0 @apb_clk {
        CONTROL @0 { enable bit rw = 0 }
    }
}
"""
    module = compile_source(source).ir
    block = module.csr_blocks[0]
    assert (block.domain, block.reset) == ("apb_clk", "apb_rst")
    assert restore(lower(module)) == module
    text = emit_experimental(module)
    assert "always_ff @(posedge apb_clk)" in text
    assert "always_ff @(posedge dsp_clk)" in text
    assert "if (apb_rst) begin" in text
    assert "if (dsp_rst) begin" in text


def test_unannotated_csr_is_ambiguous_in_multiclock_module() -> None:
    source = """
module AmbiguousCsr {
    clock a reset ra @a
    clock b reset rb @b
    csr control @0 { CONTROL @0 { enable bit rw = 0 } }
}
"""
    with pytest.raises(SemanticError, match="ambiguous clock domain for CSR"):
        compile_source(source)


def test_canonical_csr_domain_link_is_validated() -> None:
    source = """
module DomainCsr {
    clock apb_clk reset apb_rst @apb_clk
    clock dsp_clk reset dsp_rst @dsp_clk
    csr control @0 @apb_clk {
        CONTROL @0 { enable bit rw = 0 }
    }
}
"""
    canonical = lower(compile_source(source).ir)
    block = canonical.csr_blocks[0]
    with pytest.raises(CanonicalizationError, match="physical domain disagrees"):
        restore(
            replace(
                canonical,
                csr_blocks=(replace(block, domain="dsp_clk"),),
            )
        )


def test_explicit_sync_level_becomes_a_destination_domain_value() -> None:
    source = """
module StateAndCdc {
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    in flag_a : bit @clk_a
    out flag_b : bit @clk_b
    out observed : u8 @clk_b
    reg count : u8 @clk_b = 0
    rule Observe @clk_b when flag_b {
        count <- truncate<8>(count + 1)
    }
    observed = count
    connect flag_a -> flag_b { crossing sync_level }
}
"""
    module = compile_source(source).ir
    assert module.rules[0].domain == "clk_b"
    text = emit_experimental(module)
    assert '(* ASYNC_REG = "TRUE" *)' in text
    assert "always_ff @(posedge clk_b)" in text
    assert "assign rule_Observe_guard = flag_b;" in text
    assert "assign rule_Observe_fire = !rst_b" in text
    assert module_rewrite_barriers(module) == frozenset(
        {RewriteBarrier.CLOCK_DOMAIN_CROSSING}
    )


def test_one_domain_child_may_bind_to_one_exact_parent_domain() -> None:
    source = """
module DomainAChild {
    clock clk_a reset rst_a @clk_a
    in value : u8 @clk_a
    out delayed : u8 @clk_a
    reg state : u8 = 0
    state <- value
    delayed = state
}
module MultiClockParent {
    clock clk_a reset rst_a @clk_a
    clock clk_b reset rst_b @clk_b
    in value : u8 @clk_a
    out delayed : u8 @clk_a
    child : DomainAChild { value }
    delayed = child.delayed
}
"""
    module = compile_source(source).ir
    instance = module.elaborated_instances[0]
    assert (instance.clock, instance.reset) == ("clk_a", "rst_a")
    text = emit_experimental(module)
    assert ".clk_a(clk_a)" in text
    assert ".rst_a(rst_a)" in text


def test_child_domain_must_match_one_exact_parent_contract() -> None:
    source = """
module ForeignChild {
    clock foreign reset foreign_rst @foreign
    reg state : u8 = 0
    state <- truncate<8>(state + 1)
}
module Parent {
    clock a reset ra @a
    clock b reset rb @b
    child : ForeignChild
}
"""
    with pytest.raises(SemanticError, match="does not match one exact parent domain"):
        compile_source(source)


def test_child_output_cannot_hide_a_cross_domain_path() -> None:
    source = """
module DomainAChild {
    clock a reset ra @a
    out value : u8 @a
    reg state : u8 @a = 0
    state <- truncate<8>(state + 1)
    value = state
}
module Parent {
    clock a reset ra @a
    clock b reset rb @b
    out value : u8 @b
    child : DomainAChild
    value = child.value
}
"""
    with pytest.raises(
        SemanticError,
        match="implicit clock-domain crossing in assignment to 'value'",
    ):
        compile_source(source)


def test_child_input_binding_cannot_hide_a_cross_domain_path() -> None:
    source = """
module DomainBChild {
    clock b reset rb @b
    in value : u8 @b
    reg state : u8 @b = 0
    state <- value
}
module Parent {
    clock a reset ra @a
    clock b reset rb @b
    in source : u8 @a
    child : DomainBChild { value = source }
}
"""
    with pytest.raises(
        SemanticError,
        match="instance input 'child.value' in domain 'b'.*from 'a'",
    ):
        compile_source(source)


def test_multiclock_selected_identity_changes_with_state_ownership() -> None:
    moved = DUAL_COUNTER.replace(
        "reg a : u8 @clk_a = 0",
        "reg a : u8 @clk_b = 0",
    ).replace(
        "rule TickA @clk_a",
        "rule TickA @clk_b",
    ).replace(
        "out count_a : u8 @clk_a",
        "out count_a : u8 @clk_b",
    )
    first = compile_source(DUAL_COUNTER).ir
    second = compile_source(moved).ir
    assert default_selected_ir_identity(first) != default_selected_ir_identity(second)


def test_each_async_domain_owns_one_release_conditioner() -> None:
    source = """
module DualAsync {
    clock a async reset ra @a
    clock b async reset rb @b
    reg left : u8 @a = 0
    reg right : u8 @b = 0
    left <- truncate<8>(left + 1)
    right <- truncate<8>(right + 1)
}
"""
    text = emit_experimental(compile_source(source).ir)
    assert text.count('(* ASYNC_REG = "TRUE" *) logic [1:0]') == 2
    assert "always_ff @(posedge a or posedge ra)" in text
    assert "always_ff @(posedge b or posedge rb)" in text
    assert text.count("logic zlang_reset_effective_") == 2


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_two_domain_rtl_passes_strict_verilator_lint(tmp_path: Path) -> None:
    rtl = tmp_path / "DualClockCounter.sv"
    rtl.write_text(emit_experimental(compile_source(DUAL_COUNTER).ir))
    completed = subprocess.run(
        (
            "verilator",
            "--lint-only",
            "-Wall",
            "-Wno-DECLFILENAME",
            "--top-module",
            "DualClockCounter",
            str(rtl),
        ),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_two_unrelated_clocks_evolve_independently_in_rtl(tmp_path: Path) -> None:
    rtl = tmp_path / "DualClockCounter.sv"
    rtl.write_text(emit_experimental(compile_source(DUAL_COUNTER).ir))
    bench = tmp_path / "tb.sv"
    bench.write_text(
        """
module tb;
  timeunit 1ns;
  timeprecision 1ps;
  logic clk_a = 0, rst_a = 1;
  logic clk_b = 0, rst_b = 1;
  logic [7:0] count_a, count_b;
  DualClockCounter dut(.*);
  always #5 clk_a = ~clk_a;
  always #3.5 clk_b = ~clk_b;
  initial begin
    #12 rst_a = 0;
    #3 rst_b = 0;
    #39;
    if (count_a !== 8'd4) $fatal(1, "clk_a count=%0d", count_a);
    if (count_b !== 8'd12) $fatal(1, "clk_b count=%0d", count_b);
    $finish;
  end
endmodule
"""
    )
    completed = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-fatal",
            "--top-module",
            "tb",
            "--Mdir",
            str(tmp_path / "obj"),
            str(rtl),
            str(bench),
        ),
        capture_output=True,
        text=True,
        env={**os.environ, "CCACHE_DISABLE": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    executed = subprocess.run(
        (str(tmp_path / "obj" / "Vtb"),),
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0, executed.stderr + executed.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_state_consumes_explicit_cdc_value_with_unrelated_rtl_clocks(
    tmp_path: Path,
) -> None:
    source = """
module StatefulCrossing {
    clock a reset ra @a
    clock b reset rb @b
    in source : bit @a
    out synchronized : bit @b
    out count : u8 @b
    reg observed : u8 @b = 0
    rule Observe @b when synchronized {
        observed <- truncate<8>(observed + 1)
    }
    count = observed
    connect source -> synchronized { crossing sync_level }
}
"""
    rtl = tmp_path / "StatefulCrossing.sv"
    rtl.write_text(emit_experimental(compile_source(source).ir))
    bench = tmp_path / "tb.sv"
    bench.write_text(
        """
module tb;
  timeunit 1ns;
  timeprecision 1ps;
  logic a = 0, ra = 1, b = 0, rb = 1, source = 0;
  logic synchronized;
  logic [7:0] count;
  StatefulCrossing dut(.*);
  always #5 a = ~a;
  always #3.5 b = ~b;
  initial begin
    #12 ra = 0;
    #3 rb = 0;
    #1 source = 1;
    #24;
    if (synchronized !== 1'b1) $fatal(1, "level was not synchronized");
    if (count !== 8'd2) $fatal(1, "destination count=%0d", count);
    $finish;
  end
endmodule
"""
    )
    environment = {**os.environ, "CCACHE_DISABLE": "1"}
    completed = subprocess.run(
        (
            "verilator", "--binary", "--timing", "-Wall",
            "-Wno-DECLFILENAME", "-Wno-fatal", "--top-module", "tb",
            "--Mdir", str(tmp_path / "obj-cdc"), str(rtl), str(bench),
        ),
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    executed = subprocess.run(
        (str(tmp_path / "obj-cdc" / "Vtb"),),
        capture_output=True,
        text=True,
        env=environment,
    )
    assert executed.returncode == 0, executed.stderr + executed.stdout
