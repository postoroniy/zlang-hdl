"""Bounded structural/backend gate for the FFT512 SDF reference."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.build_manifest import WholeBuildManifest
from zlang.compiler import compile_file
from zlang.formal import build_recursive_formal_design
from zlang.opt import OptimizationStage, lower, restore
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples" / "fft" / "sdf_stage_numeric.zhl"
TOP = "FFT512SDFReference"
STAGES = (
    ("stage_d256", 256, 9, 8),
    ("stage_d128", 128, 8, 7),
    ("stage_d64", 64, 7, 6),
    ("stage_d32", 32, 6, 5),
    ("stage_d16", 16, 5, 4),
    ("stage_d8", 8, 4, 3),
    ("stage_d4", 4, 3, 2),
    ("stage_d2", 2, 2, 1),
    ("stage_d1", 1, 1, 1),
)
DEPTHS = tuple(item[1] for item in STAGES)


def _compile():
    return compile_file(SOURCE, top=TOP, include_clash=False)


def test_fft512_exact_specializations_edges_canonical_and_generic_target() -> None:
    result = compile_file(
        SOURCE,
        top=TOP,
        target="xc7z030ffg676-1",
        include_clash=False,
    )
    module = result.ir
    actual = {
        item.instance.name: {
            specialization.name: specialization.value
            for specialization in item.instance.specializations
        }
        for item in module.elaborated_instances
    }
    assert actual == {
        name: {
            "S": "fixed<18,16>",
            "W": "fixed<16,14>",
            "D": depth,
            "CW": counter_width,
            "IW": index_width,
        }
        for name, depth, counter_width, index_width in STAGES
    }
    assert len({item.instance_identity for item in module.elaborated_instances}) == 9
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 9
    expected_edges = {(TOP, "input", STAGES[0][0], "input")}
    expected_edges.update(
        (left[0], "output", right[0], "input")
        for left, right in zip(STAGES, STAGES[1:])
    )
    expected_edges.add((STAGES[-1][0], "output", TOP, "output"))
    assert len(expected_edges) == 10
    assert {
        (
            edge.source.owner,
            edge.source.name,
            edge.destination.owner,
            edge.destination.name,
        )
        for edge in module.hierarchical_connections
    } == expected_edges
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module
    assert result.target_planning_result is None
    assert result.target_planner_report == ""
    assert result.implementation_graph.is_generic
    assert result.implementation_graph.realization_backend == "backend_independent"
    assert result.implementation_graph.latency_knowledge == "unknown"


def test_fft512_recursive_bindings_keep_widths_paths_and_identities() -> None:
    module = _compile().ir
    design = build_recursive_formal_design(module)
    expected_widths = {
        (TOP, name): counter_width
        for name, _depth, counter_width, _index_width in STAGES
    }
    expected_specializations = {
        item.semantic_path: item.specialization_identity
        for item in module.elaborated_instances
    }
    assert sorted(expected_widths.values(), reverse=True) == list(range(9, 0, -1))
    for semantic_id in ("register:phase", "fifo:feedback.count"):
        bindings = {
            item.physical_instance_path: item
            for item in design.bindings
            if item.ref.local_semantic_id == semantic_id
        }
        assert set(bindings) == set(expected_widths)
        for path, width in expected_widths.items():
            assert bindings[path].width == width
            assert (
                bindings[path].specialization_identity
                == expected_specializations[path]
            )
        assert len({item.ref.instance_identity for item in bindings.values()}) == 9


def test_fft512_artifacts_are_deterministic_and_preserve_nine_roms() -> None:
    module = _compile().ir
    design = build_recursive_formal_design(module)
    expected_paths = {(TOP,)} | {(TOP, name) for name, *_ in STAGES}
    artifacts = tuple(
        emitter(module, recursive_design=design)
        for emitter in (emit_sv_artifact, emit_clash_artifact)
    )
    for artifact, emitter in zip(
        artifacts, (emit_sv_artifact, emit_clash_artifact), strict=True
    ):
        repeated = emitter(module, recursive_design=design)
        assert repeated.text == artifact.text
        assert repeated.artifact_hash == artifact.artifact_hash
        restored = BackendArtifact.from_json(artifact.to_json())
        assert restored.components == artifact.components
        assert restored.instances == artifact.instances
        assert restored.recursive_bindings == artifact.recursive_bindings
        assert restored.artifact_hash == artifact.artifact_hash
        assert {item.physical_instance_path for item in artifact.instances} == expected_paths
        assert sorted(
            (item.depth for item in artifact.companions), reverse=True
        ) == list(DEPTHS)
        assert {item.word_width for item in artifact.companions} == {32}
        assert len({item.semantic_id for item in artifact.companions}) == 9
        assert len({item.content_hash for item in artifact.companions}) == 9

    assert {
        (item.depth, item.word_width, item.content_hash)
        for item in artifacts[0].companions
    } == {
        (item.depth, item.word_width, item.content_hash)
        for item in artifacts[1].companions
    }


def test_fft512_clash_dispatches_exact_helpers_and_rom_shapes() -> None:
    module = _compile().ir
    helpers = {
        item.instance.name: (
            "protocol_fFTSDFStageNumeric_s" + item.specialization_identity[:8]
        )
        for item in module.elaborated_instances
    }
    clash = emit_clash(module)
    for helper in helpers.values():
        assert clash.count(f"{helper} ::") == 1
    for index, (name, *_rest) in enumerate(STAGES):
        forward = "parent_input" if index == 0 else f"{STAGES[index - 1][0]}_output"
        backward = (
            "output_backward"
            if index == len(STAGES) - 1
            else f"{STAGES[index + 1][0]}_input_ready"
        )
        assert f"{name}_result = {helpers[name]} {forward} {backward}" in clash

    regions = {
        name: clash[
            clash.index(f"{helper}_raw ::") : clash.index(f"{helper} ::")
        ]
        for name, helper in helpers.items()
    }
    for name, depth, _counter_width, _index_width in STAGES[:-1]:
        exponent = depth.bit_length() - 1
        assert f"romFilePow2 @{exponent} @32" in regions[name]
    assert "romFile (SNat @1)" in regions["stage_d1"]
    assert "romFilePow2" not in regions["stage_d1"]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_fft512_direct_sv_strict_lint_and_whole_build_publication(
    tmp_path: Path,
) -> None:
    rtl = tmp_path / f"{TOP}.sv"
    source_map = tmp_path / f"{TOP}.source-map.json"
    build_manifest = tmp_path / f"{TOP}.build.json"
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "zlang.cli",
            str(SOURCE),
            "--top",
            TOP,
            "--systemverilog",
            str(rtl),
            "--source-map",
            str(source_map),
            "--build-manifest",
            str(build_manifest),
        ),
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""

    lint = subprocess.run(
        (
            "verilator",
            "--lint-only",
            "--timing",
            "--top-module",
            TOP,
            "-Wno-DECLFILENAME",
            "-Wno-UNUSEDSIGNAL",
            "-Wno-UNUSEDPARAM",
            "-Wno-UNDRIVEN",
            str(rtl),
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert lint.returncode == 0, lint.stderr

    source_map_data = json.loads(source_map.read_text())
    assert source_map_data["backend"] == "direct_systemverilog"
    assert source_map_data["module"] == TOP
    assert isinstance(source_map_data["entries"], list)
    manifest = WholeBuildManifest.from_json(build_manifest.read_text())
    assert len(manifest.backend_builds) == 1
    backend = manifest.backend_builds[0]
    assert backend.backend == "direct_systemverilog"
    expected_companions = emit_sv_artifact(_compile().ir).companions
    assert sorted(
        (item.depth for item in expected_companions), reverse=True
    ) == list(DEPTHS)
    assert {
        (Path(item.logical_path).name, item.content_hash)
        for item in backend.companions
    } == {
        (Path(item.logical_path).name, item.file_hash)
        for item in expected_companions
    }
    assert all(
        (rtl.parent / Path(item.logical_path).name).is_file()
        for item in backend.companions
    )


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash and Verilator are required",
)
def test_fft512_real_clash_generation_and_established_rom_lint_waiver(
    tmp_path: Path,
) -> None:
    module = _compile().ir
    artifact = emit_clash_artifact(module)
    rtl = generate_verilog(
        artifact.text,
        TOP,
        tmp_path / "clash",
        find_clash_executable(),
        companions=artifact.companions,
    )[0]
    lint = subprocess.run(
        (
            "verilator",
            "--lint-only",
            "--timing",
            "--top-module",
            TOP,
            "-Wno-DECLFILENAME",
            "-Wno-UNUSEDSIGNAL",
            "-Wno-UNUSEDPARAM",
            "-Wno-UNDRIVEN",
            # Clash 1.11 romFile indexes generated ROMs with a host-width Int.
            "-Wno-WIDTHTRUNC",
            str(rtl),
        ),
        cwd=rtl.parent,
        capture_output=True,
        text=True,
    )
    assert lint.returncode == 0, lint.stderr
