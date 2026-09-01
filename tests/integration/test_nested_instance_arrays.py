"""Bounded nested same-domain compile-time instance arrays."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit as emit_clash
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.ir.hierarchy import build_hierarchy_index
from zlang.ir.recursive_formal import build_recursive_formal_design
from zlang.opt import lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate, simulate_cycles
from zlang.toolchain import generate_verilog, lint_with_verilator


COMBINATIONAL_SOURCE = r"""
module AddOne {
    in x:u8 out y:u8
    y=truncate<8>(x+1)
}
module PairAddOne {
    in x:vec<2,u8> out y:vec<2,u8>
    inst inner[2]:AddOne
    generate(i in 0..2) { inner[i].x=x[i] }
    y=generate(i in 0..2) inner[i].y
}
module NestedCombinationalArray {
    in x:bits<32> out y:bits<32>
    x_matrix:vec<2,vec<2,u8>>=bitcast<vec<2,vec<2,u8>>>(x)
    inst outer[2]:PairAddOne
    generate(i in 0..2) { outer[i].x=x_matrix[i] }
    y_matrix:vec<2,vec<2,u8>>=generate(i in 0..2) outer[i].y
    y=bitcast<bits<32>>(y_matrix)
}
"""


SEQUENTIAL_SOURCE = r"""
module CounterLane {
    clock clk reset rst
    in enable:bit out value:u8
    reg count:u8=0
    when enable { count <- truncate<8>(count+1) }
    value=count
}
module CounterPair {
    clock clk reset rst
    in enable:vec<2,bit> out value:vec<2,u8>
    inst inner[2]:CounterLane
    generate(i in 0..2) { inner[i].enable=enable[i] }
    value=generate(i in 0..2) inner[i].value
}
module NestedSequentialArray {
    clock clk reset rst
    in enable:bits<4> out value:bits<32>
    enable_matrix:vec<2,vec<2,bit>>=
        bitcast<vec<2,vec<2,bit>>>(enable)
    inst outer[2]:CounterPair
    generate(i in 0..2) { outer[i].enable=enable_matrix[i] }
    value_matrix:vec<2,vec<2,u8>>=
        generate(i in 0..2) outer[i].value
    value=bitcast<bits<32>>(value_matrix)
}
"""


READY_VALID_SOURCE = r"""
module TaggedLane {
    clock clk reset rst
    in input:rv<u8> out output:rv<u8>
    reg tag:u8=0
    input.ready=output.ready
    output.valid=input.valid
    output.payload=truncate<8>(input.payload+tag)
    when input.transfer { tag <- truncate<8>(tag+1) }
}
module DirectRvChild {
    clock clk reset rst
    in input:rv<u8> out output:rv<u8>
    inst lane:TaggedLane
    connect input -> lane.input
    connect lane.output -> output
}
module NestedReadyValidArray {
    clock clk reset rst
    in input0:rv<u8> in input1:rv<u8>
    out output0:rv<u8> out output1:rv<u8>
    inst outer[2]:DirectRvChild
    connect input0 -> outer[0].input
    connect outer[0].output -> output0
    connect input1 -> outer[1].input
    connect outer[1].output -> output1
}
"""


def _compile(source: str, top: str):
    return compile_source(source, top=top, include_clash=False).ir


def test_nested_combinational_array_has_exact_paths_and_behavior() -> None:
    module = _compile(COMBINATIONAL_SOURCE, "NestedCombinationalArray")
    hierarchy = build_hierarchy_index(module)

    assert restore(lower(module)) == module
    assert tuple(item.physical_path for item in hierarchy.entries) == (
        ("NestedCombinationalArray",),
        ("NestedCombinationalArray", "outer[0]"),
        ("NestedCombinationalArray", "outer[0]", "inner[0]"),
        ("NestedCombinationalArray", "outer[0]", "inner[1]"),
        ("NestedCombinationalArray", "outer[1]"),
        ("NestedCombinationalArray", "outer[1]", "inner[0]"),
        ("NestedCombinationalArray", "outer[1]", "inner[1]"),
    )
    assert simulate(module, x=0x010203FF) == {"y": 0x02030400}


def test_nested_sequential_array_has_persistent_independent_state() -> None:
    module = _compile(SEQUENTIAL_SOURCE, "NestedSequentialArray")
    restored = restore(lower(module))
    assert restored == module
    hierarchy = build_hierarchy_index(module)
    leaf_entries = tuple(
        item for item in hierarchy.entries if item.module.name == "CounterLane"
    )
    assert len(leaf_entries) == 4
    assert len({item.physical_path for item in leaf_entries}) == 4
    assert len({item.specialization_identity for item in leaf_entries}) == 1

    trace = simulate_cycles(
        module,
        (
            {"enable": 0x0},
            {"enable": 0xE},
            {"enable": 0x9},
            {"enable": 0x0},
            {"enable": 0xF},
            {"enable": 0x9},
        ),
        reset=(True, False, False, False, True, False),
    )
    assert [item["value"] for item in trace] == [
        0x00000000,
        0x00000000,
        0x01010100,
        0x02010101,
        0x00000000,
        0x00000000,
    ]


def test_nested_ready_valid_array_uses_recursive_persistent_abi() -> None:
    module = _compile(READY_VALID_SOURCE, "NestedReadyValidArray")
    assert restore(lower(module)) == module
    trace = simulate_cycles(
        module,
        (
            {"input0":{"payload":10,"valid":1},
             "input1":{"payload":20,"valid":1},
             "output0":{"ready":1}, "output1":{"ready":1}},
            {"input0":{"payload":10,"valid":1},
             "input1":{"payload":20,"valid":1},
             "output0":{"ready":0}, "output1":{"ready":1}},
            {"input0":{"payload":10,"valid":1},
             "input1":{"payload":20,"valid":1},
             "output0":{"ready":1}, "output1":{"ready":1}},
            {"input0":{"payload":10,"valid":1},
             "input1":{"payload":20,"valid":1},
             "output0":{"ready":1}, "output1":{"ready":1}},
        ),
        reset=(True, False, False, False),
    )
    assert [item["output0"]["payload"] for item in trace] == [10,10,10,11]
    assert [item["output1"]["payload"] for item in trace] == [20,20,21,22]
    assert trace[1]["input0"]["ready"] == 0
    assert trace[1]["input1"]["ready"] == 1


def test_nested_artifact_instances_and_direct_locators_are_deterministic() -> None:
    module = _compile(SEQUENTIAL_SOURCE, "NestedSequentialArray")
    recursive = build_recursive_formal_design(module)
    first = emit_sv_artifact(module, recursive_design=recursive)
    second = emit_sv_artifact(module, recursive_design=recursive)
    clash = emit_clash_artifact(module, recursive_design=recursive)

    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert BackendArtifact.from_json(first.to_json()).to_json() == first.to_json()
    expected_paths = tuple(item.physical_instance_path for item in first.instances)
    assert expected_paths == tuple(
        item.physical_instance_path for item in clash.instances
    )
    assert len(expected_paths) == 7
    nested = tuple(
        item for item in first.recursive_bindings
        if len(item.physical_instance_path) == 3
        and item.local_semantic_id == "register:count"
    )
    assert len(nested) == 4
    assert len({item.instance_identity for item in nested}) == 4
    assert all(item.rtl_path and item.signal_token == "count" for item in nested)
    assert len({item.rtl_path for item in nested}) == 4
    assert all(
        all(path in first.text for path in item.rtl_path)
        for item in nested
    )


@pytest.mark.parametrize(
    ("leaf", "diagnostic"),
    (
        (
            "module Leaf { clock clk reset rst in data:u8 in push:bit in pop:bit "
            "out y:u8 fifo q:fifo<u8,2> q.data=data q.push=push q.pop=pop "
            "y=q.front }",
            "transitive storage resources",
        ),
        (
            "module Leaf { clock clk reset rst interface mem:"
            "request_response<u8,u8>{ max_outstanding 1 ordering in_order } "
            "mem.request.payload=0 mem.request.valid=0 "
            "mem.response.ready=0 }",
            "request/response hierarchy",
        ),
    ),
)
def test_nested_arrays_fail_closed_for_unimplemented_state_families(
    leaf: str,
    diagnostic: str,
) -> None:
    source = leaf + r"""
module Mid { clock clk reset rst out y:u8 inst leaf:Leaf y=0 }
module Top { clock clk reset rst out y:u8 inst outer[2]:Mid y=0 }
"""
    with pytest.raises(SemanticError, match=diagnostic):
        _compile(source, "Top")


SEQUENTIAL_HARNESS = r'''
#include "VNestedSequentialArray.h"
#include "verilated.h"
static void tick(VNestedSequentialArray& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
static void set_all(VNestedSequentialArray& d, unsigned value) {
  d.enable=value&15u;
}
static unsigned values(VNestedSequentialArray& d) {
  return unsigned(d.value);
}
int main(int argc,char**argv) {
  Verilated::commandArgs(argc,argv); VNestedSequentialArray d;
  set_all(d,0); d.rst=1; tick(d); if(values(d)!=0) return 1;
  d.rst=0; set_all(d,15); tick(d); if(values(d)!=0x01010101) return 2;
  set_all(d,9); tick(d); if(values(d)!=0x02010102) return 3;
  d.rst=1; tick(d); if(values(d)!=0) return 4;
  d.rst=0; set_all(d,6); tick(d);
  return values(d)==0x00010100 ? 0 : 5;
}
'''


COMBINATIONAL_HARNESS = r'''
#include "VNestedCombinationalArray.h"
#include "verilated.h"
int main(int argc,char**argv) {
  Verilated::commandArgs(argc,argv); VNestedCombinationalArray d;
  d.x=0x010203ffu; d.eval();
  if(d.y!=0x02030400u) return 1;
  d.x=0xff00017fu; d.eval();
  return d.y==0x00010280u ? 0 : 2;
}
'''


READY_VALID_HARNESS = r'''
#include "VNestedReadyValidArray.h"
#include "verilated.h"
static void tick(VNestedReadyValidArray& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc,char**argv) {
  Verilated::commandArgs(argc,argv); VNestedReadyValidArray d;
  d.input0_payload=10; d.input0_valid=1; d.input1_payload=20; d.input1_valid=1;
  d.output0_ready=1; d.output1_ready=1; d.rst=1; tick(d);
  d.rst=0; tick(d);
  if(d.output0_payload!=11 || d.output1_payload!=21) return 1;
  d.output0_ready=0; tick(d);
  if(d.output0_payload!=11 || d.output1_payload!=22 || d.input0_ready) return 2;
  d.output0_ready=1; tick(d);
  return (d.output0_payload==12 && d.output1_payload==23) ? 0 : 3;
}
'''


def _run_verilator(
    tmp_path: Path,
    rtl: tuple[Path, ...],
    top: str,
    harness_text: str,
) -> None:
    harness = tmp_path / "test.cpp"
    harness.write_text(harness_text)
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(tmp_path / "obj"), "--top-module", top,
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(tmp_path / "obj" / f"V{top}"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize(
    ("source", "top", "harness"),
    (
        (COMBINATIONAL_SOURCE, "NestedCombinationalArray", COMBINATIONAL_HARNESS),
        (SEQUENTIAL_SOURCE, "NestedSequentialArray", SEQUENTIAL_HARNESS),
        (READY_VALID_SOURCE, "NestedReadyValidArray", READY_VALID_HARNESS),
    ),
)
def test_nested_arrays_direct_sv_are_strict_and_cycle_exact(
    tmp_path: Path,
    source: str,
    top: str,
    harness: str,
) -> None:
    module = _compile(source, top)
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(emit_sv_artifact(module).text)
    lint_with_verilator((rtl,), top)
    _run_verilator(tmp_path, (rtl,), top, harness)


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(
    ("source", "top", "harness"),
    (
        (COMBINATIONAL_SOURCE, "NestedCombinationalArray", COMBINATIONAL_HARNESS),
        (SEQUENTIAL_SOURCE, "NestedSequentialArray", SEQUENTIAL_HARNESS),
        (READY_VALID_SOURCE, "NestedReadyValidArray", READY_VALID_HARNESS),
    ),
)
def test_nested_arrays_real_clash_are_strict_and_cycle_exact(
    tmp_path: Path,
    source: str,
    top: str,
    harness: str,
) -> None:
    module = _compile(source, top)
    rtl = generate_verilog(
        emit_clash(module), top, tmp_path / "rtl", CLASH_EXECUTABLE
    )
    lint_with_verilator(rtl, top)
    _run_verilator(tmp_path, tuple(rtl), top, harness)
