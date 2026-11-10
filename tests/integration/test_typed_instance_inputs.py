# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Independent native, actual-RTL and induction controls for typed port inputs."""

from hashlib import sha256
import json
import shutil
import subprocess

import pytest

from tests.support.typed_instance_inputs import sample, source
from zlang import sim
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulation_compare import compare_program


CASES = ((5, "u5", "wire"), (5, "s5", "wire"), (65, "u65", "wire"),
         (170, "u170", "wire"), (8, "fixed<8,3>", "wire"),
         (5, "s5", "registered"), (5, "u5", "rv"))


@pytest.mark.parametrize("width,type_,kind", CASES)
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
@pytest.mark.parametrize("logic_state", (False, True))
def test_native_and_direct_sv_match_an_independent_bit_oracle(
    tmp_path, width, type_, kind, simulator, logic_state,
):
    path = tmp_path / "port.zhl"
    path.write_text(source(width, type_, kind))
    mask = (1 << width) - 1
    events, expected = [], []
    state = 0
    for cycle in range(34):
        values = {"a": (((cycle * 19) & mask) << 17) | ((1 << 65) - 1) << (width + 17),
                  "b": ((mask - cycle) & mask) << 17 | 0x1ffff,
                  "held": (mask, 0, 1, 1 << (width - 1))[cycle % 4],
                  "choose": cycle % 2, "take": (cycle // 2) % 2,
                  "enable": int(cycle % 3 != 0), "consume": int(cycle % 5 != 0)}
        reset = cycle in (0, 13, 33)
        # Establish synchronous reset before comparing architectural state.
        for edges in ((["clk"],) if cycle == 0 else ([], ["clk"])):
            if kind == "registered" and edges:
                state = 0 if reset else sample(width, values) if values["enable"] else state
            events.append({"set": values, "reset": {"rst": reset}, "edges": edges})
            expected.append({"y": state if kind == "registered" else sample(width, values),
                             "ready": values["consume"], "valid": values["enable"]})
    compared = compare_program(
        sim.compile(path, logic_state=logic_state), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == tuple(expected)


def test_unknown_padding_does_not_change_the_selected_four_state_field(tmp_path):
    path = tmp_path / "four-state.zhl"
    path.write_text(source())
    compiled = compile_source(path.read_text())
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    bits = "u" * 65 + "10ux1" + "x" * 17
    with sim.compile(path, logic_state=True).create() as machine:
        machine.set_logic("a", bits)
        machine.set("b", 0)
        machine.set("held", 0)
        for name in ("choose", "take", "enable", "consume"):
            machine.set(name, 1)
        assert machine.eval()["y"] == sim.LogicVector.parse("10ux1", 5)
    tb = f"""module tb;
        reg clk=0,rst=0,choose=1,take=1,enable=1,consume=1;
        reg [86:0] a=87'b{bits.replace('u', 'x')},b=0;
        reg [4:0] held=0; wire [4:0] y; wire ready,valid;
        {artifact.module} dut(.*);
        initial begin #1; if(y !== 5'b10xx1) $fatal(1,"field U/X leaked"); $finish; end
    endmodule"""
    rtl = tmp_path / "four-state.sv"
    rtl.write_text(artifact.text + "\n" + tb)
    output = tmp_path / "four-state.vvp"
    for command in (("iverilog", "-g2012", "-s", "tb", "-o", str(output), str(rtl)),
                    ("vvp", str(output))):
        executable = shutil.which(command[0])
        assert executable, f"required frontend missing: {command[0]}"
        result = subprocess.run([executable, *command[1:]], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr


def _proof(tmp_path, width, type_, kind, mutation=None):
    compiled = compile_source(source(width, type_, kind))
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    rtl = artifact.text
    name = "child_input_payload" if kind == "rv" else "child_x"
    if mutation == "lost-high-bit":
        rtl = rtl.replace(f"[{width-1}:0] {name};", f"[{width-2}:0] {name};")
    elif mutation == "wrong-selector":
        rtl = rtl.replace(f"assign {name} =", f"assign {name} = ~")
    elif mutation == "wrong-ready":
        rtl = rtl.replace("assign ready = child_input_ready;", "assign ready = !child_input_ready;")
    elif mutation == "wrong-reset":
        rtl = rtl.replace(f"y <= {width}'d0;", f"y <= {width}'d1;")
    if mutation is not None:
        assert rtl != artifact.text, "mutation did not reach actual emitted RTL"
    reference = (f"reg [{width-1}:0] expected;\n"
                 "always @(posedge clk) if(rst) expected<=0; else if(enable) expected<=value;"
                 if kind == "registered" else f"wire [{width-1}:0] expected=value;")
    harness = f"""module contract(input clk,rst,choose,take,enable,consume,
        input [{width+81}:0] a,b,input [{width-1}:0] held);
        wire [{width-1}:0] y;
        wire ready,valid;
        {artifact.module} dut(.*);
        wire [{width-1}:0] value=take?(choose?a[{width+16}:17]:b[{width+16}:17]):held;
        {reference}
        reg started=0;
        always @(posedge clk) begin started<=1; if(!started) assume(rst); end
        always @* if(started) begin
            assert(y==expected); assert(ready==consume); assert(valid==enable);
        end
    endmodule"""
    assert harness.count("assert(") == 3
    source_text = rtl + "\n" + harness
    (tmp_path / "actual-rtl-and-reference.sv").write_text(source_text)
    result = run_verilog_formal(
        source_text, top="contract", property_id=f"typed-input.{type_}.{kind}.{mutation or 'positive'}",
        mode=ProofMode.PROVE if mutation is None else ProofMode.BMC,
        depth=8, timeout_seconds=30, route="smtbmc", solver="z3",
        systemverilog=True, flatten=True, work_directory=tmp_path / "proof",
    )
    (tmp_path / "receipt.json").write_text(json.dumps({
        "selected_ir_identity": compiled.selected_ir_identity,
        "artifact_hash": artifact.artifact_hash,
        "harness_sha256": sha256(harness.encode()).hexdigest(),
        "source_sha256": sha256(source_text.encode()).hexdigest(),
        "depth": 8, "timeout_seconds": 30,
        "status": result.status.value, "reason": result.reason,
    }, indent=2) + "\n")
    return result


@pytest.mark.parametrize("width,type_,kind", CASES)
def test_actual_rtl_port_boundary_is_proven(tmp_path, width, type_, kind):
    result = _proof(tmp_path, width, type_, kind)
    assert result.status is FormalStatus.PROVEN, (result.status, result.reason)


@pytest.mark.parametrize("kind,mutation", (
    ("wire", "lost-high-bit"), ("wire", "wrong-selector"),
    ("registered", "wrong-reset"), ("rv", "wrong-ready"),
))
def test_corrupt_port_bindings_have_counterexamples(tmp_path, kind, mutation):
    result = _proof(tmp_path, 5, "s5", kind, mutation)
    assert result.status is FormalStatus.FAILED, (result.status, result.reason)
    assert result.counterexample is not None


def _defaulted_struct_source(spelling):
    return (
        "struct Item<W=16,SW=W+1>{delta:sint<SW> data:uint<W> flag:bit} "
        "module DefaultedStruct{in value:" + spelling + " out data:u16 "
        "out widened:s18 out packed_value:bits<34> data=value.data "
        "widened=extend<18>(value.delta) packed_value=pack(value)}"
    )


@pytest.mark.parametrize("spelling", ("Item", "Item<16,17>"))
@pytest.mark.parametrize("logic_state", (False, True))
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_defaulted_struct_native_and_actual_rtl_match_exact_field_oracle(
    tmp_path, spelling, logic_state, simulator,
):
    path = tmp_path / "defaulted.zhl"
    path.write_text(_defaulted_struct_source(spelling))
    events, expected = [], []
    for index, delta in enumerate((-65536, -1, 0, 1, 65535)):
        for data in (0, 32768, 65535):
            flag = index % 2
            events.append({"set": {"value": {"delta": delta, "data": data, "flag": flag}},
                           "edges": []})
            expected.append({"data": data, "widened": delta & 0x3ffff,
                             "packed_value": ((delta & 0x1ffff) << 17) | (data << 1) | flag})
    result = compare_program(
        sim.compile(path, logic_state=logic_state), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30,
    )
    assert result.native_trace == result.rtl_trace == tuple(expected)


@pytest.mark.parametrize("spelling", ("Item", "Item<16,17>"))
def test_defaulted_struct_retains_exact_four_state_fields(tmp_path, spelling):
    path = tmp_path / "defaulted-logic.zhl"
    path.write_text(_defaulted_struct_source(spelling))
    delta, data, flag = "u" + "10x" * 4 + "0011", "10ux" * 4, "x"
    with sim.compile(path, logic_state=True).create() as machine:
        machine.set_logic("value", delta + data + flag)
        machine.eval()
        assert machine.get_logic("data").to_bits() == data
        assert machine.get_logic("widened").to_bits() == delta[0] + delta
        assert machine.get_logic("packed_value").to_bits() == delta + data + flag


@pytest.mark.parametrize("spelling,mutation", (
    ("Item", None), ("Item<16,17>", None),
    ("Item", "width"), ("Item", "sign"), ("Item", "field-order"),
))
def test_defaulted_struct_actual_rtl_field_relation_and_mutations(tmp_path, spelling, mutation):
    compiled = compile_source(_defaulted_struct_source(spelling))
    artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
    rtl = artifact.text
    replacements = {
        "width": ("assign data = zlang_packed_value[16:1];",
                  "assign data = {1'b0,zlang_packed_value[15:1]};"),
        "sign": ("assign widened = 18'($signed(zlang_packed_value[33:17]));",
                 "assign widened = {1'b0,zlang_packed_value[33:17]};"),
        "field-order": ("{value_delta, value_data, value_flag}",
                        "{value_data, value_delta, value_flag}"),
    }
    if mutation is not None:
        old, new = replacements[mutation]
        assert rtl.count(old) == 1
        rtl = rtl.replace(old, new)
        assert rtl != artifact.text
    harness = """module contract(input clk,input [16:0] value_delta,
        input [15:0] value_data,input value_flag);
        wire [15:0] data; wire signed [17:0] widened; wire [33:0] packed_value;
        DefaultedStruct dut(.value_delta(value_delta),.value_data(value_data),
            .value_flag(value_flag),.data(data),.widened(widened),.packed_value(packed_value));
        always @* begin
            assert(data==value_data);
            assert(widened=={value_delta[16],value_delta});
            assert(packed_value=={value_delta,value_data,value_flag});
        end
    endmodule"""
    assert harness.count("assert(") == 3 and "assume" not in harness
    text = rtl + "\n" + harness
    (tmp_path / "actual-rtl-and-reference.sv").write_text(text)
    result = run_verilog_formal(
        text, top="contract", property_id=f"defaulted-struct.{spelling}.{mutation or 'positive'}",
        mode=ProofMode.PROVE if mutation is None else ProofMode.BMC,
        depth=8, timeout_seconds=30, route="smtbmc", solver="z3",
        systemverilog=True, flatten=True, work_directory=tmp_path / "proof",
    )
    (tmp_path / "receipt.json").write_text(json.dumps({
        "selected_ir_identity": compiled.selected_ir_identity,
        "artifact_hash": artifact.artifact_hash, "harness_sha256": sha256(harness.encode()).hexdigest(),
        "source_sha256": sha256(text.encode()).hexdigest(), "depth": 8, "timeout_seconds": 30,
        "status": result.status.value, "reason": result.reason,
    }, indent=2) + "\n")
    expected = FormalStatus.PROVEN if mutation is None else FormalStatus.FAILED
    assert result.status is expected, (result.status, result.reason)
    if mutation is not None:
        assert result.counterexample is not None
