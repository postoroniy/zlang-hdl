"""Real compiler-owned M39/M36 direct-production smoke coverage."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

import zlang.candidate_equivalence as orchestration
from zlang.compiler import compile_source
from zlang.formal_exploration import FormalExplorationConfig, FormalPolicy
from zlang.formal_orchestration import CompilerFormalExecutionPlan
from zlang.ir.equivalence import EquivalenceRelation
from zlang.verification_bundle import (
    VerificationBundleError,
    load_candidate_equivalence_replay,
    load_verification_bundle,
)
from zlang.verification_cli import main as verification_main
from zlang.verification_publication import publish_compilation_verification_bundle


REAL_TOOLS = all(
    shutil.which(name) for name in ("sby", "yosys", "yosys-smtbmc", "z3")
)

SOURCE = """
module CandidateEvidence {
    clock clk reset rst
    in a : u8
    out y : u8
    y = implement { a ^ 0 intent { minimize lut } }
    assert follows @ clk { y == a }
}
"""

FIXED_LATENCY_SOURCE = """
module CandidatePipelineReplay {
    clock clk reset rst
    in a, b, c, d, e, f, g, h : u8
    out y : u19
    y = implement {
        a * b + c * d + e * f + g * h
        intent { latency<=3 ii==1 dsp<=4 fmax>=100 }
    }
    assert input_ok @ clk { a == a }
}
"""


@pytest.mark.skipif(not REAL_TOOLS, reason="real SBY/Yosys/Z3 required")
@pytest.mark.parametrize(
    ("source", "relation", "depth"),
    (
        (SOURCE, EquivalenceRelation.SAME_CYCLE_VALUE, 4),
        # ``implement`` is one unified site.  This source currently selects
        # the legal same-cycle M29 realization; the retained M31 pipeline
        # table is planner metadata, not a second equivalence site.
        (FIXED_LATENCY_SOURCE, EquivalenceRelation.SAME_CYCLE_VALUE, 8),
    ),
)
def test_immutable_candidate_bundle_replays_without_source_or_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    relation: EquivalenceRelation,
    depth: int,
) -> None:
    compilation = compile_source(
        source,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_depth=depth,
        formal_timeout=30,
    )
    base = tmp_path / "base"
    publish_compilation_verification_bundle(compilation, base)
    payload = load_verification_bundle(base).verification_ir["payload"]
    plan = CompilerFormalExecutionPlan.from_data(payload["compiler_execution_plan"])
    cache = tmp_path / "cache"
    preparation_config = FormalExplorationConfig(
        FormalPolicy.AVAILABLE,
        bmc_depth=depth,
        timeout_seconds=30,
        cache_directory=cache,
        artifact_provider=compilation.formal_artifact_provider,
        tool_resolver=compilation.formal_tool_resolver,
    )
    enriched, prepared = orchestration.prepare_selected_candidate_equivalence(
        compilation, plan, preparation_config
    )
    assert len(prepared) == 1
    assert prepared[0].property.relation_kind is relation

    bundle_path = tmp_path / "bundle"
    with monkeypatch.context() as publication_guard:
        publication_guard.setattr(
            orchestration,
            "run_equivalence_formal",
            lambda *_args, **_kwargs: pytest.fail(
                "bundle publication must not execute M36"
            ),
        )
        publish_compilation_verification_bundle(
            compilation,
            bundle_path,
            compiler_execution_plan=enriched,
            prepared_candidate_equivalence=prepared,
        )
    loaded = load_verification_bundle(bundle_path)
    frozen = load_candidate_equivalence_replay(loaded)
    assert len(frozen) == 1
    assert frozen[0].property.relation_kind is relation
    companion_record = loaded.verification_ir["payload"][
        "candidate_equivalence_records"
    ][0]
    companion = loaded.read_bytes(companion_record["logical_path"])
    assert b"work_directory" not in companion
    assert str(tmp_path).encode() not in companion

    replay_config = FormalExplorationConfig(
        FormalPolicy.AVAILABLE,
        bmc_depth=depth,
        timeout_seconds=30,
        cache_directory=cache,
        work_directory=tmp_path / "work",
    )
    reports = orchestration.execute_frozen_candidate_equivalence(
        enriched, tuple(frozen), replay_config
    )
    assert len(reports) == 1
    assert all(
        item.status == "bounded_pass" for item in reports[0].evidence_records
    )

    # A fresh executor must consume the decisive immutable cache entries,
    # without rebuilding source, artifacts, or solver jobs.
    monkeypatch.setattr(
        orchestration,
        "run_equivalence_formal",
        lambda *_args, **_kwargs: pytest.fail("M36 replay missed its cache"),
    )
    cached = orchestration.execute_frozen_candidate_equivalence(
        enriched, tuple(frozen), replay_config
    )
    assert cached[0].evidence_records == reports[0].evidence_records

    if relation is EquivalenceRelation.SAME_CYCLE_VALUE:
        source_path = tmp_path / "deleted-source.zhl"
        source_path.write_text(source, encoding="utf-8")
        source_path.unlink()
        assert verification_main((
            str(bundle_path),
            "--depth", str(depth),
            "--timeout", "30",
            "--cache", str(cache),
            "--work-dir", str(tmp_path / "cli-work"),
        )) == 0
        assert "candidate equivalence site=" in capsys.readouterr().out

    companion_path = bundle_path / companion_record["logical_path"]
    original = companion_path.read_bytes()
    companion_path.write_bytes(original + b" ")
    with pytest.raises(VerificationBundleError, match="does not match"):
        load_verification_bundle(bundle_path)


@pytest.mark.skipif(not REAL_TOOLS, reason="real SBY/Yosys/Z3 required")
@pytest.mark.parametrize(
    ("policy", "expected_modes"),
    (
        (FormalPolicy.AVAILABLE, ("bmc",)),
        (
            FormalPolicy.REQUIRED_PROVEN,
            ("bmc", "prove"),
        ),
    ),
)
def test_real_direct_candidate_evidence_reuses_m39_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: FormalPolicy,
    expected_modes: tuple[str, ...],
) -> None:
    compilation = compile_source(
        SOURCE,
        formal_policy=policy,
        formal_depth=4,
        formal_timeout=30,
    )
    base = tmp_path / "base"
    publish_compilation_verification_bundle(compilation, base)
    payload = load_verification_bundle(base).verification_ir["payload"]
    plan = CompilerFormalExecutionPlan.from_data(
        payload["compiler_execution_plan"]
    )
    config = FormalExplorationConfig(
        policy,
        bmc_depth=4,
        timeout_seconds=30,
        cache_directory=tmp_path / "cache",
        artifact_provider=compilation.formal_artifact_provider,
        tool_resolver=compilation.formal_tool_resolver,
        work_directory=tmp_path / "work",
    )

    enriched, prepared = orchestration.prepare_selected_candidate_equivalence(
        compilation, plan, config
    )
    original_execute_m36 = orchestration._execute_m36
    executed_backends: list[tuple[str, str]] = []

    def execute_m36(item, config, mode, provider, toolchain):
        executed_backends.append((item.backend, mode.value))
        assert item.backend == "direct_systemverilog"
        return original_execute_m36(item, config, mode, provider, toolchain)

    monkeypatch.setattr(orchestration, "_execute_m36", execute_m36)
    reports = orchestration.execute_prepared_candidate_equivalence(
        compilation, enriched, prepared, config
    )

    assert len(reports) == 1
    assert tuple(item.mode for item in reports[0].evidence_records) == expected_modes
    assert all(item.status in {"bounded_pass", "proven"} for item in reports[0].evidence_records)
    assert executed_backends == []
    if policy is FormalPolicy.REQUIRED_PROVEN:
        assert reports[0].bounded_prerequisite is not None
    assert not reports[0].verification_failure
    # Complete M39 reuse performs no second tool discovery or execution.
    assert reports[0].tool_versions == ()
    expected_work_routes = {"m36:bmc"}
    if policy is FormalPolicy.REQUIRED_PROVEN:
        expected_work_routes.add("m36:prove")
    assert {name for name, _ in reports[0].work_directories} == expected_work_routes
    assert all(Path(path).is_dir() for _, path in reports[0].work_directories)
    assert type(reports[0]).from_json(reports[0].to_json()) == reports[0]
