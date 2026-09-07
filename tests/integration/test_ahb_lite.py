from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.simulate import simulate_cycles
from zlang.standard_bus import (
    AhbLiteInput,
    AhbLiteToRegBus,
    RegRequest,
    RegResponse,
)
from zlang.toolchain import (
    clash_subprocess_environment,
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


HARNESS = """
import std.bus.reg
import std.bus.ahb_lite

module ControlledRegTarget {
    clock hclk
    async reset hresetn @hclk { polarity active_low }
    interface regbus : RegBus<32,32>.responder @hclk
    in request_ready : bit
    in response_valid : bit
    in response_error : bit
    in response_data : u32
    out request_valid : bit = regbus.request.valid
    out request_addr : u32 = regbus.request.payload.addr
    out request_write : bit = regbus.request.payload.write
    out request_wdata : u32 = regbus.request.payload.wdata
    out request_wstrb : bits<4> = regbus.request.payload.wstrb

    regbus.request.ready = request_ready
    regbus.response.valid = response_valid
    regbus.response.payload = RegResponse{rdata=response_data error=response_error}
}

module AhbLiteHarness {
    clock hclk
    async reset hresetn @hclk { polarity active_low }
    interface ahb : AHBLite<32,32>.slave @hclk
    in request_ready : bit
    in response_valid : bit
    in response_error : bit
    in response_data : u32
    out request_valid : bit
    out request_addr : u32
    out request_write : bit
    out request_wdata : u32
    out request_wstrb : bits<4>

    frontend : AHBLiteToRegBus<32,32>
    target_model : ControlledRegTarget
    ahb -> frontend.ahb
    frontend.regbus -> target_model.regbus
    target_model.request_ready = request_ready
    target_model.response_valid = response_valid
    target_model.response_error = response_error
    target_model.response_data = response_data
    request_valid = target_model.request_valid
    request_addr = target_model.request_addr
    request_write = target_model.request_write
    request_wdata = target_model.request_wdata
    request_wstrb = target_model.request_wstrb
}
"""


def _width_harness(data_width: int) -> tuple[str, str]:
    top = f"AhbWidth{data_width}Harness"
    return top, f"""
import std.bus.ahb_lite
module {top} {{
    clock hclk
    async reset hresetn @hclk {{ polarity active_low }}
    interface ahb : AHBLite<32,{data_width}>.slave @hclk
    interface regbus : RegBus<32,{data_width}>.requester @hclk
    bridge : AHBLiteToRegBus<32,{data_width}>
    ahb -> bridge.ahb
    regbus -> bridge.regbus
}}
"""


def _bridge_ir():
    top = compile_source(
        (Path(__file__).resolve().parents[2] / "examples" / "ahb_csr_top.zhl").read_text(),
        top="AhbCsrTop",
        include_clash=False,
    ).ir
    return top.children[0]


def _bridge_inputs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ahb__HSEL": 0,
        "ahb__HADDR": 0,
        "ahb__HWRITE": 0,
        "ahb__HTRANS": 0,
        "ahb__HSIZE": 2,
        "ahb__HBURST": 0,
        "ahb__HPROT": 0,
        "ahb__HMASTLOCK": 0,
        "ahb__HWDATA": 0,
        "ahb__HREADY": 1,
        "regbus__request": {"ready": 0},
        "regbus__response": {
            "valid": 0,
            "payload": {"rdata": 0, "error": 0},
        },
    }
    values.update(overrides)
    return values


AHB_BACKEND_PARITY_TB = r"""
module tb;
  logic hclk=0, hresetn=0;
  logic ahb_HSEL=0, ahb_HWRITE=0, ahb_HMASTLOCK=0, ahb_HREADY=1;
  logic [31:0] ahb_HADDR=0, ahb_HWDATA=0, ahb_HRDATA;
  logic [1:0] ahb_HTRANS=0;
  logic [2:0] ahb_HSIZE=2, ahb_HBURST=0;
  logic [3:0] ahb_HPROT=0;
  logic ahb_HREADYOUT, ahb_HRESP;
  logic request_ready=0, response_valid=0, response_error=0;
  logic [31:0] response_data=0;
  logic request_valid, request_write;
  logic [31:0] request_addr, request_wdata;
  logic [3:0] request_wstrb;
  AhbLiteHarness dut(.*);
  task tick; begin #1 hclk=1; #1 hclk=0; end endtask
  initial begin
    tick; hresetn=1; tick; tick; tick;
    if (!ahb_HREADYOUT || ahb_HRESP) $fatal(1,"release did not reach idle");

    ahb_HSEL=1; ahb_HTRANS=2; ahb_HADDR=32'h24; ahb_HWRITE=1;
    ahb_HWDATA=32'h11111111; tick;
    ahb_HREADY=0; ahb_HADDR=32'h28; ahb_HWDATA=32'ha5a55a5a;
    request_ready=1;
    #1;
    if (!request_valid || request_addr!=32'h24 || !request_write ||
        request_wdata!=32'ha5a55a5a || request_wstrb!=4'hf)
      $fatal(1,"address/data phase mismatch");
    tick; request_ready=0;
    if (request_valid || ahb_HREADYOUT) $fatal(1,"response wait missing");

    response_valid=1; response_data=32'h76543210; ahb_HREADY=1;
    #1;
    if (!ahb_HREADYOUT || ahb_HRESP || ahb_HRDATA!=32'h76543210)
      $fatal(1,"successful completion mismatch");
    tick; response_valid=0; ahb_HSEL=0; ahb_HTRANS=0; ahb_HREADY=0;
    if (!request_valid || request_addr!=32'h28)
      $fatal(1,"held next address was not captured once");

    hresetn=0; #1;
    if (!ahb_HREADYOUT || ahb_HRESP || request_valid)
      $fatal(1,"asynchronous reset assertion mismatch");
    tick; hresetn=1; tick; tick; tick; ahb_HREADY=1;

    ahb_HSEL=1; ahb_HTRANS=3; ahb_HADDR=32'h30; ahb_HSIZE=1; tick;
    ahb_HSEL=0; ahb_HTRANS=0; ahb_HREADY=0;
    if (request_valid || ahb_HREADYOUT || !ahb_HRESP)
      $fatal(1,"local ERROR first cycle mismatch");
    tick; ahb_HREADY=1; ahb_HSEL=1; ahb_HTRANS=2;
    ahb_HSIZE=2; ahb_HADDR=32'h34;
    if (request_valid || !ahb_HREADYOUT || !ahb_HRESP)
      $fatal(1,"local ERROR second cycle mismatch");
    tick;
    ahb_HSEL=0; ahb_HTRANS=0; ahb_HREADY=0;
    if (!request_valid || request_addr!=32'h34)
      $fatal(1,"address after ERROR was not accepted exactly once");
    $finish;
  end
endmodule
"""


def _run_backend_parity_tb(files: tuple[Path, ...], root: Path) -> None:
    testbench = root / "tb.sv"
    testbench.write_text(AHB_BACKEND_PARITY_TB)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL", "-Wno-UNDRIVEN",
            *(str(item) for item in files), str(testbench),
            "-Mdir", str(root / "obj"),
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


def test_source_bridge_runs_in_generic_semantic_simulator_with_async_release() -> None:
    address_during_release = _bridge_inputs(
        ahb__HSEL=1, ahb__HADDR=0x40, ahb__HWRITE=1, ahb__HTRANS=2,
    )
    cycles = [
        _bridge_inputs(),
        address_during_release,
        address_during_release,
        _bridge_inputs(),
        _bridge_inputs(
            ahb__HSEL=1, ahb__HADDR=4, ahb__HWRITE=1,
            ahb__HTRANS=2, ahb__HWDATA=0x11111111,
        ),
        _bridge_inputs(
            ahb__HWDATA=0xA5A55A5A,
            regbus__request={"ready": 1},
        ),
        _bridge_inputs(),
        _bridge_inputs(
            regbus__response={
                "valid": 1,
                "payload": {"rdata": 0x12345678, "error": 0},
            },
        ),
        _bridge_inputs(
            ahb__HSEL=1, ahb__HADDR=8, ahb__HTRANS=2,
        ),
        _bridge_inputs(),
        _bridge_inputs(),
        _bridge_inputs(),
        _bridge_inputs(
            ahb__HSEL=1, ahb__HADDR=0x0C, ahb__HTRANS=2,
        ),
        _bridge_inputs(ahb__HWDATA=0, regbus__request={"ready": 1}),
    ]
    resets = [True, False, False, False, False, False, False, False,
              False, True, False, False, False, False]
    trace = simulate_cycles(_bridge_ir(), cycles, reset=resets)

    # The attempted address phases on the two synchronized-release edges are
    # suppressed.  A real request appears only after the third edge.
    assert all(item["regbus__request"]["valid"] == 0 for item in trace[:5])
    request = trace[5]["regbus__request"]
    assert request == {
        "payload": {
            "addr": 4,
            "write": 1,
            "wdata": 0xA5A55A5A,
            "wstrb": 0xF,
        },
        "valid": 1,
        "transfer": 1,
    }
    assert trace[7]["ahb__HRDATA"] == 0x12345678
    assert trace[7]["ahb__HREADYOUT"] == 1

    # Reset asserted in the following Request phase immediately restores the
    # idle observable state, and the two release edges again accept nothing.
    assert trace[9]["ahb__HREADYOUT"] == 1
    assert trace[9]["regbus__request"]["valid"] == 0
    assert trace[10]["regbus__request"]["valid"] == 0
    assert trace[11]["regbus__request"]["valid"] == 0
    assert trace[13]["regbus__request"]["valid"] == 1


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize("data_width", (8, 64, 1024))
def test_non_default_ahb_widths_emit_strict_direct_sv(
    data_width: int, tmp_path: Path,
) -> None:
    top, source = _width_harness(data_width)
    module = compile_source(source, top=top, include_clash=False).ir
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(emit_artifact(module).text)
    lint_with_verilator((rtl,), top)


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="Clash or Verilator unavailable",
)
def test_non_default_ahb_width_emits_real_clash(tmp_path: Path) -> None:
    top, source = _width_harness(64)
    result = compile_source(source, top=top)
    executable = find_clash_executable()
    assert executable is not None
    files = generate_verilog(result.clash, top, tmp_path / "clash", executable)
    lint_with_verilator(files, top)


def test_independent_ahb_oracle_preserves_phasing_waits_and_errors() -> None:
    requests: list[RegRequest] = []

    def access(request: RegRequest) -> RegResponse:
        requests.append(request)
        return RegResponse(0xCAFE0000 | request.addr, request.addr == 0x0C)

    bridge = AhbLiteToRegBus(access, response_latency=2)
    assert bridge.step(AhbLiteInput(reset=True)).hreadyout

    # The address-phase HWDATA is deliberately different.  Only the following
    # data-phase value may enter the RegBus request.
    address = bridge.step(AhbLiteInput(
        hsel=True, haddr=4, hwrite=True, htrans=2, hsize=2,
        hwdata=0x11111111,
    ))
    assert address.hreadyout and not address.hresp
    data = bridge.step(AhbLiteInput(hwdata=0xA5A55A5A))
    assert not data.hreadyout
    assert data.reg_request == RegRequest(4, True, 0xA5A55A5A, 0xF)
    assert requests == [data.reg_request]
    assert not bridge.step(AhbLiteInput()).hreadyout
    assert not bridge.step(AhbLiteInput()).hreadyout
    completed = bridge.step(AhbLiteInput())
    assert completed.hreadyout and completed.hrdata == 0xCAFE0004
    assert requests == [data.reg_request]

    # Local errors and downstream errors both use two response cycles.
    bridge.step(AhbLiteInput(hsel=True, haddr=2, htrans=2, hsize=2))
    first = bridge.step(AhbLiteInput())
    second = bridge.step(AhbLiteInput())
    assert (first.hreadyout, first.hresp) == (False, True)
    assert (second.hreadyout, second.hresp) == (True, True)

    bridge.step(AhbLiteInput(hsel=True, haddr=0x0C, htrans=2, hsize=2))
    bridge.step(AhbLiteInput(hwdata=0))
    bridge.step(AhbLiteInput())
    bridge.step(AhbLiteInput())
    first = bridge.step(AhbLiteInput())
    second = bridge.step(AhbLiteInput())
    assert (first.hreadyout, first.hresp) == (False, True)
    assert (second.hreadyout, second.hresp) == (True, True)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_source_ahb_bridge_rtl_address_data_phasing_stalls_and_error() -> None:
    module = compile_source(HARNESS, top="AhbLiteHarness", include_clash=False).ir
    artifact = emit_artifact(module, selected_ir_identity="ahb-lite-rtl")
    assert artifact.text.count('(* ASYNC_REG = "TRUE" *)') == 1
    testbench = r"""
module tb;
  logic hclk=0, hresetn=0;
  logic ahb_HSEL=0, ahb_HWRITE=0, ahb_HMASTLOCK=0, ahb_HREADY=1;
  logic [31:0] ahb_HADDR=0, ahb_HWDATA=0, ahb_HRDATA;
  logic [1:0] ahb_HTRANS=0;
  logic [2:0] ahb_HSIZE=2, ahb_HBURST=0;
  logic [3:0] ahb_HPROT=0;
  logic ahb_HREADYOUT, ahb_HRESP;
  logic request_ready=0, response_valid=0, response_error=0;
  logic [31:0] response_data=0;
  logic request_valid, request_write;
  logic [31:0] request_addr, request_wdata;
  logic [3:0] request_wstrb;
  AhbLiteHarness dut(.*);
  task tick; begin #1 hclk=1; #1 hclk=0; end endtask
  initial begin
    tick; tick; hresetn=1; tick; tick; tick;
    if (!ahb_HREADYOUT || ahb_HRESP) $fatal(1,"reset did not return idle");

    // Address/control phase: HWDATA here must not be captured.
    ahb_HSEL=1; ahb_HTRANS=2; ahb_HADDR=32'h4; ahb_HWRITE=1;
    ahb_HWDATA=32'h11111111; tick;
    ahb_HSEL=0; ahb_HTRANS=0; ahb_HWDATA=32'hdeadc0de;
    if (!request_valid || request_addr!=4 || !request_write ||
        request_wdata!=32'hdeadc0de || request_wstrb!=4'hf)
      $fatal(1,"write data phase was not preserved");
    repeat(2) begin
      tick;
      if (!request_valid || ahb_HREADYOUT || request_wdata!=32'hdeadc0de)
        $fatal(1,"request did not remain stable while stalled");
    end
    request_ready=1; tick; request_ready=0;
    if (request_valid || ahb_HREADYOUT) $fatal(1,"response wait missing");

    // Successful completion overlaps the next address phase.
    response_valid=1; response_data=32'h12345678;
    ahb_HSEL=1; ahb_HTRANS=2; ahb_HADDR=32'h8; ahb_HWRITE=0;
    #1;
    if (!ahb_HREADYOUT || ahb_HRESP || ahb_HRDATA!=32'h12345678)
      $fatal(1,"success response incorrect");
    tick;
    response_valid=0; ahb_HSEL=0; ahb_HTRANS=0;
    if (!request_valid || request_addr!=8 || request_write)
      $fatal(1,"back-to-back address was not captured");
    request_ready=1; tick; request_ready=0;

    // A target error is low-ready/high-response then high-ready/high-response.
    response_valid=1; response_error=1;
    #1;
    if (ahb_HREADYOUT || !ahb_HRESP) $fatal(1,"first ERROR cycle incorrect");
    tick;
    response_valid=0; response_error=0;
    if (!ahb_HREADYOUT || !ahb_HRESP) $fatal(1,"second ERROR cycle incorrect");
    tick;
    if (!ahb_HREADYOUT || ahb_HRESP) $fatal(1,"ERROR did not return idle");

    // A misaligned address is rejected locally with the same two-cycle shape.
    ahb_HSEL=1; ahb_HTRANS=2; ahb_HADDR=2; ahb_HSIZE=2; tick;
    ahb_HSEL=0; ahb_HTRANS=0;
    if (ahb_HREADYOUT || !ahb_HRESP) $fatal(1,"local ERROR first cycle incorrect");
    tick;
    if (!ahb_HREADYOUT || !ahb_HRESP) $fatal(1,"local ERROR second cycle incorrect");
    tick;

    // IDLE and BUSY HTRANS values do not create requests.
    ahb_HSEL=1; ahb_HTRANS=0; ahb_HADDR=16; tick;
    ahb_HTRANS=1; tick;
    if (request_valid || ahb_HRESP) $fatal(1,"inactive HTRANS was accepted");
    $finish;
  end
endmodule
"""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / "AhbLiteHarness.sv"
        tb = root / "tb.sv"
        rtl.write_text(artifact.text)
        tb.write_text(testbench)
        environment = os.environ.copy()
        environment["CCACHE_DISABLE"] = "1"
        built = subprocess.run(
            (
                "verilator", "--binary", "--timing", "--top-module", "tb",
                "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL", "-Wno-UNDRIVEN",
                str(rtl), str(tb), "-Mdir", str(root / "obj"),
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


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="Clash or Verilator unavailable",
)
def test_ahb_source_runs_cycle_identically_in_both_rtl_backends() -> None:
    result = compile_source(HARNESS, top="AhbLiteHarness")
    executable = find_clash_executable()
    assert executable is not None
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        direct_root = root / "direct"
        direct_root.mkdir()
        direct_rtl = direct_root / "AhbLiteHarness.sv"
        direct_rtl.write_text(
            emit_artifact(
                result.ir, selected_ir_identity="ahb-lite-parity",
            ).text
        )
        _run_backend_parity_tb((direct_rtl,), direct_root)

        clash_root = root / "clash"
        clash_root.mkdir()
        source_path = clash_root / "AhbLiteHarness.hs"
        output = root / "verilog"
        output.mkdir()
        source_path.write_text(result.clash)
        completed = subprocess.run(
            (
                executable,
                "--verilog",
                str(source_path),
                "-outputdir",
                str(output),
            ),
            env=clash_subprocess_environment(executable),
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        files = tuple(output.rglob("*.v"))
        assert files
        lint_with_verilator(files, "AhbLiteHarness")
        _run_backend_parity_tb(files, clash_root)
