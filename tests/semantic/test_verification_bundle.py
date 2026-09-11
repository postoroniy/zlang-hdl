from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading

import pytest

from zlang.backend.source_map import (
    GeneratedLineRange,
    GeneratedSourceMap,
    GeneratedSourceMapEntry,
)
from zlang.common.tool_inventory import ToolInventory
from zlang.formal import FormalToolchainContext
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
    clock_domain_contract_identity,
)
from zlang.ir.formal import (
    Counterexample,
    CoverResult,
    CoverStatus,
    CoverWitness,
    FormalResult,
    FormalStatus,
    ProofMode,
)
from zlang.source import SourceOrigin, SourceSpan
from zlang.verification_bundle import (
    VerificationBundleError,
    VerificationBundleInput,
    VerificationBundleManifest,
    VerificationCounterexampleMetadata,
    VerificationJob,
    VerificationJobResult,
    VerificationRunConfig,
    VerificationRunReport,
    load_verification_bundle,
    publish_verification_bundle,
    run_verification_bundle,
    run_verification_bundle_staged,
    verification_identity_for,
    verification_result_cache_key,
)
from zlang.verification_cli import main as verification_main


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _origin() -> SourceOrigin:
    return SourceOrigin(
        SourceSpan(7, 5, 7, 24),
        "assert count_within",
        "examples/counter.zhl",
        _digest("counter-source"),
    )


def _payload(
    hardware_identity: str,
    kinds: dict[str, str],
    *,
    vacuity_dependencies: dict[str, str] | None = None,
) -> dict[str, object]:
    dependencies = vacuity_dependencies or {}
    feasibility = set(dependencies.values())
    return {
        "formal_ir_version": 1,
        "identities": {
            "source": "source:" + _digest("counter-source"),
            "dependency": "dependency:" + _digest("counter-dependency"),
            "compiler": "compiler:" + _digest("counter-compiler"),
        },
        "hardware": {
            "high_level_ir_identity": hardware_identity,
            "selected_ir_identity": hardware_identity,
        },
        "scopes": [],
        "properties": [
            {
                "id": property_id,
                "kind": kinds[property_id],
                "generated_from": (
                    "verification-feasibility:test"
                    if property_id in feasibility else None
                ),
                "predicate": {"kind": "constant", "value": 1},
                "source_origin": _origin().to_data(),
            }
            for property_id in sorted(kinds)
        ],
        "bindings": [],
        "vacuity_dependencies": dependencies,
    }


def _inputs(*, reverse: bool = False) -> tuple[VerificationBundleInput, ...]:
    values = (
        VerificationBundleInput(
            "implementation/Counter.sv",
            "implementation",
            b"module Counter(input logic clk); endmodule\n",
        ),
        VerificationBundleInput(
            "harness/count_within.sv",
            "harness",
            b"module count_within_formal; endmodule\n",
        ),
        VerificationBundleInput(
            "config/count_within.json", "config", b'{"binding_schema":1}\n'
        ),
        VerificationBundleInput(
            "source-map/count_within.json", "source_map", b'{"mappings":[]}\n'
        ),
    )
    return tuple(reversed(values)) if reverse else values


def _job(property_id: str = "user.count_within", *, kind: str = "safety") -> VerificationJob:
    return VerificationJob(
        property_id,
        kind,
        "count_within_formal",
        ("implementation/Counter.sv", "harness/count_within.sv"),
        ("config/count_within.json",),
        ("source-map/count_within.json",),
        source_origin=_origin(),
    )


def _publish(
    root: Path,
    *,
    property_ids: tuple[str, ...] = ("user.count_within",),
    jobs: tuple[VerificationJob, ...] | None = None,
    reverse_files: bool = False,
):
    hardware_identity = "hardware:" + _digest("counter-hardware")
    selected_jobs = jobs or tuple(_job(item) for item in reversed(property_ids))
    payload = _payload(
        hardware_identity,
        {item.property_id: item.kind for item in selected_jobs},
    )
    verification_identity = verification_identity_for(
        top="Counter",
        hardware_identity=hardware_identity,
        property_ids=property_ids,
        payload=payload,
    )
    return publish_verification_bundle(
        root,
        top="Counter",
        hardware_identity=hardware_identity,
        verification_identity=verification_identity,
        property_ids=reversed(property_ids),
        verification_ir=payload,
        files=_inputs(reverse=reverse_files),
        jobs=selected_jobs,
    )


def test_bundle_is_deterministic_immutable_and_strictly_round_trips(tmp_path: Path) -> None:
    left = _publish(tmp_path / "left", reverse_files=False)
    right = _publish(tmp_path / "right", reverse_files=True)

    assert left.bundle_identity == right.bundle_identity
    assert left.to_json() == right.to_json()
    assert VerificationBundleManifest.from_json(left.to_json()) == left
    restored = load_verification_bundle(tmp_path / "left")
    assert restored.manifest == left
    assert restored.verification_ir["verification_identity"] == left.verification_identity

    # Re-publishing byte-identical inputs is permitted; changing an immutable
    # payload at the same destination is not.
    assert _publish(tmp_path / "left") == left
    changed = list(_inputs())
    changed[0] = replace(changed[0], content=b"module Counter; endmodule\n")
    with pytest.raises(VerificationBundleError, match="collides"):
        hardware = "hardware:" + _digest("counter-hardware")
        payload = _payload(hardware, {"user.count_within": "safety"})
        publish_verification_bundle(
            tmp_path / "left",
            top="Counter",
            hardware_identity=hardware,
            verification_identity=verification_identity_for(
                top="Counter", hardware_identity=hardware,
                property_ids=("user.count_within",), payload=payload,
            ),
            property_ids=("user.count_within",),
            verification_ir=payload,
            files=changed,
            jobs=(_job(),),
        )


def test_job_physical_domain_round_trip_and_cache_identity_separation(
    tmp_path: Path,
) -> None:
    synchronous = ClockDomain("clk", "rst")
    asynchronous = ClockDomain(
        "clk",
        "rst",
        reset_mode=ResetMode.ASYNCHRONOUS,
        reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
        reset_release_cycles=2,
    )

    def domain_job(domain: ClockDomain) -> VerificationJob:
        return replace(
            _job(),
            clock_domain=domain.clock,
            reset_domain=domain.reset,
            clock_domain_contract=domain,
            physical_domain_identity=clock_domain_contract_identity(domain),
        )

    sync_job = domain_job(synchronous)
    async_job = domain_job(asynchronous)
    assert VerificationJob.from_data(async_job.to_data()) == async_job

    corrupted = async_job.to_data()
    corrupted["physical_domain_identity"] = "0" * 64
    with pytest.raises(VerificationBundleError, match="does not match"):
        VerificationJob.from_data(corrupted)

    sync_manifest = _publish(tmp_path / "sync", jobs=(sync_job,))
    async_manifest = _publish(tmp_path / "async", jobs=(async_job,))
    assert sync_manifest.bundle_identity != async_manifest.bundle_identity

    config = VerificationRunConfig(depth=8)
    sync_key = verification_result_cache_key(
        load_verification_bundle(tmp_path / "sync"),
        sync_job,
        config=config,
        tool_versions=(),
    )
    async_key = verification_result_cache_key(
        load_verification_bundle(tmp_path / "async"),
        async_job,
        config=config,
        tool_versions=(),
    )
    assert sync_key != async_key


def test_job_result_exact_physical_domain_round_trip_rejects_corruption() -> None:
    domain = ClockDomain(
        "clk",
        "rst_n",
        edge=ClockEdge.FALLING,
        reset_mode=ResetMode.ASYNCHRONOUS,
        reset_polarity=ResetPolarity.ACTIVE_LOW,
        reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
        reset_release_cycles=2,
    )
    result = VerificationJobResult(
        "property.domain",
        "safety",
        "bounded_pass",
        "bmc",
        "sby",
        "z3",
        8,
        clock_domain=domain.clock,
        reset_domain=domain.reset,
        clock_domain_contract=domain,
        physical_domain_identity=clock_domain_contract_identity(domain),
    )
    data = result.to_data()
    assert VerificationJobResult.from_data(data) == result

    corrupt_identity = json.loads(json.dumps(data))
    corrupt_identity["physical_domain_identity"] = "0" * 64
    with pytest.raises(VerificationBundleError, match="does not match"):
        VerificationJobResult.from_data(corrupt_identity)

    corrupt_contract = json.loads(json.dumps(data))
    corrupt_contract["clock_domain_contract"]["edge"] = "rising"
    with pytest.raises(VerificationBundleError, match="does not match"):
        VerificationJobResult.from_data(corrupt_contract)

    corrupt_logical_domain = json.loads(json.dumps(data))
    corrupt_logical_domain["reset_domain"] = "other_rst"
    with pytest.raises(VerificationBundleError, match="domains disagree"):
        VerificationJobResult.from_data(corrupt_logical_domain)


def test_bundle_identity_excludes_execution_configuration(tmp_path: Path, monkeypatch) -> None:
    manifest = _publish(tmp_path / "bundle")
    loaded = load_verification_bundle(tmp_path / "bundle")
    calls: list[dict[str, object]] = []

    def run(source: str, **keywords: object) -> FormalResult:
        calls.append({"source": source, **keywords})
        mode = keywords["mode"]
        assert isinstance(mode, ProofMode)
        return FormalResult(
            str(keywords["property_id"]),
            FormalStatus.BOUNDED_PASS if mode is ProofMode.BMC else FormalStatus.PROVEN,
            mode,
            str(keywords["engine"]),
            str(keywords["solver"]),
            int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", run)
    bmc = run_verification_bundle(
        loaded, config=VerificationRunConfig(ProofMode.BMC, "sby", "z3", 7, 9),
        work_directory=tmp_path / "work-left",
    )
    repeated_bmc = run_verification_bundle(
        loaded, config=VerificationRunConfig(ProofMode.BMC, "sby", "z3", 7, 9),
        work_directory=tmp_path / "work-right",
    )
    prove = run_verification_bundle(
        loaded,
        config=VerificationRunConfig(ProofMode.PROVE, "sby", "z3", 31, 45),
    )

    assert bmc.bundle_identity == prove.bundle_identity == manifest.bundle_identity
    assert bmc.run_identity == repeated_bmc.run_identity
    assert bmc.results[0].work_directory != repeated_bmc.results[0].work_directory
    assert bmc.results[0].status == "bounded_pass"
    assert prove.results[0].status == "proven"
    assert bmc.to_data()["config"] != prove.to_data()["config"]
    assert bmc.bundle_identity not in json.dumps(bmc.to_data()["config"])
    assert "module Counter" in str(calls[0]["source"])
    assert "module count_within_formal" in str(calls[0]["source"])
    assert calls[0]["timeout_seconds"] == 9


def _available_toolchain() -> FormalToolchainContext:
    names = ("yosys", "sby", "yosys-smtbmc", "z3")
    return FormalToolchainContext(
        "sby", "z3", ToolInventory(names, names, tuple((name, "test") for name in names))
    )


def test_staged_execution_uses_the_compilation_session_tool_resolver_once(
    tmp_path: Path, monkeypatch
) -> None:
    _publish(tmp_path / "bundle")
    context = _available_toolchain()

    class Resolver:
        calls = 0

        def formal_context(self, *, engine: str, solver: str) -> FormalToolchainContext:
            self.calls += 1
            assert (engine, solver) == ("sby", "z3")
            return context

    resolver = Resolver()

    def run(_source: str, **keywords: object) -> FormalResult:
        return FormalResult(
            str(keywords["property_id"]),
            FormalStatus.BOUNDED_PASS,
            ProofMode.BMC,
            "sby",
            "z3",
            int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=context.versions,
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", run)
    monkeypatch.setattr(
        "zlang.verification_bundle.FormalToolchainContext.discover",
        lambda **_keywords: pytest.fail("session-owned resolver was bypassed"),
    )
    report = run_verification_bundle_staged(
        tmp_path / "bundle",
        work_directory=tmp_path / "work",
        tool_resolver=resolver,
    )

    assert report.results[0].status == "bounded_pass"
    assert resolver.calls == 1


def test_parallel_jobs_overlap_but_report_in_manifest_order(
    tmp_path: Path, monkeypatch
) -> None:
    jobs = (_job("user.first"), _job("user.second"))
    _publish(tmp_path / "bundle", property_ids=("user.first", "user.second"), jobs=jobs)
    barrier = threading.Barrier(2, timeout=2)
    second_completed = threading.Event()
    completion_order: list[str] = []

    def run(_source: str, **keywords: object) -> FormalResult:
        property_id = str(keywords["property_id"])
        barrier.wait()
        if property_id == "user.first":
            assert second_completed.wait(timeout=2)
        else:
            completion_order.append(property_id)
            second_completed.set()
        if property_id == "user.first":
            completion_order.append(property_id)
        return FormalResult(
            property_id,
            FormalStatus.BOUNDED_PASS,
            ProofMode.BMC,
            "sby",
            "z3",
            int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", run)
    report = run_verification_bundle(
        tmp_path / "bundle",
        config=VerificationRunConfig(jobs=2),
        work_directory=tmp_path / "work",
        toolchain=_available_toolchain(),
    )

    assert completion_order == ["user.second", "user.first"]
    assert [item.property_id for item in report.results] == ["user.first", "user.second"]
    assert len({item.work_directory for item in report.results}) == 2


def test_identical_concurrent_runs_serialize_one_deterministic_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    _publish(tmp_path / "bundle")
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def run(_source: str, **keywords: object) -> FormalResult:
        nonlocal calls
        with call_lock:
            calls += 1
            ordinal = calls
        if ordinal == 1:
            first_entered.set()
            assert release_first.wait(timeout=3)
        else:
            second_entered.set()
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
    arguments = {
        "config": VerificationRunConfig(depth=8),
        "work_directory": tmp_path / "work",
        "toolchain": _available_toolchain(),
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_verification_bundle, tmp_path / "bundle", **arguments)
        assert first_entered.wait(timeout=2)
        second = pool.submit(run_verification_bundle, tmp_path / "bundle", **arguments)
        assert not second_entered.wait(timeout=0.2)
        release_first.set()
        first_report = first.result(timeout=3)
        second_report = second.result(timeout=3)

    assert calls == 2
    assert second_entered.is_set()
    assert first_report.run_identity == second_report.run_identity
    assert first_report.results[0].work_directory == second_report.results[0].work_directory


def test_distinct_bundles_use_distinct_work_roots_with_same_config(
    tmp_path: Path, monkeypatch
) -> None:
    _publish(tmp_path / "left", property_ids=("user.left",), jobs=(_job("user.left"),))
    _publish(tmp_path / "right", property_ids=("user.right",), jobs=(_job("user.right"),))

    def run(_source: str, **keywords: object) -> FormalResult:
        return FormalResult(
            str(keywords["property_id"]), FormalStatus.BOUNDED_PASS, ProofMode.BMC,
            "sby", "z3", int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", run)
    config = VerificationRunConfig()
    left = run_verification_bundle(
        tmp_path / "left", config=config, work_directory=tmp_path / "work",
        toolchain=_available_toolchain(),
    )
    right = run_verification_bundle(
        tmp_path / "right", config=config, work_directory=tmp_path / "work",
        toolchain=_available_toolchain(),
    )
    left_dir = left.results[0].work_directory
    right_dir = right.results[0].work_directory
    assert left.bundle_identity != right.bundle_identity
    assert left_dir is not None and right_dir is not None
    left_path = Path(left_dir)
    right_path = Path(right_dir)
    assert left_path.parent != right_path.parent
    assert left_path.parent.is_dir() and right_path.parent.is_dir()


def test_staged_prove_runs_cover_once_and_only_proves_after_bounded_safety(
    tmp_path: Path, monkeypatch
) -> None:
    jobs = (_job("user.safety"), _job("user.cover", kind="cover"))
    _publish(tmp_path / "bundle", property_ids=("user.safety", "user.cover"), jobs=jobs)
    calls: list[tuple[str, str]] = []

    def formal(_source: str, **keywords: object) -> FormalResult:
        mode = keywords["mode"]
        assert isinstance(mode, ProofMode)
        if mode is ProofMode.PROVE:
            assert sorted(calls) == [("cover", "cover"), ("safety", "bmc")]
        calls.append(("safety", mode.value))
        return FormalResult(
            str(keywords["property_id"]),
            FormalStatus.BOUNDED_PASS if mode is ProofMode.BMC else FormalStatus.PROVEN,
            mode, "sby", "z3", int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    def cover(_source: str, **keywords: object) -> CoverResult:
        calls.append(("cover", "cover"))
        return CoverResult(
            str(keywords["property_id"]), CoverStatus.WITNESSED,
            "sby", "z3", int(keywords["depth"]),
            witness=CoverWitness(str(keywords["property_id"]), 1),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", formal)
    monkeypatch.setattr("zlang.verification_bundle.run_verilog_cover", cover)
    monkeypatch.setattr(
        "zlang.verification_bundle.FormalToolchainContext.discover",
        lambda **_kwargs: _available_toolchain(),
    )
    report = run_verification_bundle_staged(
        tmp_path / "bundle", config=VerificationRunConfig(mode=ProofMode.PROVE),
        work_directory=tmp_path / "work",
    )

    assert calls == [("cover", "cover"), ("safety", "bmc"), ("safety", "prove")]
    assert [(item.kind, item.status) for item in report.results] == [
        ("cover", "witnessed"), ("safety", "proven")
    ]
    assert [(item.kind, item.status) for item in report.bounded_results] == [
        ("cover", "witnessed"), ("safety", "bounded_pass")
    ]


def test_staged_prove_does_not_block_clean_safety_on_unrelated_skipped_cover(
    tmp_path: Path, monkeypatch
) -> None:
    jobs = (
        _job("user.safety"),
        replace(
            _job("user.advisory", kind="cover"),
            executable=False,
            reason="advisory cover has no connected observation",
        ),
    )
    _publish(
        tmp_path / "bundle",
        property_ids=("user.safety", "user.advisory"),
        jobs=jobs,
    )
    calls: list[ProofMode] = []

    def formal(_source: str, **keywords: object) -> FormalResult:
        mode = keywords["mode"]
        assert isinstance(mode, ProofMode)
        calls.append(mode)
        return FormalResult(
            str(keywords["property_id"]),
            (
                FormalStatus.BOUNDED_PASS
                if mode is ProofMode.BMC else FormalStatus.PROVEN
            ),
            mode,
            "sby",
            "z3",
            int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", formal)
    monkeypatch.setattr(
        "zlang.verification_bundle.FormalToolchainContext.discover",
        lambda **_kwargs: _available_toolchain(),
    )
    report = run_verification_bundle_staged(
        tmp_path / "bundle",
        config=VerificationRunConfig(mode=ProofMode.PROVE),
        work_directory=tmp_path / "work",
    )

    assert calls == [ProofMode.BMC, ProofMode.PROVE]
    assert [(item.kind, item.status) for item in report.results] == [
        ("cover", "skipped"), ("safety", "proven")
    ]
    assert report.outcome == "incomplete"
    assert report.exit_code == 2
    assert VerificationRunReport.from_json(report.to_json()) == report


def test_staged_structured_skips_do_not_probe_external_tools(
    tmp_path: Path, monkeypatch
) -> None:
    job = replace(
        _job("user.unbound"),
        executable=False,
        reason="required formal observations are not connected",
    )
    _publish(
        tmp_path / "bundle", property_ids=(job.property_id,), jobs=(job,)
    )
    monkeypatch.setattr(
        "zlang.verification_bundle.FormalToolchainContext.discover",
        lambda **_kwargs: pytest.fail(
            "staged structured skip probed external formal tools"
        ),
    )

    report = run_verification_bundle_staged(
        tmp_path / "bundle",
        config=VerificationRunConfig(mode=ProofMode.PROVE),
    )

    assert report.results[0].status == "skipped"
    assert report.results[0].tool_versions == ()


def test_bundle_executor_consumes_hash_bound_generated_source_map(
    tmp_path: Path, monkeypatch
) -> None:
    implementation = "module Counter(input logic clk); endmodule\n"
    implementation_hash = hashlib.sha256(implementation.encode()).hexdigest()
    selected_identity = "selected:test:counter"
    source_map = GeneratedSourceMap(
        "direct_systemverilog",
        "Counter",
        selected_identity,
        implementation_hash,
        (
            GeneratedSourceMapEntry(
                GeneratedLineRange(1, 1),
                "port:clk",
                _origin(),
            ),
        ),
    )
    files = (
        VerificationBundleInput(
            "implementation/Counter.sv",
            "implementation",
            implementation.encode(),
        ),
        VerificationBundleInput(
            "harness/count_within.sv",
            "harness",
            b"module count_within_formal; endmodule\n",
        ),
        VerificationBundleInput(
            "config/count_within.json", "config", b"{}\n"
        ),
        VerificationBundleInput(
            "source-map/count_within.json",
            "source_map",
            source_map.to_json().encode(),
        ),
    )
    job = replace(
        _job(),
        source_map_files=("source-map/count_within.json",),
    )
    hardware = "hardware:" + _digest("mapped-counter-hardware")
    payload = _payload(hardware, {job.property_id: job.kind})
    identity = verification_identity_for(
        top="Counter",
        hardware_identity=hardware,
        property_ids=(job.property_id,),
        payload=payload,
    )
    publish_verification_bundle(
        tmp_path / "bundle",
        top="Counter",
        hardware_identity=hardware,
        verification_identity=identity,
        property_ids=(job.property_id,),
        verification_ir=payload,
        files=files,
        jobs=(job,),
    )
    seen = []

    def run(_source: str, **keywords: object) -> FormalResult:
        seen.extend(keywords["diagnostic_sources"])  # type: ignore[arg-type]
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

    inventory = ToolInventory(
        ("yosys", "sby", "yosys-smtbmc", "z3"),
        ("yosys", "sby", "yosys-smtbmc", "z3"),
        (("yosys", "test"), ("sby", "test"),
         ("yosys-smtbmc", "test"), ("z3", "test")),
    )
    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", run)
    report = run_verification_bundle(
        tmp_path / "bundle",
        toolchain=FormalToolchainContext("sby", "z3", inventory),
    )

    assert report.results[0].status == "bounded_pass"
    assert len(seen) == 1
    assert seen[0].source_map == source_map
    assert seen[0].generated_text == implementation
    assert seen[0].line_offset == 0


def test_verification_identity_excludes_source_attribution() -> None:
    hardware = "hardware:" + _digest("counter-hardware")
    left = _payload(hardware, {"user.count_within": "safety"})
    right = json.loads(json.dumps(left))
    right["identities"]["source"] = "source:" + _digest("relocated-source")
    right["properties"][0]["source_origin"] = None

    left_identity = verification_identity_for(
        top="Counter", hardware_identity=hardware,
        property_ids=("user.count_within",), payload=left,
    )
    right_identity = verification_identity_for(
        top="Counter", hardware_identity=hardware,
        property_ids=("user.count_within",), payload=right,
    )
    assert left_identity == right_identity


def test_payload_rejects_wrong_job_kind_and_vacuity_links(tmp_path: Path) -> None:
    hardware = "hardware:" + _digest("counter-hardware")
    cover_payload = _payload(hardware, {"user.count_within": "cover"})
    cover_identity = verification_identity_for(
        top="Counter", hardware_identity=hardware,
        property_ids=("user.count_within",), payload=cover_payload,
    )
    with pytest.raises(VerificationBundleError, match="kind does not match"):
        publish_verification_bundle(
            tmp_path / "wrong-kind", top="Counter", hardware_identity=hardware,
            verification_identity=cover_identity,
            property_ids=("user.count_within",), verification_ir=cover_payload,
            files=_inputs(), jobs=(_job(),),
        )

    invalid_dependency = _payload(
        hardware,
        {"user.count_within": "safety"},
    )
    invalid_dependency["vacuity_dependencies"] = {
        "user.count_within": "missing.feasibility"
    }
    with pytest.raises(VerificationBundleError, match="not a cover property"):
        verification_identity_for(
            top="Counter", hardware_identity=hardware,
            property_ids=("user.count_within",), payload=invalid_dependency,
        )


def test_run_report_rejects_unknown_status_and_insufficient_proof() -> None:
    with pytest.raises(VerificationBundleError, match="unsupported safety"):
        VerificationJobResult(
            "p", "safety", "solver_crashed", "bmc", "sby", "z3", 8,
        )

    bounded = VerificationJobResult(
        "p", "safety", "bounded_pass", "bmc", "sby", "z3", 8,
    )
    with pytest.raises(VerificationBundleError, match="mode does not match"):
        VerificationRunReport(
            "verification-bundle:" + _digest("bundle"),
            "Counter",
            VerificationRunConfig(ProofMode.PROVE, "sby", "z3", 8, 30),
            (bounded,),
            (),
        )


def test_prove_report_binds_bounded_evidence_to_the_same_verification_context() -> None:
    identity = "verification-bundle:" + _digest("bundle")
    context = dict(
        property_id="p",
        kind="safety",
        engine="sby",
        solver="z3",
        depth=8,
        source_origin=_origin(),
        route="direct_sv",
        backend="systemverilog",
        artifact_hash="artifact:" + _digest("artifact"),
        binding_identity="binding:" + _digest("binding"),
        selected_ir_identity="selected:" + _digest("selected"),
        scope_id="scope.main",
        assumption_ids=("require.legal",),
        clock_domain="clk",
        reset_domain="rst",
        physical_instance_path=("top", "child"),
    )
    final = VerificationJobResult(
        status="proven", mode="prove", **context,
    )
    bounded = VerificationJobResult(
        status="bounded_pass", mode="bmc", **context,
    )
    config = VerificationRunConfig(ProofMode.PROVE, "sby", "z3", 8, 30)
    report = VerificationRunReport(
        identity, "Counter", config, (final,), (), (bounded,),
    )
    assert VerificationRunReport.from_data(report.to_data()) == report

    corruptions = (
        {"route": "direct_systemverilog"},
        {"backend": "direct_systemverilog"},
        {"artifact_hash": "artifact:" + _digest("other")},
        {"binding_identity": "binding:" + _digest("other")},
        {"selected_ir_identity": "selected:" + _digest("other")},
        {"scope_id": "scope.other"},
        {"assumption_ids": ("require.other",)},
        {"clock_domain": "other_clk"},
        {"reset_domain": "other_rst"},
        {"physical_instance_path": ("top", "other")},
        {"source_origin": replace(_origin(), construct="different")},
    )
    for corruption in corruptions:
        with pytest.raises(VerificationBundleError, match="context differs"):
            VerificationRunReport(
                identity, "Counter", config, (final,), (),
                (replace(bounded, **corruption),),
            )

    alternate_final = replace(final, route="alternate_direct_model")
    alternate_bounded = replace(bounded, route="alternate_direct_model")
    alternate = VerificationRunReport(
        identity, "Counter", config, (alternate_final,), (),
        (alternate_bounded,),
    )
    assert alternate.run_identity != report.run_identity


def test_failure_and_counterexample_metadata_are_preserved(tmp_path: Path, monkeypatch) -> None:
    loaded = load_verification_bundle(tmp_path / "bundle") if (tmp_path / "bundle").exists() else None
    if loaded is None:
        _publish(tmp_path / "bundle")
        loaded = load_verification_bundle(tmp_path / "bundle")

    def fail(_source: str, **keywords: object) -> FormalResult:
        property_id = str(keywords["property_id"])
        return FormalResult(
            property_id,
            FormalStatus.FAILED,
            ProofMode.BMC,
            "sby",
            "z3",
            8,
            Counterexample(property_id, 3, (("count", "5"),), "trace"),
            keywords["source_origin"],  # type: ignore[arg-type]
            keywords["toolchain"].versions,  # type: ignore[union-attr]
            "formal counterexample reported",
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", fail)
    report = run_verification_bundle(loaded, config=VerificationRunConfig(depth=8))
    assert report.outcome == "failed"
    assert report.exit_code == 1
    result = report.to_data()["results"][0]
    assert result["counterexample"]["cycle"] == 3
    assert result["counterexample"]["sample_cycle"] == 3
    assert result["counterexample"]["reset_state"] is None
    assert result["counterexample"]["comparison_valid_state"] is None
    assert report.results[0].counterexample_metadata == (
        VerificationCounterexampleMetadata(sample_cycle=3)
    )
    restored = VerificationRunReport.from_data(report.to_data())
    assert restored.to_data() == report.to_data()
    assert result["source_origin"]["source_unit"] == "examples/counter.zhl"

    malformed = report.to_data()
    malformed["results"][0]["counterexample"]["sample_cycle"] = -1
    with pytest.raises(VerificationBundleError, match="sample cycle"):
        VerificationRunReport.from_data(malformed)


def test_cover_job_uses_bounded_reachability_executor(tmp_path: Path, monkeypatch) -> None:
    _publish(tmp_path / "bundle", jobs=(_job(kind="cover"),))
    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal",
                        lambda *_args, **_kwargs: pytest.fail("cover used safety executor"))
    monkeypatch.setattr(
        "zlang.verification_bundle.run_verilog_cover",
        lambda _source, **kwargs: CoverResult(
            str(kwargs["property_id"]), CoverStatus.WITNESSED,
            str(kwargs["engine"]), str(kwargs["solver"]), int(kwargs["depth"]),
            witness=CoverWitness(str(kwargs["property_id"]), 4),
            source_origin=kwargs["source_origin"],
            tool_versions=kwargs["toolchain"].versions,  # type: ignore[union-attr]
        ),
    )
    report = run_verification_bundle(tmp_path / "bundle")
    assert report.outcome == "passed"
    assert report.exit_code == 0
    assert report.results[0].status == "witnessed"
    assert report.results[0].witness is not None
    assert report.results[0].witness.cycle == 4
    assert report.to_data()["results"][0]["witness"]["cycle"] == 4


def test_unreached_feasibility_cover_marks_dependent_success_vacuous(
    tmp_path: Path, monkeypatch
) -> None:
    safety_id = "user.count_within"
    feasibility_id = "scope.requirements_feasible"
    hardware = "hardware:" + _digest("counter-hardware")
    payload = _payload(
        hardware,
        {safety_id: "safety", feasibility_id: "cover"},
        vacuity_dependencies={safety_id: feasibility_id},
    )
    identity = verification_identity_for(
        top="Counter", hardware_identity=hardware,
        property_ids=(safety_id, feasibility_id), payload=payload,
    )
    publish_verification_bundle(
        tmp_path / "bundle", top="Counter", hardware_identity=hardware,
        verification_identity=identity,
        property_ids=(safety_id, feasibility_id), verification_ir=payload,
        files=_inputs(),
        jobs=(_job(safety_id), _job(feasibility_id, kind="cover")),
    )
    monkeypatch.setattr(
        "zlang.verification_bundle.run_verilog_formal",
        lambda _source, **kwargs: FormalResult(
            str(kwargs["property_id"]), FormalStatus.BOUNDED_PASS,
            ProofMode.BMC, str(kwargs["engine"]), str(kwargs["solver"]),
            int(kwargs["depth"]), source_origin=kwargs["source_origin"],
            tool_versions=kwargs["toolchain"].versions,  # type: ignore[union-attr]
        ),
    )
    monkeypatch.setattr(
        "zlang.verification_bundle.run_verilog_cover",
        lambda _source, **kwargs: CoverResult(
            str(kwargs["property_id"]), CoverStatus.BOUNDED_UNREACHED,
            str(kwargs["engine"]), str(kwargs["solver"]), int(kwargs["depth"]),
            source_origin=kwargs["source_origin"],
            tool_versions=kwargs["toolchain"].versions,  # type: ignore[union-attr]
        ),
    )

    report = run_verification_bundle(tmp_path / "bundle")
    by_id = {item.property_id: item for item in report.results}
    assert by_id[feasibility_id].status == "bounded_unreached"
    assert by_id[safety_id].status == "unknown"
    assert "vacuous" in (by_id[safety_id].reason or "")
    assert report.exit_code == 2


def test_unbound_safety_job_is_an_explicit_skip(tmp_path: Path, monkeypatch) -> None:
    job = VerificationJob(
        "user.count_within",
        "safety",
        "count_within_formal",
        executable=False,
        reason="required formal observations are not connected",
        source_origin=_origin(),
    )
    hardware = "hardware:" + _digest("counter-hardware")
    payload = _payload(hardware, {job.property_id: "safety"})
    identity = verification_identity_for(
        top="Counter", hardware_identity=hardware,
        property_ids=(job.property_id,), payload=payload,
    )
    publish_verification_bundle(
        tmp_path / "bundle",
        top="Counter",
        hardware_identity=hardware,
        verification_identity=identity,
        property_ids=(job.property_id,),
        verification_ir=payload,
        files=(),
        jobs=(job,),
    )
    monkeypatch.setattr(
        "zlang.verification_bundle.FormalToolchainContext.discover",
        lambda **_kwargs: pytest.fail(
            "structured skip probed external formal tools"
        ),
    )
    report = run_verification_bundle(tmp_path / "bundle")
    assert report.results[0].status == "skipped"
    assert report.results[0].reason == job.reason
    assert report.results[0].tool_versions == ()


def test_unbound_cover_job_keeps_cover_mode(tmp_path: Path, monkeypatch) -> None:
    job = VerificationJob(
        "user.reaches_done",
        "cover",
        "reaches_done_formal",
        executable=False,
        reason="required formal observations are not connected",
        source_origin=_origin(),
    )
    hardware = "hardware:" + _digest("counter-hardware")
    payload = _payload(hardware, {job.property_id: "cover"})
    identity = verification_identity_for(
        top="Counter", hardware_identity=hardware,
        property_ids=(job.property_id,), payload=payload,
    )
    publish_verification_bundle(
        tmp_path / "bundle", top="Counter", hardware_identity=hardware,
        verification_identity=identity, property_ids=(job.property_id,),
        verification_ir=payload, files=(), jobs=(job,),
    )
    report = run_verification_bundle(tmp_path / "bundle")
    assert report.results[0].status == "skipped"
    assert report.results[0].mode == "cover"


@pytest.mark.parametrize(
    "mutation, message",
    (
        (lambda data: data.update({"unknown": True}), "unknown field"),
        (lambda data: data.update({"top": "Other"}), "identity does not match"),
        (
            lambda data: data.update(
                {"property_ids": sorted([*data["property_ids"], "other"])}
            ),
            "jobs must describe",
        ),
    ),
)
def test_manifest_rejects_malformed_identity_and_property_links(
    tmp_path: Path, mutation, message: str
) -> None:
    manifest = _publish(tmp_path / "bundle")
    data = json.loads(manifest.to_json())
    mutation(data)
    with pytest.raises(VerificationBundleError, match=message):
        VerificationBundleManifest.from_data(data)


def test_loader_rejects_missing_tampered_and_extra_files(tmp_path: Path) -> None:
    _publish(tmp_path / "tampered")
    (tmp_path / "tampered" / "harness" / "count_within.sv").write_text("changed\n")
    with pytest.raises(VerificationBundleError, match="expected hash"):
        load_verification_bundle(tmp_path / "tampered")

    _publish(tmp_path / "missing")
    (tmp_path / "missing" / "config" / "count_within.json").unlink()
    with pytest.raises(VerificationBundleError, match="missing"):
        load_verification_bundle(tmp_path / "missing")

    _publish(tmp_path / "extra")
    (tmp_path / "extra" / "solver.log").write_text("mutable output")
    with pytest.raises(VerificationBundleError, match="unexpected file set"):
        load_verification_bundle(tmp_path / "extra")


def test_bundle_rejects_unsafe_paths_wrong_ids_and_python_only_ir(tmp_path: Path) -> None:
    hardware = "hardware:" + _digest("counter-hardware")
    with pytest.raises(VerificationBundleError, match="below 'implementation/'"):
        VerificationBundleInput("harness/x.sv", "implementation", b"")
    with pytest.raises(VerificationBundleError, match="JSON values"):
        verification_identity_for(
            top="Top",
            hardware_identity="hardware:" + _digest("h"),
            property_ids=("p",),
            payload={"bad": object()},
        )
    with pytest.raises(VerificationBundleError, match="at least one property"):
        verification_identity_for(
            top="Top",
            hardware_identity="hardware:" + _digest("h"),
            property_ids=(),
            payload={},
        )
    with pytest.raises(VerificationBundleError, match="verification identity"):
        publish_verification_bundle(
            tmp_path / "bundle",
            top="Counter",
            hardware_identity=hardware,
            verification_identity="verification:" + _digest("wrong"),
            property_ids=("user.count_within",),
            verification_ir=_payload(hardware, {"user.count_within": "safety"}),
            files=_inputs(),
            jobs=(_job(),),
        )


def test_runner_rejects_misattributed_result(tmp_path: Path, monkeypatch) -> None:
    _publish(tmp_path / "bundle")
    monkeypatch.setattr(
        "zlang.verification_bundle.run_verilog_formal",
        lambda *_args, **_kwargs: FormalResult(
            "wrong.property", FormalStatus.BOUNDED_PASS, ProofMode.BMC,
            "sby", "z3", 20,
        ),
    )
    with pytest.raises(VerificationBundleError, match="returned property"):
        run_verification_bundle(tmp_path / "bundle")


def test_runner_rejects_mismatched_execution_metadata(tmp_path: Path, monkeypatch) -> None:
    _publish(tmp_path / "bundle")
    monkeypatch.setattr(
        "zlang.verification_bundle.run_verilog_formal",
        lambda *_args, **kwargs: FormalResult(
            str(kwargs["property_id"]), FormalStatus.BOUNDED_PASS, ProofMode.BMC,
            "sby", "z3", 19, source_origin=kwargs["source_origin"],
        ),
    )
    with pytest.raises(VerificationBundleError, match="mismatched execution metadata"):
        run_verification_bundle(tmp_path / "bundle")


def test_cli_emits_text_or_json_report_without_mutating_bundle(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _publish(tmp_path / "bundle")

    def pass_result(source: str, **keywords: object) -> FormalResult:
        assert source
        return FormalResult(
            str(keywords["property_id"]), FormalStatus.BOUNDED_PASS,
            ProofMode.BMC, "sby", "z3", int(keywords["depth"]),
            source_origin=keywords["source_origin"],  # type: ignore[arg-type]
            tool_versions=keywords["toolchain"].versions,  # type: ignore[union-attr]
        )

    monkeypatch.setattr("zlang.verification_bundle.run_verilog_formal", pass_result)
    report_path = tmp_path / "reports" / "formal.json"
    status = verification_main([
        str(tmp_path / "bundle"), "--depth", "6", "--format", "json",
        "--report", str(report_path),
    ])
    assert status == 0
    output = capsys.readouterr().out
    assert json.loads(output) == json.loads(report_path.read_text())
    assert json.loads(output)["results"][0]["status"] == "bounded_pass"

    assert verification_main([
        str(tmp_path / "bundle"), "--report",
        str(tmp_path / "bundle" / "run.json"),
    ]) == 2
    assert "outside the immutable bundle" in capsys.readouterr().err
    assert not (tmp_path / "bundle" / "run.json").exists()


def test_runner_rejects_work_directory_inside_immutable_bundle(tmp_path: Path) -> None:
    _publish(tmp_path / "bundle")
    with pytest.raises(VerificationBundleError, match="outside the immutable bundle"):
        run_verification_bundle(
            tmp_path / "bundle",
            work_directory=tmp_path / "bundle" / "solver-work",
        )


def test_cli_malformed_bundle_fails_closed(tmp_path: Path, capsys) -> None:
    assert verification_main([str(tmp_path / "missing")]) == 2
    assert "missing manifest.json" in capsys.readouterr().err
