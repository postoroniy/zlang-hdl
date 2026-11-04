# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from zlang import sim


SOURCE = """module Pilot {
clock clk reset rst
in a:u8
in enable:bit
out reg y:u8=0
when enable {y <- a}
simulation vectors @clk {
 when ~rst & enable {
  read stimulus {sample -> a} else {finish}
  write results {sample=a previous=y}
 }
}
}"""


def prepare(tmp_path, source=SOURCE, records='{"sample":7}\n{"sample":11}\n'):
    path = tmp_path / "pilot.zhl"
    path.write_text(source)
    vectors = tmp_path / "input.jsonl"
    vectors.write_text(records)
    return (
        sim.compile(path, top="Pilot"),
        {"stimulus": vectors},
        {"results": tmp_path / "output.jsonl"},
    )


def test_conditional_consumption_pre_edge_values_eof_and_reset(tmp_path):
    program, inputs, outputs = prepare(tmp_path)
    with program.create(
        simulation="vectors", io_inputs=inputs, io_outputs=outputs
    ) as dut:
        dut.set("enable", 1)
        dut.reset("rst", asserted=True)
        dut.edge("clk")
        dut.reset("rst", asserted=False)
        dut.edge("clk")
        assert dut.get("y") == 7
        dut.set("enable", 0)
        dut.run_cycles("clk", 4)
        dut.set("enable", 1)
        dut.run_cycles("clk", 4)
        assert dut.get("y") == 11 and dut._io.finished
    records = [json.loads(s) for s in outputs["results"].read_text().splitlines()]
    assert [r["event"] for r in records] == [1, 6]
    assert [
        [int(r["values"][f]["bits"], 2) for f in ["sample", "previous"]]
        for r in records
    ] == [[7, 0], [11, 7]]
    assert dut.simulation_io["status"] == "finished"


@pytest.mark.parametrize("value", ['{"bits":"01ux0011"}', '{"bits":"00000000"}'])
def test_exact_tagged_input_and_log(tmp_path, value):
    source = SOURCE.replace("out reg y:u8=0", "out reg y:u8=0")
    program, inputs, outputs = prepare(tmp_path, source, '{"sample":' + value + "}\n")
    # Recompile in logic mode through the ordinary public API.
    program = sim.compile(tmp_path / "pilot.zhl", top="Pilot", logic_state=True)
    with program.create(
        simulation="vectors", io_inputs=inputs, io_outputs=outputs
    ) as dut:
        dut.set("enable", 1)
        dut.edge("clk")
    result = json.loads(outputs["results"].read_text())
    assert result["values"]["sample"] == json.loads(value)


@pytest.mark.parametrize(
    "line",
    [
        '{"sample":256}',
        '{"sample":-1}',
        '{"sample":1,"extra":0}',
        "{}",
        '{"sample":{"bits":"1"}}',
        '{"sample":1,"sample":2}',
        "{invalid",
    ],
)
def test_bad_record_preserves_completed_logs_and_fails_closed(tmp_path, line):
    program, inputs, outputs = prepare(tmp_path, records='{"sample":7}\n' + line + "\n")
    with program.create(
        simulation="vectors", io_inputs=inputs, io_outputs=outputs
    ) as dut:
        dut.set("enable", 1)
        dut.edge("clk")
        before = outputs["results"].read_bytes()
        with pytest.raises((sim.SimulationRuntimeError, ValueError)):
            dut.edge("clk")
        assert outputs["results"].read_bytes() == before
        assert dut.get("a") == 7
        with pytest.raises(sim.SimulationRuntimeError, match="failed"):
            dut.edge("clk")
    assert dut.simulation_io["status"] == "failed"


def test_external_driver_and_existing_output_rejected(tmp_path):
    program, inputs, outputs = prepare(tmp_path)
    with program.create(
        simulation="vectors", io_inputs=inputs, io_outputs=outputs
    ) as dut:
        for call in [lambda: dut.set("a", 3), lambda: dut.set_packed("a", 3)]:
            with pytest.raises(sim.SimulationRuntimeError, match="conflicts"):
                call()
    before = outputs["results"].read_bytes()
    with pytest.raises(FileExistsError):
        program.create(simulation="vectors", io_inputs=inputs, io_outputs=outputs)
    assert outputs["results"].read_bytes() == before


def test_no_activation_opens_no_channels(tmp_path):
    program, _, outputs = prepare(tmp_path)
    with program.create() as dut:
        dut.set("a", 19)
        dut.set("enable", 1)
        dut.edge("clk")
        assert dut.get("y") == 19 and dut.simulation_io is None
    assert not outputs["results"].exists()


def test_failed_edge_commits_no_log_and_cannot_resume(tmp_path):
    program, inputs, outputs = prepare(
        tmp_path,
        SOURCE.replace(
            "simulation vectors", "assert zero @clk {a==0} simulation vectors"
        ),
    )
    with program.create(
        simulation="vectors", io_inputs=inputs, io_outputs=outputs
    ) as dut:
        dut.set("enable", 1)
        with pytest.raises(Exception, match="zero"):
            dut.edge("clk")
        assert outputs["results"].read_bytes() == b""
        assert dut.get("a") == 0
    assert dut.simulation_io["status"] == "failed"


def test_input_snapshot_is_frozen_at_activation(tmp_path):
    program, inputs, outputs = prepare(tmp_path)
    with program.create(
        simulation="vectors", io_inputs=inputs, io_outputs=outputs
    ) as dut:
        inputs["stimulus"].write_text('{"sample":99}\n')
        dut.set("enable", 1)
        dut.run_cycles("clk", 3)
        assert dut.get("y") == 11
    assert [
        int(json.loads(row)["values"]["sample"]["bits"], 2)
        for row in outputs["results"].read_text().splitlines()
    ] == [7, 11]


def test_log_limit_admission_precedes_hardware_edge(tmp_path, monkeypatch):
    import zlang.simulation_io_channels as channels

    program, inputs, outputs = prepare(tmp_path)
    with program.create(
        simulation="vectors", io_inputs=inputs, io_outputs=outputs
    ) as dut:
        monkeypatch.setattr(channels, "MAX_PLAN_BYTES", 1)
        dut.set("enable", 1)
        with pytest.raises(sim.SimulationRuntimeError, match="byte limit"):
            dut.edge("clk")
        assert dut.get("y") == 0 and dut.get("a") == 0
    assert outputs["results"].read_bytes() == b""


def test_oversized_snapshot_and_nonregular_file_rejected(tmp_path, monkeypatch):
    import zlang.simulation_io_channels as channels

    monkeypatch.setattr(channels, "MAX_PLAN_BYTES", 3)
    vector = tmp_path / "large"
    vector.write_bytes(b"1234")
    with pytest.raises(sim.SimulationRuntimeError, match="byte limit"):
        channels.snapshot_input(vector)
    import os

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(sim.SimulationRuntimeError, match="regular file"):
        channels.snapshot_input(fifo)


def test_metadata_close_failure_still_releases_native_and_is_idempotent(tmp_path):
    program, inputs, outputs = prepare(tmp_path)
    dut = program.create(simulation="vectors", io_inputs=inputs, io_outputs=outputs)

    class BadMetadata:
        def write(self, value):
            raise OSError("controlled metadata write failure")

        def flush(self):
            pass

    dut._io.channels._metadata = [BadMetadata()]
    with pytest.raises(OSError, match="controlled metadata"):
        dut.close()
    assert dut._closed and dut._io.closed
    assert dut.simulation_io["status"] == "failed"
    with pytest.raises(sim.SimulationRuntimeError, match="closed"):
        dut.eval()
    dut.close()
