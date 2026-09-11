"""ZL-015: shared stateful children are independent of parent catalogs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import subprocess

import pytest

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
from zlang.toolchain import lint_with_verilator
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
    )




def test_specialization_fingerprint_tracks_only_reachable_callable_bodies() -> None:
    module = compile_source(
        FINGERPRINT_SOURCE,
        top="FingerprintTop",
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
