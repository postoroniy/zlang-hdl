"""Verification-bundle publication and execution for compiler CLI requests."""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from zlang.backend.systemverilog import SystemVerilogEmissionError
from zlang import candidate_equivalence as candidate_api
from zlang.compilation_products import CompilationResult
from zlang.compiler_verification_report import CompilerVerificationReport
from zlang.formal_exploration import FormalExplorationConfig, FormalPolicy
from zlang import formal_orchestration as orchestration_api
from zlang.ir.formal import FormalError, ProofMode
from zlang.source_identity import CLI_NAME
from zlang import verification_bundle as bundle_api
from zlang import verification_publication as publication_api

@dataclass(frozen=True)
class VerificationProducts:
    report: object | None = None
    formal_plan: orchestration_api.CompilerFormalExecutionPlan | None = None
    candidate_reports: tuple[orchestration_api.CandidateEquivalenceExecutionReport, ...] = ()
    exit_code: int = 0
    stop: bool = False


def _run_requested_verification(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    result: CompilationResult,
    verification_work_directory: Path | None,
) -> VerificationProducts:
    """Publish and optionally execute the requested formal verification product."""

    if not arguments.verify and arguments.verification_bundle is None:
        return VerificationProducts()
    verification_temporary = None
    candidate_plan_temporary = None
    verification_report = None
    compiler_formal_plan = None
    prepared_candidate_equivalence: tuple[candidate_api.PreparedCandidateEquivalenceSite, ...] = ()
    candidate_equivalence_reports: tuple[orchestration_api.CandidateEquivalenceExecutionReport, ...] = ()
    try:
        bundle_directory = arguments.verification_bundle
        if bundle_directory is None:
            verification_temporary = tempfile.TemporaryDirectory(
                prefix="zlang-verification-"
            )
            bundle_directory = Path(verification_temporary.name)
        normalized_formal_policy = FormalPolicy(
            getattr(result.implementation_request, "formal_policy")
        )
        candidate_config = FormalExplorationConfig(
            policy=normalized_formal_policy,
            max_formal_candidates=arguments.formal_max_candidates,
            bmc_depth=arguments.formal_depth,
            timeout_seconds=arguments.formal_timeout,
            cache_directory=arguments.formal_cache,
            artifact_provider=result.formal_artifact_provider,
            tool_resolver=result.formal_tool_resolver,
        )
        if arguments.verify and normalized_formal_policy is not FormalPolicy.OFF:
            candidate_plan_temporary = tempfile.TemporaryDirectory(
                prefix="zlang-candidate-plan-"
            )
            preliminary_directory = Path(candidate_plan_temporary.name)
            publication_api.publish_compilation_verification_bundle(result, preliminary_directory)
            preliminary = bundle_api.load_verification_bundle(preliminary_directory)
            preliminary_payload = preliminary.verification_ir.get("payload")
            if not isinstance(preliminary_payload, dict):
                raise bundle_api.VerificationBundleError(
                    "verification bundle has no compiler formal plan payload"
                )
            preliminary_plan = orchestration_api.CompilerFormalExecutionPlan.from_data(
                preliminary_payload.get("compiler_execution_plan")
            )
            compiler_formal_plan, prepared_candidate_equivalence = (
                candidate_api.prepare_selected_candidate_equivalence(
                    result,
                    preliminary_plan,
                    candidate_config,
                )
            )
            publication_api.publish_compilation_verification_bundle(
                result,
                bundle_directory,
                compiler_execution_plan=compiler_formal_plan,
                prepared_candidate_equivalence=prepared_candidate_equivalence,
            )
        else:
            publication_api.publish_compilation_verification_bundle(result, bundle_directory)
        loaded_bundle = bundle_api.load_verification_bundle(bundle_directory)
        verification_payload = loaded_bundle.verification_ir.get("payload")
        if not isinstance(verification_payload, dict):
            raise bundle_api.VerificationBundleError(
                "verification bundle has no compiler formal plan payload"
            )
        compiler_formal_plan = orchestration_api.CompilerFormalExecutionPlan.from_data(
            verification_payload.get("compiler_execution_plan")
        )
        if arguments.verify:
            assert verification_work_directory is not None
            verification_run_keywords: dict[str, object] = {
                "config": bundle_api.VerificationRunConfig(
                    mode=(
                        ProofMode.PROVE
                        if (arguments.verify_require or "checked") == "proven"
                        else ProofMode.BMC
                    ),
                    depth=arguments.formal_depth,
                    timeout_seconds=arguments.formal_timeout,
                    jobs=arguments.formal_jobs,
                ),
                "work_directory": verification_work_directory,
                "tool_resolver": result.formal_tool_resolver,
            }
            if arguments.formal_cache is not None:
                verification_run_keywords["cache_directory"] = arguments.formal_cache
            verification_report = bundle_api.run_verification_bundle_staged(
                bundle_directory,
                **verification_run_keywords,
            )
            if compiler_formal_plan.formal_policy is not FormalPolicy.OFF:
                candidate_config = replace(
                    candidate_config,
                    work_directory=verification_work_directory,
                )
                candidate_equivalence_reports = candidate_api.execute_prepared_candidate_equivalence(
                    result,
                    compiler_formal_plan,
                    prepared_candidate_equivalence,
                    candidate_config,
                    jobs=arguments.formal_jobs,
                )
    except (
        FormalError,
        orchestration_api.FormalOrchestrationError,
        SystemVerilogEmissionError,
        bundle_api.VerificationBundleError,
        OSError,
    ) as error:
        print(f"{CLI_NAME}: verification unavailable: {error}", file=sys.stderr)
        if verification_temporary is not None:
            verification_temporary.cleanup()
        if candidate_plan_temporary is not None:
            candidate_plan_temporary.cleanup()
        return VerificationProducts(exit_code=2, stop=True)

    verification_exit_code = 0
    if verification_report is not None:
        public_report = (
            CompilerVerificationReport(
                verification_report,
                compiler_formal_plan,
                candidate_equivalence_reports,
            )
            if candidate_equivalence_reports
            else verification_report
        )
        rendered = (
            public_report.to_json()
            if (arguments.verification_format or "text") == "json"
            else public_report.to_text()
        )
        if arguments.verification_report is not None:
            if arguments.verification_bundle is not None:
                try:
                    arguments.verification_report.resolve(strict=False).relative_to(
                        arguments.verification_bundle.resolve(strict=False)
                    )
                except ValueError:
                    pass
                else:
                    parser.error(
                        "--verification-report must be outside the immutable bundle"
                    )
            arguments.verification_report.parent.mkdir(parents=True, exist_ok=True)
            arguments.verification_report.write_text(rendered)
        print(rendered, end="")
        verification_exit_code = public_report.exit_code
    if verification_temporary is not None:
        verification_temporary.cleanup()
    if candidate_plan_temporary is not None:
        candidate_plan_temporary.cleanup()
    stop = (
        verification_exit_code != 0
        and arguments.evidence_report is None
        and arguments.build_manifest is None
    )
    return VerificationProducts(
        verification_report,
        compiler_formal_plan,
        candidate_equivalence_reports,
        verification_exit_code,
        stop,
    )
