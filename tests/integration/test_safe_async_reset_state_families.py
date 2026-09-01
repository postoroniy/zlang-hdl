"""Cross-family acceptance for async-assert/synchronized-release reset.

These tests deliberately use the public compiler, simulator, and backend
entrypoints.  They ensure that safe reset is a physical-domain contract shared
by every existing state family, rather than a counter-only emitter feature.
"""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash.emitter import emit as emit_clash
from zlang.backend.systemverilog.emitter import emit as emit_systemverilog
from zlang.compiler import compile_source
from zlang.simulate import (
    simulate_csr_cycles,
    simulate_cycles,
    simulate_request_response_cycles,
)
from zlang.toolchain import generate_verilog, lint_with_verilator


RULES = """
module SafeRuleState {
  clock clk
  async reset arst @clk
  in enable : bit
  out value : u8
  reg count : u8 = 0
  when enable { count <- truncate<8>(count + 1) }
  value = count
}
"""


FIFO_RV = """
module SafeReadyValidFifo {
  clock clk
  async reset arst @clk
  in rx : rv<u8>
  out tx : rv<u8>
  fifo queue : fifo<u8,2>
  queue.data = rx.payload
  queue.push = rx.transfer
  queue.pop = tx.transfer
  rx.ready = queue.ready
  tx.payload = queue.front
  tx.valid = queue.valid
}
"""


MEMORY = """
module SafeMemory {
  clock clk
  async reset arst @clk
  in read_address : u2
  in write_enable : bit
  in write_address : u2
  in write_data : u8
  out read_data : u8
  memory table : mem<u8,4> {
    read_latency 1
    collision write_first
  }
  table.read_address = read_address
  table.write_enable = write_enable
  table.write_address = write_address
  table.write_data = write_data
  read_data = table.read_data
}
"""


CSR = """
module SafeCsr {
  clock clk
  async reset arst @clk
  csr control @0 {
    CONTROL @0 {
      enable bit @0 rw = 0
      reserved bits<31> @31:1 reserved
    }
  }
}
"""


REQUEST_RESPONSE = """
module SafeRequester {
  clock clk
  async reset arst @clk
  interface mem : request_response<u8,u16> {
    max_outstanding 2
    ordering in_order
  }
  in request_payload : u8
  in issue : bit
  in accept : bit
  out response_payload : u16
  mem.request.payload = request_payload
  mem.request.valid = issue
  mem.response.ready = accept
  response_payload = mem.response.payload
}
"""


HIERARCHY = """
module SafeNestedChild {
  clock clk
  async reset arst @clk
  in enable : bit
  out value : u8
  reg count : u8 = 0
  when enable { count <- truncate<8>(count + 1) }
  value = count
}

module SafeNestedTop {
  clock clk
  async reset arst @clk
  in enable : bit
  out value : u8
  child : SafeNestedChild { enable }
  value = child.value
}
"""


FAMILIES = (
    ("SafeRuleState", RULES),
    ("SafeReadyValidFifo", FIFO_RV),
    ("SafeMemory", MEMORY),
    ("SafeCsr", CSR),
    ("SafeRequester", REQUEST_RESPONSE),
    ("SafeNestedTop", HIERARCHY),
)


BEHAVIOR_BENCHES = {
    "SafeReadyValidFifo": r"""
module tb;
  logic clk = 0, arst = 0;
  logic [7:0] rx_payload = 0;
  logic rx_valid = 0, tx_ready = 0;
  wire rx_ready, tx_valid;
  wire [7:0] tx_payload;
  SafeReadyValidFifo dut(.*);

  task tick;
    begin #1 clk = 1; #1 clk = 0; #1; end
  endtask

  initial begin
    #1 arst = 1; #1;
    if (tx_valid !== 0) $fatal(1, "FIFO async assertion");
    arst = 0;
    tick();
    if (tx_valid !== 0) $fatal(1, "FIFO first release edge");
    tick();
    if (tx_valid !== 0) $fatal(1, "FIFO second release edge");
    rx_payload = 8'h2a; rx_valid = 1;
    tick();
    if (tx_valid !== 1 || tx_payload !== 8'h2a)
      $fatal(1, "FIFO initial token");

    rx_valid = 0;
    #1 arst = 1; #1;
    if (tx_valid !== 0) $fatal(1, "FIFO mid-stream async clear");
    arst = 0;
    tick();
    if (tx_valid !== 0) $fatal(1, "FIFO restarted release edge one");
    tick();
    if (tx_valid !== 0) $fatal(1, "FIFO restarted release edge two");
    rx_payload = 8'h55; rx_valid = 1;
    tick();
    if (tx_valid !== 1 || tx_payload !== 8'h55)
      $fatal(1, "FIFO third-edge restart from empty");
    $finish;
  end
endmodule
""",
    "SafeMemory": r"""
module tb;
  logic clk = 0, arst = 0;
  logic [1:0] read_address = 0, write_address = 0;
  logic write_enable = 0;
  logic [7:0] write_data = 0;
  wire [7:0] read_data;
  SafeMemory dut(.*);

  task tick;
    begin #1 clk = 1; #1 clk = 0; #1; end
  endtask

  initial begin
    #1 arst = 1; #1;
    if (read_data !== 0) $fatal(1, "memory async assertion");
    arst = 0;
    tick();
    if (read_data !== 0) $fatal(1, "memory first release edge");
    tick();
    if (read_data !== 0) $fatal(1, "memory second release edge");
    write_data = 8'h2a; write_enable = 1;
    tick();
    if (read_data !== 8'h2a) $fatal(1, "memory initial write");

    write_enable = 0;
    #1 arst = 1; #1;
    if (read_data !== 0) $fatal(1, "memory mid-stream async clear");
    arst = 0;
    tick();
    if (read_data !== 0) $fatal(1, "memory restarted release edge one");
    tick();
    if (read_data !== 0) $fatal(1, "memory restarted release edge two");
    write_data = 8'h55; write_enable = 1;
    tick();
    if (read_data !== 8'h55)
      $fatal(1, "memory third-edge restart from cleared cells");
    $finish;
  end
endmodule
""",
    "SafeCsr": r"""
module tb;
  logic clk = 0, arst = 0;
  logic [31:0] addr = 0, wdata = 0;
  logic write = 0, read = 0;
  wire [31:0] rdata;
  wire ready;
  SafeCsr dut(.*);

  task tick;
    begin #1 clk = 1; #1 clk = 0; #1; end
  endtask

  task expect_enable(input logic expected, input integer tag);
    begin
      write = 0; read = 1; #1;
      if (ready !== 1 || rdata[0] !== expected)
        $fatal(1, "CSR state mismatch at %0d", tag);
      read = 0;
    end
  endtask

  initial begin
    #1 arst = 1; #1;
    expect_enable(0, 0);
    arst = 0;
    tick(); expect_enable(0, 1);
    tick(); expect_enable(0, 2);
    write = 1; wdata = 1;
    tick(); expect_enable(1, 3);

    #1 arst = 1; #1;
    expect_enable(0, 4);
    arst = 0;
    tick(); expect_enable(0, 5);
    tick(); expect_enable(0, 6);
    write = 1; wdata = 1;
    tick(); expect_enable(1, 7);
    $finish;
  end
endmodule
""",
    "SafeRequester": r"""
module tb;
  logic clk = 0, arst = 0;
  logic [7:0] request_payload = 8'h11;
  logic issue = 0, accept = 1;
  wire [15:0] response_payload;
  wire [7:0] mem_request_payload;
  wire mem_request_valid, mem_response_ready;
  logic mem_request_ready = 1;
  logic [15:0] mem_response_payload = 0;
  logic mem_response_valid = 0;
  SafeRequester dut(.*);

  task tick;
    begin #1 clk = 1; #1 clk = 0; #1; end
  endtask

  initial begin
    #1 arst = 1; #1;
    if (mem_request_valid !== 0) $fatal(1, "RR async assertion");
    arst = 0;
    tick();
    if (mem_request_valid !== 0) $fatal(1, "RR first release edge");
    tick();
    issue = 1;
    tick();
    if (mem_request_valid !== 1) $fatal(1, "RR first accepted request");
    tick();
    if (mem_request_valid !== 0) $fatal(1, "RR outstanding limit");

    #1 arst = 1; #1;
    if (mem_request_valid !== 0) $fatal(1, "RR mid-stream async clear");
    issue = 0; arst = 0;
    tick();
    if (mem_request_valid !== 0) $fatal(1, "RR restarted release edge one");
    tick();
    request_payload = 8'h55; issue = 1;
    tick();
    if (mem_request_valid !== 1 || mem_request_payload !== 8'h55)
      $fatal(1, "RR third-edge restart from empty ledger");
    tick();
    if (mem_request_valid !== 0) $fatal(1, "RR refilled ledger");
    $finish;
  end
endmodule
""",
    "SafeNestedTop": r"""
module tb;
  logic clk = 0, arst = 0, enable = 0;
  wire [7:0] value;
  SafeNestedTop dut(.*);

  task tick;
    begin #1 clk = 1; #1 clk = 0; #1; end
  endtask

  initial begin
    #1 arst = 1; #1;
    if (value !== 0) $fatal(1, "nested async assertion");
    arst = 0;
    tick();
    if (value !== 0) $fatal(1, "nested first release edge");
    tick();
    if (value !== 0) $fatal(1, "nested second release edge");
    enable = 1;
    tick();
    if (value !== 1) $fatal(1, "nested initial update");
    tick();
    if (value !== 2) $fatal(1, "nested independent child state");

    #1 arst = 1; #1;
    if (value !== 0) $fatal(1, "nested mid-stream async clear");
    enable = 0; arst = 0;
    tick();
    if (value !== 0) $fatal(1, "nested restarted release edge one");
    tick();
    if (value !== 0) $fatal(1, "nested restarted release edge two");
    enable = 1;
    tick();
    if (value !== 1) $fatal(1, "nested third-edge restart");
    $finish;
  end
endmodule
""",
}


def _module(source: str, top: str):
    return compile_source(source, top=top, include_clash=False).ir


def _idle_fifo(payload: int) -> dict[str, object]:
    return {
        "rx": {"payload": payload, "valid": 1},
        "tx": {"ready": 0},
    }


def _memory_cycle(data: int, *, write: int = 1) -> dict[str, int]:
    return {
        "read_address": 0,
        "write_enable": write,
        "write_address": 0,
        "write_data": data,
    }


def _csr_cycle(*, write: int = 0, value: int = 0) -> dict[str, int]:
    return {"addr": 0, "write": write, "wdata": value, "read": 0}


def _rr_cycle(payload: int) -> dict[str, object]:
    return {
        "request_payload": payload,
        "issue": 1,
        "accept": 1,
        "mem": {
            "request": {"ready": 1},
            "response": {"payload": 0, "valid": 0},
        },
    }


def test_rule_state_obeys_the_two_release_edge_contract() -> None:
    trace = simulate_cycles(
        _module(RULES, "SafeRuleState"),
        ({"enable": 1},) * 5,
        (True, False, False, False, False),
    )
    assert [cycle["value"] for cycle in trace] == [0, 0, 0, 0, 1]


def test_fifo_ready_valid_obeys_the_two_release_edge_contract() -> None:
    trace = simulate_cycles(
        _module(FIFO_RV, "SafeReadyValidFifo"),
        tuple(_idle_fifo(value) for value in (1, 3, 5, 7, 9)),
        (True, False, False, False, False),
    )
    assert [cycle["tx"]["valid"] for cycle in trace] == [0, 0, 0, 0, 1]
    assert trace[-1]["tx"]["payload"] == 7


def test_memory_obeys_the_two_release_edge_contract() -> None:
    trace = simulate_cycles(
        _module(MEMORY, "SafeMemory"),
        tuple(_memory_cycle(value) for value in (1, 3, 5, 7))
        + (_memory_cycle(0, write=0),),
        (True, False, False, False, False),
    )
    # Writes on the two release-hold edges are suppressed.  The third
    # deasserted edge commits 7, which appears through the one-cycle read port.
    assert [cycle["read_data"] for cycle in trace] == [0, 0, 0, 0, 7]


def test_csr_obeys_the_two_release_edge_contract() -> None:
    trace = simulate_csr_cycles(
        _module(CSR, "SafeCsr"),
        (
            _csr_cycle(write=1, value=1),
            _csr_cycle(write=1, value=1),
            _csr_cycle(write=1, value=1),
            _csr_cycle(write=1, value=1),
            _csr_cycle(),
        ),
        (True, False, False, False, False),
    )
    key = "control.CONTROL.enable"
    assert [cycle["state"][key] for cycle in trace] == [0, 0, 0, 0, 1]


def test_request_response_obeys_the_two_release_edge_contract() -> None:
    trace = simulate_request_response_cycles(
        _module(REQUEST_RESPONSE, "SafeRequester"),
        tuple(_rr_cycle(value) for value in (1, 3, 5, 7, 9)),
        (True, False, False, False, False),
    )
    assert [cycle["mem"]["request"]["valid"] for cycle in trace] == [
        0, 0, 0, 1, 1,
    ]
    assert [cycle["mem"]["outstanding"] for cycle in trace] == [0, 0, 0, 0, 1]


@pytest.mark.parametrize(("top", "source"), FAMILIES)
def test_direct_sv_is_deterministic_and_has_one_root_conditioner(
    top: str,
    source: str,
) -> None:
    module = _module(source, top)
    first = emit_systemverilog(module)
    second = emit_systemverilog(module)
    assert first == second
    assert first.count('(* ASYNC_REG = "TRUE" *)') == 1


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize(("top", "source"), FAMILIES)
def test_direct_sv_state_family_passes_strict_verilator_lint(
    top: str,
    source: str,
    tmp_path: Path,
) -> None:
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(emit_systemverilog(_module(source, top)))
    lint_with_verilator((rtl,), top)


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(("top", "source"), FAMILIES)
def test_real_clash_state_family_generates_and_lints(
    top: str,
    source: str,
    tmp_path: Path,
) -> None:
    module = _module(source, top)
    clash = emit_clash(module)
    assert clash.count("resetSynchronizer") == 1
    rtl = generate_verilog(clash, top, tmp_path / top, CLASH_EXECUTABLE)
    lint_with_verilator(rtl, top)


def _run_behavioral_verilator(
    rtl: tuple[Path, ...],
    bench_text: str,
    root: Path,
) -> None:
    bench = root / "tb.sv"
    bench.write_text(bench_text)
    object_dir = root / "obj"
    environment = {**os.environ, "CCACHE_DISABLE": "1"}
    built = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "--top-module",
            "tb",
            "--Mdir",
            str(object_dir),
            *map(str, rtl),
            str(bench),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    ran = subprocess.run(
        (str(object_dir / "Vtb"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0, ran.stderr or ran.stdout


@pytest.mark.parametrize("backend", ("direct_systemverilog", "clash"))
@pytest.mark.parametrize(
    ("top", "source"),
    tuple(
        (top, source)
        for top, source in FAMILIES
        if top in BEHAVIOR_BENCHES
    ),
)
def test_midstream_safe_reset_is_cycle_exact_in_real_rtl(
    backend: str,
    top: str,
    source: str,
    tmp_path: Path,
) -> None:
    if shutil.which("verilator") is None:
        pytest.skip("Verilator is required for reset behavior")
    if backend == "clash" and CLASH_EXECUTABLE is None:
        pytest.skip("real Clash 1.11 is required for Clash reset behavior")

    module = _module(source, top)
    root = tmp_path / backend / top
    root.mkdir(parents=True)
    if backend == "direct_systemverilog":
        path = root / f"{top}.sv"
        path.write_text(emit_systemverilog(module))
        rtl = (path,)
    else:
        assert CLASH_EXECUTABLE is not None
        rtl = generate_verilog(
            emit_clash(module), top, root / "rtl", CLASH_EXECUTABLE
        )
    _run_behavioral_verilator(rtl, BEHAVIOR_BENCHES[top], root)
