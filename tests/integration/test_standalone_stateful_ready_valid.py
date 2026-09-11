"""A stateful ready/valid block is equally valid as child and selected top."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


SOURCE = r"""
module StatefulRv {
    clock clk
    reset rst
    in enable : bit
    in rx : rv<u8>
    out tx : rv<u8>
    out count : u8

    reg value : u8 = 0
    reg valid : bit = 0
    rx.ready = (valid == 0) | tx.ready
    tx.payload = value
    tx.valid = valid
    count = value

    rule accept when enable & rx.transfer {
        value <- rx.payload
        valid <- 1
    }
    rule retire when tx.transfer { valid <- 0 }
    priority accept > retire
}
"""


def test_top_semantics_retains_one_resolved_state_owner() -> None:
    module = compile_source(SOURCE).ir
    assert tuple(item.name for item in module.registers) == ("value", "valid")
    assert tuple(item.name for item in module.rules) == ("accept", "retire")
    assert module.resolved_transition is not None


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_stalls_transfers_and_resets() -> None:
    module = compile_source(SOURCE).ir
    rtl_text = emit_experimental(module)
    bench_text = r"""
module tb;
  logic clk=0,rst=1,enable=1;
  logic [7:0] rx_payload=0; logic rx_valid=0,rx_ready;
  logic [7:0] tx_payload; logic tx_valid,tx_ready=0;
  logic [7:0] count;
  StatefulRv dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    tick; rst=0;
    rx_payload=8'h2a; rx_valid=1; tick; rx_valid=0;
    if(!tx_valid || tx_payload!=8'h2a || count!=8'h2a)
      $fatal(1,"accepted value missing");
    repeat(2) begin tick; if(!tx_valid || tx_payload!=8'h2a)
      $fatal(1,"payload changed under stall"); end
    tx_ready=1; tick;
    if(tx_valid) $fatal(1,"transfer did not retire value");
    tx_ready=0; rx_payload=8'h55; rx_valid=1; tick; rx_valid=0;
    if(!tx_valid || tx_payload!=8'h55) $fatal(1,"second value missing");
    rst=1; tick;
    if(tx_valid || count!=0) $fatal(1,"reset did not clear state");
    $finish;
  end
endmodule
"""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / "StatefulRv.sv"
        bench = root / "tb.sv"
        rtl.write_text(rtl_text)
        bench.write_text(bench_text)
        environment = os.environ.copy()
        environment["CCACHE_DISABLE"] = "1"
        built = subprocess.run(
            (
                "verilator", "--binary", "--timing", "--top-module", "tb",
                str(rtl), str(bench), "-Mdir", str(root / "obj"),
            ),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert built.returncode == 0, built.stderr or built.stdout
        run = subprocess.run(
            (str(root / "obj" / "Vtb"),),
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr or run.stdout
