"""Backend and artifact validation for the bounded FFT8 SDF composition.

This file deliberately limits itself to the three-stage hierarchy.  Numerical
stream behavior is covered separately; these tests make specialization,
companion, manifest, and physical-backend regressions independently visible.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.companions import publish_companion_bundle
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.build_manifest import WholeBuildManifest
from zlang.compiler import compile_file
from zlang.formal import build_recursive_formal_design
from zlang.opt import OptimizationStage, lower, restore
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples" / "fft" / "sdf_stage_numeric.zl"
TOP = "FFT8SDFReference"


def _compile():
    return compile_file(SOURCE, top=TOP, include_clash=False)


def _specializations(module):
    return {
        item.instance.name: {
            specialization.name: specialization.value
            for specialization in item.instance.specializations
        }
        for item in module.elaborated_instances
    }


def test_fft8_specializations_canonical_round_trip_and_generic_target() -> None:
    result = compile_file(
        SOURCE,
        top=TOP,
        target="xc7z030ffg676-1",
        include_clash=False,
    )
    module = result.ir
    assert _specializations(module) == {
        "stage_d4": {
            "S": "fixed<18,16>", "W": "fixed<16,14>",
            "D": 4, "CW": 3, "IW": 2,
        },
        "stage_d2": {
            "S": "fixed<18,16>", "W": "fixed<16,14>",
            "D": 2, "CW": 2, "IW": 1,
        },
        "stage_d1": {
            "S": "fixed<18,16>", "W": "fixed<16,14>",
            "D": 1, "CW": 1, "IW": 1,
        },
    }
    assert len({item.instance_identity for item in module.elaborated_instances}) == 3
    assert len({item.specialization_identity for item in module.elaborated_instances}) == 3
    assert {
        (
            edge.source.owner,
            edge.source.name,
            edge.destination.owner,
            edge.destination.name,
        )
        for edge in module.hierarchical_connections
    } == {
        (TOP, "input", "stage_d4", "input"),
        ("stage_d4", "output", "stage_d2", "input"),
        ("stage_d2", "output", "stage_d1", "input"),
        ("stage_d1", "output", TOP, "output"),
    }
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module
    assert result.target_planning_result is None
    assert result.target_planner_report == ""
    assert result.implementation_graph.is_generic
    assert result.implementation_graph.realization_backend == "backend_independent"
    assert result.implementation_graph.latency_knowledge == "unknown"


def test_fft8_recursive_bindings_keep_all_stage_widths_and_identities() -> None:
    module = _compile().ir
    design = build_recursive_formal_design(module)
    expected_specializations = {
        item.semantic_path: item.specialization_identity
        for item in module.elaborated_instances
    }
    expected_widths = {
        (TOP, "stage_d4"): 3,
        (TOP, "stage_d2"): 2,
        (TOP, "stage_d1"): 1,
    }
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
        assert len({item.ref.instance_identity for item in bindings.values()}) == 3


def test_fft8_artifacts_round_trip_hierarchy_and_three_distinct_roms() -> None:
    module = _compile().ir
    design = build_recursive_formal_design(module)
    expected_paths = {
        (TOP,),
        (TOP, "stage_d4"),
        (TOP, "stage_d2"),
        (TOP, "stage_d1"),
    }
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
        assert sorted(item.depth for item in artifact.companions) == [1, 2, 4]
        assert {item.word_width for item in artifact.companions} == {32}
        assert len({item.semantic_id for item in artifact.companions}) == 3
        assert len({item.content_hash for item in artifact.companions}) == 3

    assert {
        (item.depth, item.word_width, item.content_hash)
        for item in artifacts[0].companions
    } == {
        (item.depth, item.word_width, item.content_hash)
        for item in artifacts[1].companions
    }


def test_fft8_clash_calls_each_specialization_and_uses_exact_rom_shapes() -> None:
    module = _compile().ir
    helpers = {
        item.instance.name: (
            "protocol_fFTSDFStageNumeric_" + item.specialization_identity
        )
        for item in module.elaborated_instances
    }
    clash = emit_clash(module)
    for helper in helpers.values():
        assert clash.count(f"{helper} ::") == 1
    assert (
        f"stage_d4_result = {helpers['stage_d4']} "
        "parent_input stage_d2_input_ready"
    ) in clash
    assert (
        f"stage_d2_result = {helpers['stage_d2']} "
        "stage_d4_output stage_d1_input_ready"
    ) in clash
    assert (
        f"stage_d1_result = {helpers['stage_d1']} "
        "stage_d2_output output_backward"
    ) in clash

    regions = {}
    for name, helper in helpers.items():
        regions[name] = clash[
            clash.index(f"{helper}_raw ::") : clash.index(f"{helper} ::")
        ]
    assert "romFilePow2 @2 @32" in regions["stage_d4"]
    assert "romFilePow2 @1 @32" in regions["stage_d2"]
    assert "romFile (SNat @1)" in regions["stage_d1"]
    assert "romFilePow2" not in regions["stage_d1"]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_fft8_direct_sv_strict_lint_and_whole_build_publication(tmp_path: Path) -> None:
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
    assert sorted(item.depth for item in expected_companions) == [1, 2, 4]
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
def test_fft8_real_clash_generation_and_established_rom_lint_waiver(
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
            # Clash 1.11's romFile blackbox indexes the Verilog ROM with a
            # host-width Int.  This is the established generated-ROM waiver;
            # arithmetic and payload width diagnostics remain fatal.
            "-Wno-WIDTHTRUNC",
            str(rtl),
        ),
        cwd=rtl.parent,
        capture_output=True,
        text=True,
    )
    assert lint.returncode == 0, lint.stderr
