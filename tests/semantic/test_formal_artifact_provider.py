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
        {"candidate": "c", "route": {"backend": "direct_systemverilog", "depth": 8}},
    )
    reordered = FormalArtifactRecipe(
        "M36",
        "reference-miter-v1",
        {"route": {"depth": 8, "backend": "direct_systemverilog"}, "candidate": "c"},
    )
    other_namespace = FormalArtifactRecipe(
        "M39",
        "reference-miter-v1",
        {"candidate": "c", "route": {"backend": "direct_systemverilog", "depth": 8}},
    )

    assert first.identity == reordered.identity
    assert first.identity != other_namespace.identity
    assert FORMAL_ARTIFACT_NAMESPACES == {
        "prepared", "M35", "M36", "M39"
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
        return {"status": "skipped", "reason": "formal backend is unavailable"}

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
            "M39",
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
    compilation = compile_source(FORMAL_SOURCE)
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






def test_missing_scoped_assumption_is_never_silently_dropped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    compilation = compile_source(
        SCOPED_ASSUMPTION_SOURCE,
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
