"""Naming is physical metadata: cache, manifest and VPI consumers agree."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.naming import RTL_NAMING_SCHEMA, module_rtl_names
from zlang.backend.systemverilog import emit_artifact, emit_formal_artifact
from zlang.backend.systemverilog.emitter import _formal_projection_name, _physicalize_generic_callables, physical_state_root_path
from zlang.backend.systemverilog.simulation_state import (
    SYSTEMVERILOG_SIMULATION_STATE_SCHEMA,
    SystemVerilogSimulationStateBundle,
    build_systemverilog_simulation_state_bundle,
)
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design
from zlang.formal_artifact_provider import FormalArtifactRecipe
import zlang.formal_candidate as candidate_module
from zlang.ir.hierarchy import build_hierarchy_index
from zlang.simulation_state import SimulationStateError
from zlang.verification_publication import _prepared_route_recipe


SIMPLE = "module NamingMetadata { in x:u8 out y:u8 y=x }"
ARRAY = """
module NamingLane {
    clock clk reset rst
    in step : u8
    in enable : bit
    out value : u8
    reg count : u8 = 0
    tick: when enable { count <- truncate<8>(count + step) }
    value = count
}
module NamingBank {
    clock clk reset rst
    in lane_0 : u8
    in enable : bit
    out values : vec<2,u8>
    lane[2] : NamingLane
    generate(i in 0..2) {
        lane[i].step = lane_0
        lane[i].enable = enable
    }
    values = generate(i in 0..2) lane[i].value
}
module NamingAccess {
    clock clk reset rst
    in step : u8
    in enable : bit
    out values : vec<2,u8>
    bank : NamingBank { lane_0 = step enable }
    values = bank.values
}
"""


def test_naming_metadata_round_trip_does_not_promote_manifest_or_semantics():
    compilation = compile_source(SIMPLE, include_clash=False)
    for emit in (emit_artifact, emit_clash_artifact):
        artifact = emit(compilation.ir)
        assert artifact.naming_schema == RTL_NAMING_SCHEMA
        assert artifact.manifest_version == 2
        assert not artifact.physical_domains
        restored = BackendArtifact.from_json(artifact.to_json())
        assert restored.naming_schema == RTL_NAMING_SCHEMA
        assert restored.build_identity == artifact.build_identity
        legacy = replace(artifact, naming_schema=None)
        assert legacy.text == artifact.text
        assert legacy.selected_ir_identity == artifact.selected_ir_identity
        assert legacy.artifact_hash == artifact.artifact_hash
        assert legacy.build_identity != artifact.build_identity
        assert "naming_schema" not in json.loads(legacy.to_json())
        assert BackendArtifact.from_json(legacy.to_json()).naming_schema is None


def test_naming_metadata_rejects_corruption_and_stale_build_identity():
    artifact = emit_artifact(compile_source(SIMPLE, include_clash=False).ir)
    payload = json.loads(artifact.to_json())
    for invalid in ("", "future-v900", 1, True, [], {}):
        corrupt = {**payload, "naming_schema": invalid}
        with pytest.raises(ValueError, match="naming_schema"):
            BackendArtifact.from_json(json.dumps(corrupt))
        with pytest.raises(ValueError, match="naming_schema"):
            replace(artifact, naming_schema=invalid).to_json()
    with pytest.raises(ValueError, match="build identity"):
        BackendArtifact.from_json(json.dumps({**payload, "naming_schema": None}))


def test_formal_preparation_recipes_bind_naming_version(monkeypatch):
    result = compile_source(SIMPLE, include_clash=False)
    current = _prepared_route_recipe(result, "direct_systemverilog")
    assert current["compiler"]["rtl_naming"] == RTL_NAMING_SCHEMA
    previous = deepcopy(current)
    previous["compiler"].pop("rtl_naming")
    assert FormalArtifactRecipe("prepared", "route", previous).identity != FormalArtifactRecipe("prepared", "route", current).identity
    expression = result.ir.assignments[0].expression
    candidate = SimpleNamespace(expression=expression, implementation_identity="candidate", stages=())
    monkeypatch.setattr(candidate_module, "_proof_bundle_tool_route", lambda _: {"clash": "test"})
    verifier = candidate_module.M36ClashCandidateVerifier(expression, candidate_class="M27")
    recipe = verifier.preparation_cache_recipe(candidate, candidate_module.FormalExplorationConfig())
    assert recipe["compiler_schema"]["rtl_naming"] == RTL_NAMING_SCHEMA
    assert recipe["bundle"]["rtl_naming"] == RTL_NAMING_SCHEMA


def _array_bundle():
    compilation = compile_source(ARRAY, include_clash=False)
    artifact = emit_artifact(compilation.ir, selected_ir_identity=compilation.selected_ir_identity)
    return compilation, artifact, build_systemverilog_simulation_state_bundle(compilation.ir, artifact)


def test_simulation_bundle_uses_containing_scope_and_rejects_old_schema():
    compilation, artifact, bundle = _array_bundle()
    hierarchy = build_hierarchy_index(compilation.ir)
    bank_names = module_rtl_names(hierarchy.at(("NamingAccess", "bank")).module)
    first = bank_names.instance("lane[0]")
    second = bank_names.instance("lane[1]")
    assert first != "lane_0"  # public input owns this name
    assert first.startswith("lane_0_")
    assert second == "lane_1"
    root_path = ".".join(physical_state_root_path(compilation.ir))
    assert {item.vpi_path for item in bundle.locators} == {
        f"{root_path}.bank.{first}.count", f"{root_path}.bank.{second}.count",
    }
    assert bundle.schema == SYSTEMVERILOG_SIMULATION_STATE_SCHEMA
    assert bundle.schema.endswith("-v2")
    assert SystemVerilogSimulationStateBundle.from_json(bundle.to_json()) == bundle
    with pytest.raises(SimulationStateError, match="schema"):
        SystemVerilogSimulationStateBundle.from_data({**bundle.to_data(), "schema": "zlang-systemverilog-simulation-state-v1"})
    with pytest.raises(SimulationStateError, match="naming schema"):
        build_systemverilog_simulation_state_bundle(compilation.ir, replace(artifact, naming_schema=None))


def test_vpi_scope_uses_physicalized_generic_helper_reservations():
    source = """
    fn identity<type T>(x:T) { x }
    module GenericChild {
        clock clk reset rst in step:u8 out y:u8
        reg r:u8=0 r<-step y=r
    }
    module GenericNames {
        clock clk reset rst in step:u8 out y:u8 out e:u8
        e=identity(step) slot:GenericChild { step } y=slot.y
    }
    """
    probe = compile_source(source, include_clash=False)
    physical = _physicalize_generic_callables(probe.ir)
    helper = next(f.name for f in physical.callable_definitions if f.name.startswith("zlang_spec_"))
    result = compile_source(source.replace("slot", helper), include_clash=False)
    artifact = emit_artifact(result.ir, selected_ir_identity=result.selected_ir_identity)
    bundle = build_systemverilog_simulation_state_bundle(result.ir, artifact)
    expected = module_rtl_names(_physicalize_generic_callables(result.ir)).instance(helper)
    assert expected != module_rtl_names(result.ir).instance(helper)
    assert bundle.locators[0].vpi_path == f"TOP.GenericNames.{expected}.r"
    assert f" {expected} (" in artifact.text


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_rr_observations_resolve_final_formal_and_generic_scopes(tmp_path: Path):
    source = (Path(__file__).resolve().parents[2] / "examples/hierarchical_request_response_m40.zhl").read_text()
    reserved_collision = source.replace("requester", "table").replace(
        "module HierarchicalRequestResponse {",
        "module HierarchicalRequestResponse { in zlang_table : bit",
    )
    generic = ("fn identity<type T>(x:T) {x}\n" + source).replace(
        "module HierarchicalRequestResponse {",
        "module HierarchicalRequestResponse { out echo:bit = identity(fire)",
    )
    probe = _physicalize_generic_callables(compile_source(generic, include_clash=False).ir)
    helper = next(f.name for f in probe.callable_definitions if f.name.startswith("zlang_spec_"))
    generic_collision = generic.replace("requester", helper)
    for index, variant in enumerate((reserved_collision, generic_collision)):
        result = compile_source(variant, include_clash=False)
        design = build_recursive_formal_design(result.ir)
        production = emit_artifact(result.ir, recursive_design=design)
        for binding in production.recursive_bindings:
            if not binding.local_semantic_id.startswith("rr:") or binding.signal_token is None:
                continue
            assert re.search(r"\blogic\b[^;\n]*\b" + re.escape(binding.signal_token) + r"(?:\s*[,;])", production.text)
        formal = emit_formal_artifact(result.ir, design)
        assert production == emit_artifact(result.ir, recursive_design=design)
        assert formal == emit_formal_artifact(result.ir, design)
        rtl = tmp_path / f"formal{index}.sv"
        rtl.write_text(formal.text)
        lint = subprocess.run(
            ["verilator", "--lint-only", "--sv", "-Wall", "-Wno-DECLFILENAME",
             "-Wno-UNUSEDSIGNAL", "-Wno-UNUSEDPARAM", str(rtl)],
            capture_output=True, text=True, timeout=30,
        )
        assert lint.returncode == 0, lint.stdout + lint.stderr
    # A user owns this input name; formal publication must allocate a distinct
    # output, never silently bind register:r to the existing environment input.
    collision = compile_source("""
    module T {
        clock clk reset rst in go:bit in zlang_formal_obs_4:u8 out y:u8
        reg r:u8=0 when go {r<-1} y=r
    }
    """, include_clash=False)
    formal = emit_formal_artifact(collision.ir, build_recursive_formal_design(collision.ir))
    binding = next(b for b in formal.recursive_bindings if b.local_semantic_id == "register:r")
    token = binding.formal_observation_token
    assert token is not None and token != "zlang_formal_obs_4"
    assert "input wire logic [7:0] zlang_formal_obs_4" in formal.text
    assert f"output logic [7:0] {token}" in formal.text
    assert f"assign {token} = r;" in formal.text
    assert BackendArtifact.from_json(formal.to_json()).recursive_bindings == formal.recursive_bindings
    rtl = tmp_path / "formal_observation_collision.sv"
    rtl.write_text(formal.text)
    lint = subprocess.run(
        ["verilator", "--lint-only", "--sv", "-Wall", "-Wno-DECLFILENAME",
         "-Wno-UNUSEDSIGNAL", "-Wno-UNUSEDPARAM", str(rtl)],
        capture_output=True, text=True, timeout=30,
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr
    local_name = _formal_projection_name((), "register:r")
    nested = compile_source(f"""
    module CollisionChild {{
        clock clk reset rst in go:bit in {local_name}:u8 out y:u8
        reg r:u8=0 when go {{r<-1}} y=r
    }}
    module CollisionParent {{
        clock clk reset rst in go:bit out y:u8
        child:CollisionChild {{ go {local_name}=7 }} y=child.y
    }}
    """, include_clash=False)
    artifact = emit_formal_artifact(nested.ir, build_recursive_formal_design(nested.ir))
    assert f"input wire logic [7:0] {local_name}" in artifact.text
    assert re.search(r"assign " + local_name + r"_\w+ = r;", artifact.text)
    rtl = tmp_path / "formal_local_observation_collision.sv"
    rtl.write_text(artifact.text)
    lint = subprocess.run(
        ["verilator", "--lint-only", "--sv", "-Wall", "-Wno-DECLFILENAME",
         "-Wno-UNUSEDSIGNAL", "-Wno-UNUSEDPARAM", str(rtl)],
        capture_output=True, text=True, timeout=30,
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_scoped_array_vpi_names_resolve_and_state_remains_independent(tmp_path: Path):
    _, artifact, bundle = _array_bundle()
    rtl = tmp_path / "NamingAccess.sv"
    rtl.write_text(artifact.text)
    bundle.publish(tmp_path / "access")
    by_path = {b.physical_instance_path: b.binding_id for b in bundle.catalog.bindings}
    first = by_path[("NamingAccess", "bank", "lane[0]")]
    second = by_path[("NamingAccess", "bank", "lane[1]")]
    harness = tmp_path / "sim.cpp"
    harness.write_text(f"""
#include "VNamingAccess.h"
#include "verilated.h"
#include "verilated_vpi.h"
#include "access/simulation_state.hpp"
static void tick(VNamingAccess& top) {{
  top.clk=0; top.eval(); top.clk=1; top.eval(); top.clk=0; top.eval();
}}
int main(int argc, char** argv) {{
  VerilatedContext context; context.commandArgs(argc, argv);
  VNamingAccess top{{&context}};
  top.clk=0; top.rst=1; top.enable=0; top.step=3; top.eval(); tick(top);
  top.rst=0; top.eval();
  zlang_simulation::StateAccess state;
  state.write_u64("{first}", 5);
  state.write_u64("{second}", 11);
  if (state.read_u64("{first}") != 5 || state.read_u64("{second}") != 11) return 1;
  top.enable=1; tick(top);
  if (state.read_u64("{first}") != 8 || state.read_u64("{second}") != 14) return 2;
  top.enable=0; tick(top);
  if (state.read_u64("{first}") != 8 || state.read_u64("{second}") != 14) return 3;
  top.rst=1; tick(top);
  if (state.read_u64("{first}") != 0 || state.read_u64("{second}") != 0) return 4;
  return 0;
}}
""")
    obj = tmp_path / "obj"
    built = subprocess.run(
        ["verilator", "--cc", "--exe", "--build", "--vpi", "--public-flat-rw",
         "-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL", "-Wno-UNUSEDPARAM",
         "--top-module", "NamingAccess", "--Mdir", str(obj), "-o", "state_names",
         str(rtl), str(harness), f"-I{tmp_path}"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "CCACHE_DISABLE": "1"},
    )
    assert built.returncode == 0, built.stdout + built.stderr
    run = subprocess.run([str(obj / "state_names")], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stdout + run.stderr
