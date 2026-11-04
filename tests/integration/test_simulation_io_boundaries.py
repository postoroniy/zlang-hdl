# SPDX-License-Identifier: Apache-2.0
"""Independent edge, field-layout, EOF and bounded-I/O contracts."""

import json

import pytest

from zlang import sim
from zlang.simulation_io_compare import compare_io_program
from zlang.simulation_values import SimulationRuntimeError


def run(tmp_path, source, records, events, *, logic=False, rtl=True):
    path = tmp_path / "design.zhl"
    path.write_text(source)
    vectors = tmp_path / "input.jsonl"
    vectors.write_text(records)
    program = sim.compile(path, logic_state=logic)
    output = tmp_path / "output.jsonl"
    if rtl:
        compare_io_program(
            program,
            events,
            simulation="io",
            io_inputs={"src": vectors},
            io_outputs={"dst": output},
            simulator="iverilog",
            artifact_directory=tmp_path / "rtl",
        )
    else:
        with program.create(
            simulation="io", io_inputs={"src": vectors}, io_outputs={"dst": output}
        ) as dut:
            dut.run_events(events)
    return [json.loads(line) for line in output.read_text().splitlines()]


def test_exclusive_read_sites_and_eof_logging(tmp_path):
    source = """module T {clock clk reset rst in a:u8 in select:bit out y:u8 y=a
simulation io @clk {
 when select {read src {v->a} else {drive a=0 write dst {v=y}}}
 when ~select {read src {v->a} else {drive a=0 write dst {v=y}}}
 write dst {v=y}
}}"""
    records = run(
        tmp_path,
        source,
        '{"v":7}\n{"v":9}\n',
        [
            {"set": {"select": 1}, "edges": ["clk"]},
            {"set": {"select": 0}, "edges": ["clk"]},
            {"edges": ["clk"]},
        ],
    )
    assert [int(r["values"]["v"]["bits"], 2) for r in records] == [7, 9, 0, 0]


def test_two_active_reads_reject_before_commit(tmp_path):
    source = """module T {clock clk reset rst in a:u8 out y:u8 y=a
simulation io @clk {read src {v->a} else {finish} read src {v->a} else {finish} write dst {v=y}}}"""
    with pytest.raises(SimulationRuntimeError, match="one simulation read"):
        run(tmp_path, source, '{"v":7}\n', [{"edges": ["clk"]}], rtl=False)
    assert (tmp_path / "output.jsonl").read_bytes() == b""


def test_write_guard_is_not_evaluated_before_file_inputs_settle(tmp_path):
    source = """module T {clock clk reset rst in a:bit reg noise:bit out y:bit y=0
simulation io @clk {read src {v->a} else {finish}
 when a | noise {write dst {v=a | noise}}}}"""
    records = run(tmp_path, source, '{"v":1}\n', [{"edges": ["clk"]}], logic=True)
    assert records[0]["values"]["v"]["bits"] == "1"


def test_finish_cancels_remaining_unresolved_guards(tmp_path):
    source = """module T {clock clk reset rst in a:bit reg noise:bit out y:bit y=0
simulation io @clk {read src {v->a} else {finish}
 when noise {drive a=0} write dst {v=y}}}"""
    # The drive would overlap the read: place it in the exclusive EOF branch.
    source = source.replace(
        "else {finish}\n when noise {drive a=0}", "else {finish when noise {drive a=0}}"
    )
    assert run(tmp_path, source, "", [{"edges": ["clk"]}], logic=True) == []


def test_nested_struct_and_strided_vector_layout(tmp_path):
    source = """struct S {left:u3 right:s5}
module T {clock clk reset rst in a:vec<2,S> out y:vec<2,S> y=a
simulation io @clk {read src {v->a} else {finish} write dst {v=y}}}"""
    value = {"bits": "1010111111000001"}
    records = run(
        tmp_path, source, json.dumps({"v": value}) + "\n", [{"edges": ["clk"]}]
    )
    assert records[0]["values"]["v"] == value


def test_packet_fields_use_existing_protocol_ownership(tmp_path):
    source = """module T {clock clk reset rst in rx:packet<u8> in other:packet<u8> out tx:packet<u8>
arbiter [rx,other] -> tx {policy round_robin grant packet}
simulation io @clk {read src {v->rx.payload valid->rx.valid last->rx.last} else {finish}
drive tx.ready=1 write dst {v=tx.payload last=tx.last transfer=tx.transfer}}}"""
    with pytest.raises(SimulationRuntimeError, match="unsupported for packet-arbiter"):
        run(tmp_path, source, '{"v":35,"valid":1,"last":1}\n', [{"edges": ["clk"]}])


def test_active_low_reset_uses_logical_assertion(tmp_path):
    source = """module T {clock clk reset rst_n @clk {mode synchronous polarity active_low power_up unspecified}
in a:u8 out reg y:u8=0 when ~rst_n {y <- a}
simulation io @clk {when ~rst_n {read src {v->a} else {finish} write dst {v=a}}}}"""
    # Reset names are logical observations in simulation blocks only.
    source = source.replace("when ~rst_n {y <- a}", "when 1 {y <- a}")
    records = run(
        tmp_path,
        source,
        '{"v":35}\n',
        [
            {"reset": {"rst_n": True}, "edges": ["clk"]},
            {"reset": {"rst_n": False}, "edges": ["clk"]},
        ],
    )
    assert [r["event"] for r in records] == [1]


@pytest.mark.parametrize(
    ("declaration", "type_", "value", "expected"),
    [
        ("struct Word {hi:u3 lo:s5}", "Word", {"hi": 5, "lo": -1}, "10111111"),
        ("union Word {Zero Some {value:u4}}", "Word", {"bits": "10011"}, "10011"),
    ],
)
def test_nominal_packed_values_in_native_and_rtl(
    tmp_path, declaration, type_, value, expected
):
    source = (
        declaration
        + f""" module T {{clock clk reset rst in a:{type_} out y:{type_} y=a
simulation io @clk {{read src {{v->a}} else {{finish}} write dst {{v=y}}}}}}"""
    )
    result = run(
        tmp_path, source, json.dumps({"v": value}) + "\n", [{"edges": ["clk"]}]
    )
    assert result[0]["values"]["v"]["bits"] == expected


def test_request_response_fields_follow_native_and_physical_abi(tmp_path):
    source = """struct Req {data:u8} struct Resp {data:u8}
module T {clock clk reset rst
interface bus:request_response<Req,Resp> {max_outstanding 1 ordering in_order}
in value:u8 in issue:bit
bus.request.payload=Req {data=value} bus.request.valid=issue bus.response.ready=1
simulation io @clk {read src {v->bus.response.payload valid->bus.response.valid ready->bus.request.ready} else {finish}
write dst {v=bus.response.payload.data transfer=bus.response.transfer}}}"""
    result = run(
        tmp_path,
        source,
        '{"v":{"data":11},"valid":0,"ready":1}\n{"v":{"data":22},"valid":0,"ready":1}\n{"v":{"data":35},"valid":1,"ready":1}\n',
        [
            {"reset": {"rst": True}, "edges": ["clk"]},
            {"reset": {"rst": False}, "set": {"issue": 1}, "edges": ["clk"]},
            {"set": {"issue": 0}, "edges": ["clk"]},
        ],
    )
    assert result[-1]["values"] == {
        "v": {"bits": "00100011"},
        "transfer": {"bits": "1"},
    }


def test_nominal_enum_observation_retains_encoding_and_validation(tmp_path):
    source = """enum Mode:bits<2> {Idle=0 Run=2}
module T {clock clk reset rst in a:bit out y:bit y=a
mode= a ? Mode.Run : Mode.Idle
simulation io @clk {read src {v->a} else {finish} write dst {v=mode}}}"""
    records = run(tmp_path, source, '{"v":1}\n', [{"edges": ["clk"]}])
    assert records[0]["values"]["v"]["bits"] == "10"
    from zlang.simulation_io_channels import decode_field
    from zlang.compiler import compile_source
    from zlang.semantic.errors import SemanticError

    # Existing top-level enum-input restriction remains explicit.
    with pytest.raises(SemanticError, match="cannot expose enum"):
        compile_source(
            source.replace("in a:bit", "in a:Mode")
            .replace("y=a", "y=0")
            .replace("mode= a ? Mode.Run : Mode.Idle", "mode=Mode.Idle")
        )
    type_ = (
        sim.compile(tmp_path / "design.zhl")
        .simulation_blocks[0]
        .commands[1]
        .bindings[0][1]
        .type
    )
    assert decode_field("Run", type_).to_bits() == "10"
    with pytest.raises(SimulationRuntimeError, match="valid code"):
        decode_field({"bits": "11"}, type_)


def test_local_callable_used_only_by_simulation_is_retained(tmp_path):
    source = """fn twice(x:u8)->u9 {x+x}
module T {clock clk reset rst in a:u8 out y:u8 y=a local=twice(a)
simulation io @clk {read src {v->a} else {finish} write dst {v=local}}}"""
    result = run(tmp_path, source, '{"v":7}\n', [{"edges": ["clk"]}])
    assert result[0]["values"]["v"]["bits"] == "000001110"
