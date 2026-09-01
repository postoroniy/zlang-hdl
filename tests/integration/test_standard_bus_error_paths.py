"""Source-authored standard-bus error propagation regressions.

The Python models are independent transaction-level oracles.  The executable
RTL witnesses below compile the ordinary ``stdlib/bus/*.zl`` modules through
the generic hierarchy/backend path; no bus behavior is supplied by a backend
dispatcher.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.standard_bus import (
    ApbInput,
    ApbToRegBus,
    Axi4LiteToRegBus,
    AxiAw,
    AxiLiteInput,
    AxiW,
    RegResponse,
    WishboneInput,
    WishboneToRegBus,
)


ERROR_TARGET = """
module ErrorTarget {
    clock clk
    reset rst
    interface regbus : RegBus<32,32>.responder @clk

    reg pending : bit = 0
    reg data : u32 = 0
    reg error : bit = 0

    regbus.request.ready = mux(pending, 0, 1)
    regbus.response.valid = pending
    regbus.response.payload = RegResponse{rdata=data error=error}

    pending <- mux(regbus.request.transfer, 1,
                   mux(regbus.response.transfer, 0, pending))
    data <- mux(regbus.request.transfer, regbus.request.payload.addr, data)
    error <- mux(regbus.request.transfer,
                 regbus.request.payload.addr == 4, error)
}
"""


def _source(bus: str) -> tuple[str, str]:
    if bus == "axi_lite":
        top = "AxiErrorTop"
        declaration = """
module AxiErrorTop {
    clock clk reset rst
    interface axi : AXI4Lite<32,32>.slave @clk
    inst frontend : AXI4LiteToRegBus<32,32>
    inst target : ErrorTarget
    connect axi -> frontend.axi
    connect frontend.regbus -> target.regbus
}
"""
    elif bus == "apb":
        top = "ApbErrorTop"
        declaration = """
module ApbErrorTop {
    clock clk reset rst
    interface apb : APB<32,32>.slave @clk
    inst frontend : APBToRegBus<32,32>
    inst target : ErrorTarget
    connect apb -> frontend.apb
    connect frontend.regbus -> target.regbus
}
"""
    else:
        top = "WishboneErrorTop"
        declaration = """
module WishboneErrorTop {
    clock clk reset rst
    interface wb : Wishbone<32,32>.slave @clk
    inst frontend : WishboneToRegBus<32,32>
    inst target : ErrorTarget
    connect wb -> frontend.wb
    connect frontend.regbus -> target.regbus
}
"""
    return (
        f"import std.bus.reg\nimport std.bus.{bus}\n"
        + ERROR_TARGET
        + declaration,
        top,
    )


def test_independent_oracles_preserve_error_completion_semantics() -> None:
    axi = Axi4LiteToRegBus(lambda request: RegResponse(request.addr, True))
    axi.step(AxiLiteInput(reset=True))
    write = axi.step(AxiLiteInput(
        aw_valid=True, aw=AxiAw(4), w_valid=True, w=AxiW(0x55),
    ))
    assert write.b_valid and write.b.resp == 2
    assert axi.step(AxiLiteInput(b_ready=False)).b == write.b
    assert not axi.step(AxiLiteInput(b_ready=True)).b_valid

    axi_read = Axi4LiteToRegBus(lambda request: RegResponse(0x12345678, True))
    axi_read.step(AxiLiteInput(reset=True))
    read = axi_read.step(AxiLiteInput(ar_valid=True, ar=AxiAw(4)))
    assert read.r_valid and read.r.data == 0x12345678 and read.r.resp == 2
    assert axi_read.step(AxiLiteInput(r_ready=False)).r == read.r

    apb = ApbToRegBus(lambda request: RegResponse(0xA5, True))
    apb.step(ApbInput(reset=True))
    apb.step(ApbInput(psel=True, penable=False, paddr=4))
    waiting = apb.step(ApbInput(psel=True, penable=True, pready=False))
    assert not waiting.pready and not waiting.pslverr
    complete = apb.step(ApbInput(psel=True, penable=True, pready=True))
    assert complete.pready and complete.pslverr and complete.prdata == 0xA5

    wishbone = WishboneToRegBus(lambda request: RegResponse(0x5A, True))
    wishbone.step(WishboneInput(reset=True))
    assert not wishbone.step(WishboneInput(cyc=True, stb=True, adr=4)).err
    assert not wishbone.step(WishboneInput(cyc=True)).err
    failed = wishbone.step(WishboneInput(cyc=True))
    assert failed.err and not failed.ack and failed.dat_r == 0x5A


def test_source_frontends_retain_typed_error_state_and_wiring() -> None:
    axi_source, axi_top = _source("axi_lite")
    axi = compile_source(axi_source, top=axi_top).ir
    frontend = next(child for child in axi.children
                    if child.name == "AXI4LiteToRegBus")
    assert {register.name for register in frontend.registers} >= {
        "b_pending", "b_resp", "r_pending", "r_data", "r_resp",
    }

    apb_source, apb_top = _source("apb")
    apb = compile_source(apb_source, top=apb_top).ir
    apb_frontend = next(child for child in apb.children
                        if child.name == "APBToRegBus")
    assert {assignment.target.name for assignment in apb_frontend.assignments} >= {
        "apb__pready", "apb__prdata", "apb__pslverr",
    }

    wishbone_source, wishbone_top = _source("wishbone")
    wishbone = compile_source(wishbone_source, top=wishbone_top).ir
    wb_frontend = next(child for child in wishbone.children
                       if child.name == "WishboneToRegBus")
    assert {assignment.target.name for assignment in wb_frontend.assignments} >= {
        "wb__ack", "wb__err", "wb__stall", "wb__dat_r",
    }


def _run_sv(source: str, top: str, testbench: str) -> None:
    module = compile_source(source, top=top).ir
    artifact = emit_sv_artifact(module, selected_ir_identity=f"bus-error:{top}")
    assert artifact.bindings
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / f"{top}.sv"
        bench = root / "tb.sv"
        rtl.write_text(artifact.text)
        bench.write_text(testbench)
        environment = os.environ.copy()
        environment["CCACHE_DISABLE"] = "1"
        built = subprocess.run(
            ("verilator", "--binary", "--timing", "--top-module", "tb",
             str(rtl), str(bench), "-Mdir", str(root / "obj")),
            cwd=root, env=environment, capture_output=True, text=True,
        )
        assert built.returncode == 0, built.stderr or built.stdout
        run = subprocess.run(
            (str(root / "obj" / "Vtb"),), cwd=root,
            capture_output=True, text=True,
        )
        assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(shutil.which("verilator") is None,
                    reason="Verilator is unavailable")
def test_axi_error_responses_are_held_through_backpressure() -> None:
    source, top = _source("axi_lite")
    _run_sv(source, top, r"""
module tb;
  logic clk=0,rst=1;
  logic [31:0] axi_aw_payload_addr=0,axi_w_payload_data=0;
  logic [3:0] axi_w_payload_strb=4'hf;
  logic axi_aw_valid=0,axi_aw_ready,axi_w_valid=0,axi_w_ready;
  logic [1:0] axi_b_payload_resp; logic axi_b_valid,axi_b_ready=0;
  logic [31:0] axi_ar_payload_addr=0; logic axi_ar_valid=0,axi_ar_ready;
  logic [31:0] axi_r_payload_data; logic [1:0] axi_r_payload_resp;
  logic axi_r_valid,axi_r_ready=0;
  AxiErrorTop dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  integer n;
  initial begin
    tick; tick; rst=0;
    axi_aw_payload_addr=4; axi_w_payload_data=32'h55;
    axi_aw_valid=1; axi_w_valid=1;
    tick; axi_aw_valid=0; axi_w_valid=0;
    n=0; while(!axi_b_valid && n<12) begin tick; n=n+1; end
    if(!axi_b_valid || axi_b_payload_resp!=2) $fatal(1,"missing SLVERR B");
    repeat(3) begin tick; if(!axi_b_valid || axi_b_payload_resp!=2)
      $fatal(1,"B response changed under stall"); end
    axi_b_ready=1; tick; axi_b_ready=0;

    axi_ar_payload_addr=4; axi_ar_valid=1;
    tick; axi_ar_valid=0;
    n=0; while(!axi_r_valid && n<12) begin tick; n=n+1; end
    if(!axi_r_valid || axi_r_payload_resp!=2 || axi_r_payload_data!=4)
      $fatal(1,"missing SLVERR R");
    repeat(3) begin tick; if(!axi_r_valid || axi_r_payload_resp!=2 ||
      axi_r_payload_data!=4) $fatal(1,"R response changed under stall"); end
    axi_r_ready=1; tick; $finish;
  end
endmodule
""")


@pytest.mark.skipif(shutil.which("verilator") is None,
                    reason="Verilator is unavailable")
@pytest.mark.parametrize(("bus", "top", "testbench"), (
    ("apb", "ApbErrorTop", r"""
module tb;
  logic clk=0,rst=1,apb_psel=0,apb_penable=0,apb_pwrite=0;
  logic [31:0] apb_paddr=0,apb_pwdata=0,apb_prdata;
  logic apb_pready,apb_pslverr;
  ApbErrorTop dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  integer n;
  initial begin
    tick; tick; rst=0; apb_psel=1; apb_paddr=4; tick;
    apb_penable=1; n=0;
    while(!apb_pready && n<12) begin tick; n=n+1; end
    if(!apb_pready || !apb_pslverr || apb_prdata!=4)
      $fatal(1,"APB error was not propagated");
    tick; $finish;
  end
endmodule
"""),
    ("wishbone", "WishboneErrorTop", r"""
module tb;
  logic clk=0,rst=1,wb_cyc=0,wb_stb=0,wb_we=0;
  logic [31:0] wb_adr=0,wb_dat_w=0,wb_dat_r; logic [3:0] wb_sel=4'hf;
  logic wb_ack,wb_err,wb_stall;
  WishboneErrorTop dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  integer n;
  initial begin
    tick; tick; rst=0; wb_cyc=1; wb_stb=1; wb_adr=4; tick; wb_stb=0;
    n=0; while(!wb_err && n<12) begin
      if(wb_ack && wb_err) $fatal(1,"ACK and ERR asserted together");
      tick; n=n+1;
    end
    if(!wb_err || wb_ack || wb_dat_r!=4)
      $fatal(1,"Wishbone error termination mismatch");
    tick; wb_cyc=0; tick;
    wb_cyc=1; wb_stb=1; wb_adr=0; tick; wb_stb=0;
    n=0; while(!wb_ack && n<12) begin
      if(wb_ack && wb_err) $fatal(1,"ACK and ERR asserted together");
      tick; n=n+1;
    end
    if(!wb_ack || wb_err) $fatal(1,"Wishbone normal ACK mismatch");
    $finish;
  end
endmodule
"""),
))
def test_source_bus_error_completion_rtl(
    bus: str, top: str, testbench: str,
) -> None:
    source, expected_top = _source(bus)
    assert expected_top == top
    _run_sv(source, top, testbench)
