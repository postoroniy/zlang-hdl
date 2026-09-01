"""Runtime muxing of outputs from statically elaborated instance arrays."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.ir import RuntimeIndex
from zlang.ir.recursive_formal import build_recursive_formal_design
from zlang.simulate import simulate, simulate_cycles
from zlang.toolchain import generate_verilog, lint_with_verilator


SOURCE = """
module SelectLane {
    in x:u8
    out y:u9
    y=x+1
}

module RuntimeSelectedLanes {
    in values:vec<4,u8>
    in select:u2
    out y:u9
    inst lane[4]:SelectLane
    generate(i in 0..4) { lane[i].x=values[i] }
    y=lane[select].y
}
"""


STATEFUL_SOURCE = """
module SelectCounter {
    clock clk reset rst
    in enable:bit in step:u8 out value:u8
    reg count:u8=0
    when enable { count <- truncate<8>(count+step) }
    value=count
}

module RuntimeSelectedCounters {
    clock clk reset rst
    in enables:vec<2,bit> in steps:vec<2,u8> in select:u1
    out value:u8
    inst lane[2]:SelectCounter
    generate(i in 0..2) {
        lane[i].enable=enables[i]
        lane[i].step=steps[i]
    }
    value=lane[select].value
}
"""


BIT_PACKABLE_OUTPUT_CASES = (
    (
        "fixed",
        """module Lane { in x:u8 out y:SF8.8 y=fixed_raw(0x0100) }
        module Top { in values:vec<2,u8> in select:u1 out y:SF8.8
          inst lane[2]:Lane
          generate(i in 0..2) { lane[i].x=values[i] }
          y=lane[select].y }""",
        256,
        frozenset({"port:y"}),
    ),
    (
        "vector",
        """module Lane { in x:u8 out y:vec<2,u8> y=[x,~x] }
        module Top { in values:vec<2,u8> in select:u1 out y:vec<2,u8>
          inst lane[2]:Lane
          generate(i in 0..2) { lane[i].x=values[i] }
          y=lane[select].y }""",
        [34, 221],
        frozenset({"port:y"}),
    ),
    (
        "struct",
        """struct Pair { left:u8 right:u8 }
        module Lane { in x:u8 out y:Pair y=Pair { left=x right=~x } }
        module Top { in values:vec<2,u8> in select:u1 out y:Pair
          inst lane[2]:Lane
          generate(i in 0..2) { lane[i].x=values[i] }
          y=lane[select].y }""",
        {"left": 34, "right": 221},
        frozenset({"port:y.left", "port:y.right"}),
    ),
)


def _harness() -> str:
    return r'''
module tb;
  logic [7:0] values [0:3];
  logic [1:0] select;
  wire [8:0] y;
  RuntimeSelectedLanes dut(.values(values), .select(select), .y(y));
  initial begin
    values = '{8'd10, 8'd20, 8'd30, 8'd40};
    select = 0; #1; if (y !== 9'd11) $fatal(1, "lane 0");
    select = 1; #1; if (y !== 9'd21) $fatal(1, "lane 1");
    select = 2; #1; if (y !== 9'd31) $fatal(1, "lane 2");
    select = 3; #1; if (y !== 9'd41) $fatal(1, "lane 3");
    $finish;
  end
endmodule
'''


def _run(files: tuple[Path, ...], root: Path, tag: str) -> None:
    bench = root / f"tb_{tag}.sv"
    bench.write_text(_harness())
    obj = root / f"obj_{tag}"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--binary", "--timing", "-Wall", "-Wno-fatal",
            "--top-module", "tb", "--Mdir", str(obj),
            *(str(path) for path in files), str(bench),
        ),
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    run = subprocess.run(
        (str(obj / "Vtb"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


def _stateful_harness() -> str:
    return r'''
#include "VRuntimeSelectedCounters.h"
#include "verilated.h"
static void tick(VRuntimeSelectedCounters& dut) {
  dut.clk = 0; dut.eval(); dut.clk = 1; dut.eval(); dut.clk = 0; dut.eval();
}
static void set_inputs(VRuntimeSelectedCounters& dut, unsigned enables,
                       unsigned step0, unsigned step1) {
  dut.enables[0] = (enables >> 1) & 1u;
  dut.enables[1] = enables & 1u;
  dut.steps[0] = step0;
  dut.steps[1] = step1;
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VRuntimeSelectedCounters dut;
  dut.rst = 1; dut.select = 0; set_inputs(dut, 0, 0, 0); tick(dut);
  if (dut.value != 0) return 1;
  dut.rst = 0; set_inputs(dut, 3, 5, 7); tick(dut);
  dut.select = 0; dut.eval(); if (dut.value != 5) return 2;
  dut.select = 1; dut.eval(); if (dut.value != 7) return 3;
  set_inputs(dut, 2, 3, 0); tick(dut);
  dut.select = 0; dut.eval(); if (dut.value != 8) return 4;
  dut.select = 1; dut.eval(); if (dut.value != 7) return 5;
  dut.rst = 1; tick(dut);
  dut.select = 0; dut.eval(); if (dut.value != 0) return 6;
  dut.select = 1; dut.eval(); if (dut.value != 0) return 7;
  dut.rst = 0; set_inputs(dut, 3, 2, 4); tick(dut);
  dut.select = 0; dut.eval(); if (dut.value != 2) return 8;
  dut.select = 1; dut.eval(); return dut.value == 4 ? 0 : 9;
}
'''


def _run_stateful(files: tuple[Path, ...], root: Path, tag: str) -> None:
    harness = root / f"stateful_{tag}.cpp"
    harness.write_text(_stateful_harness())
    obj = root / f"stateful_obj_{tag}"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--top-module", "RuntimeSelectedCounters", "--Mdir", str(obj),
            *(str(path) for path in files), str(harness),
        ),
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    run = subprocess.run(
        (str(obj / "VRuntimeSelectedCounters"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_runtime_selected_output_has_no_dynamic_instance_identity() -> None:
    module = compile_source(
        SOURCE, top="RuntimeSelectedLanes", include_clash=False
    ).ir
    output = module.assignments[0].expression
    assert isinstance(output, RuntimeIndex)
    assert [item.instance for item in output.expression.elements] == [
        f"lane[{index}]" for index in range(4)
    ]
    assert [item.instance.name for item in module.elaborated_instances] == [
        f"lane[{index}]" for index in range(4)
    ]
    assert simulate(module, values=[10, 20, 30, 40], select=2) == {"y": 31}


@pytest.mark.parametrize(
    ("case", "source", "expected", "expected_output_bindings"),
    BIT_PACKABLE_OUTPUT_CASES,
)
@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_runtime_selected_fixed_vector_and_struct_outputs_publish_direct_artifacts(
    case: str,
    source: str,
    expected: object,
    expected_output_bindings: frozenset[str],
    tmp_path: Path,
) -> None:
    module = compile_source(source, top="Top", include_clash=False).ir
    assert simulate(module, values=[12, 34], select=1) == {"y": expected}
    recursive = build_recursive_formal_design(module)
    first = emit_sv_artifact(module, recursive_design=recursive)
    second = emit_sv_artifact(module, recursive_design=recursive)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert type(first).from_json(first.to_json()).to_json() == first.to_json()
    assert expected_output_bindings <= {
        binding.semantic_signal_id for binding in first.bindings
    }
    assert [item.physical_instance_path for item in first.instances] == [
        ("Top",),
        ("Top", "lane[0]"),
        ("Top", "lane[1]"),
    ]
    assert all("lane[select]" not in item.semantic_signal_id for item in first.bindings)
    rtl = tmp_path / f"runtime_selected_{case}.sv"
    rtl.write_text(first.text)
    lint_with_verilator((rtl,), "Top")


@pytest.mark.parametrize(
    ("case", "source", "_expected", "expected_output_bindings"),
    BIT_PACKABLE_OUTPUT_CASES,
)
@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_runtime_selected_fixed_vector_and_struct_outputs_reach_real_clash(
    case: str,
    source: str,
    _expected: object,
    expected_output_bindings: frozenset[str],
    tmp_path: Path,
) -> None:
    compilation = compile_source(source, top="Top")
    artifact = emit_clash_artifact(
        compilation.ir,
        recursive_design=build_recursive_formal_design(compilation.ir),
    )
    assert type(artifact).from_json(artifact.to_json()).to_json() == artifact.to_json()
    assert expected_output_bindings <= {
        binding.semantic_signal_id for binding in artifact.bindings
    }
    assert [item.physical_instance_path for item in artifact.instances] == [
        ("Top",),
        ("Top", "lane[0]"),
        ("Top", "lane[1]"),
    ]
    files = tuple(
        generate_verilog(
            compilation.clash,
            "Top",
            tmp_path / f"clash_{case}",
            CLASH_EXECUTABLE,
            public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
        )
    )
    assert files
    lint_with_verilator(files, "Top")


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_runtime_selected_output_direct_sv_is_deterministic_and_simulates(
    tmp_path: Path,
) -> None:
    module = compile_source(
        SOURCE, top="RuntimeSelectedLanes", include_clash=False
    ).ir
    recursive = build_recursive_formal_design(module)
    first = emit_sv_artifact(module, recursive_design=recursive)
    second = emit_sv_artifact(module, recursive_design=recursive)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert all("lane[select]" not in item.semantic_signal_id for item in first.bindings)
    assert [item.physical_instance_path for item in first.instances] == [
        ("RuntimeSelectedLanes",),
        ("RuntimeSelectedLanes", "lane[0]"),
        ("RuntimeSelectedLanes", "lane[1]"),
        ("RuntimeSelectedLanes", "lane[2]"),
        ("RuntimeSelectedLanes", "lane[3]"),
    ]
    rtl = tmp_path / "RuntimeSelectedLanes.sv"
    rtl.write_text(first.text)
    lint_with_verilator((rtl,), "RuntimeSelectedLanes")
    _run((rtl,), tmp_path, "direct")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_runtime_selected_output_clash_generates_and_simulates(
    tmp_path: Path,
) -> None:
    compilation = compile_source(SOURCE, top="RuntimeSelectedLanes")
    files = tuple(
        generate_verilog(
            compilation.clash,
            compilation.ir.name,
            tmp_path / "clash",
            CLASH_EXECUTABLE,
            public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
        )
    )
    assert files
    lint_with_verilator(files, compilation.ir.name)
    _run(files, tmp_path, "clash")


def test_stateful_runtime_selection_preserves_independent_child_state() -> None:
    module = compile_source(
        STATEFUL_SOURCE, top="RuntimeSelectedCounters", include_clash=False
    ).ir
    trace = simulate_cycles(
        module,
        (
            {"enables": [0, 0], "steps": [0, 0], "select": 0},
            {"enables": [1, 1], "steps": [5, 7], "select": 0},
            {"enables": [0, 0], "steps": [0, 0], "select": 1},
            {"enables": [1, 0], "steps": [3, 0], "select": 0},
            {"enables": [0, 0], "steps": [0, 0], "select": 1},
            {"enables": [0, 0], "steps": [0, 0], "select": 0},
            {"enables": [1, 1], "steps": [2, 4], "select": 1},
        ),
        reset=(True, False, False, False, False, True, False),
    )
    assert [item["value"] for item in trace] == [0, 0, 7, 5, 7, 0, 0]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_stateful_runtime_selection_direct_sv_simulates_reset_and_selector_changes(
    tmp_path: Path,
) -> None:
    module = compile_source(
        STATEFUL_SOURCE, top="RuntimeSelectedCounters", include_clash=False
    ).ir
    artifact = emit_sv_artifact(module)
    rtl = tmp_path / "RuntimeSelectedCounters.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), "RuntimeSelectedCounters")
    _run_stateful((rtl,), tmp_path, "direct")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_stateful_runtime_selection_clash_simulates_reset_and_selector_changes(
    tmp_path: Path,
) -> None:
    compilation = compile_source(STATEFUL_SOURCE, top="RuntimeSelectedCounters")
    files = tuple(
        generate_verilog(
            compilation.clash,
            compilation.ir.name,
            tmp_path / "stateful_clash",
            CLASH_EXECUTABLE,
            public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
        )
    )
    assert files
    lint_with_verilator(files, compilation.ir.name)
    _run_stateful(files, tmp_path, "clash")
