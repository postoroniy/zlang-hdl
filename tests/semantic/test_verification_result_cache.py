from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

from zlang.common import stable_digest
from zlang.cli import main as compiler_main
from zlang.ir.formal import (
    Counterexample,
    CoverResult,
    CoverStatus,
    FormalResult,
    FormalStatus,
    ProofMode,
)
from zlang.verification_bundle import (
    VerificationRunConfig,
    load_verification_bundle,
    publish_verification_bundle,
    run_verification_bundle,
    run_verification_bundle_staged,
    verification_identity_for,
    verification_result_cache_key,
)
from zlang.verification_cli import main as verification_main

from tests.semantic.test_verification_bundle import (
    _digest,
    _inputs,
    _job,
    _payload,
    _publish,
)


_VERSIONS = (
    ("yosys", "Yosys 1"),
    ("sby", "SBY 1"),
    ("yosys-smtbmc", "SMTBMC 1"),
    ("z3", "Z3 1"),
)


def _m35_result_entries(cache: Path) -> tuple[Path, ...]:
    return tuple((cache / "M35" / "results").glob("*.json"))


def _toolchain(monkeypatch, versions=_VERSIONS) -> None:
    monkeypatch.setattr(
        "zlang.verification_bundle.FormalToolchainContext.discover",
        lambda **keywords: SimpleNamespace(
            engine=keywords["engine"],
            solver=keywords["solver"],
            versions=tuple(versions),
        ),
    )


def test_decisive_result_cache_hit_bypasses_solver_and_strips_work_path(
    tmp_path: Path, monkeypatch,
) -> None:
    routed_job = replace(
        _job(),
        route="m35_direct_sv",
        backend="systemverilog",
        artifact_hash=_digest("artifact"),
        binding_identity=_digest("bindings"),
        selected_ir_identity=_digest("selected"),
    )
    _publish(tmp_path / "bundle", jobs=(routed_job,))
    _toolchain(monkeypatch, (*_VERSIONS, ("boolector", "Boolector old")))
    calls = 0

    def run(_source: str, **keywords: object) -> FormalResult:
        nonlocal calls
        calls += 1
        return FormalResult(
            str(keywords["property_id"]),
            FormalStatus.BOUNDED_PASS,
            ProofMode.BMC,
            str(keywords["engine"]),
            str(keywords["solver"]),
            int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", run)
    config = VerificationRunConfig(depth=9, timeout_seconds=17)
    cache = tmp_path / "cache"
    first = run_verification_bundle(
        tmp_path / "bundle",
        config=config,
        work_directory=tmp_path / "work",
        cache_directory=cache,
    )
    _toolchain(monkeypatch, (*_VERSIONS, ("boolector", "Boolector new")))
    second = run_verification_bundle(
        tmp_path / "bundle",
        config=config,
        work_directory=tmp_path / "other-work",
        cache_directory=cache,
    )

    assert calls == 1
    assert first.results[0].status == second.results[0].status == "bounded_pass"
    assert second.tool_versions[-1] == ("boolector", "Boolector new")
    assert first.results[0].work_directory is not None
    assert second.results[0].work_directory is None
    entries = _m35_result_entries(cache)
    assert len(entries) == 1
    envelope = json.loads(entries[0].read_text())
    assert envelope["schema"] == "zlang-verification-result-cache-v1"
    assert envelope["identity"]["route"] == {
        "artifact_hash": routed_job.artifact_hash,
        "backend": routed_job.backend,
        "binding_identity": routed_job.binding_identity,
        "route": routed_job.route,
        "selected_ir_identity": routed_job.selected_ir_identity,
    }


def test_legacy_flat_result_cache_entry_is_read_without_solver(
    tmp_path: Path, monkeypatch,
) -> None:
    _publish(tmp_path / "bundle")
    _toolchain(monkeypatch)
    calls = 0

    def run(_source: str, **keywords: object) -> FormalResult:
        nonlocal calls
        calls += 1
        return FormalResult(
            str(keywords["property_id"]),
            FormalStatus.BOUNDED_PASS,
            ProofMode.BMC,
            str(keywords["engine"]),
            str(keywords["solver"]),
            int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", run)
    cache = tmp_path / "cache"
    run_verification_bundle(tmp_path / "bundle", cache_directory=cache)
    canonical = next(iter(_m35_result_entries(cache)))
    legacy = cache / canonical.name
    canonical.replace(legacy)

    def fail(*_args: object, **_keywords: object) -> FormalResult:
        raise AssertionError("legacy cache hit reran solver")

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", fail)
    report = run_verification_bundle(
        tmp_path / "bundle", cache_directory=cache
    )

    assert calls == 1
    assert report.results[0].status == "bounded_pass"
    assert legacy.is_file()
    assert not canonical.exists()


def test_failed_result_and_counterexample_are_reused(tmp_path: Path, monkeypatch) -> None:
    _publish(tmp_path / "bundle")
    _toolchain(monkeypatch)
    calls = 0

    def fail(_source: str, **keywords: object) -> FormalResult:
        nonlocal calls
        calls += 1
        property_id = str(keywords["property_id"])
        return FormalResult(
            property_id,
            FormalStatus.FAILED,
            ProofMode.BMC,
            str(keywords["engine"]),
            str(keywords["solver"]),
            int(keywords["depth"]),
            Counterexample(property_id, 4, (("count", "9"),), "trace"),
            keywords["source_origin"],  # type: ignore[arg-type]
            keywords["toolchain"].versions,  # type: ignore[union-attr]
            "formal counterexample reported",
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", fail)
    cache = tmp_path / "cache"
    first = run_verification_bundle(tmp_path / "bundle", cache_directory=cache)
    second = run_verification_bundle(tmp_path / "bundle", cache_directory=cache)

    assert calls == 1
    assert first.results[0].counterexample == second.results[0].counterexample
    assert second.results[0].status == "failed"


def test_staged_proof_reuses_bounded_and_proven_results(
    tmp_path: Path, monkeypatch,
) -> None:
    _publish(tmp_path / "bundle")
    discoveries = 0

    def discover(**keywords: object) -> SimpleNamespace:
        nonlocal discoveries
        discoveries += 1
        return SimpleNamespace(
            engine=keywords["engine"],
            solver=keywords["solver"],
            versions=_VERSIONS,
        )

    monkeypatch.setattr(
        "zlang.verification_bundle.FormalToolchainContext.discover",
        discover,
    )
    calls: list[ProofMode] = []

    def run(_source: str, **keywords: object) -> FormalResult:
        mode = keywords["mode"]
        assert isinstance(mode, ProofMode)
        calls.append(mode)
        return FormalResult(
            str(keywords["property_id"]),
            (
                FormalStatus.BOUNDED_PASS
                if mode is ProofMode.BMC
                else FormalStatus.PROVEN
            ),
            mode,
            str(keywords["engine"]),
            str(keywords["solver"]),
            int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", run)
    config = VerificationRunConfig(mode=ProofMode.PROVE, depth=6)
    cache = tmp_path / "cache"
    for _ in range(2):
        report = run_verification_bundle_staged(
            tmp_path / "bundle", config=config, cache_directory=cache
        )
        assert report.results[0].status == "proven"
        assert report.bounded_results[0].status == "bounded_pass"

    assert calls == [ProofMode.BMC, ProofMode.PROVE]
    assert discoveries == 2  # once per CompilationSession-style staged invocation
    assert len(_m35_result_entries(cache)) == 2


def test_unknown_timeout_result_is_never_cached(tmp_path: Path, monkeypatch) -> None:
    _publish(tmp_path / "bundle")
    _toolchain(monkeypatch)
    calls = 0

    def timeout(_source: str, **keywords: object) -> FormalResult:
        nonlocal calls
        calls += 1
        return FormalResult(
            str(keywords["property_id"]),
            FormalStatus.UNKNOWN,
            ProofMode.BMC,
            str(keywords["engine"]),
            str(keywords["solver"]),
            int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
            reason="formal execution timed out",
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", timeout)
    cache = tmp_path / "cache"
    for _ in range(2):
        report = run_verification_bundle(
            tmp_path / "bundle", cache_directory=cache
        )
        assert report.results[0].status == "unknown"

    assert calls == 2
    assert not cache.exists()


def test_skipped_job_is_never_cached(tmp_path: Path, monkeypatch) -> None:
    skipped_job = replace(
        _job(),
        executable=False,
        reason="formal observations are unavailable",
    )
    _publish(tmp_path / "bundle", jobs=(skipped_job,))
    _toolchain(monkeypatch)

    def fail(*_args: object, **_keywords: object) -> FormalResult:
        raise AssertionError("skipped job invoked the solver")

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", fail)
    cache = tmp_path / "cache"

    for _ in range(2):
        report = run_verification_bundle(
            tmp_path / "bundle", cache_directory=cache
        )
        assert report.results[0].status == "skipped"

    assert not cache.exists()


def test_tampered_cache_hash_is_a_miss_and_is_atomically_repaired(
    tmp_path: Path, monkeypatch,
) -> None:
    _publish(tmp_path / "bundle")
    _toolchain(monkeypatch)
    calls = 0

    def run(_source: str, **keywords: object) -> FormalResult:
        nonlocal calls
        calls += 1
        return FormalResult(
            str(keywords["property_id"]),
            FormalStatus.BOUNDED_PASS,
            ProofMode.BMC,
            str(keywords["engine"]),
            str(keywords["solver"]),
            int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", run)
    cache = tmp_path / "cache"
    run_verification_bundle(tmp_path / "bundle", cache_directory=cache)
    entry = next(iter(_m35_result_entries(cache)))
    tampered = json.loads(entry.read_text())
    tampered["result"]["depth"] = 99
    entry.write_text(json.dumps(tampered))

    run_verification_bundle(tmp_path / "bundle", cache_directory=cache)

    assert calls == 2
    repaired = json.loads(entry.read_text())
    assert repaired["result"]["depth"] == 20
    assert repaired["result_hash"] == stable_digest(repaired["result"])
    assert not tuple(entry.parent.glob(".*.tmp"))


def test_cache_key_binds_route_config_and_only_relevant_tools(tmp_path: Path) -> None:
    routed_job = replace(
        _job(),
        route="m35_direct_sv",
        backend="systemverilog",
        artifact_hash=_digest("artifact"),
        binding_identity=_digest("bindings"),
    )
    _publish(tmp_path / "bundle", jobs=(routed_job,))
    loaded = load_verification_bundle(tmp_path / "bundle")
    base = VerificationRunConfig(depth=8, timeout_seconds=15, jobs=1)
    versions = (*_VERSIONS, ("boolector", "Boolector old"))
    key = verification_result_cache_key(
        loaded, routed_job, config=base, tool_versions=versions
    )

    assert key == verification_result_cache_key(
        loaded,
        routed_job,
        config=replace(base, jobs=8),
        tool_versions=(*_VERSIONS, ("boolector", "Boolector new")),
    )
    assert key != verification_result_cache_key(
        loaded,
        routed_job,
        config=replace(base, timeout_seconds=16),
        tool_versions=versions,
    )
    assert key != verification_result_cache_key(
        loaded,
        replace(routed_job, binding_identity=_digest("other bindings")),
        config=base,
        tool_versions=versions,
    )
    assert key != verification_result_cache_key(
        loaded,
        routed_job,
        config=base,
        tool_versions=tuple(
            (name, "Z3 2" if name == "z3" else version)
            for name, version in versions
        ),
    )


def test_vacuous_unknown_is_not_cached_but_decisive_cover_is(
    tmp_path: Path, monkeypatch,
) -> None:
    safety_id = "user.count_within"
    cover_id = "scope.requirements_feasible"
    hardware = "hardware:" + _digest("counter-hardware")
    jobs = (_job(safety_id), _job(cover_id, kind="cover"))
    payload = _payload(
        hardware,
        {safety_id: "safety", cover_id: "cover"},
        vacuity_dependencies={safety_id: cover_id},
    )
    publish_verification_bundle(
        tmp_path / "bundle",
        top="Counter",
        hardware_identity=hardware,
        verification_identity=verification_identity_for(
            top="Counter",
            hardware_identity=hardware,
            property_ids=(safety_id, cover_id),
            payload=payload,
        ),
        property_ids=(safety_id, cover_id),
        verification_ir=payload,
        files=_inputs(),
        jobs=jobs,
    )
    _toolchain(monkeypatch)
    safety_calls = 0
    cover_calls = 0

    def safety(_source: str, **keywords: object) -> FormalResult:
        nonlocal safety_calls
        safety_calls += 1
        return FormalResult(
            str(keywords["property_id"]), FormalStatus.BOUNDED_PASS,
            ProofMode.BMC, "sby", "z3", int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    def cover(_source: str, **keywords: object) -> CoverResult:
        nonlocal cover_calls
        cover_calls += 1
        return CoverResult(
            str(keywords["property_id"]), CoverStatus.BOUNDED_UNREACHED,
            "sby", "z3", int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
            reason="cover was not reached",
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", safety)
    monkeypatch.setattr("zlang.verification_bundle.run_verilog_cover", cover)
    cache = tmp_path / "cache"
    for _ in range(2):
        report = run_verification_bundle(
            tmp_path / "bundle", cache_directory=cache
        )
        assert {item.property_id: item.status for item in report.results} == {
            safety_id: "unknown",
            cover_id: "bounded_unreached",
        }

    assert safety_calls == 2
    assert cover_calls == 1
    assert len(_m35_result_entries(cache)) == 1


def test_standalone_cli_forwards_cache_directory(tmp_path: Path, monkeypatch, capsys) -> None:
    _publish(tmp_path / "bundle")
    captured: dict[str, object] = {}

    def run(_bundle, **keywords):
        captured.update(keywords)
        return SimpleNamespace(
            to_json=lambda: "{}\n",
            to_text=lambda: "ok\n",
            exit_code=0,
        )

    monkeypatch.setattr("zlang.verification_cli.run_verification_bundle_staged", run)
    cache = tmp_path / "cache"
    assert verification_main([
        str(tmp_path / "bundle"), "--cache", str(cache), "--format", "json",
    ]) == 0
    assert captured["cache_directory"] == cache
    assert json.loads(capsys.readouterr().out) == {}


def test_compiler_verify_reuses_formal_cache_option(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    source = tmp_path / "cache_cli.zl"
    source.write_text("""
module VerificationCacheCli {
    clock clk
    reset rst
    in a : bit
    out y : bit
    y = a
    assert follows @ clk { y == a }
}
""")
    captured: dict[str, object] = {}

    def run(_bundle, **keywords):
        captured.update(keywords)
        return SimpleNamespace(
            to_json=lambda: "{}\n",
            to_text=lambda: "cached verification\n",
            exit_code=0,
        )

    monkeypatch.setattr("zlang.cli.run_verification_bundle_staged", run)
    cache = tmp_path / "formal-cache"
    assert compiler_main((
        str(source),
        "--verify",
        "--formal-cache", str(cache),
        "--systemverilog", str(tmp_path / "VerificationCacheCli.sv"),
    )) == 0
    assert captured["cache_directory"] == cache
    assert capsys.readouterr().out == "cached verification\n"
