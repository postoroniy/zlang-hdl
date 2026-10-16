# SPDX-License-Identifier: Apache-2.0
"""Exact three-product complex arithmetic and actual Xilinx cell counts."""

from itertools import product
import json
from pathlib import Path
import random
import re
import shutil
import subprocess

import pytest

from zlang import sim
from zlang.backend.systemverilog import emit
from zlang.backend.systemverilog.target import emit_target, emit_target_artifact
from zlang.compiler import compile_file
from zlang.simulation_compare import compare_program
import zlang.simulation_compare as comparison_api


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples/fft/complex_multiply_three_dsp.zhl"
TOPS = ("ComplexMultiply3DSP", "ComplexMultiply4DSP")


def _vectors(*, corners=(-32768, -32767, -1, 0, 1, 32766, 32767), per_seed=256):
    yield from product(corners, repeat=4)
    for seed in (7, 42, 170):
        rng = random.Random(seed)
        for _ in range(per_seed):
            yield tuple(rng.randrange(-32768, 32768) for _ in range(4))


def _events(vectors):
    return [
        {"reset": {"rst": True}, "edges": ("clk",)},
        *(
            {
                "reset": {"rst": False},
                "set": {"a": {"re": ar, "im": ai}, "b": {"re": br, "im": bi}},
                "edges": ("clk",),
            }
            for ar, ai, br, bi in vectors
        ),
    ]


def test_complex_product_contract_is_joint_latency_one_ii_one() -> None:
    for top, expected in zip(TOPS, (3, 4), strict=True):
        compiled = compile_file(SOURCE, top=top, target="xc7z030ffg676-1")
        (selected,) = [item.selected for item in compiled.exploration_results]
        assert selected.cost.latency.value == 1
        assert selected.cost.ii.value == 1
        assert selected.cost.dsp.value == expected
        assert len(compiled.implementation_graph.resources) == expected


def test_physical_helpers_cannot_collide_with_public_names(tmp_path):
    from zlang.compiler import compile_source
    from tests.semantic.test_bilinear_intent import source

    text = source(2)
    for old, new in (("a", "product0"), ("b", "product0_p"), ("y", "joint_output")):
        text = re.sub(rf"\b{old}\b", new, text)
    compiled = compile_source(text, target="xc7z030ffg676-1")
    rtl = emit_target(compiled.ir, compiled.implementation_graph, simulation_model=True)
    assert rtl == emit_target(
        compiled.ir, compiled.implementation_graph, simulation_model=True
    )
    path = tmp_path / "collision.sv"
    path.write_text(rtl)
    result = subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-s",
            "BilinearWitness",
            "-o",
            str(tmp_path / "test.vvp"),
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("top", TOPS)
def test_three_and_four_products_match_exact_signed_arithmetic(top) -> None:
    vectors = list(_vectors())
    with sim.load(SOURCE, top=top) as dut:
        samples = dut.run_events(_events(vectors))
    assert samples[0] == {"product": {"re": 0, "im": 0}}
    for (ar, ai, br, bi), sample in zip(vectors, samples[1:], strict=True):
        assert sample == {"product": {"re": ar * br - ai * bi, "im": ar * bi + ai * br}}


@pytest.mark.parametrize(("top", "expected_dsp"), zip(TOPS, (3, 4), strict=True))
@pytest.mark.parametrize("explicit_target", (False, True))
def test_xilinx_synthesis_uses_exactly_three_or_four_dsp48e1(
    tmp_path,
    top,
    expected_dsp,
    explicit_target,
) -> None:
    yosys = shutil.which("yosys")
    assert yosys is not None, "the release-pinned OSS CAD Suite is required"
    rtl = tmp_path / "complex.sv"
    compiled = compile_file(
        SOURCE, top=top, target="xc7z030ffg676-1" if explicit_target else None
    )
    rtl.write_text(
        emit_target(compiled.ir, compiled.implementation_graph)
        if explicit_target
        else emit(compiled.ir)
    )
    netlist = tmp_path / "mapped.json"
    result = subprocess.run(
        [
            yosys,
            "-Q",
            "-T",
            "-p",
            f"read_verilog -sv {rtl}; synth_xilinx -family xc7 -top {top} "
            f"-flatten -noiopad -noclkbuf; check; stat; write_json {netlist}",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cells = json.loads(netlist.read_text())["modules"][top]["cells"]
    assert sum(cell["type"] == "DSP48E1" for cell in cells.values()) == expected_dsp


@pytest.mark.parametrize("top", TOPS)
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_complex_product_native_matches_generated_rtl(
    tmp_path,
    top,
    simulator,
) -> None:
    # Icarus covers the full matrix; bound Verilator's unrolled C++ testbench.
    vectors = list(
        _vectors()
        if simulator == "iverilog"
        else _vectors(corners=(-32768, 0, 32767), per_seed=32)
    )
    comparison = compare_program(
        sim.compile(SOURCE, top=top),
        _events(vectors),
        simulator=simulator,
        artifact_directory=tmp_path / "comparison",
    )
    assert len(comparison.native_outputs) == len(vectors) + 1


@pytest.mark.parametrize("top", TOPS)
@pytest.mark.parametrize("simulator", ("iverilog", "verilator"))
def test_explicit_dsp_bindings_match_native(tmp_path, monkeypatch, top, simulator):
    compiled = compile_file(SOURCE, top=top, target="xc7z030ffg676-1")
    monkeypatch.setattr(
        comparison_api,
        "emit_artifact",
        lambda module, **kwargs: emit_target_artifact(
            module, compiled.implementation_graph, simulation_model=True, **kwargs
        ),
    )
    vectors = list(_vectors(corners=(-32768, 0, 32767), per_seed=16))
    result = comparison_api.compare_program(
        sim.compile(SOURCE, top=top),
        _events(vectors),
        simulator=simulator,
        artifact_directory=tmp_path / "physical",
    )
    assert len(result.native_outputs) == len(vectors) + 1
