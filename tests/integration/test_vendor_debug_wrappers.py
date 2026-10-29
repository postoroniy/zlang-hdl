# SPDX-License-Identifier: Apache-2.0
"""Private ABI/wiring checks; these do not simulate or qualify vendor IP."""

import json
from pathlib import Path
import re
import subprocess

import pytest

from zlang import sim
from zlang.backend.systemverilog import emit


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples/fpga_debug"
FUNCTIONAL = {"clk", "rst", "input_payload", "input_valid", "input_ready", "output_payload", "output_valid", "output_ready"}

# Input-only test monitor: assertions check wiring; it implements no capture,
# transport or vendor behavior and is never included in projected examples.
MONITOR = """module vendor_debug_ila(input clk,input [15:0] probe0,probe2,input [2:0] probe1);
always @(posedge clk) begin
 if (probe1[0] !== (probe1[2] & probe1[1])) $fatal(1,"probe packing");
end
endmodule
"""
BENCH = """module bench;
reg clk=0,rst=1,input_valid=0,output_ready=0;
reg [15:0] input_payload=0;
wire input_ready,output_valid;
wire [15:0] output_payload;
WRAPPER dut(.*);
task check;
begin
 #1;
 if (input_ready!==output_ready || output_valid!==input_valid || output_payload!==input_payload) $fatal(1,"functional");
 if (dut.debug_payload!==input_payload || dut.debug_valid!==input_valid || dut.debug_ready!==output_ready || dut.debug_transfer!==(input_valid & output_ready)) $fatal(1,"tap");
end
endtask
task edge_clock; begin #1; clk=1; #1; clk=0; end endtask
initial begin
 check; edge_clock; rst=0;
 input_payload=16'h1234; input_valid=1; check; edge_clock; check; edge_clock;
 if(dut.debug_count!==0) $fatal(1,"stall advanced count");
 output_ready=1; check; edge_clock;
 input_payload=16'habcd; check; edge_clock;
 input_valid=0; check;
 if(dut.debug_count!==2) $fatal(1,"count");
 $finish;
end
endmodule
"""


def test_runnable_jsonl_example():
    events = [json.loads(line) for line in (EXAMPLES / "vendor_debug.events.jsonl").read_text().splitlines()]
    expanded = [{k: v for k, v in row.items() if k != "repeat"} for row in events for _ in range(row.get("repeat", 1))]
    with sim.compile(EXAMPLES / "vendor_debug.zhl", top="VendorDebugDut").create() as machine:
        machine.run_events(expanded)
        assert machine.get("debug_count") == 2


@pytest.mark.parametrize("filename,top", (("amd_ila_top.sv", "AmdIlaTop"), ("intel_signaltap_top.sv", "IntelSignalTapTop")))
def test_wrapper_matches_compiler_physical_abi_and_wiring(tmp_path, filename, top):
    program = sim.compile(EXAMPLES / "vendor_debug.zhl", top="VendorDebugDut")
    wrapper = (EXAMPLES / filename).read_text()
    bindings = re.search(r"VendorDebugDut dut\s*\((.*?)\);", wrapper, re.S).group(1)
    assert set(re.findall(r"\.(\w+)\(", bindings)) == {p.external_name for p in program.module.top_physical_abi.leaves}
    ports = re.search(r"module \w+\s*\((.*?)\);", wrapper, re.S).group(1)
    assert set(re.findall(r"(?:input|output) wire (?:\[\d+:\d+\] )?(\w+)", ports)) == FUNCTIONAL
    abi = {p.external_name: p for p in program.module.top_physical_abi.leaves}
    for direction, msb, lsb, name in re.findall(r"(input|output) wire (?:\[(\d+):(\d+)\] )?(\w+)", ports):
        assert abi[name].direction.value == direction
        assert abi[name].width == (int(msb) - int(lsb) + 1 if msb else 1)
    debug = {}
    for msb, lsb, names in re.findall(r"\bwire\s+(?:\[(\d+):(\d+)\]\s+)?(debug_\w+(?:,\s*debug_\w+)*)\s*;", wrapper):
        debug.update({name.strip(): int(msb) - int(lsb) + 1 if msb else 1 for name in names.split(",")})
    assert debug == {name: leaf.width for name, leaf in abi.items() if name.startswith("debug_")}
    result = _run_wrapper(tmp_path, emit(program.module), wrapper, top)
    assert result.returncode == 0, result.stdout + result.stderr


def _run_wrapper(directory, rtl, wrapper, top):
    design = directory / "design.sv"
    design.write_text(rtl + wrapper + MONITOR + BENCH.replace("WRAPPER", top))
    output = directory / "sim.vvp"
    built = subprocess.run(["iverilog", "-g2012", "-s", "bench", "-o", str(output), str(design)], capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stderr
    return subprocess.run(["vvp", str(output)], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("first,second", (("debug_payload", "debug_count"), ("debug_valid", "debug_ready")))
def test_wrapper_miswiring_is_detected(tmp_path, first, second):
    rtl = emit(sim.compile(EXAMPLES / "vendor_debug.zhl", top="VendorDebugDut").module)
    wrapper = (EXAMPLES / "intel_signaltap_top.sv").read_text()
    for name in (first, second):
        assert f".{name}({name})" in wrapper
    wrapper = wrapper.replace(f".{first}({first})", f".{first}({second})").replace(f".{second}({second})", f".{second}({first})")
    result = _run_wrapper(tmp_path, rtl, wrapper, "IntelSignalTapTop")
    assert result.returncode != 0 and "FATAL" in result.stdout


def test_amd_wrapper_requires_real_vendor_ip(tmp_path):
    rtl = emit(sim.compile(EXAMPLES / "vendor_debug.zhl", top="VendorDebugDut").module)
    design = tmp_path / "missing_ip.sv"
    design.write_text(rtl + (EXAMPLES / "amd_ila_top.sv").read_text())
    result = subprocess.run(["iverilog", "-g2012", "-s", "AmdIlaTop", "-o", str(tmp_path / "missing.vvp"), str(design)], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "vendor_debug_ila" in result.stderr
