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
from zlang.toolchain import find_clash_executable, generate_verilog


SOURCE = Path(__file__).resolve().parents[2] / "examples/verification/math_exploration.zhl"
LATENCIES = {"MathOneCycle": 0, "MathArchitecture": 0, "MathExplore": 4}
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


def test_exact_arithmetic_timing_identity_and_candidate_reports(compilations):
    rows, resets = stimulus()
    for top, result in compilations.items():
        canonical = lower(result.ir)
        assert restore(canonical) == result.ir
        assert result.ir.outputs[0].type.width == 39
        again = compile_file(SOURCE, top=top)
        assert result.selected_ir_identity == again.selected_ir_identity
        assert result.clash == again.clash
        artifact = emit_artifact(result.ir, selected_ir_identity=result.selected_ir_identity)
        repeated = emit_artifact(again.ir, selected_ir_identity=again.selected_ir_identity)
        assert artifact == repeated
        restored = BackendArtifact.from_json(artifact.to_json())
        assert restored.to_json() == artifact.to_json()
        assert any(b.semantic_signal_id == "port:y" for b in artifact.bindings)
        # Verification overlays must not affect the production implementation.
        bare = replace(result.ir, verification_scopes=(), contracts=())
        assert emit_artifact(bare).text == artifact.text
        actual = [row["y"] for row in simulate_cycles(result.ir, rows, reset=resets)]
        assert actual == oracle(rows, resets, LATENCIES[top]), top

    arch = compilations["MathArchitecture"].ir.architecture_explorations[0]
    assert arch.selected_candidate.add_depth == 3
    assert any(c.name == "transposed" and not c.legal for c in arch.candidates)
    exploration = compilations["MathExplore"].exploration_results[0]
    assert exploration.selected_candidate.stages[-1] == "pipeline:balanced_levels_dsp"
    assert exploration.selected_candidate.timing_relation.delta == 4
    assert not exploration.search_complete  # Bounded, not a global optimum claim.
    assert "physical DSP mapping is not claimed" in compilations["MathExplore"].exploration_report


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


@pytest.mark.parametrize("backend", ("direct_sv", "clash"))
def test_math_example_real_rtl_matches_full_width_oracle(backend, compilations, tmp_path):
    verilator = shutil.which("verilator")
    if not verilator:
        pytest.skip("real Verilator required")
    clash = find_clash_executable()
    if backend == "clash" and not clash:
        pytest.skip("real Clash required; set ZLANG_CLASH or ZLANG_CLASH_ROOT")
    for top, compiled in compilations.items():
        root = tmp_path / top
        root.mkdir()
        if backend == "direct_sv":
            path = root / f"{top}.sv"
            path.write_text(emit_artifact(compiled.ir).text)
            files = [path]
        else:
            files = generate_verilog(compiled.clash, top, root / "rtl", clash)
        cpp = root / "test.cpp"
        cpp.write_text(cpp_testbench(top))
        completed = subprocess.run(
            [verilator, "--cc", "--exe", "--build", "--top-module", top,
             "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
             "--Mdir", str(root / "obj"), *(str(p) for p in files), str(cpp)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "CCACHE_DISABLE": "1"},
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        run = subprocess.run([str(root / "obj" / f"V{top}")],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stdout + run.stderr
