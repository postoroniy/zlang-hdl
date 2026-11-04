# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Legal source names must not become bare SV gate/clocking keywords."""

import pytest

from tests.simulation.differential import run_differential
from tests.support.stdlib_formal import run
from zlang.backend.systemverilog import emit
from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.compiler import compile_source
from zlang.ir.formal import FormalStatus, ProofMode


@pytest.mark.parametrize("identifier", ("buf", "global", "cell", "nand", "nettype"))
def test_reserved_instance_keeps_semantic_path_and_executes(tmp_path, identifier):
    source = f"""module Child {{in x:u8 out y:u8 y=x}}
module Top {{in x:u8 out y:u8 {identifier}:Child {{x}} y={identifier}.y}}"""
    product = compile_source(source)
    node = product.ir.elaborated_instances[0]
    assert node.instance.name == identifier and node.semantic_path == (
        "Top",
        identifier,
    )
    rtl = emit(product.ir)
    assert f" zlang_{identifier} (" in rtl and f" {identifier} (" not in rtl
    assert rtl == emit(compile_source(source).ir)
    path = tmp_path / "instance.zhl"
    path.write_text(source)
    result = run_differential(
        path,
        top="Top",
        directory=tmp_path,
        events=[dict(set=dict(x=v)) for v in (0, 255, 128, 1)],
    )
    assert result.native == result.direct_sv


@pytest.mark.parametrize("identifier", ("buf", "global", "cell", "nand", "nettype"))
def test_reserved_port_collision_remains_fail_closed(identifier):
    product = compile_source(
        f"module T {{in {identifier},zlang_{identifier}:bit out y:bit y={identifier} ^ zlang_{identifier}}}"
    )
    with pytest.raises(SystemVerilogEmissionError, match="identifier collision"):
        emit(product.ir)


@pytest.mark.parametrize("identifier", ("buf", "global", "cell"))
@pytest.mark.parametrize("mutation", (False, True))
def test_reserved_port_and_instance_actual_rtl_proof(tmp_path, identifier, mutation):
    source = f"""module Child {{in {identifier}:u8 out y:u8 y={"~" if mutation else ""}{identifier}}}
module Top {{in x:u8 out y:u8 {identifier}:Child {{{identifier}=x}} y={identifier}.y}}"""
    rtl = emit(compile_source(source).ir)
    reference = """module contract(input clk,rst,input [7:0] x);
wire [7:0] y; Top dut(.x(x),.y(y)); always @* assert(y==x); endmodule"""
    result = run(
        rtl,
        reference,
        tmp_path / "proof",
        ProofMode.BMC if mutation else ProofMode.PROVE,
    )
    assert result.status is (
        FormalStatus.FAILED if mutation else FormalStatus.PROVEN
    ), result.reason


@pytest.mark.parametrize("mutation", (False, True))
def test_cell_register_reset_update_and_formal_locator(tmp_path, mutation):
    source = """module Top {
 clock clk reset rst
 in enable:bit in value:u8 out y:u8
 reg cell:u8=0
 when enable { cell <- value }
 y=cell
}"""
    product = compile_source(source)
    assert product.ir.registers[0].name == "cell"
    rtl = emit(product.ir)
    assert "logic [7:0] zlang_cell;" in rtl
    assert "zlang_cell <= " in rtl
    path = tmp_path / "cell.zhl"
    path.write_text(source)
    events = [
        dict(set=dict(enable=e, value=v), reset=dict(rst=bool(r)), edges=["clk"])
        for r, e, v in (
            (1, 1, 255),
            (0, 1, 128),
            (0, 0, 3),
            (1, 1, 42),
            (0, 1, 7),
            (0, 0, 255),
        )
    ]
    trace = run_differential(path, top="Top", directory=tmp_path, events=events)
    assert trace.native == trace.direct_sv
    contract = """module contract(input clk,rst,enable,input [7:0] value);
 wire [7:0] y;
 Top dut(.*);
 reg [7:0] expected=0;
 reg initialized=0;
 always @(posedge clk) begin
   if(rst) expected<=0; else if(enable) expected<=value;
   if(rst) initialized<=1;
 end
 always @* if(initialized) assert(y==expected);
 endmodule"""
    if mutation:
        rtl = rtl.replace("if (rst)", "if (1'b0)")
        assert rtl != emit(product.ir)
    result = run(
        rtl,
        contract,
        tmp_path / "proof",
        ProofMode.BMC if mutation else ProofMode.PROVE,
    )
    assert result.status is (
        FormalStatus.FAILED if mutation else FormalStatus.PROVEN
    ), result.reason


def test_complete_keyword_inventory_is_accepted_by_pinned_sv_lexer(tmp_path):
    from zlang.backend.identifiers import SYSTEMVERILOG_RESERVED, rtl_identifier
    from zlang.toolchain import lint_with_verilator

    # Exercise the common physical spelling through the actual target parser,
    # including keywords the ZLang grammar itself does not admit as names.
    words = sorted(SYSTEMVERILOG_RESERVED)
    assert {
        "cell",
        "bufif0",
        "endpackage",
        "global",
        "interconnect",
        "sync_reject_on",
        "uwire",
    } <= set(words)
    physical = [rtl_identifier(word) for word in words]
    assert len(set(physical)) == len(words)
    path = tmp_path / "keywords.sv"
    ports = ",".join("input wire " + word for word in physical)
    path.write_text(
        "module keywords("
        + ports
        + ",output wire y);\nassign y=^{"
        + ",".join(physical)
        + "};\nendmodule\n"
    )
    lint_with_verilator((path,), "keywords")
