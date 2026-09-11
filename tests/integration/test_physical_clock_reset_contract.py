from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog.emitter import (
    SystemVerilogEmissionError,
    emit as emit_systemverilog,
)
from zlang.ir.cdc import (
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.opt.lowering import lower, restore
from zlang.parser import ParseError, parse
from zlang.semantic import analyze
from zlang.simulate import simulate_cycles
from zlang.toolchain import lint_with_verilator


PHYSICAL = """
module PhysicalCounter {
  clock clk { edge falling }
  reset rst_n @clk {
    mode asynchronous
    polarity active_low
    power_up unspecified
  }
  in x : u8
  out y : u8
  reg q : u8 = 0
  q <- x
  y = q
}
"""


SAFE_ASYNC = """
module SafeAsyncCounter {
  clock clk
  async reset arst @clk
  in x : u8
  out y : u8
  reg q : u8 = 0
  q <- x
  y = q
}
"""


SAFE_ASYNC_FALLING_LOW = SAFE_ASYNC.replace(
    "clock clk\n  async reset arst @clk",
    "clock clk { edge falling }\n  async reset arst @clk { polarity active_low }",
).replace("arst @clk", "arst_n @clk").replace("module SafeAsyncCounter", "module SafeAsyncFallingLow")


def test_physical_domain_is_typed_and_canonical() -> None:
    syntax = parse(PHYSICAL)
    assert syntax.clock_physical[0].edge == "falling"
    assert syntax.reset_physical[0].mode == "asynchronous"
    module = analyze(syntax)
    domain = module.clock_domains[0]
    assert domain.edge is ClockEdge.FALLING
    assert domain.reset_mode is ResetMode.ASYNCHRONOUS
    assert domain.reset_polarity is ResetPolarity.ACTIVE_LOW
    assert domain.reset_release_mode is ResetReleaseMode.NATIVE
    assert domain.power_up is PowerUpPolicy.UNSPECIFIED
    assert domain.source_origin is not None
    assert restore(lower(module)) == module
    assert restore(lower(module)).clock_domains[0].source_origin == domain.source_origin


def test_legacy_domain_normalizes_to_frozen_defaults() -> None:
    module = analyze(parse(PHYSICAL.replace(
        "clock clk { edge falling }\n  reset rst_n @clk {\n"
        "    mode asynchronous\n    polarity active_low\n"
        "    power_up unspecified\n  }",
        "clock clk\n  reset rst_n @clk",
    )))
    assert module.clock_domains[0].is_legacy_default


@pytest.mark.parametrize(
    "declaration",
    (
        "clock clk { edge sideways } reset rst",
        "clock clk reset rst { mode immediate polarity active_high power_up unspecified }",
        "clock clk reset rst { mode synchronous polarity inverted power_up unspecified }",
        "clock clk reset rst { mode synchronous polarity active_high power_up magic }",
    ),
)
def test_invalid_physical_contract_values_are_rejected(declaration: str) -> None:
    with pytest.raises(ParseError):
        parse(f"module Bad {{ {declaration} }}")


def test_simulator_reset_input_remains_logical_assertion() -> None:
    module = analyze(parse(PHYSICAL))
    trace = simulate_cycles(
        module,
        ({"x": 5}, {"x": 9}, {"x": 11}, {"x": 13}),
        (False, True, False, False),
    )
    assert [item["y"] for item in trace] == [0, 0, 0, 11]


def test_direct_sv_uses_exact_physical_event_and_reset_control() -> None:
    generated = emit_systemverilog(analyze(parse(PHYSICAL)))
    assert "always_ff @(negedge clk or negedge rst_n)" in generated
    assert "if (!rst_n) begin" in generated


def test_direct_sv_scheduled_state_uses_typed_active_low_reset_polarity() -> None:
    source = """
module PhysicalScheduledFifo {
  clock clk { edge falling }
  reset rst_n @clk {
    mode asynchronous
    polarity active_low
    power_up unspecified
  }
  in push : bit
  in pop : bit
  in data : u8
  out ready : bit
  out valid : bit
  fifo queue : fifo<u8,2>
  when push { queue.push(data) }
  when pop { queue.pop() }
  ready = queue.ready
  valid = queue.valid
}
"""
    generated = emit_systemverilog(analyze(parse(source)))
    assert "assign queue_valid = rst_n && !queue_empty;" in generated
    assert "assign queue_ready = rst_n &&" in generated
    assert "assign rule_" in generated
    assert "_fire = rst_n &&" in generated
    assert "always_ff @(negedge clk or negedge rst_n)" in generated


def test_direct_sv_behavior_matches_falling_async_active_low(tmp_path: Path) -> None:
    if shutil.which("verilator") is None:
        pytest.skip("Verilator is required for physical-domain behavior")
    rtl = tmp_path / "PhysicalCounter.sv"
    rtl.write_text(emit_systemverilog(analyze(parse(PHYSICAL))))
    bench = tmp_path / "tb.sv"
    bench.write_text(
        """
module tb;
  logic clk = 1, rst_n = 1;
  logic [7:0] x = 0;
  logic [7:0] y;
  PhysicalCounter dut(.clk(clk), .rst_n(rst_n), .x(x), .y(y));
  initial begin
    #2 x = 8'd5; #1 clk = 0; #1;
    if (y !== 8'd5) $fatal(1, "falling edge update failed");
    #1 rst_n = 0; #1;
    if (y !== 8'd0) $fatal(1, "asynchronous active-low reset failed");
    rst_n = 1; x = 8'd9; #1 clk = 1; #1 clk = 0; #1;
    if (y !== 8'd9) $fatal(1, "post-reset update failed");
    $finish;
  end
endmodule
"""
    )
    obj = tmp_path / "obj"
    subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "-Wall", "-Wno-fatal", "--Mdir", str(obj), str(rtl), str(bench),
        ),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "CCACHE_DISABLE": "1"},
    )
    subprocess.run((str(obj / "Vtb"),), check=True, capture_output=True, text=True)
