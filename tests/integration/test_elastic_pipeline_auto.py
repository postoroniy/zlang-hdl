"""Cycle and real-backend validation of the bounded elastic pipeline ABI."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_formal_artifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.formal import FormalStatus
from zlang.simulate import simulate_cycles
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "elastic_pipeline_auto.zhl").read_text()


def _payload(value: int) -> dict[str, int]:
    return {
        "a": value,
        "b": 1,
        "c": 0,
        "d": 0,
        "e": 0,
        "f": 0,
        "g": 0,
        "h": 0,
    }


def _cycle(value: int = 0, *, valid: int = 1, ready: int = 1):
    return {
        "input": {"payload": _payload(value), "valid": valid},
        "output": {"ready": ready},
    }


def _module():
    return compile_source(SOURCE).ir


def _transfers(trace):
    return [
        (index, item["output"]["payload"])
        for index, item in enumerate(trace)
        if item["output"]["transfer"]
    ]


def test_simulator_has_exact_advance_latency_and_preserves_bubbles() -> None:
    cycles = [
        _cycle(valid=0),
        _cycle(5),
        _cycle(valid=0),
        _cycle(7),
        _cycle(valid=0),
        _cycle(valid=0),
        _cycle(valid=0),
        _cycle(valid=0),
    ]
    trace = simulate_cycles(
        _module(), cycles, reset=[True] + [False] * (len(cycles) - 1)
    )
    assert _transfers(trace) == [(4, 5), (6, 7)]


def test_simulator_globally_stalls_and_retires_while_replacing() -> None:
    cycles = [
        _cycle(valid=0),
        _cycle(1),
        _cycle(2),
        _cycle(3, ready=0),
        _cycle(4, ready=0),
        _cycle(4, ready=0),
        _cycle(4, ready=1),
        _cycle(valid=0),
        _cycle(valid=0),
        _cycle(valid=0),
    ]
    trace = simulate_cycles(
        _module(), cycles, reset=[True] + [False] * (len(cycles) - 1)
    )
    assert [trace[index]["output"] for index in (4, 5, 6)] == [
        {"payload": 1, "valid": 1, "transfer": 0},
        {"payload": 1, "valid": 1, "transfer": 0},
        {"payload": 1, "valid": 1, "transfer": 1},
    ]
    assert [trace[index]["input"]["ready"] for index in (4, 5, 6)] == [0, 0, 1]
    assert trace[6]["input"]["transfer"] == 1
    assert _transfers(trace) == [(6, 1), (7, 2), (8, 3), (9, 4)]


def test_simulator_reset_discards_a_partially_full_epoch() -> None:
    cycles = [
        _cycle(11),
        _cycle(12),
        _cycle(valid=0),
        _cycle(21),
        _cycle(valid=0),
        _cycle(valid=0),
        _cycle(valid=0),
    ]
    resets = [False, False, True, False, False, False, False]
    trace = simulate_cycles(_module(), cycles, reset=resets)
    assert trace[2]["input"]["ready"] == 0
    assert trace[2]["output"]["valid"] == 0
    assert _transfers(trace) == [(6, 21)]


HIERARCHY_SOURCE = SOURCE.replace(
    "module ElasticPipelineAuto",
    "module ElasticPipelineLeaf",
) + r"""

module ElasticPipelineParent {
    clock clk
    reset rst
    in input : rv<ElasticProductsInput>
    out output : rv<u19>

    inst pipe : ElasticPipelineLeaf
    connect input -> pipe.input
    connect pipe.output -> output
}
"""




BENCH = r"""
module tb;
  logic clk=0, rst=1;
  logic [7:0] input_payload_a=0, input_payload_b=1;
  logic [7:0] input_payload_c=0, input_payload_d=0;
  logic [7:0] input_payload_e=0, input_payload_f=0;
  logic [7:0] input_payload_g=0, input_payload_h=0;
  logic input_valid=0, input_ready;
  logic [18:0] output_payload;
  logic output_valid, output_ready=1;
  ElasticPipelineAuto dut(.*);

  task tick; begin #1 clk=1; #1 clk=0; end endtask
  task issue(input [7:0] value); begin
    input_payload_a=value; input_valid=1; tick;
  end endtask
  initial begin
    tick; rst=0;
    issue(8'd1); issue(8'd2);
    output_ready=0; issue(8'd3);
    input_payload_a=8'd4;
    tick;
    if (!output_valid || output_payload != 19'd1 || input_ready)
      $fatal(1,"elastic full stall mismatch");
    tick;
    if (!output_valid || output_payload != 19'd1 || input_ready)
      $fatal(1,"elastic payload changed under stall");
    output_ready=1; tick;
    if (!output_valid || output_payload != 19'd2 || !input_ready)
      $fatal(1,"simultaneous retire/replace mismatch");
    input_valid=0; tick;
    if (!output_valid || output_payload != 19'd3)
      $fatal(1,"third payload mismatch");
    tick;
    if (!output_valid || output_payload != 19'd4)
      $fatal(1,"replacement payload mismatch");
    input_payload_a=8'd9; input_valid=1; tick;
    input_valid=0; rst=1; tick;
    if (output_valid || input_ready) $fatal(1,"reset did not clear/block pipeline");
    rst=0; issue(8'd7); input_valid=0;
    repeat(2) tick;
    if (!output_valid || output_payload != 19'd7)
      $fatal(1,"first post-reset payload mismatch");
    $finish;
  end
endmodule
"""


def _run_verilator(
    rtl_files: tuple[Path, ...],
    root: Path,
    *,
    bench_text: str = BENCH,
) -> None:
    bench = root / "tb.sv"
    bench.write_text(bench_text)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    built = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "--top-module",
            "tb",
            *(str(path) for path in rtl_files),
            str(bench),
            "-Mdir",
            str(root / "obj"),
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


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_is_deterministic_and_cycle_exact(tmp_path: Path) -> None:
    module = _module()
    first = emit_experimental(module)
    second = emit_experimental(module)
    assert first == second
    assert first.count("assign zlang_elastic_advance =") == 1
    assert "else if (zlang_elastic_advance)" in first
    rtl = tmp_path / "ElasticPipelineAuto.sv"
    rtl.write_text(first)
    _run_verilator((rtl,), tmp_path)




@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("yosys", "sby", "z3")),
    reason="Yosys, SymbiYosys, and Z3 are required",
)
def test_existing_m35_ready_valid_stability_executes_on_direct_artifact() -> None:
    compiled = compile_source(SOURCE)
    recursive = build_recursive_formal_design(compiled.ir)
    artifact = emit_formal_artifact(compiled.ir, recursive)
    connected = connect_formal_design(compiled.formal_design, artifact)

    # This is the ordinary M35 ready/valid property set.  The elastic slice
    # adds no observation family and must not fall back to a handwritten
    # property or an unbound/non-executable report.
    assert len(connected.properties) == 2
    assert all(item.non_executable_reason is None for item in connected.properties)
    assert artifact.formal_observations
    payload_observation = next(
        item
        for item in artifact.formal_observations
        if item.semantic_binding_id.endswith(":port:input.payload")
    )
    assert payload_observation.physical_available
    assert payload_observation.observation_token is not None
    assert f"output logic [63:0] {payload_observation.observation_token}" in artifact.text

    harness = emit_harness(connected, depth=8)
    assert "non-executable property report" not in harness
    result = run_verilog_formal(
        harness,
        top="ElasticPipelineAuto__m35_formal",
        property_id="m35.connected.elastic-ready-valid",
        depth=8,
        systemverilog=True,
    )
    assert result.status is FormalStatus.BOUNDED_PASS
