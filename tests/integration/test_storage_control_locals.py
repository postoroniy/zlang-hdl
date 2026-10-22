"""Ordinary storage-local bindings reach native and emitted RTL unchanged."""

import pytest

from tests.semantic.test_storage_control_locals import ROM, MEMORY
from tests.simulation.differential import run_differential
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from tests.support.rtl_state_observation import assert_readonly_cells


@pytest.mark.parametrize("text", (ROM, MEMORY))
def test_storage_alias_native_direct_sv(tmp_path, text):
    path = tmp_path / "top.zhl"
    path.write_text(text)
    events = []
    for cycle in range(48):
        values = {"a": cycle % 4}
        if "memory " in text:
            values.update(d=(cycle*31) % 256, write=int(cycle < 16 or cycle % 3 == 0))
        events.append({"edges": ["clk"], "reset": {"rst": cycle in (0, 25)}, "set": values})
    traces = run_differential(path, top="Top", directory=tmp_path, events=events)
    assert traces.native == traces.direct_sv


@pytest.mark.parametrize("mutation", (None, "address", "image"))
def test_rom_alias_actual_rtl_proven(tmp_path, mutation):
    artifact = emit_artifact(compile_source(ROM).ir)
    rtl = artifact.text
    assert "logic [7:0] zlang_table_cells [0:3];" in rtl
    # Readonly initialized memory is state in the SMT model. Assert the exact
    # four initialization invariants for induction; do not assume its contents
    # or substitute a free ROM output. Basecase checks the actual companion.
    rtl = assert_readonly_cells(rtl, module="Top", name="zlang_table_cells", width=8, words=range(4))
    if mutation == "address":
        assert "zlang_table_cells[a]" in rtl
        rtl = rtl.replace("zlang_table_cells[a]", "zlang_table_cells[0]", 1)
    images = {item.logical_path: item.text for item in artifact.companions}
    if mutation == "image":
        assert len(images) == 1
        name = next(iter(images))
        assert images[name].startswith("00000000\n")
        images[name] = "00000001\n"+images[name].split("\n", 1)[1]
    reference = """module contract(input clk,rst,input [1:0] a);
 wire [7:0] y; reg [7:0] expected; reg started=0;
 Top dut(.*);
 always @(posedge clk) begin started<=1; if(!started) assume(rst);
 if(started) assert(y==expected);
 if(rst) expected<=0; else expected<={6'b0,a};
 end endmodule"""
    result = run_verilog_formal(rtl+reference, top="contract",
        property_id="compiler.rom.local", mode=ProofMode.PROVE if mutation is None else ProofMode.BMC, depth=8,
        timeout_seconds=30, systemverilog=True, flatten=True, solver="z3",
        auxiliary_files=images,
        work_directory=tmp_path)
    assert result.status is (FormalStatus.PROVEN if mutation is None else FormalStatus.FAILED), result.reason
