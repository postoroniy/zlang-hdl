"""ZL-015: shared stateful children are independent of parent catalogs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file, compile_source
from zlang.ir import Constant
from zlang.ir.callables import reachable_module_callables
from zlang.ir.hierarchy import (
    HierarchyError,
    build_hierarchy_index,
    specialization_fingerprint,
)
from zlang.ir.types import UIntType
from zlang.opt import lower, restore
from zlang.toolchain import generate_verilog, lint_with_verilator
from zlang.workspace import update_project_lock


COMMON_SOURCE = """
fn shared_mix(x : u8, seed : u8) -> u8 {
    truncate<8>(x + seed)
}

module SharedSeedRom {
    clock clk reset rst
    in address : u2
    out data : u8

    rom seeds : rom<u8,4> {
        read_latency 1
        init [3, 5, 7, 11]
    }
    seeds.read_address = address
    data = seeds.read_data
}

module SharedMath {
    clock clk reset rst
    in enable : bit
    in x : u8
    out y : u8

    child : SharedSeedRom
    reg value : u8 = 0

    child.address = truncate<2>(x)
    value <- enable ? shared_mix(x, child.data) : value
    y = value
}
"""


LEFT_SOURCE = """
import sharedspec.common

fn left_parent_only(x : u8) -> u8 { truncate<8>(x + 1) }

module LeftParent {
    clock clk reset rst
    in enable : bit
    in x : u8
    out y : u8

    child : SharedMath
    reg observed : u8 = 0

    child.enable = enable
    child.x = x
    observed <- left_parent_only(child.y)
    y = observed
}
"""


RIGHT_SOURCE = """
import sharedspec.common

fn right_parent_only_a(x : u8) -> u8 { truncate<8>(x + 2) }
fn right_parent_only_b(x : u8) -> u8 { right_parent_only_a(x) }

module RightParent {
    clock clk reset rst
    in enable : bit
    in x : u8
    out y : u8

    child : SharedMath
    reg observed : u8 = 0

    child.enable = enable
    child.x = x
    observed <- right_parent_only_b(child.y)
    y = observed
}
"""


TOP_SOURCE = """
import sharedspec.left
import sharedspec.right

module SharedStatefulTop {
    clock clk reset rst
    in enable : bit
    in x : u8
    out left : u8
    out right : u8

    left_parent : LeftParent
    right_parent : RightParent
    left_parent.enable = enable
    left_parent.x = x
    right_parent.enable = enable
    right_parent.x = x
    left = left_parent.y
    right = right_parent.y
}
"""


FINGERPRINT_SOURCE = """
fn reachable(x : u8) -> u8 { truncate<8>(x + 1) }
fn unused(x : u8) -> u8 { truncate<8>(x + 2) }

module FingerprintChild {
    in x : u8
    out y : u8
    y = reachable(x)
}

module FingerprintTop {
    in x : u8
    out a : u8
    out b : u8
    first : FingerprintChild
    second : FingerprintChild
    first.x = x
    second.x = x
    a = first.y
    b = second.y
}
"""


def _write_project(root: Path) -> tuple[Path, Path]:
    source_root = root / "src"
    source_root.mkdir(parents=True)
    manifest = root / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="sharedspec"\nversion="1"\n'
        'source-root="src"\n'
    )
    (source_root / "common.zhl").write_text(COMMON_SOURCE)
    (source_root / "left.zhl").write_text(LEFT_SOURCE)
    (source_root / "right.zhl").write_text(RIGHT_SOURCE)
    top = source_root / "top.zhl"
    top.write_text(TOP_SOURCE)
    update_project_lock(manifest)
    return manifest, top


def _compile_project(tmp_path: Path):
    manifest, top = _write_project(tmp_path / "shared-project")
    return compile_file(
        top,
        project=manifest,
        top="SharedStatefulTop",
        include_clash=False,
    )


def test_shared_stateful_specialization_is_parent_catalog_independent(
    tmp_path: Path,
) -> None:
    first = _compile_project(tmp_path)
    module = first.ir
    assert restore(lower(module)) == module

    hierarchy = build_hierarchy_index(module)
    shared_math = tuple(
        item for item in hierarchy.specializations
        if item.key.module_name == "SharedMath"
    )
    seed_rom = tuple(
        item for item in hierarchy.specializations
        if item.key.module_name == "SharedSeedRom"
    )
    assert len(shared_math) == 1
    assert len(seed_rom) == 1
    assert shared_math[0].occurrence_paths == (
        ("SharedStatefulTop", "left_parent", "child"),
        ("SharedStatefulTop", "right_parent", "child"),
    )
    assert seed_rom[0].occurrence_paths == (
        ("SharedStatefulTop", "left_parent", "child", "child"),
        ("SharedStatefulTop", "right_parent", "child", "child"),
    )

    left_math = hierarchy.at(shared_math[0].occurrence_paths[0]).module
    right_math = hierarchy.at(shared_math[0].occurrence_paths[1]).module
    assert tuple(item.name for item in left_math.functions) != tuple(
        item.name for item in right_math.functions
    )
    assert tuple(
        item.name for item in reachable_module_callables(left_math)
    ) == ("shared_mix",)
    assert tuple(
        item.name for item in reachable_module_callables(right_math)
    ) == ("shared_mix",)

    left_seed = hierarchy.at(seed_rom[0].occurrence_paths[0]).module
    right_seed = hierarchy.at(seed_rom[0].occurrence_paths[1]).module
    assert tuple(item.name for item in left_seed.functions) != tuple(
        item.name for item in right_seed.functions
    )
    assert reachable_module_callables(left_seed) == ()
    assert reachable_module_callables(right_seed) == ()

    direct = emit_sv_artifact(module)
    clash = emit_clash_artifact(module)
    second = compile_file(
        tmp_path / "shared-project" / "src" / "top.zhl",
        project=tmp_path / "shared-project" / "zlang.toml",
        top="SharedStatefulTop",
        include_clash=False,
    ).ir
    assert emit_sv_artifact(second) == direct
    assert emit_clash_artifact(second) == clash
    assert direct.text.count("module SharedMath") == 1
    suffix = shared_math[0].key.specialization_identity[:8]
    assert clash.text.count(f"sharedMath_s{suffix} ::") == 1


def test_specialization_fingerprint_tracks_only_reachable_callable_bodies() -> None:
    module = compile_source(
        FINGERPRINT_SOURCE,
        top="FingerprintTop",
        include_clash=False,
    ).ir
    child = module.children[0]
    reachable = next(item for item in child.functions if item.name == "reachable")
    unused = next(item for item in child.functions if item.name == "unused")
    baseline = specialization_fingerprint(child)

    unused_changed = replace(
        child,
        functions=tuple(
            replace(item, body=Constant(99, UIntType(8)))
            if item.callee_identity == unused.callee_identity
            else item
            for item in child.functions
        ),
    )
    assert specialization_fingerprint(unused_changed) == baseline

    reachable_changed = replace(
        child,
        functions=tuple(
            replace(item, body=Constant(99, UIntType(8)))
            if item.callee_identity == reachable.callee_identity
            else item
            for item in child.functions
        ),
    )
    assert specialization_fingerprint(reachable_changed) != baseline

    first, second = module.elaborated_instances
    malformed = replace(
        module,
        children=(child, reachable_changed),
        elaborated_instances=(
            first,
            replace(second, specialization_identity=first.specialization_identity),
        ),
    )
    with pytest.raises(
        HierarchyError,
        match="specialization identity .* is reused for incompatible",
    ):
        build_hierarchy_index(malformed)


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_shared_stateful_specialization_generates_real_backend_rtl(
    tmp_path: Path,
) -> None:
    module = _compile_project(tmp_path).ir
    direct = emit_sv_artifact(module)
    direct_path = tmp_path / "direct" / "SharedStatefulTop.sv"
    direct_path.parent.mkdir()
    direct_path.write_text(direct.text)
    for companion in direct.companions:
        (direct_path.parent / companion.logical_path).write_text(companion.text)
    lint_with_verilator((direct_path,), "SharedStatefulTop")

    clash = emit_clash_artifact(module)
    rtl = generate_verilog(
        clash.text,
        "SharedStatefulTop",
        tmp_path / "clash",
        CLASH_EXECUTABLE,
        companions=clash.companions,
    )
    # Clash 1.11 widens the index of its generated ROM array to host Int.
    # Acknowledge only that known primitive warning; all other warnings remain
    # fatal, as in the standalone ROM backend regression.
    completed = subprocess.run(
        [
            str(shutil.which("verilator")),
            "--lint-only",
            "-Wno-WIDTHTRUNC",
            "--top-module",
            "SharedStatefulTop",
            *(str(path) for path in rtl),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
