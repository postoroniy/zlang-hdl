"""Focused compiler-owned selected-candidate M36/M38 orchestration tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

import zlang.candidate_equivalence as orchestration
from zlang.backend.manifest import BackendArtifact, MANIFEST_VERSION
from zlang.candidate_sites import (
    CandidateRankRecord,
    CandidateRewriteKind,
    CandidateSiteKind,
    CandidateSiteLedger,
    CandidateSiteRecord,
    SelectedCandidateSite,
)
from zlang.common.tool_inventory import ToolInventory
from zlang.formal import FormalToolchainContext
from zlang.formal_artifact_provider import FormalArtifactProvider
from zlang.formal_candidate import PreparedCandidateEquivalence
from zlang.equivalence import MiterTraceMetadata
from zlang.formal_exploration import FormalExplorationConfig, FormalPolicy
from zlang.formal_exploration import (
    FormalExplorationRecord,
    formal_execution_recipe_identity,
)
from zlang.formal_orchestration import (
    CandidateEquivalencePlanReference,
    CompilerFormalExecutionPlan,
)
from zlang.ir import (
    BitType,
    ComparisonWindow,
    CrossBackendMode,
    CrossBackendProperty,
    CrossBackendRelation,
    CrossBackendResult,
    CrossBackendStatus,
    EquivalenceBinding,
    EquivalenceMode,
    EquivalenceProperty,
    EquivalenceRelation,
    EquivalenceResult,
    EquivalenceStatus,
    FormalStatus,
    ProofMode,
    FormalExecutionPlan,
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalSkipCode,
    FormalSkipReason,
)
from zlang.ir.equivalence import BindingSide, SignalRole
from zlang.ir.expressions import InputRef


REFERENCE = "a" * 64
CLASH_HASH = "b" * 64
DIRECT_HASH = "c" * 64
CANDIDATE = "selected:candidate"


def _toolchain(version: str = "test-v1") -> FormalToolchainContext:
    names = ("yosys", "sby", "yosys-smtbmc", "z3")
    return FormalToolchainContext(
        "sby",
        "z3",
        ToolInventory(names, names, tuple((name, version) for name in names)),
    )


def _property() -> EquivalenceProperty:
    return EquivalenceProperty(
        "m36.candidate",
        EquivalenceRelation.SAME_CYCLE_VALUE,
        "reference",
        CANDIDATE,
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


def _prepared(backend: str, implementation_hash: str) -> PreparedCandidateEquivalence:
    return PreparedCandidateEquivalence(
        _property(),
        "module m36_candidate; endmodule\n",
        "m36_candidate",
        REFERENCE,
        implementation_hash,
        "d" * 64,
        "m36.candidate",
        "e" * 64,
        "f" * 64,
        backend,
    )


def _m36_result(backend: str, implementation_hash: str) -> EquivalenceResult:
    return EquivalenceResult(
        "m36.candidate",
        EquivalenceStatus.BOUNDED_PASS,
        EquivalenceMode.BMC,
        "sby",
        "z3",
        8,
        EquivalenceRelation.SAME_CYCLE_VALUE,
        0,
        backend,
        REFERENCE,
        implementation_hash,
        MANIFEST_VERSION,
        CANDIDATE,
    )


def _artifact(backend: str, module: str) -> BackendArtifact:
    text = f"module {module}(output result); assign result = 1'b0; endmodule\n"
    digest = hashlib.sha256(text.encode()).hexdigest()
    binding = EquivalenceBinding(
        MANIFEST_VERSION,
        BindingSide.IMPLEMENTATION,
        "port:result",
        CANDIDATE,
        module,
        "result",
        1,
        "bit",
        SignalRole.OUTPUT,
        None,
        None,
        backend,
        digest,
    )
    return BackendArtifact(backend, module, CANDIDATE, digest, text, (binding,))


def _reference_artifact() -> BackendArtifact:
    text = "module Reference(output result); assign result = 1'b0; endmodule\n"
    digest = hashlib.sha256(text.encode()).hexdigest()
    binding = EquivalenceBinding(
        MANIFEST_VERSION,
        BindingSide.REFERENCE,
        "port:result",
        CANDIDATE,
        "Reference",
        "result",
        1,
        "bit",
        SignalRole.OUTPUT,
        None,
        None,
        "semantic_reference",
        digest,
    )
    return BackendArtifact(
        "semantic_reference", "Reference", CANDIDATE, digest, text, (binding,)
    )


def _replay_site() -> tuple[
    CandidateSiteRecord,
    orchestration.FrozenCandidateEquivalenceSite,
]:
    candidate = SimpleNamespace(implementation_identity=CANDIDATE)
    rank = CandidateRankRecord(CANDIDATE, "semantic:candidate", 1, (0,))
    site = CandidateSiteRecord(
        CandidateSiteKind.SOURCE_EXPLORE,
        "owner",
        "y",
        "source:expression",
        CANDIDATE,
        (rank,),
        CandidateRewriteKind.OUTPUT_ASSIGNMENT,
    )
    selected = SelectedCandidateSite(site, candidate, object(), "m27")
    reference = _reference_artifact()
    clash_artifact = _artifact("clash", "ClashReplay")
    direct_artifact = _artifact("direct_systemverilog", "DirectReplay")

    def prepared(backend: str, artifact: BackendArtifact):
        return PreparedCandidateEquivalence(
            _property(),
            "module replay; endmodule\n",
            "replay",
            reference.artifact_hash,
            artifact.artifact_hash,
            "d" * 64,
            _property().id,
            "e" * 64,
            hashlib.sha256(backend.encode()).hexdigest(),
            backend,
            reference,
            artifact,
            (),
        )

    clash = prepared("clash", clash_artifact)
    direct = prepared("direct_systemverilog", direct_artifact)
    m38_property = orchestration._m38_property(selected, _property())
    plan = CandidateEquivalencePlanReference(
        site.identity,
        CANDIDATE,
        orchestration._m36_plan(
            selected, _property(), backend="clash", prepared=clash
        ),
        orchestration._m36_plan(
            selected,
            _property(),
            backend="direct_systemverilog",
            prepared=direct,
        ),
        orchestration._m38_plan(
            selected, m38_property, clash, direct, unavailable_reasons=()
        ),
        "port:result",
    )
    return site, orchestration.FrozenCandidateEquivalenceSite(
        plan, _property(), m38_property, clash, direct
    )


def test_frozen_candidate_replay_codec_is_strict_and_path_free() -> None:
    _, frozen = _replay_site()
    data = frozen.to_data()
    assert orchestration.FrozenCandidateEquivalenceSite.from_data(data) == frozen
    rendered = json.dumps(data, sort_keys=True)
    assert "/tmp/" not in rendered
    assert "work_directory" not in rendered

    corrupted = json.loads(rendered)
    corrupted["clash"]["harness_hash"] = "0" * 64
    with pytest.raises(ValueError, match="replay identity"):
        orchestration.FrozenCandidateEquivalenceSite.from_data(corrupted)

    corrupted = json.loads(rendered)
    corrupted["direct_systemverilog"]["implementation_artifact"]["text"] += " "
    with pytest.raises(ValueError, match="artifact text hash"):
        orchestration.FrozenCandidateEquivalenceSite.from_data(corrupted)


def test_frozen_candidate_executes_without_m39_reselection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site, frozen = _replay_site()
    safety = FormalGoalPlan(
        "goal:safety", "property:safety", FormalPlanGoalKind.SAFETY,
        "clk", "rst", (), ("port:y",), "selected:module",
        ComparisonWindow.same_cycle(), 1,
        skip_reason=FormalSkipReason(
            FormalSkipCode.ROUTE_UNAVAILABLE, "unit-test route"
        ),
    )
    compiler_plan = CompilerFormalExecutionPlan(
        "selected:module",
        FormalExecutionPlan(
            "selected:module", "verification:test", (safety,)
        ),
        CandidateSiteLedger((site,)),
        FormalPolicy.REQUIRED_BMC,
        candidate_equivalence_plans=(frozen.plan,),
    )
    calls: list[str] = []

    def execute_m36(prepared, _config, mode, _provider, _toolchain):
        calls.append(f"m36:{prepared.backend}:{mode.value}")
        return EquivalenceResult(
            _property().id,
            EquivalenceStatus.BOUNDED_PASS,
            mode,
            "sby",
            "z3",
            8,
            EquivalenceRelation.SAME_CYCLE_VALUE,
            0,
            prepared.backend,
            prepared.reference_artifact_hash,
            prepared.implementation_artifact_hash,
            MANIFEST_VERSION,
            CANDIDATE,
        )

    def execute_m38(property_, left, right, _config, mode, _provider, _toolchain):
        calls.append(f"m38:{mode.value}")
        return CrossBackendResult(
            property_.id,
            CrossBackendStatus.BOUNDED_PASS,
            mode,
            "sby",
            "z3",
            8,
            property_.relation,
            0,
            CANDIDATE,
            "clash",
            "direct_systemverilog",
            left.implementation_artifact_hash,
            right.implementation_artifact_hash,
            MANIFEST_VERSION,
            observable_signal_id="port:result",
        )

    monkeypatch.setattr(orchestration, "_execute_m36", execute_m36)
    monkeypatch.setattr(orchestration, "_execute_m38", execute_m38)
    monkeypatch.setattr(
        orchestration,
        "_selected_m39_record",
        lambda *_args: pytest.fail("frozen replay must not inspect M39 selection"),
    )
    config = FormalExplorationConfig(
        FormalPolicy.REQUIRED_BMC,
        bmc_depth=8,
        tool_resolver=SimpleNamespace(
            formal_context=lambda **_kwargs: _toolchain()
        ),
    )
    reports = orchestration.execute_frozen_candidate_equivalence(
        compiler_plan, (frozen,), config
    )
    assert len(reports) == 1
    assert calls == [
        "m36:clash:bmc",
        "m36:direct_systemverilog:bmc",
        "m38:bmc",
    ]


def test_m36_decisive_result_reuses_disk_cache_and_tool_version_is_in_recipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def execute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _m36_result("direct_systemverilog", DIRECT_HASH)

    monkeypatch.setattr(orchestration, "run_equivalence_formal", execute)
    config = FormalExplorationConfig(
        FormalPolicy.REQUIRED_BMC,
        bmc_depth=8,
        cache_directory=tmp_path,
    )
    prepared = _prepared("direct_systemverilog", DIRECT_HASH)
    first = orchestration._execute_m36(
        prepared, config, EquivalenceMode.BMC,
        FormalArtifactProvider(tmp_path), _toolchain(),
    )
    second = orchestration._execute_m36(
        prepared, config, EquivalenceMode.BMC,
        FormalArtifactProvider(tmp_path), _toolchain(),
    )
    assert first == second
    assert calls == 1

    # Route-relevant version changes must not consume stale evidence.
    orchestration._execute_m36(
        prepared, config, EquivalenceMode.BMC,
        FormalArtifactProvider(tmp_path), _toolchain("test-v2"),
    )
    assert calls == 2


def test_m36_execution_forwards_authoritative_emitted_trace_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = MiterTraceMetadata("ref_collision_safe", "impl_collision_safe")
    prepared = replace(
        _prepared("direct_systemverilog", DIRECT_HASH),
        trace_metadata=expected,
    )
    observed = None

    def execute(*_args, **kwargs):
        nonlocal observed
        observed = kwargs["trace_metadata"]
        return _m36_result("direct_systemverilog", DIRECT_HASH)

    monkeypatch.setattr(orchestration, "run_equivalence_formal", execute)
    result = orchestration._execute_m36(
        prepared,
        FormalExplorationConfig(FormalPolicy.REQUIRED_BMC, bmc_depth=8),
        EquivalenceMode.BMC,
        FormalArtifactProvider(),
        _toolchain(),
    )
    assert result.status is EquivalenceStatus.BOUNDED_PASS
    assert observed == expected


def test_m38_decisive_result_reuses_disk_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clash_artifact = _artifact("clash", "ClashCandidate")
    direct_artifact = _artifact("direct_systemverilog", "DirectCandidate")
    clash = PreparedCandidateEquivalence(
        _property(), "", "m36", REFERENCE, clash_artifact.artifact_hash,
        "d" * 64, "m36.candidate", "e" * 64, "1" * 64, "clash",
        implementation_artifact=clash_artifact,
    )
    direct = PreparedCandidateEquivalence(
        _property(), "", "m36", REFERENCE, direct_artifact.artifact_hash,
        "d" * 64, "m36.candidate", "e" * 64, "2" * 64,
        "direct_systemverilog", implementation_artifact=direct_artifact,
    )
    property_ = CrossBackendProperty(
        "m38.candidate",
        CrossBackendRelation.SAME_CYCLE_VALUE,
        CANDIDATE,
        ("port:result",),
        None,
        None,
        0,
        0,
    )
    expected = CrossBackendResult(
        property_.id,
        CrossBackendStatus.BOUNDED_PASS,
        CrossBackendMode.BMC,
        "sby",
        "z3",
        8,
        property_.relation,
        0,
        CANDIDATE,
        "clash",
        "direct_systemverilog",
        clash_artifact.artifact_hash,
        direct_artifact.artifact_hash,
        MANIFEST_VERSION,
        observable_signal_id="port:result",
    )
    calls = 0

    def execute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(orchestration, "run_cross_backend_formal", execute)
    config = FormalExplorationConfig(
        FormalPolicy.REQUIRED_BMC,
        bmc_depth=8,
        cache_directory=tmp_path,
    )
    first = orchestration._execute_m38(
        property_, clash, direct, config, CrossBackendMode.BMC,
        FormalArtifactProvider(tmp_path), _toolchain(),
    )
    second = orchestration._execute_m38(
        property_, clash, direct, config, CrossBackendMode.BMC,
        FormalArtifactProvider(tmp_path), _toolchain(),
    )
    assert first == second == expected
    assert calls == 1


def test_selected_m39_record_is_unique_per_mode() -> None:
    site = SimpleNamespace(
        site=SimpleNamespace(identity="site", selected_candidate_identity=CANDIDATE)
    )
    def record(mode: ProofMode, status: FormalStatus) -> FormalExplorationRecord:
        return FormalExplorationRecord(
            CANDIDATE,
            1,
            "valid",
            "M36_clash",
            FormalPolicy.REQUIRED_PROVEN,
            mode,
            8,
            status,
            "hit",
            status is FormalStatus.PROVEN,
            "retained",
        )

    bmc = record(ProofMode.BMC, FormalStatus.BOUNDED_PASS)
    prove = record(ProofMode.PROVE, FormalStatus.PROVEN)
    compilation = SimpleNamespace(ir=object(), exploration_results=())

    def records(_module, _results):
        return ((site.site, bmc), (site.site, prove))

    original = orchestration.candidate_formal_record_sites
    orchestration.candidate_formal_record_sites = records
    try:
        assert orchestration._selected_m39_record(
            compilation, site, EquivalenceMode.BMC
        ) == bmc
        assert orchestration._selected_m39_record(
            compilation, site, EquivalenceMode.PROVE
        ) == prove
    finally:
        orchestration.candidate_formal_record_sites = original


def test_exact_m39_reuse_requires_full_execution_recipe() -> None:
    reference = InputRef("reference", BitType())
    implementation = InputRef("implementation", BitType())
    candidate = SimpleNamespace(
        expression=implementation,
        implementation_identity=CANDIDATE,
        semantic_identity="semantic:candidate",
        timing_relation=None,
        stages=(),
    )
    rank = CandidateRankRecord(CANDIDATE, "semantic:candidate", 1, (0,))
    site = CandidateSiteRecord(
        CandidateSiteKind.SOURCE_EXPLORE,
        "owner",
        "y",
        "source:expression",
        CANDIDATE,
        (rank,),
        CandidateRewriteKind.OUTPUT_ASSIGNMENT,
    )
    selected = SelectedCandidateSite(site, candidate, reference, "m27")
    provider = FormalArtifactProvider()

    def resolver(version: str):
        return SimpleNamespace(
            formal_context=lambda **_kwargs: _toolchain(version),
            clash_context=lambda: SimpleNamespace(
                executable="/test/clash",
                version=f"clash-{version}",
            ),
        )

    config = FormalExplorationConfig(
        FormalPolicy.REQUIRED_BMC,
        bmc_depth=8,
        timeout_seconds=17,
        dependency_identity="1" * 64,
        artifact_provider=provider,
        tool_resolver=resolver("test-v1"),
    )
    prepared = _prepared("clash", CLASH_HASH)
    verifier = orchestration.M36ClashCandidateVerifier(
        reference,
        candidate_class="m27",
        artifact_provider=provider,
    )
    cache_identity = {
        "property_identity": prepared.property_identity,
        "artifact_hash": prepared.implementation_artifact_hash,
        "reference_artifact_hash": prepared.reference_artifact_hash,
        "implementation_artifact_hash": prepared.implementation_artifact_hash,
        "harness_hash": prepared.harness_hash,
        "assumptions_identity": prepared.assumptions_identity,
        "backend_identity": prepared.backend_identity,
    }
    recipe = formal_execution_recipe_identity(
        candidate, config, verifier, cache_identity
    )
    record = FormalExplorationRecord(
        candidate_identity=CANDIDATE,
        rank=1,
        semantic_legality="valid",
        formal_route="M36_clash",
        policy=FormalPolicy.REQUIRED_BMC,
        mode=ProofMode.BMC,
        depth=8,
        status=FormalStatus.BOUNDED_PASS,
        cache_state="executed",
        eligible=True,
        reason="bounded proof satisfied",
        backend="clash",
        artifact_hash=prepared.implementation_artifact_hash,
        engine="sby",
        solver="z3",
        property_identity=prepared.property_identity,
        harness_hash=prepared.harness_hash,
        assumptions_identity=prepared.assumptions_identity,
        backend_identity=prepared.backend_identity,
        reference_artifact_hash=prepared.reference_artifact_hash,
        implementation_artifact_hash=prepared.implementation_artifact_hash,
        execution_recipe_identity=recipe,
    )

    assert orchestration._m39_recipe_matches(
        record, prepared, selected, config, EquivalenceMode.BMC, provider
    )
    assert not orchestration._m39_recipe_matches(
        record,
        prepared,
        selected,
        replace(config, timeout_seconds=18),
        EquivalenceMode.BMC,
        provider,
    )
    assert not orchestration._m39_recipe_matches(
        record,
        prepared,
        selected,
        replace(config, tool_resolver=resolver("test-v2")),
        EquivalenceMode.BMC,
        provider,
    )
    assert not orchestration._m39_recipe_matches(
        record,
        prepared,
        selected,
        replace(config, dependency_identity="2" * 64),
        EquivalenceMode.BMC,
        provider,
    )
    assert not orchestration._m39_recipe_matches(
        record,
        prepared,
        selected,
        replace(config, schema_version="zlang-formal-exploration-test-v2"),
        EquivalenceMode.BMC,
        provider,
    )


def test_required_proven_runs_and_retains_bmc_before_prove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = SimpleNamespace(implementation_identity=CANDIDATE)
    rank = CandidateRankRecord(CANDIDATE, "semantic:candidate", 1, (0,))
    site = CandidateSiteRecord(
        CandidateSiteKind.SOURCE_EXPLORE,
        "owner",
        "y",
        "source:expression",
        CANDIDATE,
        (rank,),
        CandidateRewriteKind.OUTPUT_ASSIGNMENT,
    )
    selected = SelectedCandidateSite(site, candidate, object(), "m27")
    clash_artifact = _artifact("clash", "ClashCandidate")
    direct_artifact = _artifact("direct_systemverilog", "DirectCandidate")
    clash = PreparedCandidateEquivalence(
        _property(), "", "m36", REFERENCE, clash_artifact.artifact_hash,
        "d" * 64, "m36.candidate", "e" * 64, "1" * 64, "clash",
        implementation_artifact=clash_artifact,
    )
    direct = PreparedCandidateEquivalence(
        _property(), "", "m36", REFERENCE, direct_artifact.artifact_hash,
        "d" * 64, "m36.candidate", "e" * 64, "2" * 64,
        "direct_systemverilog", implementation_artifact=direct_artifact,
    )
    clash_plan = orchestration._m36_plan(
        selected, _property(), backend="clash", prepared=clash
    )
    direct_plan = orchestration._m36_plan(
        selected, _property(), backend="direct_systemverilog", prepared=direct
    )
    m38_property = orchestration._m38_property(selected, _property())
    m38_plan = orchestration._m38_plan(
        selected, m38_property, clash, direct, unavailable_reasons=()
    )
    candidate_plan = CandidateEquivalencePlanReference(
        site.identity,
        CANDIDATE,
        clash_plan,
        direct_plan,
        m38_plan,
        "port:result",
    )
    top_identity = "selected:module"
    safety = FormalGoalPlan(
        "goal:safety",
        "property:safety",
        FormalPlanGoalKind.SAFETY,
        "clk",
        "rst",
        (),
        ("port:y",),
        top_identity,
        ComparisonWindow.same_cycle(),
        1,
        skip_reason=FormalSkipReason(
            FormalSkipCode.ROUTE_UNAVAILABLE, "unit-test route"
        ),
    )
    compiler_plan = CompilerFormalExecutionPlan(
        top_identity,
        FormalExecutionPlan(top_identity, "verification:test", (safety,)),
        CandidateSiteLedger((site,)),
        FormalPolicy.REQUIRED_PROVEN,
        candidate_equivalence_plans=(candidate_plan,),
    )
    prepared_site = orchestration.PreparedCandidateEquivalenceSite(
        selected,
        candidate_plan,
        _property(),
        m38_property,
        clash,
        direct,
    )
    calls: list[tuple[str, str]] = []

    def execute_m36(prepared, _config, mode, _provider, _toolchain):
        calls.append((prepared.backend, mode.value))
        return EquivalenceResult(
            _property().id,
            (
                EquivalenceStatus.BOUNDED_PASS
                if mode is EquivalenceMode.BMC
                else EquivalenceStatus.PROVEN
            ),
            mode,
            "sby",
            "z3",
            8,
            EquivalenceRelation.SAME_CYCLE_VALUE,
            0,
            prepared.backend,
            REFERENCE,
            prepared.implementation_artifact_hash,
            MANIFEST_VERSION,
            CANDIDATE,
        )

    def execute_m38(
        property_, left, right, _config, mode, _provider, _toolchain
    ):
        calls.append(("m38", mode.value))
        return CrossBackendResult(
            property_.id,
            (
                CrossBackendStatus.BOUNDED_PASS
                if mode is CrossBackendMode.BMC
                else CrossBackendStatus.PROVEN
            ),
            mode,
            "sby",
            "z3",
            8,
            property_.relation,
            0,
            CANDIDATE,
            "clash",
            "direct_systemverilog",
            left.implementation_artifact_hash,
            right.implementation_artifact_hash,
            MANIFEST_VERSION,
            observable_signal_id="port:result",
        )

    monkeypatch.setattr(orchestration, "_execute_m36", execute_m36)
    monkeypatch.setattr(orchestration, "_execute_m38", execute_m38)
    monkeypatch.setattr(orchestration, "_selected_m39_record", lambda *_args: None)
    monkeypatch.setattr(orchestration, "_m39_recipe_matches", lambda *_args: False)
    compilation = SimpleNamespace(
        ir=object(),
        exploration_results=(),
        formal_artifact_provider=FormalArtifactProvider(),
        formal_tool_resolver=SimpleNamespace(
            formal_context=lambda **_kwargs: _toolchain()
        ),
    )
    reports = orchestration.execute_prepared_candidate_equivalence(
        compilation,
        compiler_plan,
        (prepared_site,),
        FormalExplorationConfig(
            FormalPolicy.REQUIRED_PROVEN,
            bmc_depth=8,
            tool_resolver=compilation.formal_tool_resolver,
        ),
    )

    assert calls == [
        ("clash", "bmc"),
        ("direct_systemverilog", "bmc"),
        ("m38", "bmc"),
        ("clash", "prove"),
        ("direct_systemverilog", "prove"),
        ("m38", "prove"),
    ]
    assert reports[0].bounded_prerequisite is not None
    assert tuple(item.mode for item in reports[0].evidence_records) == (
        "bmc", "bmc", "bmc", "prove", "prove", "prove"
    )
    assert reports[0].from_json(reports[0].to_json()) == reports[0]

    metadata_report = replace(
        reports[0],
        tool_versions=(("sby", "test-v1"), ("z3", "test-v1")),
        work_directories=(("m38:bmc", "/tmp/zlang-m38-bmc"),),
    )
    assert metadata_report.from_json(metadata_report.to_json()) == metadata_report
    corrupted = metadata_report.to_data()
    corrupted["work_directories"] = [
        ["m38:bmc", "/tmp/one"],
        ["m38:bmc", "/tmp/two"],
    ]
    with pytest.raises(ValueError, match="duplicate route labels"):
        metadata_report.from_data(corrupted)

    substituted_m38 = replace(
        m38_plan,
        required_observations=("port:other",),
    )
    with pytest.raises(
        ValueError,
        match="M38 observations differ from the M36 implementation observable",
    ):
        CandidateEquivalencePlanReference(
            site.identity,
            CANDIDATE,
            clash_plan,
            direct_plan,
            substituted_m38,
            "port:result",
        )


def test_independent_candidate_sites_run_in_parallel_with_stable_report_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = SimpleNamespace(implementation_identity=CANDIDATE)

    def make_site(owner: str, target: str):
        rank = CandidateRankRecord(CANDIDATE, "semantic:candidate", 1, (0,))
        site = CandidateSiteRecord(
            CandidateSiteKind.SOURCE_EXPLORE,
            owner,
            target,
            f"source:{target}",
            CANDIDATE,
            (rank,),
            CandidateRewriteKind.OUTPUT_ASSIGNMENT,
        )
        selected = SelectedCandidateSite(site, candidate, object(), "m27")
        # The semantic sites are independent, but their selected implementation
        # and proof inputs are deliberately identical.  The provider must
        # coalesce this into one recipe and both reports must name its one
        # recipe-addressed workspace.
        direct_artifact = _artifact("direct_systemverilog", "Direct_shared")
        direct = PreparedCandidateEquivalence(
            _property(), "", "m36", REFERENCE,
            direct_artifact.artifact_hash, "d" * 64, "m36.candidate",
            "e" * 64, stable_hash("shared"), "direct_systemverilog",
            implementation_artifact=direct_artifact,
        )
        clash_plan = orchestration._m36_plan(
            selected, _property(), backend="clash", prepared=None,
            unavailable_reason="unit-test Clash route unavailable",
        )
        direct_plan = orchestration._m36_plan(
            selected, _property(), backend="direct_systemverilog",
            prepared=direct,
        )
        m38_property = orchestration._m38_property(selected, _property())
        m38_plan = orchestration._m38_plan(
            selected, m38_property, None, direct,
            unavailable_reasons=("unit-test Clash route unavailable",),
        )
        plan = CandidateEquivalencePlanReference(
            site.identity, CANDIDATE, clash_plan, direct_plan, m38_plan,
            "port:result",
        )
        return site, orchestration.PreparedCandidateEquivalenceSite(
            selected, plan, _property(), m38_property, None, direct,
        )

    def stable_hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    first_site, first = make_site("owner:first", "first")
    second_site, second = make_site("owner:second", "second")
    ordered = tuple(sorted(
        ((first_site, first), (second_site, second)),
        key=lambda item: item[0].identity,
    ))
    safety = FormalGoalPlan(
        "goal:safety", "property:safety", FormalPlanGoalKind.SAFETY,
        "clk", "rst", (), ("port:y",), "selected:module",
        ComparisonWindow.same_cycle(), 1,
        skip_reason=FormalSkipReason(
            FormalSkipCode.ROUTE_UNAVAILABLE, "unit-test route"
        ),
    )
    compiler_plan = CompilerFormalExecutionPlan(
        "selected:module",
        FormalExecutionPlan(
            "selected:module", "verification:test", (safety,)
        ),
        CandidateSiteLedger(tuple(item[0] for item in ordered)),
        FormalPolicy.REQUIRED_BMC,
        candidate_equivalence_plans=tuple(item[1].plan for item in ordered),
    )
    calls = 0

    def execute_m36(*_args, **kwargs):
        nonlocal calls
        calls += 1
        work_directory = kwargs["work_directory"]
        assert work_directory is not None
        Path(work_directory).mkdir(parents=True, exist_ok=True)
        return _m36_result(
            "direct_systemverilog",
            ordered[0][1].direct_systemverilog.implementation_artifact_hash,
        )

    monkeypatch.setattr(orchestration, "run_equivalence_formal", execute_m36)
    compilation = SimpleNamespace(
        formal_artifact_provider=FormalArtifactProvider(),
        formal_tool_resolver=SimpleNamespace(
            formal_context=lambda **_kwargs: _toolchain()
        ),
    )
    config = FormalExplorationConfig(
        FormalPolicy.REQUIRED_BMC,
        bmc_depth=8,
        work_directory=tmp_path / "formal-work",
    )
    prepared = tuple(item[1] for item in ordered)
    reports = orchestration.execute_prepared_candidate_equivalence(
        compilation, compiler_plan, prepared, config, jobs=2,
    )

    assert tuple(item.plan.site_identity for item in reports) == tuple(
        item[0].identity for item in ordered
    )
    assert calls == 1
    workspaces = tuple(
        dict(item.work_directories)["m36:direct_systemverilog:bmc"]
        for item in reports
    )
    assert workspaces[0] == workspaces[1]
    assert Path(workspaces[0]).is_dir()

    repeated = orchestration.execute_prepared_candidate_equivalence(
        compilation, compiler_plan, prepared, config, jobs=2,
    )
    assert calls == 1
    assert tuple(item.work_directories for item in repeated) == tuple(
        item.work_directories for item in reports
    )
    with pytest.raises(ValueError, match="positive integer"):
        orchestration.execute_prepared_candidate_equivalence(
            compilation, compiler_plan, tuple(item[1] for item in ordered),
            FormalExplorationConfig(FormalPolicy.REQUIRED_BMC), jobs=0,
        )
