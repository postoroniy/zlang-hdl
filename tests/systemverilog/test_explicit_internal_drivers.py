from __future__ import annotations

from pathlib import Path
import re

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.backend.systemverilog.composed import rv_fifo_helper
from zlang.backend.systemverilog.target import _dsp48e1_simulation_model
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]

# A declaration initializer is not a continuous combinational driver.  Keep
# this guard deliberately narrow: public ``input wire``/``output wire`` ports
# and hand-written testbench/formal initialization remain legal and are not
# production module-scope ``wire name = expression`` declarations.
INITIALIZED_WIRE = re.compile(r"(?m)^[ \t]*wire\b[^;\n]*=")

GLOBAL_MASKED_MEMORY = """
module MaskedGlobalMemory {
  clock clk
  reset rst
  in address:u2
  in write_enable:bit
  in data:u16
  in mask:bits<2>
  out q:u16

  memory table:mem<u16,4> {
    read_latency 1
    collision write_first
  }
  table.read_address=address
  table.write_enable=write_enable
  table.write_address=address
  table.write_data=data
  table.write_mask=mask
  q=table.read_data
}
"""


def _emit_example(example: str, *, top: str | None = None) -> str:
    module = compile_source(
        (ROOT / "examples" / example).read_text(),
        top=top,
        include_clash=False,
    ).ir
    return emit_experimental(module)


def _assert_no_initialized_wires(text: str) -> None:
    assert INITIALIZED_WIRE.findall(text) == []


def _assert_explicit_driver(text: str, signal: str) -> None:
    declaration = re.compile(
        rf"(?m)^[ \t]*logic\b[^;\n]*\b{re.escape(signal)}\b[^;\n]*;"
    )
    continuous_driver = re.compile(
        rf"(?m)^[ \t]*assign[ \t]+{re.escape(signal)}[ \t]*="
    )
    procedural_driver = re.compile(
        rf"(?m)^[ \t]+{re.escape(signal)}[ \t]*="
    )
    assert declaration.search(text) is not None
    assert (
        continuous_driver.search(text) is not None
        or ("always_comb" in text and procedural_driver.search(text) is not None)
    )


@pytest.mark.parametrize(
    ("example", "top", "signals"),
    (
        (
            "fifo_bridge.zl",
            None,
            (
                "queue_front",
                "queue_valid",
                "queue_ready",
                "queue_push_request",
                "queue_pop_request",
                "queue_push",
                "queue_pop",
            ),
        ),
        (
            "fft_sdf_stage_atomic_transition.zl",
            "SDFStateStage",
            (
                "feedback_front",
                "feedback_empty",
                "feedback_full",
                "feedback_valid",
                "feedback_ready",
                "feedback_overflow",
                "feedback_underflow",
            ),
        ),
        (
            "packet_round_robin.zl",
            None,
            ("zlang_transfer", "zlang_grant_complete"),
        ),
        (
            "cdc_async_fifo.zl",
            None,
            ("zlang_push", "zlang_pop"),
        ),
        (
            "cdc_handshake.zl",
            None,
            ("zlang_source_transfer", "zlang_destination_transfer"),
        ),
    ),
)
def test_production_internal_combinational_signals_have_explicit_drivers(
    example: str,
    top: str | None,
    signals: tuple[str, ...],
) -> None:
    text = _emit_example(example, top=top)

    _assert_no_initialized_wires(text)
    for signal in signals:
        _assert_explicit_driver(text, signal)


def test_global_memory_mask_helpers_have_explicit_drivers() -> None:
    module = compile_source(GLOBAL_MASKED_MEMORY, include_clash=False).ir
    text = emit_experimental(module)

    _assert_no_initialized_wires(text)
    for signal in (
        "zlang_table_write_mask",
        "zlang_table_write_mask_expanded",
        "zlang_table_write_merged",
    ):
        _assert_explicit_driver(text, signal)


def test_composed_fifo_helper_has_explicit_drivers() -> None:
    text = rv_fifo_helper("zlang_test_fifo", width=8, depth=4)

    _assert_no_initialized_wires(text)
    _assert_explicit_driver(text, "push")
    _assert_explicit_driver(text, "pop")


def test_target_simulation_helper_has_explicit_drivers_and_keeps_wire_ports() -> None:
    text = _dsp48e1_simulation_model()

    _assert_no_initialized_wires(text)
    for signal in (
        "a_value",
        "d_value",
        "b_value",
        "preadd",
        "product_comb",
        "product",
        "product_extended",
        "result_value",
    ):
        _assert_explicit_driver(text, signal)

    assert "input wire [29:0] A" in text
    assert "output wire [47:0] P" in text


def test_source_form_guard_does_not_reject_ports_or_testbench_initialization() -> None:
    legal_non_production_source = """
module tb(
  input wire [7:0] stimulus,
  output wire [7:0] observed
);
  logic clk = 1'b0;
  logic [7:0] history;
  initial begin
    history = stimulus;
  end
  assign observed = history;
endmodule
"""

    _assert_no_initialized_wires(legal_non_production_source)
