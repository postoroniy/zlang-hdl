from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


SOURCE = """
struct ChildView { valid:bit value:u8 }
module Child {
    in x:u8
    out view:ChildView
    view=ChildView { valid=x != 0 value=x }
}
module ParentRuleChildOutput {
    clock clk reset rst
    in x:u8
    out observation:u8
    reg held:u8=0
    inst child:Child { x }
    rule capture when child.view.valid {
        held <- child.view.value
    }
    observation=held
}
"""


HARNESS = r'''#include "VParentRuleChildOutput.h"
#include "verilated.h"
static void tick(VParentRuleChildOutput& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VParentRuleChildOutput d;
  d.x=0; d.rst=1; tick(d);
  if (d.observation != 0) return 1;
  d.rst=0; d.x=5; tick(d);
  if (d.observation != 5) return 2;
  d.x=0; tick(d);
  if (d.observation != 5) return 3;
  d.x=9; tick(d);
  if (d.observation != 9) return 4;
  d.rst=1; d.x=7; tick(d);
  return d.observation == 0 ? 0 : 5;
}
'''


FIFO_SOURCE = """
module ChildValue {
    in x:u8
    out valid:bit
    out value:u8
    valid=x != 0
    value=x
}
module ParentChildFifo {
    clock clk reset rst
    in x:u8 in pop:bit
    out front:u8 out valid:bit
    fifo queue:fifo<u8,2>
    inst child:ChildValue { x }
    rule enqueue when child.valid { queue.push(child.value) }
    rule dequeue when pop { queue.pop() }
    front=queue.front
    valid=queue.valid
}
"""


FIFO_HARNESS = r'''#include "VParentChildFifo.h"
#include "verilated.h"
static void tick(VParentChildFifo& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VParentChildFifo d;
  d.rst=1; d.x=0; d.pop=0; tick(d);
  if (d.valid != 0) return 1;
  d.rst=0; d.x=5; tick(d);
  if (d.valid != 1 || d.front != 5) return 2;
  d.x=7; tick(d);
  if (d.valid != 1 || d.front != 5) return 3;
  d.x=0; d.pop=1; tick(d);
  if (d.valid != 1 || d.front != 7) return 4;
  d.pop=0; d.rst=1; tick(d);
  return d.valid == 0 ? 0 : 5;
}
'''


MEMORY_SOURCE = """
module ChildData { in x:u8 out value:u8 value=x }
module ParentChildMemory {
    clock clk reset rst
    in x:u8 in address:u2 in write:bit in read:bit
    out value:u8
    memory table:mem<u8,4> { read_latency 1 collision write_first }
    inst child:ChildData { x }
    rule store when write { table.write(address, child.value) }
    rule fetch when read { table.read(address) }
    value=table.read_data
}
"""


MEMORY_HARNESS = r'''#include "VParentChildMemory.h"
#include "verilated.h"
static void tick(VParentChildMemory& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VParentChildMemory d;
  d.rst=1; d.x=0; d.address=0; d.write=0; d.read=0; tick(d);
  if (d.value != 0) return 1;
  d.rst=0; d.x=5; d.address=1; d.write=1; tick(d);
  d.write=0; d.read=1; tick(d);
  if (d.value != 5) return 2;
  d.x=9; d.write=1; d.read=1; tick(d);
  if (d.value != 9) return 3;
  d.x=0; d.address=2; d.write=0; d.read=1; tick(d);
  if (d.value != 0) return 4;
  d.rst=1; d.read=0; tick(d);
  d.rst=0; d.address=1; d.read=1; tick(d);
  return d.value == 0 ? 0 : 5;
}
'''


SEQUENTIAL_SOURCE = """
struct CounterView { valid:bit value:u8 }
module CounterChild {
    clock clk reset rst
    out view:CounterView
    reg count:u8=0
    rule advance when 1 { count <- truncate<8>(count + 1) }
    view=CounterView { valid=count != 0 value=count }
}
module SequentialChildParent {
    clock clk reset rst
    out observation:u8
    reg held:u8=0
    inst counter:CounterChild
    rule capture when counter.view.valid { held <- counter.view.value }
    observation=held
}
"""


SEQUENTIAL_HARNESS = r'''#include "VSequentialChildParent.h"
#include "verilated.h"
static void tick(VSequentialChildParent& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VSequentialChildParent d;
  d.rst=1; tick(d);
  if (d.observation != 0) return 1;
  d.rst=0; tick(d);
  if (d.observation != 0) return 2;
  tick(d);
  if (d.observation != 1) return 3;
  tick(d);
  if (d.observation != 2) return 4;
  d.rst=1; tick(d);
  return d.observation == 0 ? 0 : 5;
}
'''


RECURSIVE_STATEFUL_SOURCE = """
module RecursiveLeaf {
    clock clk reset rst
    in input : rv<u8>
    in allow : bit
    out output : rv<u8>
    out consumed : bit
    out value : u8

    reg observed : u8 = 0

    input.ready = output.ready & allow
    output.payload = input.payload
    output.valid = input.valid & allow
    consumed = input.transfer
    value = input.payload
    rule remember when input.transfer {
        observed <- input.payload
    }
}

module RecursiveStatefulWrapper {
    clock clk reset rst
    in input : rv<u8>
    out output : rv<u8>
    out accepted : u8

    fifo captured : fifo<u8,2>
    reg count : u8 = 0
    can_accept = captured.ready

    inst leaf : RecursiveLeaf { allow = can_accept }
    connect input -> leaf.input
    connect leaf.output -> output

    rule capture when leaf.consumed {
        captured.push(leaf.value)
        count <- truncate<8>(count + 1)
    }

    accepted = count
}

module RecursiveStatefulTop {
    clock clk reset rst
    in input : rv<u8>
    out output : rv<u8>
    out accepted : u8

    inst wrapper : RecursiveStatefulWrapper
    connect input -> wrapper.input
    connect wrapper.output -> output
    accepted = wrapper.accepted
}
"""


RECURSIVE_STATEFUL_HARNESS = r'''#include "VRecursiveStatefulTop.h"
#include "verilated.h"
static void settle(VRecursiveStatefulTop& d) { d.clk=0; d.eval(); }
static void tick(VRecursiveStatefulTop& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VRecursiveStatefulTop d;
  d.input_payload=0; d.input_valid=0; d.output_ready=1; d.rst=1;
  tick(d);
  if (d.accepted != 0 || d.input_ready != 0) return 1;

  d.rst=0; d.input_valid=1; d.input_payload=5; settle(d);
  if (d.input_ready != 1 || d.output_valid != 1 || d.output_payload != 5) return 2;
  tick(d);
  if (d.accepted != 1) return 3;

  d.input_payload=7; tick(d);
  if (d.accepted != 2) return 4;

  d.input_payload=9; settle(d);
  if (d.input_ready != 0 || d.output_valid != 0) return 5;
  tick(d);
  if (d.accepted != 2) return 6;

  d.rst=1; tick(d);
  if (d.accepted != 0 || d.input_ready != 0) return 7;
  d.rst=0; d.input_payload=11; tick(d);
  return d.accepted == 1 ? 0 : 8;
}
'''


def _simulate(files: tuple[Path, ...], tmp_path: Path, tag: str) -> None:
    _simulate_harness(
        files, tmp_path, tag, "ParentRuleChildOutput", HARNESS
    )


def _simulate_harness(
    files: tuple[Path, ...],
    tmp_path: Path,
    tag: str,
    top: str,
    source: str,
) -> None:
    harness = tmp_path / f"harness_{tag}.cpp"
    harness.write_text(source)
    object_directory = tmp_path / f"obj_{tag}"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--top-module", top,
            "--Mdir", str(object_directory), "-o", "child_rule_sim",
            *(str(path) for path in files), str(harness),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_directory / "child_rule_sim"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_artifacts_are_deterministic_and_manifest_round_trips() -> None:
    module = compile_source(SOURCE, include_clash=False).ir
    for emitter in (emit_sv_artifact, emit_clash_artifact):
        first = emitter(module)
        second = emitter(module)
        assert first.text == second.text
        assert first.artifact_hash == second.artifact_hash
        restored = BackendArtifact.from_json(first.to_json())
        assert restored.artifact_hash == first.artifact_hash
        assert restored.bindings == first.bindings
        assert {item.semantic_signal_id for item in first.bindings} >= {
            "port:x", "port:observation", "clock", "reset"
        }


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_child_output_rule_operands_lint_and_simulate(
    tmp_path: Path,
) -> None:
    artifact = emit_sv_artifact(compile_source(SOURCE, include_clash=False).ir)
    rtl = tmp_path / "ParentRuleChildOutput.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "ParentRuleChildOutput")
    _simulate((rtl,), tmp_path, "sv")


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize(
    ("source", "top", "harness", "tag"),
    (
        (FIFO_SOURCE, "ParentChildFifo", FIFO_HARNESS, "fifo"),
        (MEMORY_SOURCE, "ParentChildMemory", MEMORY_HARNESS, "memory"),
    ),
)
def test_direct_sv_composes_child_instances_with_scheduled_storage(
    tmp_path: Path,
    source: str,
    top: str,
    harness: str,
    tag: str,
) -> None:
    artifact = emit_sv_artifact(compile_source(source, include_clash=False).ir)
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), top)
    _simulate_harness((rtl,), tmp_path, tag, top, harness)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_sequential_child_output_uses_pre_edge_snapshot(
    tmp_path: Path,
) -> None:
    top = "SequentialChildParent"
    artifact = emit_sv_artifact(
        compile_source(SEQUENTIAL_SOURCE, include_clash=False).ir
    )
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), top)
    _simulate_harness(
        (rtl,), tmp_path, "sequential_sv", top, SEQUENTIAL_HARNESS
    )


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_clash_child_output_rule_operands_lint_and_simulate(tmp_path: Path) -> None:
    compilation = compile_source(SOURCE)
    rtl = tuple(
        generate_verilog(
            compilation.clash,
            "ParentRuleChildOutput",
            tmp_path / "clash",
            find_clash_executable(),
        )
    )
    assert rtl
    lint_with_verilator(rtl, "ParentRuleChildOutput")
    _simulate(rtl, tmp_path, "clash")


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_clash_sequential_child_output_uses_pre_edge_snapshot(
    tmp_path: Path,
) -> None:
    top = "SequentialChildParent"
    compilation = compile_source(SEQUENTIAL_SOURCE)
    rtl = tuple(
        generate_verilog(
            compilation.clash,
            top,
            tmp_path / "clash_sequential",
            find_clash_executable(),
        )
    )
    lint_with_verilator(rtl, top)
    _simulate_harness(
        rtl, tmp_path, "sequential_clash", top, SEQUENTIAL_HARNESS
    )


def test_clash_recursive_stateful_wrapper_uses_closed_transition_abi() -> None:
    top = "RecursiveStatefulTop"
    module = compile_source(
        RECURSIVE_STATEFUL_SOURCE, top=top, include_clash=False
    ).ir
    clash = emit_clash_artifact(module).text

    assert clash.count("protocol_recursiveStatefulWrapper ::") == 1
    assert "leaf_consumed" in clash
    assert "RecursiveLeafComponentInput <$>" in clash
    assert "recursiveLeafComponentConsumed <$> leaf_result" in clash
    assert "rule_capture_guard = leaf_consumed" in clash
    assert "captured_count = register" in clash
    assert "count = register" in clash
    wrapper = clash[
        clash.index("protocol_recursiveStatefulWrapper ::"):
        clash.index("protocol_recursiveLeaf ::")
    ]
    assert "parent_input" not in wrapper


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_clash_recursive_stateful_wrapper_lints_and_simulates(
    tmp_path: Path,
) -> None:
    top = "RecursiveStatefulTop"
    module = compile_source(
        RECURSIVE_STATEFUL_SOURCE, top=top, include_clash=False
    ).ir
    rtl = tuple(
        generate_verilog(
            emit_clash_artifact(module).text,
            top,
            tmp_path / "clash_recursive_stateful",
            find_clash_executable(),
        )
    )
    lint_with_verilator(rtl, top)
    _simulate_harness(
        rtl,
        tmp_path,
        "recursive_stateful_clash",
        top,
        RECURSIVE_STATEFUL_HARNESS,
    )
