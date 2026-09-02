from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit as emit_clash
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.ir.hierarchy import build_hierarchy_index
from zlang.opt import lower, restore
from zlang.opt.lowering import CanonicalizationError
from zlang.semantic import SemanticError
from zlang.simulate import simulate_cycles
from zlang.toolchain import generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "rv_fifo_instance_array.zhl").read_text()


def _module():
    return compile_source(
        SOURCE, top="RvBufferedLaneArray", include_clash=False
    ).ir


def test_rv_fifo_array_semantic_canonical_and_physical_identity() -> None:
    module = _module()
    restored = restore(lower(module))
    hierarchy = build_hierarchy_index(module)

    assert restored == module
    assert [item.instance.name for item in module.elaborated_instances] == [
        "lane[0]", "lane[1]"
    ]
    assert len({item.instance_identity for item in module.elaborated_instances}) == 2
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 1
    assert all(len(child.fifos) == 1 for child in module.children)
    assert [item.physical_name for item in hierarchy.children_of((module.name,))] == [
        "lane[0]", "lane[1]"
    ]
    assert len(module.hierarchical_connections) == 4


def test_canonical_reused_fifo_specialization_rejects_different_depth() -> None:
    canonical = lower(_module())
    children = list(canonical.children)
    child = children[1]
    children[1] = replace(
        child,
        fifos=(replace(child.fifos[0], depth=child.fifos[0].depth + 1),),
    )

    with pytest.raises(
        CanonicalizationError,
        match="specialization identity .* incompatible typed .* content",
    ):
        restore(replace(canonical, children=tuple(children)))


def test_rv_fifo_array_simulator_has_exact_independent_trace() -> None:
    cycles = (
        {"input0":{"payload":0,"valid":0}, "input1":{"payload":0,"valid":0},
         "output0":{"ready":0}, "output1":{"ready":1}},
        {"input0":{"payload":0x10,"valid":1}, "input1":{"payload":0x20,"valid":1},
         "output0":{"ready":0}, "output1":{"ready":1}},
        {"input0":{"payload":0x11,"valid":1}, "input1":{"payload":0x21,"valid":1},
         "output0":{"ready":0}, "output1":{"ready":1}},
        {"input0":{"payload":0x12,"valid":1}, "input1":{"payload":0,"valid":0},
         "output0":{"ready":0}, "output1":{"ready":1}},
        {"input0":{"payload":0x12,"valid":1}, "input1":{"payload":0,"valid":0},
         "output0":{"ready":1}, "output1":{"ready":1}},
        {"input0":{"payload":0,"valid":0}, "input1":{"payload":0,"valid":0},
         "output0":{"ready":0}, "output1":{"ready":1}},
        {"input0":{"payload":0,"valid":0}, "input1":{"payload":0,"valid":0},
         "output0":{"ready":0}, "output1":{"ready":1}},
        {"input0":{"payload":0x30,"valid":1}, "input1":{"payload":0x40,"valid":1},
         "output0":{"ready":0}, "output1":{"ready":0}},
    )
    trace = simulate_cycles(
        _module(), cycles, reset=(True, False, False, False, False, False, True, False)
    )
    assert [item["input0"]["ready"] for item in trace] == [0,1,1,0,1,0,0,1]
    assert [item["output0"]["payload"] for item in trace] == [0,0,0x10,0x10,0x10,0x11,0,0]
    assert [item["output0"]["valid"] for item in trace] == [0,0,1,1,1,1,0,0]
    assert [item["output1"]["payload"] for item in trace] == [0,0,0x20,0x21,0,0,0,0]
    assert trace[3]["input1"]["ready"] == 1
    assert trace[4]["input0"]["transfer"] == 1  # full FIFO pop+push


def test_rv_fifo_array_artifacts_are_deterministic_and_structural() -> None:
    module = _module()
    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    clash = emit_clash(module)

    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert first.to_json() == second.to_json()
    assert BackendArtifact.from_json(first.to_json()).to_json() == first.to_json()
    assert first.text.count("module RvBufferedLane__") == 1
    assert first.text.count("RvBufferedLane__") == 3
    assert first.text.count("logic [1:0] queue_count;") == 1
    assert clash.count("protocol_rvBufferedLane ::") == 1
    assert clash.count("result = protocol_rvBufferedLane") == 2


def test_rv_fifo_array_rejects_multiple_fifo_resources() -> None:
    source = SOURCE.replace(
        "fifo queue : fifo<u8,2>",
        "fifo queue : fifo<u8,2>\n    fifo extra : fifo<u8,2>\n"
        "    extra.data=input.payload extra.push=0 extra.pop=0",
    )
    with pytest.raises(SemanticError, match="supports at most one FIFO resource"):
        compile_source(source, top="RvBufferedLaneArray", include_clash=False)


HARNESS = r'''
#include "VRvBufferedLaneArray.h"
#include "verilated.h"
static void tick(VRvBufferedLaneArray& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc,char**argv) {
  Verilated::commandArgs(argc,argv); VRvBufferedLaneArray d;
  d.input0_payload=0; d.input0_valid=0; d.input1_payload=0; d.input1_valid=0;
  d.output0_ready=0; d.output1_ready=1; d.rst=1; tick(d);
  if (d.input0_ready || d.input1_ready || d.output0_valid || d.output1_valid) return 1;
  d.rst=0; d.input0_payload=0x10; d.input0_valid=1;
  d.input1_payload=0x20; d.input1_valid=1; d.eval();
  if (!d.input0_ready || !d.input1_ready) return 2;
  tick(d); if (!d.output0_valid || d.output0_payload!=0x10 || d.output1_payload!=0x20) return 3;
  d.input0_payload=0x11; d.input1_payload=0x21; d.eval();
  if (!d.input0_ready || !d.input1_ready) return 4;
  tick(d); if (d.output0_payload!=0x10 || d.output1_payload!=0x21) return 5;
  d.input0_payload=0x12; d.input1_valid=0; d.eval();
  if (d.input0_ready || d.output0_payload!=0x10 || !d.output1_valid
      || d.output1_payload!=0x21 || !d.input1_ready) return 6;
  tick(d); if (d.output0_payload!=0x10) return 7; // stable while stalled
  d.output0_ready=1; d.eval();
  if (!d.input0_ready) return 8; // full lane accepts simultaneous pop/push
  tick(d); if (d.output0_payload!=0x11 || !d.output0_valid) return 9;
  d.input0_valid=0; d.output0_ready=0; tick(d);
  if (d.output0_payload!=0x11) return 10;
  d.rst=1; tick(d);
  if (d.input0_ready || d.input1_ready || d.output0_valid || d.output1_valid) return 11;
  d.rst=0; d.input0_payload=0x30; d.input0_valid=1;
  d.input1_payload=0x40; d.input1_valid=1; d.output1_ready=0; tick(d);
  if (!d.output0_valid || !d.output1_valid) return 12;
  return (d.output0_payload==0x30 && d.output1_payload==0x40) ? 0 : 13;
}
'''


def _run_verilator(tmp_path: Path, rtl: tuple[Path, ...]) -> None:
    harness = tmp_path / "test.cpp"
    harness.write_text(HARNESS)
    environment = os.environ.copy(); environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(tmp_path / "obj"),
            "--top-module", "RvBufferedLaneArray",
            *(str(path) for path in rtl), str(harness),
        ), cwd=tmp_path, env=environment, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(tmp_path / "obj" / "VRvBufferedLaneArray"),),
        capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_rv_fifo_array_direct_sv_strict_verilator_cycle_trace(tmp_path: Path) -> None:
    source = tmp_path / "RvBufferedLaneArray.sv"
    source.write_text(emit_sv_artifact(_module()).text)
    lint_with_verilator((source,), "RvBufferedLaneArray")
    _run_verilator(tmp_path, (source,))


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_rv_fifo_array_real_clash_strict_verilator_cycle_trace(tmp_path: Path) -> None:
    module = _module()
    rtl = generate_verilog(
        emit_clash(module), module.name, tmp_path / "rtl", CLASH_EXECUTABLE
    )
    lint_with_verilator(rtl, module.name)
    _run_verilator(tmp_path, tuple(rtl))
