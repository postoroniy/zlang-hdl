"""Cycle and direct-SV checks for async-assert/synchronized-release reset."""

from __future__ import annotations

import hashlib
from pathlib import Path
import os
import re
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog.emitter import emit as emit_systemverilog
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles, simulate_hierarchical_scalar_cycles
from zlang.toolchain import lint_with_verilator


SAFE_COUNTER = """
module SafeCounter {
  clock clk
  async reset arst @clk
  in x : u8
  out y : u8
  reg q : u8 = 0
  q <- x
  y = q
}
"""


SAFE_HIERARCHY = """
module SafeChild {
  clock clk
  async reset arst @clk
  in x : u8
  out y : u8
  reg q : u8 = 0
  q <- x
  y = q
}

module SafeTop {
  clock clk
  async reset arst @clk
  in x : u8
  out y : u8
  inst child : SafeChild { x }
  y = child.y
}
"""


CREDIT_TO_RV = """
module CreditToRvSafeReset {
  clock clk
  async reset arst @clk
  in rx : credit<u8,2>
  out tx : rv<u8>
  rx -> tx { buffer 2 adapter credit_to_rv }
}
"""


LEGACY_RESET_COUNTER = """
module LegacyResetCounter {
  clock clk
  reset rst
  in x : u8
  out y : u8
  reg q : u8 = 0
  q <- x
  y = q
}
"""


STATELESS_SAFE_RESET = """
module StatelessSafeReset {
  clock clk
  async reset arst @clk
  in x : u8
  out y : u8
  y = x
}
"""


def _compile(source: str, *, top: str | None = None):
    return compile_source(
        source,
        top=top,
    ).ir


def test_simulator_holds_safe_reset_for_two_release_edges() -> None:
    module = _compile(SAFE_COUNTER)
    trace = simulate_cycles(
        module,
        ({"x": 3}, {"x": 5}, {"x": 7}, {"x": 11}, {"x": 13}),
        (True, False, False, False, False),
    )

    # The assertion cycle and the first two deasserted active edges reset q.
    # The third deasserted edge accepts x=11; q is observable next cycle.
    assert [item["y"] for item in trace] == [0, 0, 0, 0, 11]


def test_simulator_reassertion_restarts_the_release_sequence() -> None:
    module = _compile(SAFE_COUNTER)
    trace = simulate_cycles(
        module,
        (
            {"x": 1}, {"x": 2}, {"x": 3}, {"x": 4},
            {"x": 5}, {"x": 6}, {"x": 7}, {"x": 8}, {"x": 9},
        ),
        (True, False, True, False, False, False, False, False, False),
    )

    # The second assertion discards the partially completed release.  Normal
    # state resumes only after two new deasserted active edges.
    assert [item["y"] for item in trace] == [0, 0, 0, 0, 0, 0, 6, 7, 8]


def test_hierarchical_simulator_conditions_only_the_public_root() -> None:
    module = _compile(SAFE_HIERARCHY, top="SafeTop")
    trace = simulate_hierarchical_scalar_cycles(
        module,
        ({"x": 3}, {"x": 5}, {"x": 7}, {"x": 11}, {"x": 13}),
        (True, False, False, False, False),
    )

    # A duplicated child conditioner would add two more release cycles.
    assert [item["y"] for item in trace] == [0, 0, 0, 0, 11]


def test_direct_sv_emits_one_root_conditioner_and_native_child_reset_abi() -> None:
    module = _compile(SAFE_HIERARCHY, top="SafeTop")
    generated = emit_systemverilog(module)

    assert generated.count('(* ASYNC_REG = "TRUE" *)') == 1
    assert generated.count("always_ff @(posedge clk or posedge arst)") == 2
    effective = next(
        word.rstrip(";")
        for line in generated.splitlines()
        if line.strip().startswith("logic zlang_reset_effective_")
        for word in (line.strip().split()[1],)
    )
    assert f".arst({effective})" in generated
    assert f"always_ff @(posedge clk or posedge {effective})" not in generated


def test_direct_sv_adapter_consumes_effective_reset_without_reconditioning() -> None:
    generated = emit_systemverilog(_compile(CREDIT_TO_RV))
    effective = next(
        word.rstrip(";")
        for line in generated.splitlines()
        if line.strip().startswith("logic zlang_reset_effective_")
        for word in (line.strip().split()[1],)
    )

    assert generated.count('(* ASYNC_REG = "TRUE" *)') == 1
    assert f".rst({effective})" in generated
    assert ".rst(arst)" not in generated
    assert f"rx_send && !{effective}" in generated
    assert "rx_send && !arst" not in generated


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_raw_active_low_adapter_gates_traffic_only_when_reset_is_deasserted(
    tmp_path: Path,
) -> None:
    source = CREDIT_TO_RV.replace(
        "async reset arst @clk",
        "reset rst_n @clk { mode asynchronous polarity active_low "
        "power_up unspecified }",
    )
    generated = emit_systemverilog(_compile(source))
    assert "rx_send && rst_n" in generated
    assert "rx_send && !rst_n" not in generated
    assert "zlang_reset_release_" not in generated
    rtl = tmp_path / "CreditToRvSafeReset.sv"
    rtl.write_text(generated)
    lint_with_verilator((rtl,), "CreditToRvSafeReset")


def test_direct_sv_native_reset_text_has_no_conditioner() -> None:
    synchronous = _compile(SAFE_COUNTER.replace("async reset arst", "reset arst"))
    raw_async = _compile(SAFE_COUNTER.replace(
        "async reset arst @clk",
        "reset arst @clk { mode asynchronous polarity active_high "
        "power_up unspecified }",
    ))

    synchronous_text = emit_systemverilog(synchronous)
    raw_async_text = emit_systemverilog(raw_async)
    assert "zlang_reset_release_" not in synchronous_text
    assert "zlang_reset_effective_" not in synchronous_text
    assert "always_ff @(posedge clk)" in synchronous_text
    assert "if (arst) begin" in synchronous_text
    assert "zlang_reset_release_" not in raw_async_text
    assert "zlang_reset_effective_" not in raw_async_text
    assert "always_ff @(posedge clk or posedge arst)" in raw_async_text


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_state_free_safe_reset_does_not_emit_an_unused_conditioner(
    tmp_path: Path,
) -> None:
    generated = emit_systemverilog(_compile(STATELESS_SAFE_RESET))
    assert "zlang_reset_release_" not in generated
    assert "zlang_reset_effective_" not in generated
    rtl = tmp_path / "StatelessSafeReset.sv"
    rtl.write_text(generated)
    lint_with_verilator((rtl,), "StatelessSafeReset")




def test_direct_sv_safe_reset_preserves_falling_edge_and_active_low_polarity() -> None:
    source = SAFE_COUNTER.replace(
        "clock clk\n  async reset arst @clk",
        "clock clk { edge falling }\n  async reset arst_n @clk { "
        "polarity active_low }",
    ).replace("arst @clk", "arst_n @clk")
    generated = emit_systemverilog(_compile(source))

    assert generated.count('(* ASYNC_REG = "TRUE" *)') == 1
    assert "always_ff @(negedge clk or negedge arst_n)" in generated
    assert "if (!arst_n) zlang_reset_release_" in generated
    assert "<= 2'b00;" in generated
    assert ", 1'b1};" in generated
    assert "always_ff @(negedge clk or negedge zlang_reset_effective_" in generated


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_reset_conditioner_private_names_cannot_collide_with_user_ports(
    tmp_path: Path,
) -> None:
    baseline = emit_systemverilog(_compile(SAFE_COUNTER))
    stage = re.search(
        r'ASYNC_REG = "TRUE" \*\) logic \[1:0\] (\w+);', baseline
    )
    effective = re.search(r"logic (zlang_reset_effective_\w+);", baseline)
    assert stage is not None and effective is not None
    stage_name = stage.group(1)
    effective_name = effective.group(1)
    source = SAFE_COUNTER.replace(
        "  out y : u8\n",
        f"  out y : u8\n  out {stage_name} : bit\n"
        f"  out {effective_name} : bit\n",
    ).replace(
        "  y = q\n",
        f"  y = q\n  {stage_name} = 0\n  {effective_name} = 0\n",
    )

    generated = emit_systemverilog(_compile(source))
    allocated = re.search(
        r'ASYNC_REG = "TRUE" \*\) logic \[1:0\] (\w+);', generated
    )
    assert allocated is not None
    assert allocated.group(1) != stage_name
    assert f"logic {effective_name};" not in generated
    assert generated == emit_systemverilog(_compile(source))
    rtl = tmp_path / "SafeCounter.sv"
    rtl.write_text(generated)
    lint_with_verilator((rtl,), "SafeCounter")


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_safe_falling_active_low_release_behavior(tmp_path: Path) -> None:
    source = SAFE_COUNTER.replace(
        "clock clk\n  async reset arst @clk",
        "clock clk { edge falling }\n  async reset arst_n @clk { "
        "polarity active_low }",
    )
    rtl = tmp_path / "SafeCounter.sv"
    rtl.write_text(emit_systemverilog(_compile(source)))
    bench = tmp_path / "tb.sv"
    bench.write_text(
        """
module tb;
  logic clk = 1, arst_n = 0;
  logic [7:0] x = 8'd5;
  logic [7:0] y;
  SafeCounter dut(.*);

  task falling_edge;
    begin
      #1 clk = 0; #1 clk = 1; #1;
    end
  endtask

  initial begin
    #1;
    if (y !== 0) $fatal(1, "active-low asynchronous assertion");
    arst_n = 1;
    falling_edge();
    if (y !== 0) $fatal(1, "first falling release edge");
    falling_edge();
    if (y !== 0) $fatal(1, "second falling release edge");
    falling_edge();
    if (y !== 5) $fatal(1, "third falling edge must restart state");
    #1 arst_n = 0; #1;
    if (y !== 0) $fatal(1, "between-edge active-low assertion");
    arst_n = 1; x = 8'd9;
    falling_edge();
    falling_edge();
    falling_edge();
    if (y !== 9) $fatal(1, "active-low release did not restart");
    $finish;
  end
endmodule
"""
    )
    obj = tmp_path / "obj_falling_low"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "-Wall", "-Wno-fatal", "--Mdir", str(obj), str(rtl), str(bench),
        ),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "CCACHE_DISABLE": "1"},
    )
    assert built.returncode == 0, built.stderr or built.stdout
    ran = subprocess.run(
        (str(obj / "Vtb"),), check=False, capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stderr or ran.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_safe_release_and_between_edge_reassertion(tmp_path: Path) -> None:
    rtl = tmp_path / "SafeCounter.sv"
    rtl.write_text(emit_systemverilog(_compile(SAFE_COUNTER)))
    bench = tmp_path / "tb.sv"
    bench.write_text(
        """
module tb;
  logic clk = 0, arst = 0;
  logic [7:0] x = 0;
  logic [7:0] y;
  SafeCounter dut(.clk(clk), .arst(arst), .x(x), .y(y));

  task clock_edge;
    begin
      #1 clk = 1; #1 clk = 0; #1;
    end
  endtask

  initial begin
    #1 arst = 1; #1;
    if (y !== 8'd0) $fatal(1, "async assertion failed");
    arst = 0; x = 8'd5;
    clock_edge();
    if (y !== 8'd0) $fatal(1, "first release edge was not held");
    clock_edge();
    if (y !== 8'd0) $fatal(1, "second release edge was not held");
    clock_edge();
    if (y !== 8'd5) $fatal(1, "third edge did not resume state");

    // Assertion is asynchronous with respect to clk.
    #1 arst = 1; #1;
    if (y !== 8'd0) $fatal(1, "between-edge assertion failed");
    arst = 0; x = 8'd9;
    clock_edge();
    // Reassert before release completes: the two-edge sequence restarts.
    #1 arst = 1; #1 arst = 0;
    clock_edge();
    if (y !== 8'd0) $fatal(1, "reasserted first edge was not held");
    clock_edge();
    if (y !== 8'd0) $fatal(1, "reasserted second edge was not held");
    clock_edge();
    if (y !== 8'd9) $fatal(1, "reasserted third edge did not resume state");
    $finish;
  end
endmodule
"""
    )
    obj = tmp_path / "obj"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "-Wall", "-Wno-fatal", "--Mdir", str(obj), str(rtl), str(bench),
        ),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "CCACHE_DISABLE": "1"},
    )
    assert built.returncode == 0, built.stderr
    run = subprocess.run(
        (str(obj / "Vtb"),), check=False, capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout
