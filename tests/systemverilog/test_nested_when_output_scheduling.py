"""Direct-SV must honor scalar-output conflicts for every rule shape."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import SystemVerilogEmissionError, emit_artifact
from zlang.compiler import compile_source


SOURCE = """
module OutputPriorityOnly {
    clock clk reset rst
    in high, low : bit
    out event, high_seen, low_seen : bit
    reg high_state : bit = 0
    reg low_state : bit = 0

    priority {
        higher: when high { event <- 1 high_state <- 1 }
        lower: when low { event <- 0 low_state <- 1 }
    }

    high_seen = high_state
    low_seen = low_state
}
"""


HARNESS = r"""
#include "VOutputPriorityOnly.h"
#include "verilated.h"

static void tick(VOutputPriorityOnly& d) {
    d.clk = 0; d.eval();
    d.clk = 1; d.eval();
    d.clk = 0; d.eval();
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    VOutputPriorityOnly d;
    d.high = 0; d.low = 0; d.rst = 1; tick(d);
    d.rst = 0; d.high = 1; d.low = 1; d.eval();
    if (!d.zlang_event || d.high_seen || d.low_seen) return 1;
    tick(d);
    if (!d.zlang_event || !d.high_seen || d.low_seen) return 2;
    d.high = 0; d.low = 0; d.eval();
    return (d.zlang_event || !d.high_seen || d.low_seen) ? 3 : 0;
}
"""


def test_many_effects_in_one_branch_emit_one_activation_signal() -> None:
    registers = " ".join(f"reg r{index}:bit=0" for index in range(12))
    writes = " ".join(f"r{index} <- 1" for index in range(12))
    packed = ",".join(f"r{index}" for index in range(12))
    source = (
        "module SharedBranch { clock clk reset rst in go,select:bit "
        f"out y:bits<12> {registers} "
        f"when go {{ when select {{ {writes} }} }} "
        f"y=concat({packed}) }}"
    )
    module = compile_source(source, include_clash=False).ir
    generated = emit_artifact(module).text

    assert generated.count("logic zlang_condition_0_active;") == 1
    assert "zlang_condition_1_active" not in generated
    assert len(generated) < 12_000


def test_conditional_activation_private_name_collision_fails_closed() -> None:
    module = compile_source(
        "module Collision { clock clk reset rst in go,select:bit "
        "out zlang_condition_0_active:bit reg x:bit=0 "
        "when go { when select { x <- 1 } } "
        "zlang_condition_0_active=x }",
        include_clash=False,
    ).ir

    with pytest.raises(
        SystemVerilogEmissionError,
        match="conditional-action predicate 0.*zlang_condition_0_active",
    ):
        emit_artifact(module)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_unnested_output_conflict_suppresses_the_losing_rule_atomically(
    tmp_path: Path,
) -> None:
    module = compile_source(
        SOURCE, top="OutputPriorityOnly", include_clash=False
    ).ir
    artifact = emit_artifact(module)

    # The legacy per-register emitter cannot implement a conflict owned only
    # by a scalar output.  Presence of the shared accepted-fire signals proves
    # this ordinary (unnested) rule module took the authoritative transition
    # route too.
    assert "rule_higher_fire" in artifact.text
    assert "rule_lower_fire" in artifact.text
    assert "assign zlang_event =" in artifact.text

    rtl = tmp_path / "OutputPriorityOnly.sv"
    harness = tmp_path / "output_priority.cpp"
    rtl.write_text(artifact.text)
    harness.write_text(HARNESS)
    object_directory = tmp_path / "obj"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    build = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--top-module", "OutputPriorityOnly",
            "--Mdir", str(object_directory), "-o", "output_priority",
            str(rtl), str(harness),
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    run = subprocess.run(
        (str(object_directory / "output_priority"),),
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
