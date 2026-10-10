# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from unittest.mock import patch

import pytest

import zlang.opt.saturation as saturation_module
from zlang.backend.systemverilog import emit_artifact_with_source_map
from zlang.compilation_session import CompilationSession
from zlang.compiler import compile_source
from zlang.exploration import ExplorationBounds
from zlang.intent_structural_exploration import (
    IntentStructuralExplorationCache,
    IntentStructuralExplorationKey,
    StructuralExplorationRecipe,
    explore_intent_structures,
)
from zlang.source import SourceOrigin, SourceSpan


def _ordinary_root(expression: str, output_type: str = "u8"):
    result = compile_source(
        f"module Root {{ in x:u8 out y:{output_type} y={expression} }}",
        source_unit="tests/fixtures/intent_structural_exploration.zhl",
    )
    return result.ir.assignments[0].expression, result.ir.equivalences


def _explore(root, equivalences=(), *, structures: int = 8):
    return explore_intent_structures(
        root,
        equivalences,
        max_saturation_iterations=6,
        max_eclasses=4_096,
        max_enodes=16_384,
        max_raw_extractions=16,
        max_structural_alternatives=structures,
    )


def test_limits_are_positive_and_have_hard_safety_ceilings() -> None:
    with pytest.raises(ValueError, match="positive"):
        ExplorationBounds(max_saturation_iterations=0)
    with pytest.raises(ValueError, match="hard safety ceiling"):
        ExplorationBounds(max_enodes=262_145)


def test_source_is_retained_when_egraph_work_bound_is_exceeded() -> None:
    root, equivalences = _ordinary_root("x | 0")
    with patch.object(
        saturation_module,
        "_engine_graph_counts",
        return_value=(4_097, 16_385),
    ):
        explored = _explore(root, equivalences)

    assert len(explored.alternatives) == 1
    assert explored.alternatives[0].is_source
    assert explored.stats.truncated
    assert explored.stats.eclasses == 4_097
    assert explored.stats.enodes == 16_385
    assert any("limit exceeded" in item for item in explored.stats.rejection_reasons)


def test_structural_exploration_does_not_generate_commutative_aliases() -> None:
    result = compile_source(
        "module Product { in a:u4 in b:u4 out y:u8 y=a*b }"
    )
    root = result.ir.assignments[0].expression

    explored = _explore(root, result.ir.equivalences)

    assert len(explored.alternatives) == 1
    assert explored.alternatives[0].is_source
    assert explored.stats.raw_extractions == explored.stats.retained_structures == 1


def test_exact_simplification_is_a_distinct_bounded_structure() -> None:
    root, equivalences = _ordinary_root("x | 0")

    explored = _explore(root, equivalences)
    source_only = _explore(root, equivalences, structures=1)

    assert len(explored.alternatives) == 2
    assert len({item.signature.identity for item in explored.alternatives}) == 2
    assert explored.alternatives[0].is_source
    assert not explored.alternatives[1].is_source
    assert source_only.alternatives == explored.alternatives[:1]
    assert source_only.stats.truncated


def test_intent_selects_exact_value_before_bounded_pipeline_provider() -> None:
    combinational = compile_source(
        "module Shift { in a:u8 out y:u16 "
        "y=implement { a*8 intent { minimize lut } } }"
    )
    pipelined = compile_source(
        "module ShiftPipe { clock clk reset rst in a:u8 out y:u16 "
        "y=implement { a*8 intent { latency == 1 minimize ff } } }"
    )

    direct = combinational.exploration_results[0].selected_candidate
    timed = pipelined.exploration_results[0].selected_candidate
    assert direct.stages == ("value",)
    assert timed.stages[0] == "value"
    assert any(stage.startswith("pipeline:") for stage in timed.stages)
    assert direct.cost.latency.value == 0
    assert timed.cost.latency.value == 1
    assert direct.selected_value_identity == timed.selected_value_identity


def test_exact_value_identity_is_bound_to_candidate_evidence_not_source_semantics() -> None:
    result = compile_source(
        "module Evidence { in x:u8 out y:u8 "
        "y=implement { x | 0 intent { minimize lut } } }"
    )
    exploration = result.exploration_results[0]
    selected = exploration.selected_candidate

    assert selected.stages == ("value",)
    assert selected.semantic_identity != exploration.source_semantic_identity
    assert selected.selected_value_identity
    assert any(
        record.candidate_identity == selected.implementation_identity
        for record in exploration.formal_records
    )


def test_repeated_fresh_compilation_keeps_value_rtl_and_map_identity() -> None:
    source = (
        "module Stable { in x:u8 out y:u8 "
        "y=implement { (x | 0) ^ 0 intent { minimize lut } } }"
    )

    products = []
    for _ in range(3):
        result = compile_source(
            source,
            source_unit="tests/fixtures/intent_structural_stable.zhl",
        )
        selected = result.exploration_results[0].selected_candidate
        artifact, source_map = emit_artifact_with_source_map(result.ir)
        products.append(
            (
                selected.selected_value_identity,
                selected.implementation_identity,
                selected.stages,
                artifact.text,
                artifact.artifact_hash,
                source_map.to_json(),
            )
        )

    assert products[1:] == products[:-1]


def test_session_cache_saturates_identical_sites_once_across_intents() -> None:
    source = """
module Shared {
  clock clk
  reset rst
  in x:u8
  out small:u8
  out fast:u8
  small=implement { x|0 intent { minimize lut } }
  fast=implement { x|0 intent { latency==1 minimize ff } }
}
"""
    session = CompilationSession(source)
    import zlang.intent_structural_exploration as structural

    with patch.object(structural, "saturate", wraps=structural.saturate) as run:
        result = session.materialize()

    assert run.call_count == 1
    assert session.intent_structural_cache.info().misses == 1
    assert session.intent_structural_cache.info().hits == 1
    assert result.exploration_results[0].selected_candidate.stages == ("value",)
    assert result.exploration_results[1].selected_candidate.stages[0] == "value"
    assert any(
        stage.startswith("pipeline:")
        for stage in result.exploration_results[1].selected_candidate.stages
    )


def test_independent_sessions_do_not_share_a_process_global_cache() -> None:
    source = (
        "module Isolated { in x:u8 out y:u8 "
        "y=implement { x|0 intent { minimize lut } } }"
    )
    import zlang.intent_structural_exploration as structural

    with patch.object(structural, "saturate", wraps=structural.saturate) as run:
        CompilationSession(source).materialize()
        CompilationSession(source).materialize()

    assert run.call_count == 2


def test_cached_recipe_rebinds_each_current_source_origin() -> None:
    root, equivalences = _ordinary_root("x | 0")
    first_origin = SourceOrigin(SourceSpan(2, 3, 2, 8), "first")
    second_origin = SourceOrigin(SourceSpan(7, 2, 7, 7), "second")
    cache = IntentStructuralExplorationCache()

    first = explore_intent_structures(
        replace(root, origin=first_origin),
        equivalences,
        max_saturation_iterations=6,
        max_eclasses=4_096,
        max_enodes=16_384,
        max_raw_extractions=16,
        max_structural_alternatives=8,
        cache=cache,
    )
    second = explore_intent_structures(
        replace(root, origin=second_origin),
        equivalences,
        max_saturation_iterations=6,
        max_eclasses=4_096,
        max_enodes=16_384,
        max_raw_extractions=16,
        max_structural_alternatives=8,
        cache=cache,
    )

    assert cache.info().hits == 1
    assert first.alternatives[1].expression.origin == first_origin
    assert second.alternatives[1].expression.origin == second_origin
    assert first.alternatives[1].selected_value_identity == second.alternatives[1].selected_value_identity


def test_different_structural_limits_are_distinct_cache_keys() -> None:
    root, equivalences = _ordinary_root("x | 0")
    cache = IntentStructuralExplorationCache()
    for structures in (1, 8):
        explore_intent_structures(
            root,
            equivalences,
            max_saturation_iterations=6,
            max_eclasses=4_096,
            max_enodes=16_384,
            max_raw_extractions=16,
            max_structural_alternatives=structures,
            cache=cache,
        )
    assert cache.info().misses == 2
    assert cache.info().hits == 0


def test_cache_evicts_least_recently_used_structural_recipe() -> None:
    first_root, first_equivalences = _ordinary_root("x | 0")
    second_root, second_equivalences = _ordinary_root("x ^ 0")
    cache = IntentStructuralExplorationCache(
        max_entries=1,
        max_total_enodes=262_144,
    )

    for root, equivalences in (
        (first_root, first_equivalences),
        (second_root, second_equivalences),
        (first_root, first_equivalences),
    ):
        explore_intent_structures(
            root,
            equivalences,
            max_saturation_iterations=6,
            max_eclasses=4_096,
            max_enodes=16_384,
            max_raw_extractions=16,
            max_structural_alternatives=8,
            cache=cache,
        )

    info = cache.info()
    assert info.misses == 3
    assert info.hits == 0
    assert info.evictions == 2
    assert info.entries == 1


def test_cache_hit_preserves_fresh_session_rtl_map_and_candidate_identity() -> None:
    source = (
        "module StableSession { in x:u8 out y:u8 "
        "y=implement { (x | 0) ^ 0 intent { minimize lut } } }"
    )
    cache = IntentStructuralExplorationCache()

    def compile_product(structural_cache=None):
        session = CompilationSession(
            source,
            source_unit="tests/fixtures/intent_structural_session_stable.zhl",
            intent_structural_cache=structural_cache,
        )
        result = session.materialize()
        selected = result.exploration_results[0].selected_candidate
        artifact, source_map = emit_artifact_with_source_map(result.ir)
        return (
            selected.semantic_identity,
            selected.selected_value_identity,
            selected.implementation_identity,
            selected.stages,
            artifact.text,
            artifact.artifact_hash,
            source_map.to_json(),
        )

    miss_product = compile_product(cache)
    hit_product = compile_product(cache)
    cold_product = compile_product()

    assert cache.info().misses == 1
    assert cache.info().hits == 1
    assert miss_product == hit_product == cold_product


def test_cache_single_flights_and_does_not_retain_failures() -> None:
    cache = IntentStructuralExplorationCache(max_entries=2, max_total_enodes=4)
    key = IntentStructuralExplorationKey("a" * 64, "b" * 64, (1, 1, 1, 1, 1))
    started = Event()
    release = Event()
    calls = 0

    def compute() -> StructuralExplorationRecipe:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        raise ValueError("deliberate cache failure")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cache.get_or_compute, key, compute)
        assert started.wait(timeout=2)
        second = executor.submit(cache.get_or_compute, key, compute)
        release.set()
        with pytest.raises(ValueError, match="deliberate"):
            first.result()
        with pytest.raises(ValueError, match="deliberate"):
            second.result()

    assert calls == 1
    assert cache.info().waits == 1
    with pytest.raises(ValueError, match="deliberate"):
        cache.get_or_compute(key, compute)
    assert calls == 2
