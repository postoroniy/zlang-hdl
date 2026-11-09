# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Independent native/Direct-SV/formal controls for hierarchy observations."""

from hashlib import sha256
import itertools
import json
from pathlib import Path

import pytest

from tests.support.hierarchy_observation_cases import (
    GENERATED_LOCALS, delayed_observation_source, observation_source,
)
from zlang import sim
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.formal import run_verilog_formal
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.simulation_compare import compare_program


@pytest.mark.parametrize("width,signed", ((2, False), (8, False), (8, True)))
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
@pytest.mark.parametrize("clocked", (False, True))
def test_generated_locals_match_the_independent_lane_oracle(tmp_path, width, signed, simulator, clocked):
    type_ = ("s" if signed else "u") + str(width)
    path = tmp_path / "generated.zhl"
    source = GENERATED_LOCALS.replace("u8", type_)
    if clocked:
        source = source.replace("module GeneratedLocals {", "module GeneratedLocals {clock clk reset rst")
    path.write_text(source)
    values = range(4) if width == 2 else (-128, -1, 0, 1, 127) if signed else (0, 1, 127, 128, 255)
    pairs = list(itertools.product(values, repeat=2))
    if width == 8 and not signed:
        pairs.extend(((3, 9), (12, 201), (255, 0)))
    events = [{"edges": [], "set": {"samples": list(pair)}} for pair in pairs]
    mask = (1 << width) - 1
    expected = tuple({"results": (a & mask) | ((b & mask) << width)} for a, b in pairs)
    compared = compare_program(
        sim.compile(path), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == expected
    assert all(isinstance(value, int) for row in compared.rtl_trace for value in row.values())


def _observation_events(manual, delayed=False):
    events = [{"set": {"enable": 0, "consume": 0, "data": 0},
               "reset": {"rst": True}, "edges": ["clk"]}]
    initial = {"observed": 0, "observed_valid": 0, "observed_ready": 0, "fired": 0}
    if not manual:
        initial["accepted"] = 0
    if delayed:
        initial["other"] = 0
    expected = [initial]
    accepted = 0
    previous_ready = previous_enable = 0
    for cycle in range(49):
        values = {"enable": (cycle // 2) % 2, "consume": cycle % 3 != 0,
                  "data": (0, 1, 127, 128, 255, 19, 201)[cycle % 7]}
        values["consume"] = int(values["consume"])
        reset = cycle in (0, 13, 25, 48)
        for edges in ([], ["clk"]):
            if edges:
                accepted = 0 if reset else (accepted + (values["enable"] & values["consume"])) & 255
                previous_ready = 0 if reset else values["consume"]
                previous_enable = 0 if reset else values["enable"]
            ready = previous_ready if delayed else values["consume"]
            row = {"observed": values["data"], "observed_valid": values["enable"],
                   "observed_ready": ready,
                   "fired": values["enable"] & ready}
            if not manual:
                row["accepted"] = accepted
            if delayed:
                row["other"] = previous_enable
            events.append({"set": values, "reset": {"rst": reset}, "edges": edges})
            expected.append(row)
    return events, tuple(expected)


@pytest.mark.parametrize("manual", (False, True))
@pytest.mark.parametrize("logic_state", (False, True))
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_endpoint_observations_preserve_stalls_edges_reset_and_values(
    tmp_path, manual, logic_state, simulator,
):
    path = tmp_path / "observation.zhl"
    path.write_text(observation_source(manual=manual))
    events, expected = _observation_events(manual)
    compared = compare_program(
        sim.compile(path, logic_state=logic_state), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == expected
    assert all(isinstance(value, int) for row in compared.rtl_trace for value in row.values())


@pytest.mark.parametrize("logic_state", (False, True))
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_delayed_driver_observation_reuses_state_latency_and_reset(tmp_path, logic_state, simulator):
    path = tmp_path / "delayed_observation.zhl"
    path.write_text(delayed_observation_source())
    events, expected = _observation_events(manual=True, delayed=True)
    compared = compare_program(
        sim.compile(path, logic_state=logic_state), events, simulator=simulator,
        artifact_directory=tmp_path / "comparison", build_timeout=60, timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == expected
    assert all(isinstance(value, int) for row in compared.rtl_trace for value in row.values())


def test_generated_aliases_keep_unknown_bits_in_their_own_lane(tmp_path):
    path = tmp_path / "logic_generation.zhl"
    path.write_text(GENERATED_LOCALS)
    with sim.compile(path, logic_state=True).create() as machine:
        for bits in ("u0x1010101x0u010", "1111111100000000"):
            machine.set_logic("samples", bits)
            assert machine.eval()["results"] == sim.LogicVector.parse(bits, 16)


def test_manual_transfer_and_payload_keep_u_x_and_controlling_zero(tmp_path):
    path = tmp_path / "logic_observation.zhl"
    path.write_text(observation_source(manual=True))
    with sim.compile(path, logic_state=True).create() as machine:
        machine.set_logic("data", "u0x10101")
        machine.set_logic("enable", "x")
        machine.set("consume", 0)
        row = machine.eval()
        assert row["observed"] == sim.LogicVector.parse("u0x10101", 8)
        assert row["observed_valid"] == sim.LogicVector.parse("x", 1)
        assert row["fired"] == sim.LogicVector.parse("0", 1)
        machine.set("consume", 1)
        assert machine.eval()["fired"] == sim.LogicVector.parse("x", 1)


def _formal_text(kind, mutation=None):
    if kind.startswith("generated"):
        source = GENERATED_LOCALS
        clocked = kind == "generated-clocked"
        if clocked:
            source = source.replace("module GeneratedLocals {", "module GeneratedLocals {clock clk reset rst")
        if mutation == "reuse-first-lane":
            source = source.replace("chosen=samples[k]", "chosen=samples[0]")
        elif mutation == "swap-lanes":
            source = source.replace("chosen=samples[k]", "chosen=samples[1-k]")
        compiled = compile_source(source)
        artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
        clock_ports = ".clk(clk),.rst(rst)," if clocked else ""
        harness = f"""module contract(input clk,rst,input [15:0] samples);
          wire [15:0] results;
          {artifact.module} dut({clock_ports}.samples(samples),.results(results));
          always @* assert(results==samples);
        endmodule
        """
    else:
        manual = kind.startswith("manual")
        delayed = kind == "manual-delayed"
        source = delayed_observation_source() if delayed else observation_source(manual=manual)
        if mutation == "wrong-payload":
            source = source.replace("requester.bus.response.payload.value", "requester.bus.response.payload.complement")
        elif mutation == "wrong-transfer":
            source = source.replace("local_fired=peer.bus.response.transfer",
                                    "local_fired=peer.bus.response.valid | peer.bus.response.ready")
        elif mutation == "wrong-reset":
            source = source.replace("out reg accepted:u8=0", "out reg accepted:u8=1")
        elif mutation == "wrong-ready":
            source = source.replace("peer.bus.response.ready=consume", "peer.bus.response.ready=1")
        elif mutation == "extra-observer-delay":
            source = source.replace("local_observed_ready=peer.bus.response.ready",
                                    "local_observed_ready=delay<1>(peer.bus.response.ready)")
        elif mutation == "wrong-delayed-driver":
            source = source.replace("peer.bus.response.ready=delay<1>(consume)",
                                    "peer.bus.response.ready=delay<1>(enable)")
        compiled = compile_source(source)
        artifact = emit_artifact(compiled.ir, selected_ir_identity=compiled.selected_ir_identity)
        count_ports = "wire [7:0] accepted; reg [7:0] expected_count;" if not manual else ""
        count_connection = ",.accepted(accepted)" if not manual else ""
        count_update = ("if(rst) expected_count<=0; else if(enable && consume) expected_count<=expected_count+8'd1;"
                        if not manual else "")
        count_assert = "assert(accepted==expected_count);" if not manual else ""
        delay_ports = "wire other; reg expected_ready,expected_other;" if delayed else ""
        delay_connection = ",.other(other)" if delayed else ""
        delay_update = ("if(rst) begin expected_ready<=0;expected_other<=0;end "
                        "else begin expected_ready<=consume;expected_other<=enable;end"
                        if delayed else "")
        ready_reference = "expected_ready" if delayed else "consume"
        delay_assert = "assert(other==expected_other);" if delayed else ""
        harness = f"""module contract(input clk,rst,enable,consume,input [7:0] data);
          wire [7:0] observed; wire observed_valid,observed_ready,fired;
          {count_ports}
          {delay_ports}
          {artifact.module} dut(.clk(clk),.rst(rst),.enable(enable),.consume(consume),.data(data),
            .observed(observed),.observed_valid(observed_valid),.observed_ready(observed_ready),.fired(fired){count_connection}{delay_connection});
          reg started=0;
          always @(posedge clk) begin
            started<=1; if(!started) assume(rst);
            {count_update}
            {delay_update}
          end
          always @* if(started) begin
            assert(observed==data); assert(observed_valid==enable);
            assert(observed_ready=={ready_reference}); assert(fired==(enable && {ready_reference}));
            {count_assert}
            {delay_assert}
          end
        endmodule
        """
    if mutation == "assert-to-assume":
        harness = harness.replace("assert(", "assume(")
    return compiled, artifact, harness


def _prove(tmp_path, kind, mutation=None):
    compiled, artifact, harness = _formal_text(kind, mutation)
    expected_assertions = {"generated": 1, "generated-clocked": 1, "connected": 5,
                           "manual": 4, "manual-delayed": 5}[kind]
    if harness.count("assert(") != expected_assertions:
        raise ValueError("independent reference assertion inventory changed")
    source = artifact.text + "\n" + harness
    (tmp_path / "actual-rtl-and-reference.sv").write_text(source)
    result = run_verilog_formal(
        source, top="contract", property_id=f"hierarchy-observation.{kind}.{mutation or 'positive'}",
        mode=ProofMode.PROVE if mutation is None else ProofMode.BMC,
        depth=8, timeout_seconds=30, route="smtbmc", solver="z3",
        systemverilog=True, flatten=True, work_directory=tmp_path / "proof",
    )
    (tmp_path / "qualification.json").write_text(json.dumps({
        "selected_ir_identity": compiled.selected_ir_identity,
        "artifact_hash": artifact.artifact_hash,
        "source_sha256": sha256(source.encode()).hexdigest(),
        "harness_sha256": sha256(harness.encode()).hexdigest(),
        "checker_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "assertion_count": expected_assertions,
        "depth": 8, "timeout_seconds": 30,
        "status": result.status.value, "reason": result.reason,
    }, indent=2) + "\n")
    return result


@pytest.mark.parametrize("kind", ("generated", "generated-clocked", "connected", "manual", "manual-delayed"))
def test_actual_rtl_matches_independent_reference_unbounded(tmp_path, kind):
    result = _prove(tmp_path, kind)
    assert result.status is FormalStatus.PROVEN, (result.status, result.reason)


@pytest.mark.parametrize("kind,mutation", (
    ("generated", "reuse-first-lane"), ("generated", "swap-lanes"),
    ("connected", "wrong-payload"), ("connected", "wrong-reset"),
    ("manual", "wrong-transfer"),
    ("manual", "wrong-ready"),
    ("manual-delayed", "extra-observer-delay"),
    ("manual-delayed", "wrong-delayed-driver"),
))
def test_actual_rtl_mutations_have_counterexamples(tmp_path, kind, mutation):
    result = _prove(tmp_path, kind, mutation)
    assert result.status is FormalStatus.FAILED, (result.status, result.reason)
    assert result.counterexample is not None


def test_assertion_removal_never_qualifies_as_proven(tmp_path):
    with pytest.raises(ValueError, match="assertion inventory changed"):
        _prove(tmp_path, "manual", "assert-to-assume")
