"""Equivalence lowering must expose actual bindings without weakening the miter."""

import json
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from tests.integration.test_signed_product_pipeline_physical import (
    SMALL_FORMAL_SOURCE, TARGET,
)
from tests.semantic.test_candidate_equivalence_orchestration import _prepared
from zlang.candidate_equivalence import _semantic_equivalence_execution_recipe
from zlang.compiler import compile_source
from zlang.costs import CandidateCost
from zlang.equivalence import run_equivalence_formal
from zlang.formal import FormalToolchainContext, run_verilog_formal
from zlang.formal_candidate import (
    PhysicalTargetFormalCandidate,
    SemanticEquivalenceDirectSystemVerilogCandidateVerifier,
    prepared_candidate_equivalence_from_data,
    prepared_candidate_equivalence_to_data,
)
from zlang.formal_configuration import EQUIVALENCE_PREPARATION_SCHEMA
from zlang.formal_exploration import FormalExplorationConfig
from zlang.ir.equivalence import EquivalenceMode, EquivalenceStatus
from zlang.ir.formal import FormalResult, FormalStatus
from zlang.pipeline_scheduling import erase_pipeline_timing


@pytest.mark.parametrize("flatten", (False, True))
def test_low_level_formal_flatten_is_explicit(flatten, tmp_path, monkeypatch):
    context = SimpleNamespace(missing=(), versions=())
    monkeypatch.setattr("zlang.formal.resolve_formal_toolchain", lambda **_: context)
    monkeypatch.setattr("zlang.formal.run_bounded_process", lambda *_, **__: CompletedProcess("sby", 0, "", ""))
    monkeypatch.setattr("zlang.formal._read_sby_status", lambda *_: (
        SimpleNamespace(state="PASS", return_code=0, engine_code=0), None,
    ))
    result = run_verilog_formal(
        "module m; always @* assert(1); endmodule", top="m", property_id="p",
        depth=8, timeout_seconds=30, work_directory=tmp_path, flatten=flatten,
    )
    assert result.status is FormalStatus.BOUNDED_PASS
    config = (tmp_path / "m.sby").read_text()
    assert ("prep -top m -flatten" in config) is flatten
    assert "depth 8" in config
    assert "smtbmc z3" in config


@pytest.mark.parametrize("mode", (EquivalenceMode.BMC, EquivalenceMode.PROVE))
def test_equivalence_requests_flatten_without_changing_obligations(mode, monkeypatch):
    bundle = _prepared()
    seen = {}

    def execute(source, **options):
        seen.update(options, source=source)
        return FormalResult(
            options["property_id"], FormalStatus.UNKNOWN, options["mode"],
            "sby", "z3", options["depth"], reason="deliberately inconclusive",
        )

    monkeypatch.setattr("zlang.formal.run_verilog_formal", execute)
    result = run_equivalence_formal(
        bundle.property, bundle.source, top=bundle.top, mode=mode,
        depth=8, timeout_seconds=30, trace_metadata=bundle.trace_metadata,
    )
    assert seen["flatten"] is True
    assert seen["source"] == bundle.source
    assert seen["depth"] == 8 and seen["timeout_seconds"] == 30
    assert seen["mode"].value == mode.value
    assert result.status is EquivalenceStatus.UNKNOWN


def test_preparation_policy_versions_every_cache_boundary(monkeypatch):
    import zlang.formal_candidate as preparation

    compiled = compile_source(SMALL_FORMAL_SOURCE, target=TARGET)
    graph = compiled.implementation_graph
    value = compiled.ir.assignments[0].expression
    candidate = PhysicalTargetFormalCandidate(
        expression=value, module=compiled.ir, implementation_graph=graph,
        semantic_identity=graph.semantic_region_identity,
        implementation_identity=graph.identity,
        cost=CandidateCost.estimate(lut=0, ff=0, dsp=2, latency=3, ii=1),
    )
    verifier = SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        erase_pipeline_timing(value), candidate_class="pipeline",
        clock_domain_contract=compiled.ir.clock_domains[0],
    )
    config = FormalExplorationConfig()
    tools = FormalToolchainContext.discover()
    first = verifier.prepare(candidate, config)
    recipe = verifier.preparation_cache_recipe(candidate, config)
    execution = _semantic_equivalence_execution_recipe(first, config, EquivalenceMode.BMC, tools)
    assert recipe["equivalence_preparation"] == EQUIVALENCE_PREPARATION_SCHEMA
    assert execution["equivalence_preparation"] == EQUIVALENCE_PREPARATION_SCHEMA

    # One provider is deliberately retained: a policy change must miss its
    # in-memory preparation cache too, not just a future disk cache.
    monkeypatch.setattr(preparation, "EQUIVALENCE_PREPARATION_SCHEMA", "different-policy")
    second = verifier.prepare(candidate, config)
    assert verifier.preparation_cache_recipe(candidate, config) != recipe
    assert second.backend_identity != first.backend_identity
    assert second.source == first.source
    assert second.implementation_artifact_hash == first.implementation_artifact_hash
    assert second.harness_hash == first.harness_hash

    monkeypatch.setattr("zlang.formal_configuration.EQUIVALENCE_PREPARATION_SCHEMA", "different-policy")
    # Even a frozen bundle with the previous backend ID cannot reuse the old
    # execution record under a different lowering recipe.
    changed = _semantic_equivalence_execution_recipe(first, config, EquivalenceMode.BMC, tools)
    assert changed != execution


def test_pre_flatten_frozen_bundle_is_rejected():
    data = prepared_candidate_equivalence_to_data(_prepared())
    data["schema"] = "zlang-prepared-candidate-equivalence-v2"
    with pytest.raises(ValueError, match="schema"):
        prepared_candidate_equivalence_from_data(data)


def test_flattened_dsp_miter_uses_actual_port_widths(tmp_path):
    compiled = compile_source(SMALL_FORMAL_SOURCE, target=TARGET)
    graph = compiled.implementation_graph
    value = compiled.ir.assignments[0].expression
    candidate = PhysicalTargetFormalCandidate(
        expression=value, module=compiled.ir, implementation_graph=graph,
        semantic_identity=graph.semantic_region_identity,
        implementation_identity=graph.identity,
        cost=CandidateCost.estimate(lut=0, ff=0, dsp=2, latency=3, ii=1),
    )
    verifier = SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        erase_pipeline_timing(value), candidate_class="pipeline",
        clock_domain_contract=compiled.ir.clock_domains[0],
    )
    bundle = verifier.prepare(candidate, FormalExplorationConfig())
    result = run_equivalence_formal(
        bundle.property, bundle.source, top=bundle.top, depth=8,
        timeout_seconds=30, work_directory=tmp_path,
        trace_metadata=bundle.trace_metadata,
    )
    assert result.status is EquivalenceStatus.BOUNDED_PASS, result.reason
    model = json.loads((tmp_path / bundle.top / "model/design.json").read_text())
    assert [module["name"] for module in model["modules"]] == [bundle.top]
    multipliers = [
        cell
        for sort in model["modules"][0]["cell_sorts"] if sort["type"] == "$mul"
        for cell in sort["cells"]
    ]
    assert multipliers
    assert all(max(int(cell["parameters"][p], 2) for p in ("A_WIDTH", "B_WIDTH")) <= 5 for cell in multipliers)
    # No source, primitive, property, reset, or history logic is patched by
    # the proof runner; only standard Yosys lowering crosses the hierarchy.
    assert (tmp_path / f"{bundle.top}.v").read_text() == bundle.source
    data = prepared_candidate_equivalence_to_data(bundle)
    assert prepared_candidate_equivalence_from_data(data) == bundle
    with pytest.raises(ValueError, match="replay identity"):
        prepared_candidate_equivalence_from_data(dict(data, source=bundle.source.replace("assert", "assume")))
