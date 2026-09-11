"""Consolidated real-RTL witnesses for physical-only hierarchy renaming."""

from __future__ import annotations

from pathlib import Path
import os
import re
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.naming import (
    build_component_name_plan, module_rtl_names, validate_component_name_plans,
)
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.ir.hierarchy import build_hierarchy_index
from zlang.simulate import simulate_cycles


SOURCE = """
fn passthrough(logic:u8, zlang_logic:u8) -> u8 { logic | zlang_logic }
fn keyword_args(int:u8, byte:u8, ref:u8, unsigned:u8, time:u8, var:u8) -> u8 {
    int | byte | ref | unsigned | time | var
}
module Counter {
    clock clk reset rst
    in enable:bit in step:u8
    out ready:bit out value:u8
    reg count:u8=0
    when enable { count <- truncate<8>(count+step) }
    ready=count!=0 value=count
}
module NamingTop {
    clock clk reset rst
    in cfg_go,a_go,b_go,lit_go:bit
    in cfg_step,a_step,b_step,lit_step:u8
    out cfg_value,a_value,b_value,lit_value:u8
    out parent_count,parent_fires:u8
    out cfg_nonzero:bit
    reg cfg_ready:u8=9
    reg rule_tick_fire:u8=11
    cfg:Counter { enable=cfg_go step=keyword_args(passthrough(cfg_step,0),0,0,0,0,0) }
    lane[2]:Counter
    enables=[a_go,b_go]
    steps=[a_step,b_step]
    generate(i in 0..2) { lane[i].enable=enables[i] lane[i].step=steps[i] }
    lane_0:Counter { enable=lit_go step=lit_step }
    tick:when cfg_go { cfg_ready <- truncate<8>(cfg_ready+1) }
    when lit_go { rule_tick_fire <- truncate<8>(rule_tick_fire+1) }
    cfg_value=cfg.value a_value=lane[0].value
    b_value=lane[1].value lit_value=lane_0.value
    parent_count=cfg_ready parent_fires=rule_tick_fire
    cfg_nonzero=cfg.ready
}
"""

OUTPUTS = ("cfg_value", "a_value", "b_value", "lit_value", "parent_count", "parent_fires", "cfg_nonzero")


def _cycles():
    patterns = (
        ((0, 0, 0, 0), (0, 0, 0, 0), True),
        ((1, 1, 1, 1), (5, 7, 11, 13), False),
        ((1, 0, 1, 0), (3, 6, 9, 12), False),
        ((0, 0, 0, 1), (1, 2, 3, 2), False),
        ((0, 0, 0, 0), (99, 88, 77, 66), False),
        ((1, 1, 1, 1), (1, 1, 1, 1), True),
        ((0, 1, 0, 1), (1, 2, 3, 4), False),
        ((0, 0, 0, 0), (0, 0, 0, 0), False),
    )
    cycles = []
    resets = []
    before = []
    after = []
    counts = [0] * 4
    parent = [9, 11]

    def output():
        return dict(zip(OUTPUTS, (*counts, *parent, int(counts[0] != 0)), strict=True))

    for enables, steps, reset in patterns:
        cycles.append(dict(zip(
            ("cfg_go", "a_go", "b_go", "lit_go", "cfg_step", "a_step", "b_step", "lit_step"),
            (*enables, *steps), strict=True,
        )))
        resets.append(reset)
        if reset:
            counts = [0] * 4
            parent = [9, 11]
        before.append(output())
        if not reset:
            counts = [(value + step) & 255 if enable else value for value, step, enable in zip(counts, steps, enables, strict=True)]
            parent[0] += enables[0]
            parent[1] += enables[3]
        after.append(output())
    return cycles, resets, before, after


def _strict_lint(files: tuple[Path, ...], top: str, *, cwd: Path) -> None:
    completed = subprocess.run((
        "verilator", "--lint-only", "--sv", "--top-module", top,
        "-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL", "-Wno-UNUSEDPARAM",
        *(str(path) for path in files),
    ), cwd=cwd, text=True, capture_output=True, timeout=90)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _run_trace(files: tuple[Path, ...], directory: Path, cycles, resets, expected) -> None:
    harness = directory / "trace.cpp"
    statements = []
    for index, (values, reset, result) in enumerate(zip(cycles, resets, expected, strict=True)):
        statements.extend(f"dut.{name}={value};" for name, value in values.items())
        statements.append(f"dut.rst={int(reset)}; tick(dut);")
        for name in OUTPUTS:
            statements.append(
                f'if(dut.{name}!={result[name]}) {{std::cerr<<"cycle {index} {name} "<<unsigned(dut.{name})<<" expected {result[name]}\\n";return 1;}}'
            )
    harness.write_text(
        '#include "VNamingTop.h"\n#include "verilated.h"\n#include <iostream>\n'
        'static void tick(VNamingTop& dut){dut.clk=0;dut.eval();dut.clk=1;dut.eval();dut.clk=0;dut.eval();}\n'
        'int main(int argc,char**argv){Verilated::commandArgs(argc,argv);VNamingTop dut{};\n'
        + "\n".join(statements) + '\nreturn 0;}\n'
    )
    environment = dict(os.environ, CCACHE_DISABLE="1")
    completed = subprocess.run((
        "verilator", "--cc", "--exe", "--build", "--sv", "--top-module", "NamingTop",
        "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL", "-Wno-UNUSEDPARAM",
        "--Mdir", str(directory / "obj"), *(str(path) for path in files), str(harness),
    ), cwd=directory, env=environment, text=True, capture_output=True, timeout=180)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = subprocess.run((str(directory / "obj" / "VNamingTop"),), cwd=directory,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator required")
def test_independently_emitted_incompatible_specializations_coexist_in_one_hdl_unit(tmp_path: Path) -> None:
    artifacts = []
    plans = []
    for top, width in (("TopA", 8), ("TopB", 16)):
        source = (
            "module Child<W=8> { in x:uint<W> out y:uint<W> y=x } "
            f"module {top} {{ in x:u{width} out y:u{width} child:Child<{width}>{{x}} y=child.y }}"
        )
        module = compile_source(source, top=top).ir
        artifacts.append(emit_artifact(module))
        plans.append(build_component_name_plan(build_hierarchy_index(module)))
    validate_component_name_plans(*plans)
    child_names = [plan.entries[0].physical_name for plan in plans]
    assert child_names[0] != child_names[1]
    assert all(re.fullmatch(r"Child_s[0-9a-f]{8}", name) for name in child_names)
    files = []
    for artifact in artifacts:
        path = tmp_path / f"{artifact.module}.sv"
        path.write_text(artifact.text)
        files.append(path)
    wrapper = tmp_path / "Combined.sv"
    wrapper.write_text(
        "`default_nettype none\nmodule Combined(input logic [7:0] a,input logic [15:0] b,"
        "output logic [7:0] x,output logic [15:0] y);\n"
        "TopA a_impl(.x(a),.y(x));\nTopB b_impl(.x(b),.y(y));\nendmodule\n"
    )
    _strict_lint((*files, wrapper), "Combined", cwd=tmp_path)
