"""Standalone in-order requester/responder backend parity."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.ir.interfaces import RequestResponseRole
from zlang.opt import OptimizationStage, lower, restore
from zlang.simulate import simulate_request_response_cycles
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


SOURCE = r"""
struct Request { data : u8 }
struct Response { data : u8 }

module StandaloneRequester {
    clock clk
    reset rst
    in request_data : u8
    in issue : bit
    in consume : bit
    out response_data : u8
    interface mem : request_response<Request,Response> {
        max_outstanding 2
        ordering in_order
    }

    mem.request.payload = Request { data = request_data }
    mem.request.valid = issue
    mem.response.ready = consume
    response_data = mem.response.payload.data
}

module StandaloneResponder {
    clock clk
    reset rst
    in accept : bit
    in produce : bit
    in response_data : u8
    out accepted : bit
    interface mem : request_response<Request,Response> {
        max_outstanding 2
        ordering in_order
    }

    mem.request.ready = accept
    mem.response.payload = Response { data = response_data }
    mem.response.valid = produce
    accepted = mem.request.transfer
}
"""


def _module(name: str):
    return compile_source(SOURCE, top=name, include_clash=False).ir


def test_roles_and_canonical_links_are_exact() -> None:
    requester = _module("StandaloneRequester")
    responder = _module("StandaloneResponder")
    assert requester.request_responses[0].role is RequestResponseRole.REQUESTER
    assert responder.request_responses[0].role is RequestResponseRole.RESPONDER
    for module in (requester, responder):
        restored = restore(lower(module, stage=OptimizationStage.HIGH_LEVEL))
        assert restored == module
        interface = restored.request_responses[0]
        assert all(
            assignment.target is interface
            for assignment in restored.assignments
            if assignment.target.name == interface.name
        )


def test_role_aware_simulator_accounts_stalls_limits_and_same_cycle_response() -> None:
    requester = _module("StandaloneRequester")
    request_cycles = [
        {"request_data": 1, "issue": 1, "consume": 1,
         "mem": {"request": {"ready": 1},
                 "response": {"payload": {"data": 90}, "valid": 0}}},
        {"request_data": 2, "issue": 1, "consume": 1,
         "mem": {"request": {"ready": 1},
                 "response": {"payload": {"data": 91}, "valid": 0}}},
        {"request_data": 3, "issue": 1, "consume": 1,
         "mem": {"request": {"ready": 1},
                 "response": {"payload": {"data": 92}, "valid": 0}}},
        {"request_data": 4, "issue": 1, "consume": 1,
         "mem": {"request": {"ready": 1},
                 "response": {"payload": {"data": 93}, "valid": 1}}},
        {"request_data": 5, "issue": 1, "consume": 1,
         "mem": {"request": {"ready": 1},
                 "response": {"payload": {"data": 94}, "valid": 0}}},
    ]
    result = simulate_request_response_cycles(requester, request_cycles)
    assert [cycle["mem"]["request"]["transfer"] for cycle in result] == [1, 1, 0, 0, 1]
    assert [cycle["mem"]["response"]["transfer"] for cycle in result] == [0, 0, 0, 1, 0]
    assert [cycle["mem"]["outstanding"] for cycle in result] == [0, 1, 2, 2, 1]
    assert result[3]["response_data"] == 93

    # The unbuffered endpoint accepts a zero-latency response only alongside
    # the request it completes; the committed outstanding count stays zero.
    same_cycle = simulate_request_response_cycles(
        requester,
        [{"request_data": 7, "issue": 1, "consume": 1,
          "mem": {"request": {"ready": 1},
                  "response": {"payload": {"data": 99}, "valid": 1}}}],
    )[0]
    assert same_cycle["mem"]["request"]["transfer"] == 1
    assert same_cycle["mem"]["response"]["transfer"] == 1
    assert same_cycle["mem"]["outstanding"] == 0

    responder = _module("StandaloneResponder")
    responder_cycles = [
        {"accept": 1, "produce": 0, "response_data": 10,
         "mem": {"request": {"payload": {"data": 1}, "valid": 1},
                 "response": {"ready": 1}}},
        {"accept": 1, "produce": 0, "response_data": 11,
         "mem": {"request": {"payload": {"data": 2}, "valid": 1},
                 "response": {"ready": 1}}},
        {"accept": 1, "produce": 0, "response_data": 12,
         "mem": {"request": {"payload": {"data": 3}, "valid": 1},
                 "response": {"ready": 1}}},
        {"accept": 1, "produce": 1, "response_data": 13,
         "mem": {"request": {"payload": {"data": 4}, "valid": 1},
                 "response": {"ready": 0}}},
        {"accept": 1, "produce": 1, "response_data": 13,
         "mem": {"request": {"payload": {"data": 4}, "valid": 1},
                 "response": {"ready": 1}}},
    ]
    result = simulate_request_response_cycles(responder, responder_cycles)
    assert [cycle["mem"]["request"]["transfer"] for cycle in result] == [1, 1, 0, 0, 0]
    assert [cycle["mem"]["response"]["transfer"] for cycle in result] == [0, 0, 0, 0, 1]
    assert result[3]["mem"]["response"] == {
        "payload": {"data": 13}, "valid": 1, "transfer": 0,
    }


def test_backend_artifacts_are_deterministic_and_publish_role_aware_leaves() -> None:
    for name, expected_role in (
        ("StandaloneRequester", "requester"),
        ("StandaloneResponder", "responder"),
    ):
        module = _module(name)
        sv_first = emit_sv_artifact(module)
        sv_second = emit_sv_artifact(module)
        clash_first = emit_clash_artifact(module)
        clash_second = emit_clash_artifact(module)
        assert (sv_first.text, sv_first.artifact_hash) == (
            sv_second.text, sv_second.artifact_hash
        )
        assert (clash_first.text, clash_first.artifact_hash) == (
            clash_second.text, clash_second.artifact_hash
        )
        for artifact in (sv_first, clash_first):
            restored = type(artifact).from_json(artifact.to_json())
            # Manifests intentionally do not duplicate the emitted source
            # payload; identity and typed bindings must round-trip exactly.
            assert restored.artifact_hash == artifact.artifact_hash
            assert restored.selected_ir_identity == artifact.selected_ir_identity
            assert restored.bindings == artifact.bindings
            request_valid = next(
                item for item in artifact.bindings
                if item.semantic_signal_id == "port:mem.request.valid"
            )
            response_valid = next(
                item for item in artifact.bindings
                if item.semantic_signal_id == "port:mem.response.valid"
            )
            assert request_valid.protocol_role == expected_role
            assert response_valid.protocol_role == expected_role


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize("top", ("StandaloneRequester", "StandaloneResponder"))
def test_direct_sv_is_strict_lint_clean(top: str, tmp_path: Path) -> None:
    artifact = emit_sv_artifact(_module(top))
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(artifact.text)
    completed = subprocess.run(
        (
            "verilator", "--lint-only", "-Wall", "-Wno-DECLFILENAME",
            "-Wno-UNUSED", "-Wno-UNDRIVEN", "--top-module", top,
            str(rtl),
        ),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_responder_zero_latency_and_reset_behavior(tmp_path: Path) -> None:
    rtl = tmp_path / "StandaloneResponder.sv"
    rtl.write_text(emit_sv_artifact(_module("StandaloneResponder")).text)
    bench = tmp_path / "tb.sv"
    bench.write_text(r"""
module tb;
  logic clk=0, rst=1, accept=1, produce=1;
  logic [7:0] response_data=8'h5a;
  logic accepted;
  logic [7:0] mem_request_payload_data=8'h07;
  logic mem_request_valid=1, mem_request_ready;
  logic [7:0] mem_response_payload_data;
  logic mem_response_valid, mem_response_ready=1;
  StandaloneResponder dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    #1;
    if(mem_request_ready || mem_response_valid) $fatal(1,"reset did not mask endpoint");
    tick; rst=0; #1;
    if(!mem_request_ready || !mem_response_valid || !accepted)
      $fatal(1,"same-cycle request/response was not admitted");
    if(mem_response_payload_data != 8'h5a) $fatal(1,"response payload mismatch");
    tick; produce=0; mem_request_valid=0; #1;
    if(mem_response_valid) $fatal(1,"outstanding count changed on simultaneous transfer");
    rst=1; tick; #1;
    if(mem_request_ready || mem_response_valid) $fatal(1,"mid-stream reset not enforced");
    $finish;
  end
endmodule
""")
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator", "--binary", "--timing", "--top-module", "tb",
            str(rtl), str(bench), "-Mdir", str(tmp_path / "obj"),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    run = subprocess.run(
        (str(tmp_path / "obj" / "Vtb"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="Clash or Verilator unavailable",
)
@pytest.mark.parametrize("top", ("StandaloneRequester", "StandaloneResponder"))
def test_real_clash_1_11_generates_lint_clean_rtl(top: str, tmp_path: Path) -> None:
    module = _module(top)
    generated = generate_verilog(
        emit_clash(module), module.name, tmp_path / f"clash-{top}"
    )
    lint_with_verilator(generated, module.name)
