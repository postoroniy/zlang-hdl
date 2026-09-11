"""One exact arithmetic example, independent oracle, and both real RTL paths."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import random
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_file
from zlang.opt import lower, restore
from zlang.simulate import simulate_cycles


SOURCE = Path(__file__).resolve().parents[2] / "examples/verification/math_exploration.zhl"
LATENCIES = {"MathOneCycle": 0, "MathArchitecture": 0, "MathExplore": 1}
INPUTS = tuple(f"{side}{index}" for index in range(8) for side in "ab")


def stimulus():
    rng = random.Random(20260908)
    rows = [dict.fromkeys(INPUTS, value) for value in (0, 1, 65535, 32768)]
    rows += [
        {name: (65535 if name == selected else 0) for name in INPUTS}
        for selected in INPUTS
    ]
    rows += [{name: rng.randrange(65536) for name in INPUTS} for _ in range(76)]
    # Flush the internal pipeline using ordinary zero-valued input samples.
    rows += [dict.fromkeys(INPUTS, 0) for _ in range(4)]
    resets = [index in (0, 38, 39, 71) for index in range(len(rows))]
    return rows, resets


def oracle(rows, resets, latency):
    stages = [0] * latency
    values = []
    for row, reset in zip(rows, resets, strict=True):
        exact = sum(row[f"a{i}"] * row[f"b{i}"] for i in range(8))
        assert exact < 1 << 35
        if reset:
            stages = [0] * latency
        values.append(stages[0] if latency else exact)
        if latency and not reset:
            stages = stages[1:] + [exact]
    return values


@pytest.fixture(scope="module")
def compilations():
    return {top: compile_file(SOURCE, top=top) for top in LATENCIES}




def cpp_testbench(top: str) -> str:
    rows, resets = stimulus()
    checks = []
    for cycle, (row, reset, expected) in enumerate(zip(
        rows, resets, oracle(rows, resets, LATENCIES[top]), strict=True
    )):
        checks += [" ".join(f"d.{name}={value};" for name, value in row.items())]
        checks += [f"d.rst={int(reset)}; d.clk=0; d.eval();"]
        # Simulator reset cycles represent the reset edge. Sample synchronous
        # reset only after that edge; ordinary cycles are sampled before commit.
        if reset:
            checks += ["tick(d);"]
        checks += [f'if(d.y != {expected}ULL) {{ std::fprintf(stderr,"cycle {cycle}: %llu != {expected}\\n",(unsigned long long)d.y); return 1; }}']
        if not reset:
            checks += ["tick(d);"]
    return f'''#include "V{top}.h"
#include <cstdio>
static void tick(V{top}& d) {{ d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval(); }}
int main() {{ V{top} d; {' '.join(checks)} return 0; }}
'''
