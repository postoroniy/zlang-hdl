"""Accepted rule-fire observations share the production physical reset epoch."""

from __future__ import annotations

from itertools import product
from copy import deepcopy
from pathlib import Path
import os
import re
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_artifact, emit_formal_artifact
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design
from zlang.formal_artifact_provider import FormalArtifactProvider, FormalArtifactRecipe
from zlang.verification_publication import _prepared_route_recipe


PROFILES = tuple(product(("sync", "raw", "safe"), (False, True), (False, True)))


def _source(name: str, mode: str, low: bool, falling: bool, nested: bool) -> str:
    clock = "clock clk { edge falling }" if falling else "clock clk"
    polarity = "active_low" if low else "active_high"
    reset = (
        f"async reset rst @clk {{ polarity {polarity} }}"
        if mode == "safe" else
        f"reset rst @clk {{ mode {'asynchronous' if mode == 'raw' else 'synchronous'} "
        f"polarity {polarity} power_up unspecified }}"
    )

    def component(module_name: str, child: str | None) -> str:
        descendant = "" if child is None else f"""
            child : {child} {{ go }}
            out child_y : u8 = child.y
        """
        return f"""
        module {module_name} {{
            {clock} {reset}
            in go : bit
            out y : u8
            reg r : u8 = 0
            tick: when go {{ r <- truncate<8>(r + 1) }}
            y = r
            {descendant}
        }}
        """

    if not nested:
        return component(name, None)
    return (
        component(name + "Leaf", None)
        + component(name + "Middle", name + "Leaf")
        + component(name, name + "Middle")
    )


@pytest.fixture(scope="module")
def artifacts():
    values = []
    for ordinal, (profile, nested) in enumerate(product(PROFILES, (False, True))):
        name = f"RuleReset{ordinal}"
        module = compile_source(
            _source(name, *profile, nested), top=name,
            source_unit=f"rule-reset-{ordinal}.zhl",
        ).ir
        production = emit_artifact(module)
        formal = emit_formal_artifact(module, build_recursive_formal_design(module))
        assert emit_artifact(module) == production
        assert emit_formal_artifact(module, build_recursive_formal_design(module)) == formal
        values.append((profile, nested, formal))
    return values


def test_formal_rule_reset_is_typed_deterministic_and_conditioned_once(artifacts):
    for (mode, low, falling), nested, formal in artifacts:
        assert formal.text.count('(* ASYNC_REG = "TRUE" *)') == (mode == "safe")
        fires = [b for b in formal.recursive_bindings if b.local_semantic_id == "rule:tick.fire"]
        assert len(fires) == (3 if nested else 1)
        assert all(b.physical_available and b.formal_observation_token for b in fires)
        registers = [b for b in formal.recursive_bindings if b.local_semantic_id == "register:r"]
        assert len(registers) == len(fires)
        assert {b.physical_instance_path for b in fires} == {b.physical_instance_path for b in registers}
        assert all(b.source_origin is not None for b in fires)
        edge = "negedge" if falling else "posedge"
        assert f"always_ff @({edge} clk" in formal.text
        # The root expression consumes the exact allocated conditioner, not
        # the raw reset; the child expressions consume their native ABI port.
        root_fire = next(b for b in fires if len(b.physical_instance_path) == 1)
        assignment = next(line for line in formal.text.splitlines() if f"assign {root_fire.formal_observation_token} =" in line)
        if mode == "safe":
            effective = re.search(r"logic (zlang_reset_effective_\w+);", formal.text)
            assert effective is not None and effective.group(1) in assignment
        assert f"1'd{int(low)}" in assignment


def test_prepared_formal_cache_cannot_reuse_raw_rule_reset_recipe():
    result = compile_source(
        _source("ResetCache", "safe", True, False, False), source_unit="rule-reset-cache.zhl",
    )
    current = _prepared_route_recipe(result, "direct_systemverilog")
    assert current["compiler"]["verification_publication"] == 7
    previous = deepcopy(current)
    previous["compiler"]["verification_publication"] = 6
    kind = "direct-systemverilog-formal-route"
    assert FormalArtifactRecipe("prepared", kind, previous).identity != FormalArtifactRecipe("prepared", kind, current).identity
    provider = FormalArtifactProvider()
    provider.get_or_prepare("prepared", kind, previous, lambda: "old-reset", fingerprint=lambda value: value)
    assert provider.get_or_prepare("prepared", kind, current, lambda: "effective-reset", fingerprint=lambda value: value) == "effective-reset"


def _testbench(artifacts):
    declarations, instances, checks = [], [], []
    for i, ((mode, low, falling), nested, artifact) in enumerate(artifacts):
        declarations += [f"logic clk{i} = 1'b{int(falling)}, rst{i} = 1'b{int(low)}, go{i} = 0;", f"wire [7:0] y{i};"]
        connections = [f".clk(clk{i})", f".rst(rst{i})", f".go(go{i})", f".y(y{i})"]
        if nested:
            connections.append(".child_y()")
        bindings = {b.formal_observation_token: b for b in artifact.recursive_bindings if b.physical_available}
        fire_names, state_names = [], []
        for token, binding in sorted(bindings.items()):
            local = binding.local_semantic_id
            name = f"case{i}_{token}"
            if local in {"rule:tick.fire", "register:r"}:
                width = "" if local == "rule:tick.fire" else "[7:0] "
                declarations.append(f"wire {width}{name};")
                connections.append(f".{token}({name})")
                (fire_names if local == "rule:tick.fire" else state_names).append(name)
            else:
                connections.append(f".{token}()")
        top = next(b.rtl_module for b in bindings.values())
        instances.append(f"{top} dut{i} ({', '.join(connections)});")
        external, released, count, go = False, 2, 0, False

        def sampled():
            fire = int(go and not external and not (mode == "safe" and released < 2))
            return [f"if ({name} !== 1'b{fire}) $fatal(1, \"fire case {i}\");" for name in fire_names]

        def state():
            return [f"if ({name} !== 8'd{count}) $fatal(1, \"state case {i}\");" for name in state_names]

        def reset(asserted):
            nonlocal external, released, count
            external = asserted
            if asserted:
                released = 0
                if mode != "sync":
                    count = 0
            checks.append(f"rst{i} = 1'b{int(asserted != low)}; #2;")
            checks.extend(sampled())
            if mode != "sync":
                checks.extend(state())

        def enable(value):
            nonlocal go
            go = value
            checks.append(f"go{i} = 1'b{int(value)}; #2;")
            checks.extend(sampled())

        def tick():
            nonlocal count, released
            checks.extend(sampled())
            if external or (mode == "safe" and released < 2):
                count = 0
            elif go:
                count += 1
            if not external:
                released = min(2, released + 1)
            checks.append(f"clk{i} = 1'b{int(not falling)}; #2;")
            checks.extend(state())
            checks.extend(sampled())
            checks.append(f"clk{i} = 1'b{int(falling)}; #2;")

        # Reset assertion and release occur strictly between active edges.
        reset(True)
        enable(True)
        tick()
        reset(False)
        tick()  # first release edge
        reset(True)  # discard a partial release sequence
        tick()
        reset(False)
        tick()
        tick()  # second release edge still commits reset state
        tick()  # third edge accepts the first post-reset action
        tick()
        enable(False)
        tick()
        enable(True)
        tick()
        reset(True)  # asynchronous state must clear before any clock edge
        tick()
        reset(False)
        tick()
        tick()
        tick()
    return "\n".join([
        "`default_nettype none", "module tb;", *declarations, *instances,
        "initial begin", "#2;", *checks, '$display("rule reset profiles passed");',
        "$finish; end", "endmodule", "`default_nettype wire", "",
    ])


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_formal_rule_fire_matches_state_across_all_reset_profiles(artifacts, tmp_path: Path):
    # One strict real-RTL build exercises all 12 physical profiles in both
    # standalone and depth-two hierarchy, not 24 separate tool invocations.
    rtl = tmp_path / "design.sv"
    rtl.write_text("\n".join(artifact.text for _, _, artifact in artifacts))
    tb = tmp_path / "tb.sv"
    tb.write_text(_testbench(artifacts))
    obj = tmp_path / "obj"
    build = subprocess.run(
        ["verilator", "--binary", "--timing", "-Wall", "-Wno-DECLFILENAME",
         "-Wno-UNUSEDSIGNAL", "-Wno-UNUSEDPARAM", "-Wno-PINCONNECTEMPTY",
         "--top-module", "tb", "--Mdir", str(obj), str(rtl), str(tb)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "CCACHE_DISABLE": "1"},
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(obj / "Vtb")], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "rule reset profiles passed" in run.stdout
