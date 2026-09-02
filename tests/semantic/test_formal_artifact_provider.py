from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import zlang.formal_candidate as candidate_module
import zlang.formal_exploration as exploration_module
import zlang.verification_publication as publication_module
from zlang.backend.systemverilog import (
    SystemVerilogEmissionError,
    emit_formal_artifact as emit_systemverilog_formal_artifact,
)
from zlang.compilation_session import CompilationSession
from zlang.compiler import compile_source
from zlang.formal_artifact_provider import (
    FORMAL_ARTIFACT_CACHE_SCHEMA,
    FORMAL_ARTIFACT_NAMESPACES,
    FormalArtifactNamespace,
    FormalArtifactProvider,
    FormalArtifactRecipe,
)
from zlang.ir.comparison_window import ComparisonWindow
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.ir.equivalence import EquivalenceProperty, EquivalenceRelation
from zlang.ir.types import BitType


SOURCE = "module ArtifactProviderSmoke { in a:u8 out y:u8 y=a }"
FORMAL_SOURCE = """
module CachedVerificationPublication {
    clock clk reset rst
    in a : bit
    out y : bit
    y = a
    assert passthrough @ clk { y == a }
}
"""
ROM_FORMAL_SOURCE = """
module CachedVerificationRom {
    clock clk reset rst
    in address : u2
    out y : u8
    rom table : rom<u8,4> {
        read_latency 1
        init generate(i in 0..4) i
    }
    table.read_address = address
    y = table.read_data
    assert bounded_data @ clk { y <= 3 }
}
"""
SCOPED_ASSUMPTION_SOURCE = """
module MissingScopedAssumption {
    clock clk reset rst
    in allow : bit
    out y : bit
    y = allow
    assume legal @ clk disable iff rst { allow }
    guarantee follows @ clk disable iff rst { y == allow }
}
"""


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return dict(value)


def test_recipe_identity_is_deterministic_and_namespaced() -> None:
    first = FormalArtifactRecipe(
        FormalArtifactNamespace.M36,
        "reference-miter-v1",
        {"candidate": "c", "route": {"backend": "clash", "depth": 8}},
    )
    reordered = FormalArtifactRecipe(
        "M36",
        "reference-miter-v1",
        {"route": {"depth": 8, "backend": "clash"}, "candidate": "c"},
    )
    other_namespace = FormalArtifactRecipe(
        "M38",
        "reference-miter-v1",
        {"candidate": "c", "route": {"backend": "clash", "depth": 8}},
    )

    assert first.identity == reordered.identity
    assert first.identity != other_namespace.identity
    assert FORMAL_ARTIFACT_NAMESPACES == {
        "prepared", "M35", "M36", "M38", "M39"
    }
    detached = first.inputs
    detached["candidate"] = "mutated"
    assert first.inputs["candidate"] == "c"


def test_session_owned_hit_avoids_generator_and_detects_mutation() -> None:
    provider = FormalArtifactProvider(max_entries=4)
    calls = 0

    def prepare() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"artifact_hash": f"hash-{calls}", "bindings": ["a", "y"]}

    first = provider.get_or_prepare(
        "prepared",
        "direct-systemverilog-v1",
        {"selected_ir": "selected"},
        prepare,
        fingerprint=lambda value: value,
    )
    second = provider.get_or_prepare(
        "prepared",
        "direct-systemverilog-v1",
        {"selected_ir": "selected"},
        prepare,
        fingerprint=lambda value: value,
    )
    assert first is second
    assert calls == 1

    first["bindings"] = ["corrupt"]
    repaired = provider.get_or_prepare(
        "prepared",
        "direct-systemverilog-v1",
        {"selected_ir": "selected"},
        prepare,
        fingerprint=lambda value: value,
    )
    assert calls == 2
    assert repaired == {"artifact_hash": "hash-2", "bindings": ["a", "y"]}
    assert provider.stats.corruptions == 1


def test_codec_backed_entry_is_atomic_and_hash_corruption_is_a_miss(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "formal-artifacts"
    calls = 0

    def prepare() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"recipe": "proof-bundle", "generation": calls}

    first_provider = FormalArtifactProvider(cache_root)
    recipe = first_provider.recipe(
        "M39",
        "proof-bundle-recipe-v1",
        {"candidate": "candidate-1", "mode": "bmc"},
    )
    assert first_provider.memoize(
        recipe,
        prepare,
        encode=lambda value: value,
        decode=_mapping,
    )["generation"] == 1
    path = first_provider.entry_path(recipe)
    assert path is not None and path.is_file()
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["schema"] == FORMAL_ARTIFACT_CACHE_SCHEMA
    assert envelope["namespace"] == "M39"
    assert envelope["recipe_identity"] == recipe.identity
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))

    hit_provider = FormalArtifactProvider(cache_root)
    hit = hit_provider.memoize(
        recipe,
        lambda: (_ for _ in ()).throw(
            AssertionError("valid disk cache reran the generator")
        ),
        encode=lambda value: value,
        decode=_mapping,
    )
    assert hit["generation"] == 1
    assert hit_provider.stats.disk_hits == 1

    # Keep the old hash while changing the value.  A fresh provider must reject
    # the entry, invoke the generator, and atomically replace the bad envelope.
    envelope["value"]["generation"] = 99
    path.write_text(json.dumps(envelope), encoding="utf-8")
    second_provider = FormalArtifactProvider(cache_root)
    repaired = second_provider.memoize(
        recipe,
        prepare,
        encode=lambda value: value,
        decode=_mapping,
    )
    assert repaired["generation"] == 2
    assert calls == 2
    assert second_provider.stats.corruptions == 1
    repaired_envelope = json.loads(path.read_text(encoding="utf-8"))
    assert repaired_envelope["value"]["generation"] == 2


def test_shared_provider_reuses_m36_reference_miter_bundle(
    monkeypatch,
) -> None:
    provider = FormalArtifactProvider()
    reference = SimpleNamespace(identity="reference")
    implementation = SimpleNamespace(identity="implementation")
    candidate = SimpleNamespace(
        expression=implementation,
        implementation_identity="candidate",
        stages=(),
    )
    config = candidate_module.FormalExplorationConfig()
    calls = 0
    property_ = EquivalenceProperty(
        "property",
        EquivalenceRelation.SAME_CYCLE_VALUE,
        "reference",
        "candidate",
        BitType(),
        (),
        "port:result",
        "port:result",
        0,
        0,
        1,
        1,
        None,
        None,
        None,
        None,
        0,
        ComparisonWindow.same_cycle(),
    )
    bundle = candidate_module._ProofBundle(
        property_,
        "module proof_bundle; endmodule\n",
        "proof_bundle",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "property",
        "assumptions",
        "backend",
    )

    monkeypatch.setattr(
        candidate_module,
        "expression_semantic_identity",
        lambda value: value.identity,
    )
    monkeypatch.setattr(
        candidate_module,
        "_proof_bundle_tool_route",
        lambda _config: {"clash": "test", "formal_versions": []},
    )

    def build(_self, _candidate, _config):
        nonlocal calls
        calls += 1
        return bundle

    monkeypatch.setattr(
        candidate_module.M36ClashCandidateVerifier,
        "_build_bundle",
        build,
    )
    first = candidate_module.M36ClashCandidateVerifier(
        reference,
        artifact_provider=provider,
    )
    second = candidate_module.M36ClashCandidateVerifier(
        reference,
        artifact_provider=provider,
    )

    assert first._bundle(candidate, config) is bundle
    assert second._bundle(candidate, config) is bundle
    assert calls == 1


def test_m39_preparation_recipe_separates_exact_physical_domains(
    monkeypatch,
) -> None:
    reference = SimpleNamespace(identity="reference")
    implementation = SimpleNamespace(identity="implementation")
    candidate = SimpleNamespace(
        expression=implementation,
        implementation_identity="candidate",
        stages=(),
    )
    config = candidate_module.FormalExplorationConfig()
    monkeypatch.setattr(
        candidate_module,
        "expression_semantic_identity",
        lambda value: value.identity,
    )
    monkeypatch.setattr(
        candidate_module,
        "_proof_bundle_tool_route",
        lambda _config: {
            "clash": "/test/clash",
            "clash_version": "Clash test",
            "formal_versions": [],
        },
    )
    domains = (
        ClockDomain("clk", "rst"),
        ClockDomain("clk", "rst", edge=ClockEdge.FALLING),
        ClockDomain(
            "clk", "rst", reset_polarity=ResetPolarity.ACTIVE_LOW
        ),
        ClockDomain(
            "clk",
            "rst",
            reset_mode=ResetMode.ASYNCHRONOUS,
            reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
            reset_release_cycles=2,
        ),
    )
    recipes = tuple(
        candidate_module.M36ClashCandidateVerifier(
            reference,
            clock_domain_contract=domain,
        ).preparation_cache_recipe(candidate, config)
        for domain in domains
    )
    assert all(recipe is not None for recipe in recipes)
    rendered = tuple(
        json.dumps(recipe, sort_keys=True, separators=(",", ":"))
        for recipe in recipes
    )
    assert len(set(rendered)) == len(domains)
    assert [recipe["bundle"]["clock_domain_contract"] for recipe in recipes] == [
        {
            "clock": domain.clock,
            "reset": domain.reset,
            "edge": domain.edge.value,
            "reset_mode": domain.reset_mode.value,
            "reset_polarity": domain.reset_polarity.value,
            "power_up": domain.power_up.value,
            "reset_release_mode": domain.reset_release_mode.value,
            "reset_release_cycles": domain.reset_release_cycles,
        }
        for domain in domains
    ]


def test_retained_m39_workspace_is_unique_to_exact_property_recipe(
    tmp_path: Path,
) -> None:
    property_ = EquivalenceProperty(
        "property",
        EquivalenceRelation.SAME_CYCLE_VALUE,
        "reference",
        "candidate",
        BitType(),
        (),
        "port:result",
        "port:result",
        0,
        0,
        1,
        1,
        None,
        None,
        None,
        None,
        0,
        ComparisonWindow.same_cycle(),
    )
    first = candidate_module._ProofBundle(
        property_,
        "module proof_bundle; endmodule\n",
        "proof_bundle",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "property-a",
        "assumptions",
        "backend",
    )
    second = replace(
        first,
        property_identity="property-b",
        reference_artifact_hash="d" * 64,
        harness_hash="e" * 64,
    )
    config = candidate_module.FormalExplorationConfig(
        dependency_identity="f" * 64,
        work_directory=tmp_path / "work",
    )
    candidate = SimpleNamespace()

    first_path = candidate_module._m39_work_directory(
        candidate,
        "same-selected-candidate",
        first,
        config,
        candidate_module.ProofMode.BMC,
    )
    repeated_path = candidate_module._m39_work_directory(
        candidate,
        "same-selected-candidate",
        first,
        config,
        candidate_module.ProofMode.BMC,
    )
    second_path = candidate_module._m39_work_directory(
        candidate,
        "same-selected-candidate",
        second,
        config,
        candidate_module.ProofMode.BMC,
    )

    assert first_path == repeated_path
    assert first_path != second_path
    assert first_path.parent == (tmp_path / "work" / "m39").resolve()
    assert second_path.parent == first_path.parent


def test_unknown_timeout_and_environment_skip_are_not_memoized(
    tmp_path: Path,
) -> None:
    provider = FormalArtifactProvider(tmp_path / "cache")
    calls = {"timeout": 0, "skip": 0}

    def timeout() -> dict[str, object]:
        calls["timeout"] += 1
        return {"status": "unknown", "reason": "formal execution timed out"}

    def skipped() -> dict[str, object]:
        calls["skip"] += 1
        return {"status": "skipped", "reason": "Clash executable is unavailable"}

    for _ in range(2):
        assert provider.get_or_prepare(
            "M35",
            "timeout-result-v1",
            {"goal": "g"},
            timeout,
            encode=lambda value: value,
            decode=_mapping,
        )["status"] == "unknown"
        assert provider.get_or_prepare(
            "M38",
            "environment-result-v1",
            {"goal": "g"},
            skipped,
            encode=lambda value: value,
            decode=_mapping,
        )["status"] == "skipped"

    assert calls == {"timeout": 2, "skip": 2}
    assert provider.entry_count == 0
    assert not (tmp_path / "cache").exists()


def test_provider_memory_is_bounded_by_lru_capacity() -> None:
    provider = FormalArtifactProvider(max_entries=1)
    calls = {"a": 0, "b": 0}

    def prepare(name: str) -> dict[str, object]:
        calls[name] += 1
        return {"name": name}

    provider.get_or_prepare("M35", "goal-v1", {"id": "a"}, lambda: prepare("a"))
    provider.get_or_prepare("M35", "goal-v1", {"id": "b"}, lambda: prepare("b"))
    provider.get_or_prepare("M35", "goal-v1", {"id": "a"}, lambda: prepare("a"))

    assert calls == {"a": 2, "b": 1}
    assert provider.entry_count == 1
    assert provider.stats.evictions == 2


def test_compilation_session_owns_provider_and_explicit_cache_root(
    tmp_path: Path,
) -> None:
    session = CompilationSession(
        SOURCE,
        include_clash=False,
        formal_cache=tmp_path / "formal-cache",
    )
    result = session.materialize()

    assert result.formal_artifact_provider is session.formal_artifact_provider
    assert session.formal_artifact_provider.cache_root == (
        tmp_path / "formal-cache" / "artifacts"
    )


def test_repeated_publication_reuses_direct_route_and_exact_m35_checker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    compilation = compile_source(FORMAL_SOURCE, include_clash=False)
    artifact = emit_systemverilog_formal_artifact(
        compilation.ir,
        compilation.recursive_formal_design,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    calls = {"route": 0, "checker": 0}
    original_harness = publication_module.emit_harness

    def emit_route(*_args: object, **_kwargs: object):
        calls["route"] += 1
        return artifact

    def emit_checker(*args: object, **kwargs: object):
        calls["checker"] += 1
        return original_harness(*args, **kwargs)

    monkeypatch.setattr(publication_module, "emit_formal_artifact", emit_route)
    monkeypatch.setattr(publication_module, "emit_harness", emit_checker)

    first = publication_module.publish_compilation_verification_bundle(
        compilation,
        tmp_path / "first",
    )
    checker_calls = calls["checker"]
    second = publication_module.publish_compilation_verification_bundle(
        compilation,
        tmp_path / "second",
    )

    assert calls["route"] == 1
    assert checker_calls > 0
    assert calls["checker"] == checker_calls
    assert first.to_json() == second.to_json()


def test_repeated_fallback_publication_does_not_rerun_clash_preparation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    compilation = compile_source(FORMAL_SOURCE, include_clash=False)
    fallback_artifact = emit_systemverilog_formal_artifact(
        compilation.ir,
        compilation.recursive_formal_design,
        selected_ir_identity=compilation.selected_ir_identity,
    )
    fallback_calls = 0

    def unsupported(*_args: object, **_kwargs: object):
        raise SystemVerilogEmissionError("forced direct route outage")

    def prepare_fallback(*_args: object, **_kwargs: object):
        nonlocal fallback_calls
        fallback_calls += 1
        return fallback_artifact, None

    monkeypatch.setattr(publication_module, "emit_formal_artifact", unsupported)
    monkeypatch.setattr(
        publication_module,
        "_clash_recipe_context",
        lambda *_args: (
            {"executable": "test-clash", "version": "test"},
            "test-clash",
        ),
    )
    monkeypatch.setattr(
        publication_module,
        "_try_clash_formal_fallback",
        prepare_fallback,
    )

    publication_module.publish_compilation_verification_bundle(
        compilation,
        tmp_path / "first-fallback",
    )
    publication_module.publish_compilation_verification_bundle(
        compilation,
        tmp_path / "second-fallback",
    )

    assert fallback_calls == 1


def test_fresh_session_restores_complete_fallback_route_without_backend_rerun(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = tmp_path / "formal-cache"
    first = compile_source(
        ROM_FORMAL_SOURCE,
        include_clash=False,
        formal_cache=cache,
        source_unit="tests/fixtures/cached_verification_rom.zhl",
    )
    fallback_artifact = emit_systemverilog_formal_artifact(
        first.ir,
        first.recursive_formal_design,
        selected_ir_identity=first.selected_ir_identity,
    )
    assert fallback_artifact.companions
    fallback_calls = 0

    def unsupported(*_args: object, **_kwargs: object):
        raise SystemVerilogEmissionError("forced direct route outage")

    def prepare_fallback(*_args: object, **_kwargs: object):
        nonlocal fallback_calls
        fallback_calls += 1
        return fallback_artifact, None

    monkeypatch.setattr(publication_module, "emit_formal_artifact", unsupported)
    monkeypatch.setattr(
        publication_module,
        "_clash_recipe_context",
        lambda *_args: (
            {"executable": "test-clash", "version": "test"},
            "test-clash",
        ),
    )
    monkeypatch.setattr(
        publication_module,
        "_try_clash_formal_fallback",
        prepare_fallback,
    )
    first_manifest = publication_module.publish_compilation_verification_bundle(
        first,
        tmp_path / "first-session",
    )
    assert fallback_calls == 1

    # A fresh compilation owns a fresh in-memory provider.  Only the strict
    # disk codec can avoid invoking the backend callback here.
    second = compile_source(
        ROM_FORMAL_SOURCE,
        include_clash=False,
        formal_cache=cache,
        source_unit="tests/fixtures/cached_verification_rom.zhl",
    )
    second_manifest = publication_module.publish_compilation_verification_bundle(
        second,
        tmp_path / "second-session",
    )
    assert fallback_calls == 1
    assert second.formal_artifact_provider is not None
    assert second.formal_artifact_provider.stats.disk_hits >= 1
    assert first_manifest.to_json() == second_manifest.to_json()
    assert {
        item.logical_path: (
            tmp_path / "first-session" / item.logical_path
        ).read_bytes()
        for item in first_manifest.files
    } == {
        item.logical_path: (
            tmp_path / "second-session" / item.logical_path
        ).read_bytes()
        for item in second_manifest.files
    }
    first_companion = next(
        item for item in first_manifest.files if item.kind == "companion"
    )
    second_companion = next(
        item for item in second_manifest.files if item.kind == "companion"
    )
    assert first_companion == second_companion
    assert (
        tmp_path / "first-session" / first_companion.logical_path
    ).read_bytes() == (
        tmp_path / "second-session" / second_companion.logical_path
    ).read_bytes()


def test_missing_scoped_assumption_is_never_silently_dropped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    compilation = compile_source(
        SCOPED_ASSUMPTION_SOURCE,
        include_clash=False,
        source_unit="tests/fixtures/missing_scoped_assumption.zhl",
    )
    assumptions = tuple(
        item for item in compilation.formal_design.properties
        if item.kind.value == "assumption"
    )
    assert len(assumptions) == 1
    corrupted = replace(
        compilation,
        formal_design=replace(
            compilation.formal_design,
            properties=tuple(
                item for item in compilation.formal_design.properties
                if item is not assumptions[0]
            ),
        ),
    )
    monkeypatch.setattr(
        publication_module,
        "_try_clash_formal_fallback",
        lambda *_args, **_kwargs: pytest.fail(
            "missing semantic assumption needlessly probed Clash"
        ),
    )

    manifest = publication_module.publish_compilation_verification_bundle(
        corrupted,
        tmp_path / "bundle",
    )

    assert manifest.jobs
    assert all(not item.executable for item in manifest.jobs)
    assert all(
        "assumption_unavailable: verification scope references unavailable "
        "assumption(s)" in (item.reason or "")
        for item in manifest.jobs
    )
    assert all(
        tuple(item.assumption_ids) == (assumptions[0].id,)
        for item in manifest.jobs
    )


def test_fresh_m39_verifier_uses_preparation_index_before_clash_or_solver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = tmp_path / "formal-cache"
    reference = SimpleNamespace(identity="reference")
    implementation = SimpleNamespace(identity="implementation")
    candidate = SimpleNamespace(
        expression=implementation,
        implementation_identity="candidate",
        semantic_identity="semantic",
        timing_relation=None,
        stages=(),
    )
    builds = 0
    solver_calls = 0
    allow_build = True
    property_ = EquivalenceProperty(
        "property",
        EquivalenceRelation.SAME_CYCLE_VALUE,
        "reference",
        "candidate",
        BitType(),
        (),
        "port:result",
        "port:result",
        0,
        0,
        1,
        1,
        None,
        None,
        None,
        None,
        0,
        ComparisonWindow.same_cycle(),
    )
    bundle = candidate_module._ProofBundle(
        property_,
        "module indexed_bundle; endmodule\n",
        "indexed_bundle",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "property",
        "d" * 64,
        "e" * 64,
    )

    monkeypatch.setattr(
        candidate_module,
        "expression_semantic_identity",
        lambda value: value.identity,
    )
    monkeypatch.setattr(
        candidate_module,
        "_proof_bundle_tool_route",
        lambda _config: {
            "clash": "/test/clash",
            "clash_version": "Clash test",
            "formal_versions": [["sby", "test"], ["z3", "test"]],
        },
    )
    monkeypatch.setattr(
        exploration_module,
        "tool_versions",
        lambda _requested=None: (("sby", "test"), ("z3", "test")),
    )
    monkeypatch.setattr(
        candidate_module,
        "generate_verilog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("preparation-index hit reran Clash")
        ),
    )

    class IndexedVerifier(candidate_module.M36ClashCandidateVerifier):
        def _build_bundle(self, _candidate, _config):
            nonlocal builds
            if not allow_build:
                raise AssertionError("preparation-index hit rebuilt the M36 bundle")
            builds += 1
            return bundle

        def __call__(self, value, config):
            nonlocal solver_calls
            prepared = self._bundle(value, config)
            solver_calls += 1
            return {
                "status": candidate_module.FormalStatus.BOUNDED_PASS,
                "mode": candidate_module.ProofMode.BMC,
                "depth": config.bmc_depth,
                "engine": config.engine,
                "solver": config.solver,
                "backend": "clash",
                "artifact_hash": prepared.implementation_artifact_hash,
                "reference_artifact_hash": prepared.reference_artifact_hash,
                "implementation_artifact_hash": (
                    prepared.implementation_artifact_hash
                ),
                "property_identity": prepared.property_identity,
                "harness_hash": prepared.harness_hash,
                "assumptions_identity": prepared.assumptions_identity,
                "backend_identity": prepared.backend_identity,
            }

    first_provider = FormalArtifactProvider(cache / "artifacts")
    first_config = exploration_module.FormalExplorationConfig(
        exploration_module.FormalPolicy.REQUIRED_BMC,
        bmc_depth=7,
        cache_directory=cache,
        artifact_provider=first_provider,
    )
    first_verifier = IndexedVerifier(
        reference,
        artifact_provider=first_provider,
    )
    first, first_state, invalid, first_recipe = exploration_module._execute_stage(
        candidate,
        first_config,
        first_verifier,
        route="M36_clash",
    )
    assert first.status is candidate_module.FormalStatus.BOUNDED_PASS
    assert first_recipe.startswith("m39-execution:")
    assert first_state == "executed"
    assert not invalid
    assert builds == solver_calls == 1
    index_files = tuple(
        (cache / "artifacts" / "M39" / "preparation-index").glob("*.json")
    )
    assert len(index_files) == 1
    index = json.loads(index_files[0].read_text(encoding="utf-8"))
    assert index["schema"] == "zlang-m39-preparation-index-v1"
    assert index["index_hash"]

    allow_build = False
    second_provider = FormalArtifactProvider(cache / "artifacts")
    second_config = exploration_module.FormalExplorationConfig(
        exploration_module.FormalPolicy.REQUIRED_BMC,
        bmc_depth=7,
        cache_directory=cache,
        artifact_provider=second_provider,
    )
    second_verifier = IndexedVerifier(
        reference,
        artifact_provider=second_provider,
    )
    second, second_state, invalid, second_recipe = exploration_module._execute_stage(
        candidate,
        second_config,
        second_verifier,
        route="M36_clash",
    )

    assert second.status is candidate_module.FormalStatus.BOUNDED_PASS
    assert second_recipe == first_recipe
    assert second_state == "hit"
    assert not invalid
    assert builds == solver_calls == 1

    # The index is advisory only after strict validation.  Corrupting its hash
    # forces a provider lookup, while the independently validated prepared
    # artifact and proof entries avoid both a second Clash build and a second
    # solver execution before repairing the index.
    index["index_hash"] = "0" * 64
    index_files[0].write_text(json.dumps(index), encoding="utf-8")
    allow_build = True
    third_provider = FormalArtifactProvider(cache / "artifacts")
    third_config = exploration_module.FormalExplorationConfig(
        exploration_module.FormalPolicy.REQUIRED_BMC,
        bmc_depth=7,
        cache_directory=cache,
        artifact_provider=third_provider,
    )
    third_verifier = IndexedVerifier(
        reference,
        artifact_provider=third_provider,
    )
    third, third_state, invalid, third_recipe = exploration_module._execute_stage(
        candidate,
        third_config,
        third_verifier,
        route="M36_clash",
    )
    assert third.status is candidate_module.FormalStatus.BOUNDED_PASS
    assert third_recipe == first_recipe
    assert third_state == "hit"
    assert not invalid
    assert builds == 1
    assert solver_calls == 1
    repaired_index = json.loads(index_files[0].read_text(encoding="utf-8"))
    assert repaired_index["index_hash"] != "0" * 64
