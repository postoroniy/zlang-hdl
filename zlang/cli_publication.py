"""Artifact and whole-build publication for compiler CLI requests."""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from zlang import build_manifest as manifest_api
from zlang import build_publication as publication_api
from zlang.common import stable_digest
from zlang.backend import companions as companion_api
from zlang.backend import manifest as backend_manifest
from zlang.backend import source_map as source_map_api
from zlang.simulation_state import SimulationStateError
from zlang.backend.systemverilog import simulation_state as sv_state
from zlang.opt.render import render as render_optimization_ir
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA
from zlang import synthesis as synthesis_api
from zlang import toolchain as toolchain_api
from zlang import formal_orchestration as orchestration_api
from zlang.compilation_products import CompilationResult
from zlang import platform_constraints as constraint_api
from zlang import evidence_report as evidence_api
from zlang import generated_navigation_bundle as navigation_api
from zlang import cli_arguments
from zlang.cli_verification import VerificationProducts as _VerificationProducts

def _query_tool_version(executable: str, argument: str) -> str:
    """Query a tool only after that exact tool has executed successfully."""

    completed = subprocess.run(
        [executable, argument],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return "version-unreported"
    output = (completed.stdout.strip() or completed.stderr.strip()).splitlines()
    return output[0].strip() if output else "version-unreported"


def _tool_execution(
    *,
    role: str,
    tool: str,
    version: str,
    argv_shape: tuple[str, ...],
    outputs: tuple[str, ...] = (),
) -> manifest_api.ToolExecutionRecord:
    identity = stable_digest({
        "schema": "zlang-tool-execution-v1",
        "role": role,
        "tool": tool,
        "version": version,
        "argv_shape": list(argv_shape),
        "outputs": list(outputs),
    })
    return manifest_api.ToolExecutionRecord(
        execution_id=f"{tool}.{identity}",
        role=role,
        tool=tool,
        version=version,
        argv_shape=argv_shape,
        status="passed",
        exit_code=0,
        outputs=outputs,
    )


def _report_format(path: Path, *, fallback: str = "text") -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".sv", ".sva"}:
        return "systemverilog"
    if suffix == ".sby":
        return "sby"
    if suffix == ".md":
        return "markdown"
    return fallback
@dataclass(frozen=True)
class _PublishedArtifacts:
    companion_paths: tuple[Path, ...]
    constraints: tuple[tuple[Path, constraint_api.ConstraintArtifact], ...]
    source_map: source_map_api.GeneratedSourceMap | None
    evidence: tuple[manifest_api.EvidenceRecord, ...]
    rendered_evidence: evidence_api.RenderedEvidenceReport | None
    tool_executions: tuple[manifest_api.ToolExecutionRecord, ...]


def _publish_requested_artifacts(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    result: CompilationResult,
    *,
    constraint_requested: bool,
    selected_platform_profile: constraint_api.PlatformConstraintProfile | None,
    systemverilog_output: Path | None,
    direct_systemverilog: str,
    direct_artifact: backend_manifest.BackendArtifact | None,
    simulation_state_bundle: sv_state.SystemVerilogSimulationStateBundle | None,
    source_digest: str,
    synthesis_report: str,
    saturation_report: str,
    verification: _VerificationProducts,
) -> _PublishedArtifacts:
    """Publish requested files and return their immutable manifest inputs."""

    if arguments.implementation_manifest is not None and direct_artifact is None:
        parser.error("--implementation-manifest requires --systemverilog")
    if not (cli_arguments.has_explicit_artifact_sink(arguments) or arguments.verify):
        parser.error("select --check, --systemverilog, --verify, or an artifact output")
    constraint_products: list[tuple[Path, constraint_api.ConstraintArtifact]] = []
    if constraint_requested:
        if result.physical_inputs.project_manifest is None:
            parser.error("constraint publication requires a zlang.toml project")
        try:
            if selected_platform_profile is None:
                raise constraint_api.PlatformConstraintError(
                    f"profile '{arguments.profile}' has no platform clock declaration"
                )
            if direct_artifact is None:
                raise constraint_api.PlatformConstraintError(
                    "constraint publication requires a generated backend artifact"
                )
            constraint = selected_platform_profile.clocks[0]
            for path, format in (
                (arguments.constraints_xdc, constraint_api.ConstraintFormat.XDC),
                (arguments.constraints_sdc, constraint_api.ConstraintFormat.SDC),
            ):
                if path is not None:
                    constraint_products.append((
                        path,
                        constraint_api.build_constraint_artifact(
                            result.ir, direct_artifact, constraint, format
                        ),
                    ))
        except (constraint_api.PlatformConstraintError, OSError) as error:
            parser.error(str(error))
    try:
        cli_arguments.preflight_companion_paths(
            arguments,
            systemverilog_output=systemverilog_output,
            direct_companions=(direct_artifact.companions if direct_artifact else ()),
            compilation_inputs=result.physical_inputs.all_paths,
        )
    except ValueError as error:
        parser.error(str(error))

    direct_companion_paths: tuple[Path, ...] = ()
    source_map = None
    tool_executions: list[manifest_api.ToolExecutionRecord] = []
    if systemverilog_output is not None:
        systemverilog_output.parent.mkdir(parents=True, exist_ok=True)
        try:
            direct_companion_paths = companion_api.publish_companion_bundle(
                direct_artifact.companions if direct_artifact else (),
                systemverilog_output.parent,
            )
        except companion_api.CompanionArtifactError as error:
            parser.error(str(error))
        systemverilog_output.write_text(direct_systemverilog)
        if arguments.verilator_lint:
            verilator_executable = arguments.verilator or shutil.which("verilator")
            if verilator_executable is None:
                parser.error("--verilator-lint requires Verilator on PATH")
            try:
                toolchain_api.lint_with_verilator(
                    (
                        systemverilog_output,
                        *((arguments.contracts_sva,) if arguments.contracts_sva else ()),
                    ),
                    result.ir.name,
                    verilator_executable,
                )
            except toolchain_api.ToolchainError as error:
                parser.error(str(error))
            tool_executions.append(_tool_execution(
                role="rtl_lint",
                tool="verilator",
                version=_query_tool_version(verilator_executable, "--version"),
                argv_shape=(
                    "verilator", "--lint-only", "--top-module", "<top>",
                    "<rtl-inputs>",
                ),
            ))
    if arguments.simulation_state_bundle is not None:
        assert simulation_state_bundle is not None
        assert systemverilog_output is not None
        try:
            simulation_state_bundle.validate_rtl_file(systemverilog_output)
            simulation_state_bundle.publish(arguments.simulation_state_bundle)
        except (OSError, SimulationStateError) as error:
            parser.error(str(error))
    for path, constraint_artifact in constraint_products:
        try:
            constraint_api.publish_constraint_artifact(constraint_artifact, path)
        except constraint_api.PlatformConstraintError as error:
            parser.error(str(error))
    if arguments.implementation_manifest is not None:
        arguments.implementation_manifest.parent.mkdir(parents=True, exist_ok=True)
        arguments.implementation_manifest.write_text(direct_artifact.to_json())
    if arguments.source_map is not None or arguments.generated_navigation_bundle is not None:
        assert direct_artifact is not None
        source_map = source_map_api.build_generated_source_map(result.ir, direct_artifact)
        if arguments.source_map is not None:
            arguments.source_map.parent.mkdir(parents=True, exist_ok=True)
            arguments.source_map.write_text(source_map.to_json())
    if arguments.generated_navigation_bundle is not None:
        assert direct_artifact is not None
        assert source_map is not None
        assert systemverilog_output is not None
        root_identity = result.ir.root_module_identity
        try:
            navigation_api.publish_generated_navigation_bundle(
                arguments.generated_navigation_bundle,
                artifact=direct_artifact,
                source_map=source_map,
                generated_path=systemverilog_output,
                root_source_unit=(
                    root_identity.logical_path
                    if root_identity is not None
                    else arguments.source.name
                ),
                root_source_digest=source_digest,
            )
        except navigation_api.GeneratedNavigationBundleError as error:
            parser.error(str(error))

    report_contents = {
        "contracts_sva": lambda: result.contracts_sva,
        "implementation_report": lambda: result.implementation_report,
        "cost_report": lambda: result.cost_report,
        "pipeline_report": lambda: result.pipeline_report,
        "architecture_report": lambda: result.architecture_report,
        "exploration_report": lambda: result.exploration_report,
        "implementation_policy_report": lambda: result.implementation_policy_report,
        "backend_implementation_report": lambda: result.backend_implementation_report,
        "synthesis_report": lambda: synthesis_report,
        "saturation_report": lambda: saturation_report,
        "csr_markdown": lambda: result.csr_markdown,
        "csr_json": lambda: result.csr_json,
    }
    for attribute, contents in report_contents.items():
        path = getattr(arguments, attribute)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents())
    for attribute in ("optimization_ir", "high_level_ir"):
        path = getattr(arguments, attribute)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_optimization_ir(getattr(result, attribute)))

    evidence_records: tuple[manifest_api.EvidenceRecord, ...] = ()
    rendered_evidence = None
    if arguments.evidence_report is not None or arguments.build_manifest is not None:
        try:
            records = [evidence_api.evidence_for_typed_module(
                result.ir,
                high_level_ir_identity=result.high_level_ir_identity,
            )]
            if result.ir.timing_contract is not None:
                records.append(evidence_api.evidence_for_validated_timing(
                    result.ir,
                    selected_ir_identity=result.selected_ir_identity,
                ))
            records.extend(orchestration_api.collect_formal_selection_evidence(result))
            if verification.report is not None:
                records.extend(evidence_api.evidence_from_verification_report(verification.report))
            for report in verification.candidate_reports:
                records.extend(report.evidence_records)
            evidence_records = tuple(records)
            if arguments.evidence_report is not None:
                rendered_evidence = evidence_api.build_evidence_report(
                    "build.evidence",
                    evidence_records,
                    format=arguments.evidence_format,
                    formal_execution_plan=verification.formal_plan,
                    candidate_equivalence=verification.candidate_reports,
                    logical_path=(
                        "reports/evidence.json"
                        if arguments.evidence_format == "json"
                        else "reports/evidence.txt"
                    ),
                )
                arguments.evidence_report.parent.mkdir(parents=True, exist_ok=True)
                arguments.evidence_report.write_text(rendered_evidence.content)
        except (
            evidence_api.EvidenceReportError,
            orchestration_api.FormalOrchestrationError,
        ) as error:
            parser.error(str(error))
    return _PublishedArtifacts(
        direct_companion_paths,
        tuple(constraint_products),
        source_map,
        evidence_records,
        rendered_evidence,
        tuple(tool_executions),
    )


def _publish_build_manifest(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    result: CompilationResult,
    *,
    source_bytes: bytes,
    direct_artifact: backend_manifest.BackendArtifact | None,
    systemverilog_output: Path | None,
    direct_companion_paths: tuple[Path, ...],
    constraint_products: tuple[tuple[Path, constraint_api.ConstraintArtifact], ...],
    source_map: source_map_api.GeneratedSourceMap | None,
    evidence_records: tuple[manifest_api.EvidenceRecord, ...],
    rendered_evidence: evidence_api.RenderedEvidenceReport | None,
    synthesis_feedback: synthesis_api.SynthesisFeedbackResult | None,
    tool_executions: tuple[manifest_api.ToolExecutionRecord, ...],
    selected_platform_profile: constraint_api.PlatformConstraintProfile | None,
) -> None:
    """Publish one complete manifest from already materialized artifacts."""

    if arguments.build_manifest is None:
        return
    try:
        executions = list(tool_executions)
        root_publication = publication_api.source_publication(
            arguments.source,
            compiled_bytes=source_bytes,
        )
        all_publications: list[publication_api.PhysicalPublication] = [root_publication]
        backend_builds = []
        reports: list[manifest_api.ReportRecord] = []
        constraint_publications: dict[str, list[publication_api.PhysicalPublication]] = {}
        for path, constraint_artifact in constraint_products:
            publication = publication_api.published_file_from_path(
                "backends/"
                f"{constraint_artifact.backend}/constraints/"
                f"{result.ir.name}.{constraint_artifact.format.value}",
                path,
                kind=f"platform_constraint:{constraint_artifact.format.value}",
            )
            constraint_publications.setdefault(
                constraint_artifact.backend, []
            ).append(publication)

        def add_report(
            attribute: str,
            kind: str,
            *,
            format: str | None = None,
            evidence_ids: tuple[str, ...] = (),
        ) -> None:
            path = getattr(arguments, attribute)
            if path is None:
                return
            suffix = path.suffix.lower() or ".txt"
            logical_path = f"reports/{attribute.replace('_', '-')}{suffix}"
            record, publication = publication_api.report_from_path(
                f"build.{attribute.replace('_', '.')}",
                kind,
                format or _report_format(path),
                logical_path,
                path,
                evidence_ids=evidence_ids,
            )
            reports.append(record)
            all_publications.append(publication)

        source_map_hash = None
        source_map_publication = None
        if arguments.source_map is not None:
            source_map_hash = hashlib.sha256(
                arguments.source_map.read_bytes()
            ).hexdigest()
            source_map_publication = publication_api.published_file_from_path(
                "backends/direct_systemverilog/source-map.json",
                arguments.source_map,
                kind="generated_source_map",
            )

        if direct_artifact is not None and systemverilog_output is not None:
            direct_files = [publication_api.published_file_from_path(
                publication_api.backend_logical_path(
                    "direct_systemverilog", result.ir.name, "sv"
                ),
                systemverilog_output,
                kind="direct_systemverilog",
            )]
            if arguments.implementation_manifest is not None:
                direct_files.append(publication_api.published_file_from_path(
                    "backends/direct_systemverilog/backend-artifact.json",
                    arguments.implementation_manifest,
                    kind="backend_artifact_manifest",
                ))
            if (
                source_map_publication is not None
                and source_map is not None
                and source_map.backend == "direct_systemverilog"
            ):
                direct_files.append(source_map_publication)
            direct_companions = tuple(
                publication_api.published_file_from_path(
                    f"backends/direct_systemverilog/companions/{path.name}",
                    path,
                    kind="rom_image",
                )
                for path in direct_companion_paths
            )
            direct_companions = (
                *direct_companions,
                *constraint_publications.get("direct_systemverilog", ()),
            )
            direct_plan = result.backend_implementation_plans.plan_for(
                "systemverilog"
            )
            backend_builds.append(publication_api.backend_build_record(
                artifact=direct_artifact,
                plan=direct_plan,
                publications=direct_files,
                companions=direct_companions,
                selected_ir_identity=result.selected_ir_identity,
                implementation_graph_identity=(
                    result.implementation_graph.identity
                    if result.implementation_graph is not None else None
                ),
                source_map_hash=(
                    source_map_hash
                    if source_map is not None
                    and source_map.backend == "direct_systemverilog"
                    else None
                ),
            ))
            all_publications.extend((*direct_files, *direct_companions))

        report_specs = (
            ("contracts_sva", "contracts", None, ()),
            ("implementation_report", "implementation", None, ()),
            ("cost_report", "cost", None, ()),
            ("pipeline_report", "pipeline", None, ()),
            ("architecture_report", "architecture", None, ()),
            ("exploration_report", "exploration", None, ()),
            ("implementation_policy_report", "implementation_policy", None, ()),
            ("backend_implementation_report", "backend_planning", None, ()),
            ("synthesis_report", "synthesis", None, ()),
            ("optimization_ir", "selected_ir", None, ()),
            ("high_level_ir", "high_level_ir", None, ()),
            ("saturation_report", "saturation", None, ()),
            ("csr_markdown", "csr", None, ()),
            ("csr_json", "csr", None, ()),
        )
        for attribute, kind, format, linked_evidence in report_specs:
            add_report(
                attribute,
                kind,
                format=format,
                evidence_ids=linked_evidence,
            )
        if arguments.evidence_report is not None:
            assert rendered_evidence is not None
            evidence_publication = publication_api.published_file_from_path(
                rendered_evidence.record.logical_path,
                arguments.evidence_report,
                kind="report:evidence",
            )
            if (
                evidence_publication.record.content_hash
                != rendered_evidence.record.content_hash
            ):
                raise manifest_api.BuildManifestError(
                    "published evidence report differs from its report record"
                )
            reports.append(rendered_evidence.record)
            all_publications.append(evidence_publication)

        if synthesis_feedback is not None:
            synthesis_outputs = tuple(
                report.logical_path
                for report in reports
                if report.kind == "synthesis" and report.logical_path is not None
            )
            executions.extend((
                _tool_execution(
                    role="synthesis_frontend",
                    tool="zlang-direct-systemverilog",
                    version=synthesis_feedback.frontend_version,
                    argv_shape=("zlang", "<candidate>", "--systemverilog", "<rtl>"),
                    outputs=synthesis_outputs,
                ),
                _tool_execution(
                    role="synthesis_measurement",
                    tool="yosys",
                    version=synthesis_feedback.yosys_version,
                    argv_shape=("yosys", "<normalized-script>"),
                    outputs=synthesis_outputs,
                ),
            ))

        request = result.implementation_request
        policy = result.implementation_policy
        assert request is not None and policy is not None
        platform_profile_identity = (
            selected_platform_profile.identity
            if selected_platform_profile is not None else None
        )
        profile_identity = (
            None
            if arguments.profile is None
            else stable_digest(
                {
                    "schema": (
                        "zlang-selected-profile-v2"
                        if platform_profile_identity is not None
                        else "zlang-selected-profile-v1"
                    ),
                    "name": arguments.profile,
                    "request_identity": request.identity,
                    **(
                        {"platform_constraint_identity": platform_profile_identity}
                        if platform_profile_identity is not None else {}
                    ),
                }
            )
        )
        manifest_metadata = [("module", result.ir.name)]
        for _, constraint_artifact in constraint_products:
            manifest_metadata.extend((
                (
                    f"platform_constraint.{constraint_artifact.backend}."
                    f"{constraint_artifact.format.value}.identity",
                    constraint_artifact.identity,
                ),
                (
                    f"platform_constraint.{constraint_artifact.backend}."
                    f"{constraint_artifact.format.value}.reset",
                    ":".join((
                        constraint_artifact.reset_mode,
                        constraint_artifact.reset_polarity,
                        constraint_artifact.power_up,
                    )),
                ),
            ))
        if result.ir.dependency_closure is not None:
            manifest_metadata.extend((
                (
                    "dependency_closure_identity",
                    result.ir.dependency_closure.identity,
                ),
                (
                    "dependency_lock_identity",
                    result.ir.dependency_closure.lock_identity,
                ),
            ))
        manifest = manifest_api.WholeBuildManifest(
            root_source=root_publication.record,
            dependency_closure=publication_api.dependency_records(
                result.ir.dependency_closure,
                library_dependencies=result.ir.library_dependencies,
            ),
            high_level_ir=manifest_api.CanonicalIrRef(
                "high_level",
                result.high_level_ir_identity,
                CANONICAL_IR_IDENTITY_SCHEMA,
                result.high_level_ir_identity.split(":", 1)[1],
            ),
            selected_ir=manifest_api.CanonicalIrRef(
                "selected",
                result.selected_ir_identity,
                CANONICAL_IR_IDENTITY_SCHEMA,
                result.selected_ir_identity.split(":", 1)[1],
            ),
            implementation_request_identity=request.identity,
            implementation_policy_identity=policy.identity,
            profile_identity=profile_identity,
            backend_builds=tuple(backend_builds),
            tool_executions=tuple(executions),
            reports=tuple(reports),
            evidence=evidence_records,
            metadata=tuple(manifest_metadata),
        )
        publication_api.publish_manifest_atomically(
            manifest,
            arguments.build_manifest,
            publications=all_publications,
        )
    except manifest_api.BuildManifestError as error:
        parser.error(str(error))
