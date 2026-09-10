from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.build_manifest import WholeBuildManifest
import zlang.cli as cli_module
from zlang.cli import main as compiler_main
from zlang.compiler import compile_source
from zlang.evidence_report import EvidenceReportPayload
from zlang.formal_exploration import FormalPolicy
from zlang.formal_orchestration import CompilerFormalExecutionPlan
from zlang.ir.formal import Counterexample, CoverWitness, ProofMode
from zlang.ir.formal_planning import FormalExecutionPlan
import zlang.verification_bundle as verification_bundle_module
from zlang.verification_bundle import (
    VerificationBundleError,
    VerificationJobResult,
    VerificationRunReport,
    load_verification_bundle,
)
from zlang.verification_cli import main as verification_main
from zlang.verification_publication import publish_compilation_verification_bundle


FORMAL_TOOLS = all(
    shutil.which(tool) for tool in ("yosys", "sby", "yosys-smtbmc", "z3")
)


def test_async_bundle_publishes_effective_and_physical_reset_trace_bindings(
    tmp_path: Path,
) -> None:
    source = """
module AsyncTraceBindings {
    clock clk
    async reset arst_n @clk { polarity active_low }
    out y : bit
    reg state : bit = 0
    state <- ~state
    y = state
    assert known @ clk { y == state }
}
"""
    directory = tmp_path / "bundle"
    publish_compilation_verification_bundle(
        compile_source(source, include_clash=False), directory
    )
    payload = load_verification_bundle(directory).verification_ir["payload"]
    binding_sets = payload["binding_sets"]
    assert binding_sets
    for binding_set in binding_sets:
        bindings = {
            item["semantic_signal_id"]: item
            for item in binding_set["bindings"]
        }
        assert bindings["physical_reset"]["rtl_name"] == "arst_n"
        assert bindings["trace:reset"]["rtl_name"].startswith(
            "zlang_formal_reset_active"
        )
        assert bindings["trace:reset"]["rtl_name"] != "arst_n"


def test_verification_overlay_does_not_change_production_rtl() -> None:
    hardware = """
module VerificationHashStable {
    clock clk
    reset rst
    in a : bit
    out y : bit
    y = a
}
"""
    verified = hardware.replace(
        "    y = a\n",
        "    y = a\n    assert follows @ clk { y == a }\n"
        "    cover high @ clk { y }\n",
    )
    plain = compile_source(hardware)
    with_goals = compile_source(verified)
    assert plain.clash == with_goals.clash
    assert emit_experimental(plain.ir) == emit_experimental(with_goals.ir)
    assert "follows: assert property" in with_goals.contracts_sva
    assert "high: cover property" in with_goals.contracts_sva


def test_compiler_bundle_is_independent_of_execution_depth(tmp_path: Path) -> None:
    source = tmp_path / "counter.zhl"
    source.write_text(SOURCE)
    shallow = tmp_path / "shallow"
    deep = tmp_path / "deep"

    assert compiler_main((
        str(source), "--verification-bundle", str(shallow),
        "--formal-depth", "3",
    )) == 0
    assert compiler_main((
        str(source), "--verification-bundle", str(deep),
        "--formal-depth", "31",
    )) == 0

    shallow_manifest = load_verification_bundle(shallow).manifest
    deep_manifest = load_verification_bundle(deep).manifest
    assert shallow_manifest.bundle_identity == deep_manifest.bundle_identity
    assert shallow_manifest.verification_identity == deep_manifest.verification_identity
    assert all(not item.config_files for item in shallow_manifest.jobs)
    assert all(item.kind != "config" for item in shallow_manifest.files)


def test_verification_identity_is_origin_insensitive_but_bundle_keeps_attribution(
    tmp_path: Path,
) -> None:
    source = """
module OriginStableVerification {
    clock clk
    reset rst
    in a : bit
    out y : bit
    y = a
    assert same @ clk { y == a }
}
"""
    shifted = source.replace("    assert same", "\n    assert same")
    left = publish_compilation_verification_bundle(
        compile_source(source, include_clash=False), tmp_path / "left"
    )
    right = publish_compilation_verification_bundle(
        compile_source(shifted, include_clash=False), tmp_path / "right"
    )

    assert left.hardware_identity == right.hardware_identity
    assert left.verification_identity == right.verification_identity
    assert left.bundle_identity != right.bundle_identity
    left_origin = left.jobs[0].source_origin
    right_origin = right.jobs[0].source_origin
    assert left_origin is not None and right_origin is not None
    assert left_origin.span.start_line + 1 == right_origin.span.start_line


def test_consistently_corrupted_goal_and_job_selected_ir_reject_against_hardware(
    tmp_path: Path,
) -> None:
    source = """
module SelectedIrCrossLink {
    clock clk reset rst
    in a : bit
    out y : bit
    y = a
    assert same @ clk { y == a }
}
"""
    publish_compilation_verification_bundle(
        compile_source(source, include_clash=False), tmp_path / "bundle"
    )
    loaded = load_verification_bundle(tmp_path / "bundle")
    payload = json.loads(json.dumps(loaded.verification_ir["payload"]))
    execution = FormalExecutionPlan.from_data(payload["execution_plan"])
    compiler_plan = CompilerFormalExecutionPlan.from_data(
        payload["compiler_execution_plan"]
    )
    fake_selected = "selected:" + "f" * 64
    fake_goals = tuple(
        replace(goal, selected_ir_identity=fake_selected)
        for goal in execution.goals
    )
    fake_execution = replace(
        execution,
        compilation_identity=fake_selected,
        goals=fake_goals,
    )
    fake_compiler = replace(
        compiler_plan,
        selected_ir_identity=fake_selected,
        verification_plan=fake_execution,
    )
    payload["execution_plan"] = fake_execution.to_data()
    payload["compiler_execution_plan"] = fake_compiler.to_data()
    fake_jobs = tuple(
        replace(job, selected_ir_identity=fake_selected)
        for job in loaded.manifest.jobs
    )

    with pytest.raises(
        VerificationBundleError,
        match="formal execution plan selected-IR identity does not match hardware",
    ):
        verification_bundle_module._validate_verification_payload(
            payload,
            property_ids=loaded.manifest.property_ids,
            jobs=fake_jobs,
        )


@pytest.mark.parametrize(
    "option",
    (
        ("--verification-format", "json"),
        ("--verify-require", "proven"),
        ("--verification-work-dir", "solver-work"),
    ),
)
def test_verification_execution_options_require_verify(
    tmp_path: Path, option: tuple[str, str]
) -> None:
    source = tmp_path / "counter.zhl"
    source.write_text(SOURCE)
    with pytest.raises(SystemExit) as raised:
        compiler_main((str(source), *option))
    assert raised.value.code == 2


@pytest.mark.parametrize("request_sby", (False, True))
def test_legacy_formal_views_reject_multi_job_plans_with_bundle_guidance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    request_sby: bool,
) -> None:
    source = tmp_path / "legacy-mixed.zhl"
    source.write_text(
        "module LegacyMixed { clock clk reset rst out y:bit y=0 "
        "assert stable @ clk { y == 0 } cover seen @ clk { y == 0 } }",
        encoding="utf-8",
    )
    harness = tmp_path / "formal.sv"
    arguments = [str(source), "--formal-harness", str(harness)]
    if request_sby:
        arguments.extend(("--formal-sby", str(tmp_path / "formal.sby")))

    with pytest.raises(SystemExit) as raised:
        compiler_main(tuple(arguments))
    assert raised.value.code == 2
    diagnostic = capsys.readouterr().err
    assert "legacy combined formal output would be incomplete" in diagnostic
    assert "--verification-bundle" in diagnostic
    assert not harness.exists()


SOURCE = """
module VerificationCliCounter {
    clock clk
    reset rst
    out y : u3
    reg count : u3 = 0
    count <- truncate<3>(count + 1)
    y = count

    assert within @ clk { count <= 7 }
    cover reaches_two @ clk { count == 2 }
    cover unreachable @ clk { count > 7 }
}
"""


def _stubbed_verification_report(
    bundle: Path, *, failed: bool = False,
) -> VerificationRunReport:
    loaded = load_verification_bundle(bundle)
    tools = (("yosys", "test-0.68"), ("z3", "test-4.8.12"))
    results = []
    for job in loaded.manifest.jobs:
        if job.kind == "cover":
            results.append(VerificationJobResult(
                job.property_id, "cover", "witnessed", "cover", "sby", "z3", 5,
                witness=CoverWitness(job.property_id, 2, (("port:y", "0b1"),)),
                source_origin=job.source_origin, tool_versions=tools,
            ))
        else:
            is_failed = failed and not any(
                item.status == "failed" for item in results
            )
            results.append(VerificationJobResult(
                job.property_id,
                "safety",
                "failed" if is_failed else "bounded_pass",
                "bmc",
                "sby",
                "z3",
                5,
                counterexample=(
                    Counterexample(
                        job.property_id, 1, (("port:y", "0b0"),), "test trace",
                    )
                    if is_failed else None
                ),
                source_origin=job.source_origin,
                tool_versions=tools,
            ))
    return VerificationRunReport(
        loaded.manifest.bundle_identity,
        loaded.manifest.top,
        cli_module.VerificationRunConfig(
            mode=ProofMode.BMC, depth=5, timeout_seconds=30,
        ),
        tuple(results),
        tools,
    )


@pytest.mark.parametrize(("failed", "expected_exit"), ((False, 0), (True, 1)))
def test_verify_results_join_evidence_and_whole_build_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
    expected_exit: int,
) -> None:
    source = tmp_path / "evidence.zhl"
    source.write_text("""
module VerificationEvidence {
    clock clk
    reset rst
    in a : bit
    out y : bit
    y = a
    assert follows @ clk { y == a }
    cover high @ clk { y }
}
""")
    rtl = tmp_path / "VerificationEvidence.sv"
    evidence_path = tmp_path / "evidence.json"
    manifest_path = tmp_path / "build.json"

    def execute(
        bundle: Path,
        *,
        config: object,
        work_directory: Path,
        **_keywords: object,
    ) -> VerificationRunReport:
        assert work_directory
        assert getattr(config, "jobs") == 2
        return _stubbed_verification_report(bundle, failed=failed)

    monkeypatch.setattr(cli_module, "run_verification_bundle_staged", execute)
    status = compiler_main((
        str(source),
        "--verify",
        "--formal-depth", "5",
        "--formal-timeout", "30",
        "--formal-jobs", "2",
        "--systemverilog", str(rtl),
        "--evidence-report", str(evidence_path),
        "--evidence-format", "json",
        "--build-manifest", str(manifest_path),
    ))
    capsys.readouterr()
    assert status == expected_exit

    evidence_payload = json.loads(evidence_path.read_text())
    compiler_plan = evidence_payload["formal_execution_plan"]
    assert compiler_plan is not None
    assert compiler_plan["formal_policy"] == "off"
    assert compiler_plan["m39_attempts"] == []
    assert compiler_plan["verification_plan"]["goals"]
    evidence = evidence_payload["evidence"]
    verification = [
        item for item in evidence if item["claim"].startswith("verification.")
    ]
    assert verification
    assert {item["status"] for item in verification} >= {
        "failed" if failed else "bounded_pass", "witnessed",
    }
    assert all(item["source_origin"] is not None for item in verification)
    assert all(item["route"] == "verification_bundle" for item in verification)
    assert not any(item["claim"].startswith(("m36.", "m38.", "m39.")) for item in evidence)

    manifest = WholeBuildManifest.from_json(manifest_path.read_text())
    manifest_verification = [
        item for item in manifest.evidence
        if item.claim.startswith("verification.")
    ]
    assert [item.evidence_id for item in manifest_verification] == [
        item["evidence_id"] for item in verification
    ]
    report = next(item for item in manifest.reports if item.kind == "evidence")
    assert set(report.evidence_ids) == {item.evidence_id for item in manifest.evidence}


def test_verify_and_formal_policy_publish_one_strict_common_evidence_view(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = """
module JointFormalEvidence {
    clock clk reset rst
    in a : u8
    out y : u8
    y = implement { a ^ 0 intent { minimize lut } }
    assert follows @ clk { y == a }
}
"""
    source = tmp_path / "joint.zhl"
    source.write_text(source_text, encoding="utf-8")

    class Verifier:
        formal_route = "M36_clash"

        @staticmethod
        def identity(candidate):
            return {
                "property_identity": "m36.joint." + candidate.implementation_identity,
                "reference_artifact_hash": "a" * 64,
                "implementation_artifact_hash": "b" * 64,
                "artifact_hash": "b" * 64,
                "harness_hash": "c" * 64,
                "backend_identity": "d" * 64,
                "assumptions_identity": "e" * 64,
            }

        def cache_identity(self, candidate, _config):
            return self.identity(candidate)

        def __call__(self, candidate, config):
            from zlang.ir.formal import FormalStatus

            return {
                "status": FormalStatus.BOUNDED_PASS,
                "mode": ProofMode.BMC,
                "depth": config.bmc_depth,
                "backend": "clash",
                "engine": "sby",
                "solver": "z3",
                **self.identity(candidate),
            }

    compilation = compile_source(
        source_text,
        include_clash=False,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=Verifier(),
    )
    monkeypatch.setattr(
        cli_module,
        "compile_file_snapshot",
        lambda *_args, **_kwargs: compilation,
    )
    monkeypatch.setattr(
        cli_module,
        "run_verification_bundle_staged",
        lambda bundle, *, config, work_directory, **_keywords:
        _stubbed_verification_report(bundle),
    )
    monkeypatch.setattr(
        cli_module,
        "prepare_selected_candidate_equivalence",
        lambda _result, plan, _config: (plan, ()),
    )
    monkeypatch.setattr(
        cli_module,
        "execute_prepared_candidate_equivalence",
        lambda _result, _plan, _prepared, _config, *, jobs=1: (),
    )
    evidence_path = tmp_path / "joint-evidence.json"
    status = compiler_main((
        str(source),
        "--verify",
        "--formal-policy", "available",
        "--evidence-report", str(evidence_path),
        "--evidence-format", "json",
    ))
    capsys.readouterr()
    assert status == 0

    payload = EvidenceReportPayload.from_json(
        evidence_path.read_text(encoding="utf-8")
    )
    assert payload.formal_execution_plan is not None
    assert payload.formal_execution_plan.formal_policy is FormalPolicy.AVAILABLE
    assert payload.formal_execution_plan.m39_attempts
    assert any(
        item.claim == "m39.formal_candidate_eligibility"
        for item in payload.evidence
    )
    assert any(item.claim.startswith("verification.") for item in payload.evidence)


def test_candidate_equivalence_public_trigger_requires_verify_and_non_off_policy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = """
module CandidateTrigger {
    clock clk reset rst
    in a : u8
    out y : u8
    y = implement { a ^ 0 intent { minimize lut } }
    assert follows @ clk { y == a }
}
"""
    source = tmp_path / "trigger.zhl"
    source.write_text(source_text, encoding="utf-8")

    class Verifier:
        formal_route = "M36_clash"

        @staticmethod
        def cache_identity(candidate, _config=None):
            return {
                "property_identity": "m36.trigger." + candidate.implementation_identity,
                "reference_artifact_hash": "a" * 64,
                "implementation_artifact_hash": "b" * 64,
                "artifact_hash": "b" * 64,
                "harness_hash": "c" * 64,
                "backend_identity": "d" * 64,
                "assumptions_identity": "e" * 64,
            }

        def __call__(self, candidate, config):
            from zlang.ir.formal import FormalStatus

            return {
                "status": FormalStatus.BOUNDED_PASS,
                "mode": ProofMode.BMC,
                "depth": config.bmc_depth,
                "backend": "clash",
                "engine": "sby",
                "solver": "z3",
                **self.cache_identity(candidate),
            }

    off = compile_source(
        source_text,
        include_clash=False,
        formal_policy=FormalPolicy.OFF,
    )
    available = compile_source(
        source_text,
        include_clash=False,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=Verifier(),
    )

    def compile_snapshot(*_args, **kwargs):
        return available if kwargs.get("formal_policy") == "available" else off

    calls = {"prepare": 0, "execute": 0}

    def prepare(_result, plan, _config):
        calls["prepare"] += 1
        return plan, ()

    def execute(_result, _plan, _prepared, _config, *, jobs=1):
        assert jobs == 1
        calls["execute"] += 1
        return ()

    monkeypatch.setattr(cli_module, "compile_file_snapshot", compile_snapshot)
    monkeypatch.setattr(
        cli_module,
        "run_verification_bundle_staged",
        lambda bundle, *, config, work_directory, **_keywords:
        _stubbed_verification_report(bundle),
    )
    monkeypatch.setattr(
        cli_module, "prepare_selected_candidate_equivalence", prepare
    )
    monkeypatch.setattr(
        cli_module, "execute_prepared_candidate_equivalence", execute
    )

    assert compiler_main((str(source),)) == 0
    capsys.readouterr()
    assert calls == {"prepare": 0, "execute": 0}

    assert compiler_main((str(source), "--verify")) == 0
    capsys.readouterr()
    assert calls == {"prepare": 0, "execute": 0}

    assert compiler_main((
        str(source),
        "--formal-policy", "available",
        "--verification-bundle", str(tmp_path / "bundle-only"),
    )) == 0
    capsys.readouterr()
    assert calls == {"prepare": 0, "execute": 0}

    assert compiler_main((
        str(source),
        "--verify",
        "--formal-policy", "available",
        "--verification-bundle", str(tmp_path / "joint"),
    )) == 0
    capsys.readouterr()
    assert calls == {"prepare": 1, "execute": 1}


def test_joint_candidate_failure_is_rendered_and_controls_cli_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = """
module CandidateReportExit {
    clock clk reset rst
    in a : u8
    out y : u8
    y = implement { a ^ 0 intent { minimize lut } }
    assert follows @ clk { y == a }
}
"""
    source = tmp_path / "report-exit.zhl"
    source.write_text(source_text, encoding="utf-8")

    class Verifier:
        formal_route = "M36_clash"

        @staticmethod
        def cache_identity(candidate, _config=None):
            return {
                "property_identity": "m36.exit." + candidate.implementation_identity,
                "reference_artifact_hash": "a" * 64,
                "implementation_artifact_hash": "b" * 64,
                "artifact_hash": "b" * 64,
                "harness_hash": "c" * 64,
                "backend_identity": "d" * 64,
                "assumptions_identity": "e" * 64,
            }

        def __call__(self, candidate, config):
            from zlang.ir.formal import FormalStatus

            return {
                "status": FormalStatus.BOUNDED_PASS,
                "mode": ProofMode.BMC,
                "depth": config.bmc_depth,
                "backend": "clash",
                "engine": "sby",
                "solver": "z3",
                **self.cache_identity(candidate),
            }

    compilation = compile_source(
        source_text,
        include_clash=False,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=Verifier(),
    )
    candidate_result = object()

    class Combined:
        def __init__(self, verification, plan, candidate):
            assert verification.outcome == "passed"
            assert plan.formal_policy is FormalPolicy.AVAILABLE
            assert candidate == (candidate_result,)
            self.exit_code = 1

        @staticmethod
        def to_json() -> str:
            return '{"candidate_equivalence":"failed"}\n'

        @staticmethod
        def to_text() -> str:
            return "candidate equivalence failed\n"

    monkeypatch.setattr(
        cli_module,
        "compile_file_snapshot",
        lambda *_args, **_kwargs: compilation,
    )
    monkeypatch.setattr(
        cli_module,
        "run_verification_bundle_staged",
        lambda bundle, *, config, work_directory, **_keywords:
        _stubbed_verification_report(bundle),
    )
    monkeypatch.setattr(
        cli_module,
        "prepare_selected_candidate_equivalence",
        lambda _result, plan, _config: (plan, (object(),)),
    )
    monkeypatch.setattr(
        cli_module,
        "execute_prepared_candidate_equivalence",
        lambda _result, _plan, _prepared, _config, *, jobs=1: (candidate_result,),
    )
    monkeypatch.setattr(cli_module, "CompilerVerificationReport", Combined)
    report = tmp_path / "verification.json"

    status = compiler_main((
        str(source),
        "--verify",
        "--formal-policy", "available",
        "--verification-report", str(report),
        "--verification-format", "json",
    ))
    captured = capsys.readouterr()

    assert status == 1
    assert captured.out == '{"candidate_equivalence":"failed"}\n'
    assert report.read_text(encoding="utf-8") == captured.out


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real SBY/Yosys/Z3 toolchain required")
def test_compiler_publishes_and_executes_safety_and_cover_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "counter.zhl"
    source.write_text(SOURCE)
    bundle = tmp_path / "verify"
    report = tmp_path / "report.json"
    work = tmp_path / "solver-work"

    status = compiler_main((
        str(source), "--verification-bundle", str(bundle), "--verify",
        "--verification-report", str(report), "--verification-format", "json",
        "--verification-work-dir", str(work),
        "--formal-depth", "5", "--formal-timeout", "30",
    ))
    captured = capsys.readouterr()
    assert status == 0
    assert "module VerificationCliCounter" not in captured.out
    assert '"bounded_pass"' in captured.out
    assert '"witnessed"' in captured.out
    assert '"bounded_unreached"' in captured.out
    assert report.read_text() == captured.out
    loaded = load_verification_bundle(bundle)
    assert {item.kind for item in loaded.manifest.jobs} == {"safety", "cover"}
    assert (bundle / "verification-ir.json").is_file()
    assert (bundle / "source-map" / "formal.json").is_file()
    report_data = json.loads(report.read_text())
    assert all(item["work_directory"] for item in report_data["results"])
    witnessed = next(
        item for item in report_data["results"] if item["status"] == "witnessed"
    )
    assert witnessed["witness"]["values"]
    assert any(
        name.startswith(("port:", "register:", "fifo:"))
        for name, _value in witnessed["witness"]["values"]
    )
    assert list(work.glob("*-bmc-*/job-*/solver.stdout.log"))
    assert list(work.glob("*-bmc-*/job-*/*.sby"))


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real SBY/Yosys/Z3 toolchain required")
def test_bundle_replays_after_source_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "counter.zhl"
    source.write_text(SOURCE)
    bundle = tmp_path / "verify"
    work = tmp_path / "replay-work"
    assert compiler_main((
        str(source), "--verification-bundle", str(bundle), "--formal-depth", "5",
    )) == 0
    source.unlink()

    status = verification_main((
        str(bundle), "--depth", "5", "--timeout", "30", "--format", "text",
        "--work-dir", str(work),
    ))
    output = capsys.readouterr().out
    assert status == 0
    assert "bounded_pass" in output
    assert "witnessed" in output
    assert "bounded_unreached" in output
    assert list(work.glob("*-bmc-*/job-*/solver.stdout.log"))


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real SBY/Yosys/Z3 toolchain required")
def test_source_assertion_mutation_exits_one_with_origin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "broken.zhl"
    source.write_text(SOURCE.replace("count <= 7", "count != 2"))
    status = compiler_main((
        str(source), "--verify", "--verification-format", "json",
        "--formal-depth", "5", "--formal-timeout", "30",
    ))
    output = capsys.readouterr().out
    assert status == 1
    assert '"failed"' in output
    assert '"source_unit": "broken.zhl"' in output
    failed = next(
        item for item in json.loads(output)["results"]
        if item["status"] == "failed"
    )
    assert failed["counterexample"]["cycle"] is not None
    assert failed["counterexample"]["sample_cycle"] == (
        failed["counterexample"]["cycle"]
    )
    assert failed["counterexample"]["reset_state"] == "0"
    assert failed["counterexample"]["comparison_valid_state"] is None
    assert failed["counterexample"]["values"]
    assert any(
        name.startswith(("port:", "register:", "fifo:"))
        for name, _value in failed["counterexample"]["values"]
    )


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real SBY/Yosys/Z3 toolchain required")
def test_safe_async_reset_executes_and_mutation_keeps_source_attribution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "async.zhl"
    source.write_text("""
module AsyncVerification {
    clock clk
    async reset arst @clk
    out y : bit
    reg state : bit = 0
    state <- ~state
    y = state
    assert known @ clk { y == state }
}
""")
    status = compiler_main((
        str(source),
        "--verify",
        "--verification-format", "json",
        "--formal-depth", "6",
    ))
    passed = json.loads(capsys.readouterr().out)
    assert status == 0
    assert passed["outcome"] == "passed"
    assert {item["status"] for item in passed["results"]} == {"bounded_pass"}
    assert {item["reset_domain"] for item in passed["results"]} == {"arst"}
    assert {
        item["clock_domain_contract"]["reset_release_mode"]
        for item in passed["results"]
    } == {"synchronized"}
    assert all(
        item["physical_domain_identity"]
        for item in passed["results"]
    )

    source.write_text(
        source.read_text().replace("y == state", "state == 0"),
        encoding="utf-8",
    )
    status = compiler_main((
        str(source),
        "--verify",
        "--verification-format", "json",
        "--formal-depth", "6",
    ))
    failed_report = json.loads(capsys.readouterr().out)
    assert status == 1
    failed = next(
        item for item in failed_report["results"]
        if item["status"] == "failed"
    )
    assert failed["source_origin"]["source_unit"] == "async.zhl"
    assert failed["source_origin"]["construct"] == "assert known"
    assert failed["counterexample"] is not None
    assert failed["counterexample"]["reset_state"] == "0"


def test_unsupported_quantized_predicate_is_a_source_error_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "quantized.zhl"
    source.write_text(
        "module BadFixedGoal { clock clk reset rst "
        "in a:fixed<16,8> out y:bit y=0 "
        "assert quantized { "
        "quantize<fixed<12,4>>(a){round floor overflow wrap} == 0 "
        "} }"
    )
    status = compiler_main((str(source), "--check", "--diagnostic-format", "json"))
    captured = capsys.readouterr()
    assert status == 1
    diagnostic = json.loads(captured.err)
    assert diagnostic["code"] == "ZL-VERIFY-PREDICATE"
    assert "quantized fixed-point" in diagnostic["message"]
    assert "Traceback" not in captured.err


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real SBY/Yosys/Z3 toolchain required")
def test_proven_requirement_never_promotes_bmc_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "identity.zhl"
    source.write_text("""
module ProvenIdentity {
    clock clk
    reset rst
    in a : bit
    out y : bit
    y = a
    assert identity @ clk { y == a }
}
""")
    work = tmp_path / "prove-work"
    assert compiler_main((
        str(source), "--verify", "--verify-require", "proven",
        "--verification-format", "json", "--formal-depth", "3",
        "--formal-timeout", "30", "--verification-work-dir", str(work),
    )) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert {item["status"] for item in report["results"]} == {"proven"}
    assert {
        item["status"] for item in report["bounded_results"]
    } == {"bounded_pass"}
    assert list(work.glob("*-bmc-*/job-*/solver.stdout.log"))
    assert list(work.glob("*-prove-*/job-*/solver.stdout.log"))


@pytest.mark.skipif(not FORMAL_TOOLS, reason="real SBY/Yosys/Z3 toolchain required")
def test_dynamically_unreachable_requirement_is_reported_vacuous(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "vacuous.zhl"
    source.write_text("""
module VacuousScope {
    clock clk
    reset rst
    in a : bit
    out y : bit
    y = a
    contract impossible @ clk {
        require contradiction { a & !a }
        ensure identity { y == a }
    }
}
""")
    status = compiler_main((
        str(source), "--verify", "--verification-format", "json",
        "--formal-depth", "4", "--formal-timeout", "30",
    ))
    output = capsys.readouterr().out
    assert status == 2
    assert '"bounded_unreached"' in output
    assert '"status": "unknown"' in output
    assert "vacuous" in output
